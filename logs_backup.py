"""Finisar log backup.

Moves log files whose names contain a barcode returned by SQL Server into
monthly MOCHI archives (<output_dir>/<YYYY>/<YYYY-MM>.mochi, in-archive path
<SUBFOLDER>/<filename>) and indexes every file in SQLite.

    resume leftover staging -> query -> scan -> stage -> commit -> verify -> index -> delete

A file is always in exactly one place: the source tree, staging, or a
committed and verified archive. Staged copies are deleted only after their
month's archive was verified and indexed; on any failure they stay, and the
next run resumes them first.
"""
import argparse
import datetime as dt
import html
import itertools
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import blake3

log = logging.getLogger("logs_backup")

DELIMITERS = "_.-"  # characters allowed on each side of a barcode in a filename
MANIFEST_BATCH = 2000  # manifest lines synced before the matching files are moved
WORKERS = min(os.cpu_count() or 1, 8)  # hashing threads, and months committed side by side

SCHEMA_VERSION = 2
SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS scan_session (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT CHECK(status IN ('running','success','failed')) NOT NULL,
    query_from_date TEXT NOT NULL,
    query_to_date TEXT NOT NULL,
    total_barcodes INTEGER DEFAULT 0,
    total_files_moved INTEGER DEFAULT 0,
    total_errors INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_barcode (
    id INTEGER PRIMARY KEY,
    scan_session_id INTEGER NOT NULL REFERENCES scan_session(id),
    barcode TEXT NOT NULL,
    last_update TEXT NOT NULL,
    status TEXT CHECK(status IN ('matched','no_files_found','error')) NOT NULL,
    UNIQUE(barcode, last_update)
);
CREATE TABLE IF NOT EXISTS file_storage_volume (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    volume_name TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    head_commit_id TEXT,
    head_seq INTEGER,
    entry_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scanned_log_entry (
    id INTEGER PRIMARY KEY,
    scan_session_id INTEGER NOT NULL REFERENCES scan_session(id),
    barcode TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    source_subfolder TEXT NOT NULL,
    content_hash TEXT,
    commit_seq INTEGER,
    original_size INTEGER NOT NULL,
    file_storage_volume_id INTEGER NOT NULL REFERENCES file_storage_volume(id),
    UNIQUE(relative_path, file_storage_volume_id)
);
CREATE INDEX IF NOT EXISTS idx_scanned_log_entry_barcode ON scanned_log_entry(barcode);
CREATE INDEX IF NOT EXISTS idx_session_barcode_session ON session_barcode(scan_session_id);
"""


class BackupError(Exception):
    pass


def now():
    return f"epoch:{int(time.time())}"


def quiet(step, message="", done=0, total=None):
    """Default progress callback: does nothing. A front end (backup_ui.py) passes its own.

    step is one of "resume", "query", "scan", "stage", "commit"; total None means unknown."""


# ---------------------------------------------------------------- config

def program_dir():
    """The folder this program is in: the .exe's folder when built with build_exe.bat (PyInstaller
    unpacks the code to a temporary folder, so __file__ would point there), else this file's folder."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_config(given=None):
    """The config file: `given` if passed, else config.json in the current folder, else next to this program."""
    if given:
        return Path(given)
    places = [Path.cwd() / "config.json", program_dir() / "config.json"]
    found = next((p for p in places if p.is_file()), None)
    if not found:
        raise BackupError("no config.json found in " + " or ".join(str(p.parent) for p in places)
                          + ": copy config.example.json to config.json there, or pass the config's path")
    return found


class Config:
    def __init__(self, path):
        self.path = Path(path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.source_dir = Path(raw["source_dir"])
        self.output_dir = Path(raw["output_dir"])
        # Staging must be on the source drive: files are moved there by rename.
        self.staging_dir = Path(raw.get("staging_dir") or self.source_dir / ".staging")
        self.db_path = Path(raw["db_path"])
        self.subfolders = raw.get("subfolders", ["BURNIN", "ESS", "JTAG", "STM", "Xray"])
        self.extensions = {e.lstrip(".").lower() for e in raw.get("extensions", ["txt", "zip"])}
        self.recursive = raw.get("recursive", True)  # scan folders inside each subfolder too
        if not isinstance(self.recursive, bool):
            raise BackupError('"recursive" in config.json must be true or false')
        self.lookback_days = raw.get("lookback_days", 2)
        self.date_range = raw.get("date_range")  # optional ["YYYY-MM-DD", "YYYY-MM-DD"]
        self.mochi = raw.get("mochi")  # None: found by check_mochi() (PATH, cargo's bin, a MOCHI checkout)
        self.email = raw.get("email")  # optional {"api_base_url": ..., "to": ...}
        # SQL Server login: "sql_server" in config.json, each value overridden by its DB_* variable.
        # A missing password is asked for by backup_ui.py (hidden) instead of being stored.
        sql, env = raw.get("sql_server") or {}, os.environ
        self.sql = {key: env.get(var) or sql.get(key) for key, var in (
            ("server", "DB_SERVER"), ("port", "DB_PORT"), ("database", "DB_NAME"),
            ("user", "DB_USER"), ("password", "DB_PASSWORD"), ("encryption", "DB_ENCRYPTION"))}
        for name in ("source_dir", "output_dir", "staging_dir", "db_path"):
            if any(ord(c) < 32 for c in str(getattr(self, name))):  # "E:\backup" in JSON is E: + backspace + ackup
                raise BackupError(f"{name} in config.json has a broken path: write Windows paths with / (E:/backup)")
        for sub in self.subfolders:
            if not sub or sub in (".", "..") or any(c in sub for c in "/\\:"):
                raise BackupError(f"subfolder {sub!r} must be a single plain folder name")

    @property
    def manifest(self):
        return self.staging_dir / "manifest.jsonl"

    def archive(self, month):
        return self.output_dir / month[:4] / f"{month}.mochi"


# ---------------------------------------------------------------- SQLite index

def open_index(path):
    """Opens (or creates) the SQLite index. The caller closes it: on Windows an open connection
    keeps index.db locked, so nothing can move or delete it until then."""
    path.parent.mkdir(parents=True, exist_ok=True)  # SQLite creates the file, not its folder
    try:
        db = sqlite3.connect(path)
    except sqlite3.Error as e:
        raise BackupError(f"cannot open the index {path}: {e}") from e
    try:
        return _check_index(db, path)
    except BaseException:
        db.close()
        raise


def _check_index(db, path):
    try:
        db.execute("PRAGMA user_version")
    except sqlite3.Error as e:
        raise BackupError(f"cannot open the index {path}: {e}") from e
    version = db.execute("PRAGMA user_version").fetchone()[0]
    columns = {r[1] for t in ("file_storage_volume", "scanned_log_entry")
               for r in db.execute(f"PRAGMA table_info({t})")}
    if version not in (0, SCHEMA_VERSION) or {"sha256", "chunk_start_byte"} & columns:
        raise BackupError(f"{path} is an index of another format/version; move it aside to start a new one")
    db.executescript(SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};")
    return db


def start_session(db, query_from, query_to):
    with db:
        return db.execute(
            "INSERT INTO scan_session (started_at, status, query_from_date, query_to_date)"
            " VALUES (?, 'running', ?, ?)", (now(), query_from, query_to)).lastrowid


def complete_session(db, sid, status, barcodes=0, moved=0, errors=0):
    with db:
        db.execute(
            "UPDATE scan_session SET completed_at = ?, status = ?, total_barcodes = ?,"
            " total_files_moved = ?, total_errors = ? WHERE id = ?",
            (now(), status, barcodes, moved, errors, sid))


def query_window(db, cfg):
    """(from, to): from = last successful to-date - 1 day (None on the first run), to = today - lookback."""
    if cfg.date_range:
        return tuple(cfg.date_range)
    to = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=cfg.lookback_days)
    row = db.execute("SELECT query_to_date FROM scan_session WHERE status = 'success'"
                     " AND query_to_date != 'resumed' ORDER BY id DESC LIMIT 1").fetchone()
    start = dt.date.fromisoformat(row[0]) - dt.timedelta(days=1) if row else None
    return (start.isoformat() if start else None), to.isoformat()


# ---------------------------------------------------------------- SQL Server

SQL_HINTS = {
    20002: "the server answered but the connection handshake failed. Check the port (a named instance"
           " HOST\\NAME uses its own port), then the \"encryption\" setting (docs/operations.md)",
    20009: "the server cannot be reached. Check the server name or IP, the port and the firewall",
    18456: "login failed. Check the user and password",
    4060: "cannot open the database. Check the database name and the user's permissions",
}


def sql_error(e):
    """One readable line from a pymssql error, plus a hint for the usual failures."""
    parts = e.args[0] if e.args and isinstance(e.args[0], tuple) else e.args
    code = parts[0] if parts and isinstance(parts[0], int) else None
    text = " ".join(p.decode(errors="replace") if isinstance(p, bytes) else str(p) for p in parts[1:] or parts)
    # Text starting with "DB-Lib error message N" is a client-side error: its code is N (pymssql can
    # keep reporting the previous server error's code). Otherwise the server's message comes first.
    client = re.match(r"DB-Lib error message (\d+)", text)
    if client:
        code = int(client.group(1))
    lines = [ln.strip() for ln in re.split(r"DB-Lib error message \d+, severity \d+:|\n", text) if ln.strip()]
    message = lines[0] if lines else text
    if code:
        message += f" (error {code})"
    return f"{message}: {SQL_HINTS[code]}" if code in SQL_HINTS else message


def set_encryption(mode):
    """Chooses FreeTDS's login encryption: "request" (FreeTDS's default), "require" or "off".

    false/true (JSON) or "false"/"true", "0"/"1", "no"/"yes" (DB_ENCRYPTION) mean "off"/"require".
    FreeTDS reads this only from a freetds.conf, so one is written to the temp folder and used."""
    given = mode
    mode = str(mode).strip().lower()  # None -> "none", False -> "false"
    mode = {"none": "request", "": "request", "false": "off", "no": "off", "0": "off",
            "true": "require", "yes": "require", "1": "require"}.get(mode, mode)
    if mode not in ("request", "require", "off"):
        raise BackupError(f'sql_server "encryption" must be "request", "require" or "off", not {given!r}')
    if mode == "off":
        log.warning("SQL Server connection without encryption (\"encryption\": \"off\" in config.json)")
    conf = Path(tempfile.gettempdir()) / f"logs_backup_freetds_{mode}.conf"
    conf.write_text(f"[global]\n    encryption = {mode}\n", encoding="utf-8")
    os.environ["FREETDSCONF"] = str(conf)


def query_candidates(sql_login, date_from, date_to):
    """[(barcode, 'YYYY-MM-DD HH:MM:SS')] for the window. sql_login is Config.sql."""
    where, params = "sft.last_update < %s", [date_to]
    if date_from:
        where, params = "sft.last_update >= %s AND " + where, [date_from, date_to]
    sql = ("SELECT DISTINCT sfs.serial_no, CONVERT(VARCHAR(50), sft.last_update, 120)"
           " FROM ForthUtilities.dbo.ShipmentFinisarSerial sfs WITH (NOLOCK)"
           " JOIN ForthUtilities.dbo.ShipmentFinisarTrace sft WITH (NOLOCK) ON sfs.trace_id = sft.trace_id"
           f" WHERE {where}")
    return [(str(b).strip(), str(d)) for b, d in run_sql(sql_login, sql, params)]


def check_sql(sql_login):
    """Connects once and returns the server's version line (backup_ui.py --check-sql)."""
    return str(run_sql(sql_login, "SELECT @@VERSION", [], retries=0)[0][0]).splitlines()[0]


def run_sql(sql_login, sql, params, retries=None):
    """Rows of one query, retried with exponential backoff (DB_RETRY_ATTEMPTS, default 3)."""
    import pymssql  # imported here so the rest of the module works without it

    missing = [k for k in ("server", "database", "user", "password") if not sql_login.get(k)]
    if missing:
        raise BackupError(f"SQL Server login incomplete, missing: {', '.join(missing)}. Add it to"
                          " \"sql_server\" in config.json, or set the DB_* environment variables")
    set_encryption(sql_login.get("encryption"))
    server = sql_login["server"]
    port = None if "\\" in server else str(sql_login.get("port") or 1433)  # HOST\NAME: port found by name
    env = os.environ
    retries = int(env.get("DB_RETRY_ATTEMPTS", 3)) if retries is None else retries
    delay = int(env.get("DB_RETRY_BASE_DELAY_MS", 2000)) / 1000
    for attempt in range(retries + 1):
        try:
            with pymssql.connect(server=server, port=port,
                                 database=sql_login["database"], user=sql_login["user"],
                                 password=sql_login["password"],
                                 timeout=int(env.get("DB_REQUEST_TIMEOUT_MS", 30000)) // 1000) as conn:
                cur = conn.cursor()
                cur.execute(sql, tuple(params))
                return cur.fetchall()
        except pymssql.Error as e:
            if attempt == retries:
                raise BackupError(f"SQL Server query failed: {sql_error(e)}") from e
            log.warning("SQL Server query failed (%s), retrying in %.0fs", sql_error(e), delay * 2 ** attempt)
            time.sleep(delay * 2 ** attempt)


# ---------------------------------------------------------------- scan

def find_barcodes(name, barcodes):
    """Barcodes in `name` that start and end at a delimiter (_ . -) or at the name's start/end."""
    starts = [0] + [i + 1 for i, c in enumerate(name) if c in DELIMITERS]
    ends = [i for i, c in enumerate(name) if c in DELIMITERS] + [len(name)]
    return {name[s:e] for s in starts for e in ends if e > s and name[s:e] in barcodes}


def has_extension(name, extensions):
    return "." in name and name.rsplit(".", 1)[1].lower() in extensions


def walk_files(top, recursive, skip, on_error):
    """Yields (folder path below `top` ending in "/", or "", DirEntry) for each file in `top`, and with
    `recursive` in every folder below it. Folders are read as they go (never held whole in memory);
    symlinks are not followed and folders in `skip` are not entered. An unreadable folder is passed
    to on_error(folder, error) and skipped."""
    stack = [(Path(top), "")]
    while stack:
        folder, rel = stack.pop()
        try:
            with os.scandir(folder) as listing:
                for item in listing:
                    if item.is_dir(follow_symlinks=False):
                        if recursive and Path(item.path).resolve() not in skip:
                            stack.append((Path(item.path), f"{rel}{item.name}/"))
                    elif item.is_file(follow_symlinks=False):
                        yield rel, item
        except OSError as e:
            on_error(folder, e)


def scan(cfg, dates, report=quiet):
    """Matched files as manifest entries, plus error and scanned-file counts. Each subfolder is scanned
    at every depth (config "recursive", default on); the staging and output folders are never entered."""
    entries, errors, seen = [], 0, 0
    skip = {cfg.staging_dir.resolve(), cfg.output_dir.resolve()}

    def unreadable(folder, e):
        nonlocal errors
        log.warning("skipping folder %s: %s", folder, e)
        errors += 1

    for sub in cfg.subfolders:
        for rel, item in walk_files(cfg.source_dir / sub, cfg.recursive, skip, unreadable):
            seen += 1
            if seen % 2000 == 0:
                report("scan", f"{len(entries)} matches", seen)
            if not has_extension(item.name, cfg.extensions):
                continue
            found = find_barcodes(item.name, dates)
            if len(found) != 1:
                errors += len(found) > 1  # several barcodes: ambiguous
                continue
            barcode = found.pop()
            month = dates[barcode][:7]
            if not (len(month) == 7 and month[:4].isdigit() and month[4] == "-" and month[5:].isdigit()):
                errors += 1
                continue
            entries.append({"relative_path": f"{sub}/{rel}{item.name}", "source_subfolder": sub,
                            "barcode": barcode, "original_size": item.stat().st_size, "volume_name": month})
    report("scan", f"{len(entries)} matches", seen, seen)
    log.info("scanned %d files, %d matches", seen, len(entries))
    return entries, errors, seen


def staged_path(cfg, entry):
    return cfg.staging_dir / entry["volume_name"] / entry["relative_path"]


# ---------------------------------------------------------------- stage

def stage(cfg, sid, entries, report=quiet):
    """Moves files into staging/<YYYY-MM>/<SUB>/; each manifest line is synced before its file moves.

    Returns the number of files that could not be moved (they stay in the source).
    Renames run on one thread: measured, threads made them slower (they queue on the folder)."""
    errors = 0
    cfg.staging_dir.mkdir(parents=True, exist_ok=True)
    if os.stat(cfg.staging_dir).st_dev != os.stat(cfg.source_dir).st_dev:
        raise BackupError(f"staging_dir {cfg.staging_dir} must be on the same drive as source_dir (files move by rename)")
    for d in {staged_path(cfg, e).parent for e in entries}:
        d.mkdir(parents=True, exist_ok=True)
    with open(cfg.manifest, "w", encoding="utf-8") as manifest:
        manifest.write(json.dumps({"scan_session_id": sid}) + "\n")
        for i in range(0, len(entries), MANIFEST_BATCH):
            batch = entries[i:i + MANIFEST_BATCH]
            manifest.writelines(json.dumps(e) + "\n" for e in batch)
            manifest.flush()
            os.fsync(manifest.fileno())  # the batch is listed on disk before any of it moves
            for e in batch:
                src, dst = cfg.source_dir / e["relative_path"], staged_path(cfg, e)
                try:
                    if dst.exists():  # never overwrite: it may be the only copy
                        raise FileExistsError(dst)
                    os.rename(src, dst)
                except OSError as err:  # stays in the source; a later run picks it up
                    log.warning("not staged %s: %s", src, err)
                    errors += 1
            report("stage", "", i + len(batch), len(entries))
    return errors


def read_manifest(cfg):
    """(session id, entries) or None. A truncated last line is skipped."""
    try:
        lines = cfg.manifest.read_text(encoding="utf-8").splitlines()
        sid = json.loads(lines[0])["scan_session_id"]
    except (OSError, IndexError, KeyError, ValueError):
        return None
    entries = []
    for line in lines[1:]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass
    return sid, entries


# ---------------------------------------------------------------- MOCHI

MOCHI_EXE = "mochi.exe" if os.name == "nt" else "mochi"


APP_PATHS_KEY = r"Software\Microsoft\Windows\CurrentVersion\App Paths\mochi.exe"
MOCHI_INSTALL_KEY = r"Software\The MOCHI Project contributors\MOCHI"  # Tauri's install-location key


def registry_mochi(reg=None):
    """mochi.exe paths the MOCHI setup recorded in the Windows registry ([] elsewhere), in order:
    App Paths\\mochi.exe (setup 0.9.15+; per user, then per machine), then Tauri's install-location
    key plus bin\\mochi.exe (setup 0.9.14). `reg` is the winreg module (tests pass a stand-in)."""
    if reg is None:
        if os.name != "nt":
            return []
        import winreg as reg
    found = []
    for root, key, add in ((reg.HKEY_CURRENT_USER, APP_PATHS_KEY, ""), (reg.HKEY_LOCAL_MACHINE, APP_PATHS_KEY, ""),
                           (reg.HKEY_CURRENT_USER, MOCHI_INSTALL_KEY, "bin")):
        try:
            with reg.OpenKey(root, key) as k:
                value = reg.QueryValueEx(k, "")[0]
        except OSError:
            continue
        if value:
            value = os.path.expandvars(str(value).strip('"'))
            found.append(str(Path(value) / add / MOCHI_EXE) if add else value)
    return found


def mochi_places(configured):
    """Where to look for the mochi command-line program, in order. A path set in config.json is the
    only place used; otherwise the path the MOCHI setup registered (registry_mochi), PATH, the MOCHI
    setup's bin folder (%LOCALAPPDATA%/MOCHI/bin), cargo's bin folder, next to this program, and a
    MOCHI checkout beside this one (D:/GitHub/MOCHI/target/release/mochi.exe next to
    D:/GitHub/Finisar_Logs_backup)."""
    if configured and configured != "mochi":  # "mochi" (the old example config) also means: find it
        return [str(configured)]
    here = program_dir()
    setup = os.environ.get("LOCALAPPDATA")
    places = [*registry_mochi(), shutil.which("mochi"), setup and Path(setup) / "MOCHI" / "bin" / MOCHI_EXE,
              Path.home() / ".cargo" / "bin" / MOCHI_EXE, here / MOCHI_EXE,
              here.parent / "MOCHI" / "target" / "release" / MOCHI_EXE,
              here.parent / "mochi" / "target" / "release" / MOCHI_EXE]
    return list(dict.fromkeys(str(p) for p in places if p))  # in order, without repeats


def desktop_app_hint():
    """A note if only the MOCHI desktop app is installed (setups before the one that added bin/mochi.exe)."""
    setup = os.environ.get("LOCALAPPDATA")
    if (setup and (Path(setup) / "MOCHI" / "mochi-desktop.exe").is_file()) or shutil.which("mochi-desktop"):
        return (" The MOCHI desktop app is installed, but this version of its setup does not include the"
                " mochi command-line program: install a newer MOCHI setup (it adds bin\\mochi.exe), or build it.")
    return ""


def check_mochi(cfg):
    """Finds mochi (see mochi_places), sets cfg.mochi to it and returns its version line.

    Raises BackupError naming every place it looked if none starts."""
    tried = []
    for exe in mochi_places(cfg.mochi):
        try:
            p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            tried.append(f"{exe}: {e.strerror if isinstance(e, OSError) and e.strerror else e}")
            continue
        if p.returncode == 0 and p.stdout.startswith("mochi "):
            cfg.mochi = shutil.which(exe) or str(Path(exe).resolve())  # full path: not searched again
            return f"{p.stdout.splitlines()[0]} ({exe})"
        tried.append(f"{exe}: '--version' gave exit {p.returncode}")
    raise BackupError("the mochi program was not found. Looked at:\n  " + "\n  ".join(tried)
                      + "\nBuild it in the MOCHI repository (cargo build --release -p mochi-cli), then set"
                      " \"mochi\" in config.json to the full path of mochi.exe (with /)." + desktop_app_hint())


def mochi(cfg, *args):
    """Runs `mochi --json ARGS`. Exit 0 and 2 (degraded, data committed / checked) pass; others raise."""
    if not cfg.mochi or not Path(cfg.mochi).is_absolute():
        check_mochi(cfg)  # finds it and sets cfg.mochi to its full path
    try:
        p = subprocess.run([cfg.mochi, "--json", "--no-local-history", *map(str, args)],
                           capture_output=True, text=True)
    except OSError as e:
        raise BackupError(f"the mochi program could not be started ({cfg.mochi}): {e}") from e
    try:
        out = json.loads(p.stdout)
    except ValueError:
        out = {}
    if p.returncode not in (0, 2):
        raise BackupError(f"mochi {args[0]} {args[1]}: exit {p.returncode} {out.get('error') or p.stderr.strip()}")
    return out


def head_files(cfg, archive):
    """{path: content_hash} of the archive head, plus (seq, commit_id)."""
    if not archive.exists():
        return {}, None
    out = mochi(cfg, "list", archive)
    files = {e["path"]: e["content_hash"] for e in out["entries"] if e["kind"] == "file"}
    return files, (out["seq"], out["commit_id"])


def b3(path):
    return blake3.blake3(max_threads=blake3.blake3.AUTO).update_mmap(path).hexdigest()


def hash_all(pool, paths, chunk=500):
    """BLAKE3 of every path on the pool's threads, in order. A lazy iterator, so it runs
    while the caller does other work; chunks keep it to a few hundred tasks, not one per file."""
    chunks = [paths[i:i + chunk] for i in range(0, len(paths), chunk)]
    return itertools.chain.from_iterable(pool.map(lambda c: [b3(p) for p in c], chunks))


def commit(cfg, month):
    """One MOCHI commit of staging/<month>/: each <SUB> directory merges into the archive as <SUB>/<file>."""
    archive = cfg.archive(month)
    archive.parent.mkdir(parents=True, exist_ok=True)
    inputs = sorted(d for d in (cfg.staging_dir / month).iterdir() if d.is_dir())
    out = mochi(cfg, "append" if archive.exists() else "create", archive, *inputs)
    if out.get("durability") != "durable":
        log.warning("%s: DEGRADED (not an error): %s", month, out.get("durability_note"))


def commit_month(cfg, month, entries, resume=False):
    """Commits a month's staged files (one commit), then checks every file and the archive.

    Touches neither SQLite nor staging. Returns (hashes, head)."""
    archive, paths = cfg.archive(month), [e["relative_path"] for e in entries]
    with ThreadPoolExecutor(WORKERS) as pool:
        hashing = hash_all(pool, [staged_path(cfg, e) for e in entries])  # runs while mochi commits
        if resume:
            # A crash between commit and index leaves the batch already committed:
            # commit only if the head does not hold every file with the same hash.
            hashes = dict(zip(paths, hashing))
            head, _ = head_files(cfg, archive)
            if any(head.get(p) != h for p, h in hashes.items()):
                commit(cfg, month)
        else:
            commit(cfg, month)
            hashes = dict(zip(paths, hashing))
    head, head_id = head_files(cfg, archive)
    bad = [p for p, h in hashes.items() if head.get(p) != h]
    if bad:
        raise BackupError(f"{month}: {len(bad)} staged files differ from the archive head, e.g. {bad[0]}")
    mochi(cfg, "verify", archive, "--level", "content")
    return hashes, head_id


def index_month(db, cfg, sid, month, entries, hashes, head):
    seq, commit_id = head
    archive, stamp = cfg.archive(month), now()
    with db:
        row = db.execute("SELECT id FROM file_storage_volume WHERE volume_name = ?", (month,)).fetchone()
        values = (str(archive), archive.stat().st_size, commit_id, seq, stamp)
        if row:
            vid = row[0]
            db.execute("UPDATE file_storage_volume SET path = ?, file_size = ?, head_commit_id = ?,"
                       " head_seq = ?, updated_at = ? WHERE id = ?", values + (vid,))
        else:
            vid = db.execute("INSERT INTO file_storage_volume (path, file_size, head_commit_id, head_seq,"
                             " created_at, updated_at, volume_name, entry_count) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                             values + (stamp, month)).lastrowid
        db.executemany(
            "INSERT INTO scanned_log_entry (scan_session_id, barcode, relative_path, source_subfolder,"
            " content_hash, commit_seq, original_size, file_storage_volume_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(relative_path, file_storage_volume_id) DO UPDATE SET scan_session_id = excluded.scan_session_id,"
            " barcode = excluded.barcode, content_hash = excluded.content_hash, commit_seq = excluded.commit_seq,"
            " original_size = excluded.original_size",
            [(sid, e["barcode"], e["relative_path"], e["source_subfolder"], hashes[e["relative_path"]], seq,
              e["original_size"], vid) for e in entries])
        db.execute("UPDATE file_storage_volume SET entry_count ="
                   " (SELECT COUNT(*) FROM scanned_log_entry WHERE file_storage_volume_id = ?) WHERE id = ?", (vid, vid))


def finish_staged(db, cfg, report=quiet, resume=False):
    """Archives whatever the manifest lists in staging. Returns a summary dict, or None if nothing is staged."""
    manifest = read_manifest(cfg)
    if manifest is None:
        return None
    sid, listed = manifest
    if not db.execute("SELECT 1 FROM scan_session WHERE id = ?", (sid,)).fetchone():
        log.warning("staging references missing session %s; recording into a new one", sid)
        sid = start_session(db, "resumed", "resumed")
    months = defaultdict(list)
    for e in listed:
        if staged_path(cfg, e).is_file():  # listed but never moved: still in the source
            months[e["volume_name"]].append(e)

    archived, failures = [], []
    report("commit", "", 0, len(months))
    with ThreadPoolExecutor(min(WORKERS, max(len(months), 1))) as pool:
        jobs = {m: pool.submit(commit_month, cfg, m, es, resume) for m, es in months.items()}
        for month, job in sorted(jobs.items()):  # SQLite and deletes stay on this thread
            try:
                hashes, head = job.result()
                index_month(db, cfg, sid, month, months[month], hashes, head)
            except (BackupError, OSError, sqlite3.Error) as e:
                log.error("%s kept in staging: %s", month, e)
                failures.append(e)
                continue
            for e in months[month]:
                staged_path(cfg, e).unlink()
            archived += months[month]
            log.info("%s: %d files committed (LOCAL_COMMITTED), verified and indexed", month, len(months[month]))
            report("commit", month, len(plan(archived)), len(months))
    if failures:
        raise BackupError(f"{len(failures)} month(s) failed; staging kept for the next run: {failures[0]}")

    cfg.manifest.unlink()
    leftovers = 0
    for d, _, files in sorted(os.walk(cfg.staging_dir), key=lambda w: -len(w[0])):
        for f in files:  # not in the manifest: never deleted
            log.warning("left in staging (not listed in the manifest): %s", Path(d) / f)
            leftovers += 1
        if not os.listdir(d):
            os.rmdir(d)
    barcodes = len({e["barcode"] for e in archived})
    complete_session(db, sid, "success", barcodes, len(archived), leftovers)
    return {"files_moved": len(archived), "barcodes": barcodes, "errors": leftovers, "volumes": plan(archived)}


# ---------------------------------------------------------------- run

def lock_run(cfg):
    """Holds <output_dir>/.backup.lock so two runs never share staging. Returns the open lock file.

    The OS releases the lock when the process ends, even after a crash, so it never goes stale."""
    try:
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BackupError(f"cannot create output_dir {cfg.output_dir}: {e.strerror or e}") from e
    lock = open(cfg.output_dir / ".backup.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise BackupError("another backup run is in progress (lock held on .backup.lock); try again when it ends")
    return lock


def plan(entries):
    """{month: (barcodes, files, bytes)} of matched files, sorted by month.

    Used for what a run would archive (confirm, dry run) and what it did archive (summary)."""
    months = defaultdict(list)
    for e in entries:
        months[e["volume_name"]].append(e)
    return {month: (len({e["barcode"] for e in es}), len(es), sum(e["original_size"] for e in es))
            for month, es in sorted(months.items())}


def run(cfg, candidates_fn=None, dry_run=False, report=quiet, confirm=None):
    """One backup run. Returns a summary dict whose "status" is "done", "dry_run" or "cancelled".

    confirm(volumes) -> bool, if given, is asked after the scan and before any file moves;
    False stops the run with nothing moved and nothing recorded.
    candidates_fn(date_from, date_to) defaults to querying SQL Server (tests pass their own)."""
    candidates_fn = candidates_fn or (lambda date_from, date_to: query_candidates(cfg.sql, date_from, date_to))
    with lock_run(cfg):
        return _run(cfg, candidates_fn, dry_run, report, confirm)


def prepare_folders(cfg):
    """Creates the output, staging and index folders if they do not exist yet. The source folder is
    never created: a missing one is a wrong setting, and the scan says so."""
    for name, folder in (("output_dir", cfg.output_dir), ("staging_dir", cfg.staging_dir),
                         ("db_path's folder", cfg.db_path.parent)):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise BackupError(f"cannot create {name} {folder}: {e.strerror or e}") from e
    if not cfg.source_dir.is_dir():
        log.warning("source_dir %s does not exist: nothing to scan", cfg.source_dir)


def _run(cfg, candidates_fn, dry_run, report, confirm):
    # Checked before anything moves: without mochi, staged files could not be committed.
    try:
        log.info("using %s", check_mochi(cfg))
    except BackupError as e:
        if not dry_run:
            raise
        log.warning("a real run would stop here: %s", e)
    if not dry_run:
        prepare_folders(cfg)
    db = open_index(cfg.db_path)
    try:
        return _run_with_index(db, cfg, candidates_fn, dry_run, report, confirm)
    finally:
        db.close()  # an open connection keeps index.db locked on Windows


def _run_with_index(db, cfg, candidates_fn, dry_run, report, confirm):
    resumed = 0
    if not dry_run and cfg.manifest.exists():
        report("resume", "finishing the batch an earlier run left in staging")
        # The resumed batch's progress is shown on the "resume" row.
        finished = finish_staged(db, cfg, lambda _step, *args: report("resume", *args), resume=True)
        resumed = finished["files_moved"] if finished else 0
        report("resume", f"{resumed} files", 1, 1)

    date_from, date_to = query_window(db, cfg)
    window = (date_from, date_to)
    report("query", f"{date_from or '(start)'} .. {date_to}")
    log.info("querying candidates %s .. %s", date_from or "(start)", date_to)
    candidates = candidates_fn(date_from, date_to)
    dates = dict(candidates)  # barcode -> last_update (the last row wins)
    report("query", f"{len(dates):,} candidate barcodes", 1, 1)
    entries, errors, seen = scan(cfg, dates, report)
    volumes = plan(entries)
    matched = {e["barcode"] for e in entries}
    info = {"window": window, "candidates": len(dates), "scanned": seen, "volumes": volumes, "resumed": resumed,
            "matched_barcodes": len(matched), "matched_files": len(entries)}
    if dry_run:
        log.info("dry run: %d of %d candidate barcodes would be archived (%d files): %s",
                 len(matched), len(dates), len(entries), volumes)
        return dict(info, status="dry_run", files_moved=0, barcodes=0, errors=errors)
    if entries and confirm and not confirm(volumes):
        return dict(info, status="cancelled", files_moved=0, barcodes=0, errors=errors)

    sid = start_session(db, date_from or "", date_to)
    try:
        fresh = [(b, d) for b, d in candidates if not db.execute(
            "SELECT 1 FROM session_barcode WHERE barcode = ? AND last_update = ?", (b, d)).fetchone()]
        with db:
            db.executemany("INSERT OR IGNORE INTO session_barcode (scan_session_id, barcode, last_update, status)"
                           " VALUES (?, ?, ?, ?)",
                           [(sid, b, d, "matched" if b in matched else "no_files_found") for b, d in fresh])
        result = {"files_moved": 0, "barcodes": 0, "errors": 0, "volumes": {}}
        if entries:
            errors += stage(cfg, sid, entries, report)
            result = finish_staged(db, cfg, report)
        errors += result["errors"]
        complete_session(db, sid, "success", len(fresh), result["files_moved"], errors)
    except Exception:
        complete_session(db, sid, "failed", errors=errors + 1)
        raise
    unmatched = sorted({b for b, _ in fresh} - matched)
    return {**info, **result, "status": "done", "session_id": sid, "errors": errors, "unmatched": unmatched}


# ---------------------------------------------------------------- export

RESTORE_TEXT = """Finisar log backup export, {when}

What is here
  archives/        the monthly .mochi archives (output_dir)
  index.db         the SQLite index (db_path)
  config.json      the config, with the SQL Server password removed
  staging/         only after an interrupted run: files in no archive yet (their only copy)
  export-manifest.json   size and BLAKE3 of every file, checked when it was copied

To restore on another PC
  1. Copy this folder there.
  2. Edit config.json: output_dir = <this folder>/archives, db_path = <this folder>/index.db
     (or copy them where you want and point there), and set source_dir and mochi.
  3. If staging/ exists: copy its contents into <source_dir>/.staging before the first run.
  4. python backup_ui.py --check-sql, then --dry-run, then a real run.
"""


def copy_checked(src, dst):
    """Copies src to dst via a .partial file; the copy's BLAKE3 must equal the source's. Returns it."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".partial")
    digest = blake3.blake3(max_threads=blake3.blake3.AUTO)
    with open(src, "rb") as f, open(tmp, "wb") as out:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            digest.update(chunk)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    if b3(tmp) != digest.hexdigest():
        tmp.unlink()
        raise BackupError(f"copy of {src} does not match the original")
    os.utime(tmp, ns=(src.stat().st_atime_ns, src.stat().st_mtime_ns))
    os.replace(tmp, dst)
    return digest.hexdigest()


def export(cfg, target, report=quiet):
    """Copies what is needed to restore this setup into `target` (see RESTORE_TEXT), every file checked
    by BLAKE3. Archives unchanged since the last export to the same target are not copied again.
    Holds the run lock, so no run changes an archive meanwhile. Returns {item: (files, bytes, copied)}."""
    target = Path(target).resolve()
    for name, folder in (("source_dir", cfg.source_dir), ("output_dir", cfg.output_dir)):
        folder = folder.resolve()
        if target == folder or folder in target.parents or target in folder.parents:
            raise BackupError(f"export target {target} must not be inside or contain {name} {folder}")
    with lock_run(cfg):
        listing = target / "export-manifest.json"
        try:
            previous = json.loads(listing.read_text(encoding="utf-8"))["files"]
        except (OSError, ValueError, KeyError):
            previous = {}
        archives = sorted(cfg.output_dir.glob("*/*.mochi"))
        staged = sorted(p for p in cfg.staging_dir.rglob("*") if p.is_file()) if cfg.staging_dir.exists() else []
        jobs = [(p, f"archives/{p.relative_to(cfg.output_dir).as_posix()}", "archives") for p in archives]
        jobs += [(p, f"staging/{p.relative_to(cfg.staging_dir).as_posix()}", "staging") for p in staged]
        files, items = {}, defaultdict(lambda: [0, 0, 0])  # item -> [files, bytes, copied]
        total = len(jobs) + 2
        for i, (src, rel, item) in enumerate(jobs):
            report("export", rel, i, total)
            st, old, dst = src.stat(), previous.get(rel), target / rel
            unchanged = (old and old["size"] == st.st_size and old["mtime_ns"] == st.st_mtime_ns
                         and dst.is_file() and dst.stat().st_size == st.st_size)
            files[rel] = old if unchanged else {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
                                                "blake3": copy_checked(src, dst)}
            items[item][0] += 1
            items[item][1] += st.st_size
            items[item][2] += not unchanged

        report("export", "index.db", len(jobs), total)
        if cfg.db_path.is_file():  # SQLite's backup API gives a consistent copy, unlike a file copy
            target.mkdir(parents=True, exist_ok=True)
            tmp = target / "index.db.partial"
            tmp.unlink(missing_ok=True)
            source, copy = sqlite3.connect(cfg.db_path), sqlite3.connect(tmp)
            try:
                source.backup(copy)
            finally:
                copy.close()
                source.close()
            os.replace(tmp, target / "index.db")
            files["index.db"] = {"size": (target / "index.db").stat().st_size, "blake3": b3(target / "index.db")}
            items["index.db"] = [1, files["index.db"]["size"], 1]

        target.mkdir(parents=True, exist_ok=True)
        raw = json.loads(cfg.path.read_text(encoding="utf-8"))
        if isinstance(raw.get("sql_server"), dict) and raw["sql_server"].get("password"):
            raw["sql_server"]["password"] = None  # never exported: asked again on the next run
        (target / "config.json").write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        items["config.json"] = [1, (target / "config.json").stat().st_size, 1]

        when = dt.datetime.now().isoformat(timespec="seconds")
        (target / "RESTORE.txt").write_text(RESTORE_TEXT.format(when=when), encoding="utf-8")
        listing.write_text(json.dumps({"exported_at": when, "files": files}, indent=1), encoding="utf-8")
        report("export", "done", total, total)
        log.info("exported to %s: %s", target, dict(items))
        return {item: tuple(v) for item, v in items.items()}


def send_email(cfg, summary):
    """Best effort: POSTs an HTML summary to {api_base_url}/api/email/send."""
    rows = "".join(f"<tr><td>{m}</td><td>{bc}</td><td>{n}</td><td>{b}</td></tr>"
                   for m, (bc, n, b) in summary["volumes"].items())
    unmatched = ", ".join(html.escape(b) for b in summary["unmatched"][:2000])
    body = (f"<h3>Finisar log backup, session {summary['session_id']}</h3>"
            f"<p>Window {summary['window'][0] or '(start)'} .. {summary['window'][1]}<br>"
            f"Candidates {summary['candidates']}, barcodes archived {summary['barcodes']}, "
            f"files scanned {summary['scanned']}, "
            f"files committed (LOCAL_COMMITTED) {summary['files_moved']}, errors {summary['errors']}</p>"
            f"<table border=1><tr><th>Archive</th><th>Barcodes</th><th>Files</th><th>Bytes</th></tr>{rows}</table>"
            f"<p>Unmatched barcodes ({len(summary['unmatched'])}): {unmatched}</p>")[:95_000]
    payload = {"to": cfg.email["to"], "subject": "Finisar log backup report", "body": body}
    try:
        req = urllib.request.Request(cfg.email["api_base_url"].rstrip("/") + "/api/email/send",
                                     json.dumps(payload).encode(), {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).close()
    except OSError as e:
        log.warning("email report not sent: %s", e)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("config", nargs="?", help="config file (default: config.json in the current folder, else next to this program)")
    parser.add_argument("--dry-run", action="store_true", help="query and scan only; move nothing")
    parser.add_argument("--export", metavar="DIR", help="copy archives, index and config to DIR, then stop")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config(find_config(args.config))
    if args.export:
        export(cfg, args.export)
        return
    summary = run(cfg, dry_run=args.dry_run)
    log.info("%s: %s", summary["status"], json.dumps({k: v for k, v in summary.items() if k != "unmatched"}))
    if summary["status"] == "done" and cfg.email:
        send_email(cfg, summary)


if __name__ == "__main__":
    main()
