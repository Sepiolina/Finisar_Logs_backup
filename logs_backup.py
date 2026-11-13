"""Finisar log backup.

Moves log files whose names contain a barcode returned by SQL Server into
monthly MOCHI archives (<output_dir>/<YYYY>/<YYYY-MM>.mochi, in-archive path
<SUBFOLDER>/<filename>) and indexes every file in SQLite.

    resume leftover staging -> query -> scan -> stage -> commit -> verify -> index -> delete

A file is always in the source tree, staging, or a committed and verified
archive. Files are moved into staging by rename (local disk), or copied there
(network drive: the original stays in the source until it is archived).
Staged copies, and copied originals, are deleted only after their month's
archive was verified and indexed; on any failure they stay, and the next run
resumes them first.
"""
import argparse
import base64
import contextlib
import copy
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
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import blake3

log = logging.getLogger("logs_backup")

DELIMITERS = "_.-"  # characters allowed on each side of a barcode in a filename
MANIFEST_BATCH = 2000  # manifest lines synced before the matching files are moved
WORKERS = min(os.cpu_count() or 1, 8)  # hashing threads, and months committed side by side
NETWORK_FILE_THREADS = 16  # folders listed, files copied/renamed/deleted at once on a network drive
STAGING_SPACE_MARGIN = 1.1  # copy staging needs the batch's size plus this margin free
BATCH_FILES = 10_000  # files staged and committed per batch: a stop waits for at most one batch's commit
# MOCHI 0.9.16's seven presets for .mochi, fastest to smallest: the Zstandard level passed to
# `mochi create --compression-level` (None: the writer's default, 3). The level is recorded in the
# archive when it is created (spec §13) and kept by every append, so a change applies to new months.
COMPRESSION_PRESETS = {"turbo": -4, "very_fast": -1, "fast": 1, "balanced": None,
                       "maximum": 19, "ultra": 20, "extreme": 22}
APP_NAME = "FinisarLogsBackup"
PROGRESS_EVERY = 100  # files between progress updates while staging
# Windows: start mochi without a console window. The desktop app and FinisarLogsBackup.exe have no
# console, so without this every mochi call (several per batch) flashed up an empty black window.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

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
CREATE TABLE IF NOT EXISTS run_pace (
    id INTEGER PRIMARY KEY,
    scan_session_id INTEGER,
    files INTEGER NOT NULL,
    seconds REAL NOT NULL,
    recorded_at TEXT NOT NULL
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
-- Sessions run on a custom date range: "continue" ignores them when it picks its start date.
CREATE TABLE IF NOT EXISTS custom_window_session (scan_session_id INTEGER PRIMARY KEY REFERENCES scan_session(id));
-- A run's plan: the files it still has to stage. Kept when a run stops, so it can continue later
-- (another day, after a restart) without asking SQL Server or scanning again.
CREATE TABLE IF NOT EXISTS pending_run (scan_session_id INTEGER PRIMARY KEY REFERENCES scan_session(id),
    query_from TEXT, query_to TEXT NOT NULL, custom INTEGER NOT NULL, total_files INTEGER NOT NULL,
    created_at TEXT NOT NULL, stopped_at TEXT);
CREATE TABLE IF NOT EXISTS pending_file (id INTEGER PRIMARY KEY,
    scan_session_id INTEGER NOT NULL REFERENCES pending_run(scan_session_id), entry TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_session_barcode_session ON session_barcode(scan_session_id);
"""


class BackupError(Exception):
    pass


class Stopped(Exception):
    """Raised inside a run when its stop event is set. The run returns status "stopped"."""


def check_stop(stop):
    if stop is not None and stop.is_set():
        raise Stopped


@contextlib.contextmanager
def keep_awake():
    """Windows: the PC does not go to sleep while a run works (a sleep mid-copy breaks the network
    connection to the log folder). The screen may still turn off. Elsewhere: nothing."""
    if os.name != "nt":
        yield
        return
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    try:
        yield
    finally:
        kernel32.SetThreadExecutionState(0x80000000)


def now():
    return f"epoch:{int(time.time())}"


def quiet(step, message="", done=0, total=None):
    """Default progress callback: does nothing. A front end (backup_ui.py) passes its own.

    step is one of "resume", "query", "scan", "stage", "commit"; total None means unknown.
    "stage" and "commit" count files over the whole run (a run works in batches of BATCH_FILES).

    "total" is the whole run: done = files archived so far, total = files to archive, and
    message = the estimated finish time (time.time() seconds, a float), or None while there is
    no pace to go by yet. It is sent before the confirm question too (the expected duration)."""


# ---------------------------------------------------------------- config

def program_dir():
    """The folder this program is in: the .exe's folder when built with build_exe.bat (PyInstaller
    unpacks the code to a temporary folder, so __file__ would point there), else this file's folder."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def settings_dir():
    """Where the desktop app keeps its settings and log: %APPDATA%\\FinisarLogsBackup on Windows,
    ~/.config/FinisarLogsBackup elsewhere. FINISAR_BACKUP_HOME overrides it (tests, portable use)."""
    if os.environ.get("FINISAR_BACKUP_HOME"):
        return Path(os.environ["FINISAR_BACKUP_HOME"])
    base = os.environ.get("APPDATA") if os.name == "nt" else os.environ.get("XDG_CONFIG_HOME")
    return Path(base or Path.home() / ".config") / APP_NAME


def local_data_dir():
    """This PC's own disk, for data that must not roam or cross the network: copy staging when the
    archive is on a network drive, and snapshots of an index kept on one.
    %LOCALAPPDATA%\\FinisarLogsBackup on Windows, ~/.cache/FinisarLogsBackup elsewhere;
    under FINISAR_BACKUP_HOME when set."""
    if os.environ.get("FINISAR_BACKUP_HOME"):
        return Path(os.environ["FINISAR_BACKUP_HOME"]) / "local"
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_CACHE_HOME")
    return Path(base or Path.home() / ".cache") / APP_NAME


def settings_path():
    return settings_dir() / "settings.json"


def load_settings(path=None):
    """The saved settings (the same keys as config.json), or None if none were saved yet."""
    path = Path(path or settings_path())
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise BackupError(f"cannot read the settings {path}: {e}") from e


def save_settings(raw, path=None):
    """Writes the settings atomically (a crash never leaves half a file). Returns the path."""
    path = Path(path or settings_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def find_config(given=None):
    """The settings file: `given` if passed, else config.json in the current folder, else next to this
    program, else the settings the desktop app saved (settings_path())."""
    if given:
        return Path(given)
    places = [Path.cwd() / "config.json", program_dir() / "config.json", settings_path()]
    found = next((p for p in places if p.is_file()), None)
    if not found:
        raise BackupError("no settings found: open the desktop app (FinisarLogsBackup.exe) and fill in Settings,"
                          " or copy config.example.json to config.json in " + str(places[0].parent))
    return found


# ---------------------------------------------------------------- saved password (Windows DPAPI)

def _dpapi(data, encrypt):
    """CryptProtectData / CryptUnprotectData for the current Windows user (no prompt)."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in, blob_out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):  # UI_FORBIDDEN
        raise BackupError("Windows could not " + ("encrypt" if encrypt else "decrypt") + " the saved password"
                          + ("" if encrypt else " (saved by another Windows user?): enter it again in Settings"))
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def can_protect():
    return os.name == "nt"


def protect(text):
    """The password encrypted for this Windows user (DPAPI), as text for the settings file."""
    if not can_protect():
        raise BackupError("saving the password is only available on Windows")
    return base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")


def unprotect(token):
    if not can_protect():
        raise BackupError("the saved password can only be read on Windows, by the user who saved it")
    return _dpapi(base64.b64decode(token), False).decode("utf-8")


class Config:
    def __init__(self, path=None, raw=None):
        """From a settings file (config.json or the app's settings.json), or from `raw`, a dict with
        the same keys (the desktop app)."""
        self.path = Path(path) if path else None
        if raw is None:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        for key in ("source_dir", "output_dir", "db_path"):
            if not raw.get(key):
                raise KeyError(key)
        self.source_dir = Path(raw["source_dir"])
        self.output_dir = Path(raw["output_dir"])
        # "move": files are renamed into staging, which must be on the source drive (default there).
        # "copy": files are copied to staging on a local disk (default <output_dir>/.staging) and
        # deleted from the source only once archived. None: "copy" on a network drive, else "move".
        self.staging_mode = raw.get("staging_mode")
        if self.staging_mode not in (None, "move", "copy"):
            raise BackupError('"staging_mode" in config.json must be "move", "copy" or null')
        self.compression = raw.get("compression") or "balanced"
        if self.compression not in COMPRESSION_PRESETS:
            raise BackupError('"compression" in config.json must be one of '
                              + ", ".join(f'"{k}"' for k in COMPRESSION_PRESETS) + ", or null")
        self.staging_dir_set = bool(raw.get("staging_dir"))
        self.staging_dir = Path(raw["staging_dir"]) if self.staging_dir_set else None  # see mode
        self.db_path = Path(raw["db_path"])
        self.subfolders = raw.get("subfolders", ["BURNIN", "ESS", "JTAG", "STM", "Xray"])
        self.extensions = {e.lstrip(".").lower() for e in raw.get("extensions", ["txt", "zip"])}
        # Renames and deletes at once. None: 16 on a network drive (they wait on the network), else 1.
        self.file_threads = raw.get("file_threads")
        if self.file_threads is not None and not (isinstance(self.file_threads, int)
                                                  and not isinstance(self.file_threads, bool)
                                                  and 1 <= self.file_threads <= 64):
            raise BackupError('"file_threads" in config.json must be a whole number from 1 to 64, or null')
        self.batch_files = raw.get("batch_files") or BATCH_FILES
        if not (isinstance(self.batch_files, int) and not isinstance(self.batch_files, bool)
                and 100 <= self.batch_files <= 1_000_000):
            raise BackupError('"batch_files" in config.json must be a whole number from 100 to 1000000, or null')
        self.recursive = raw.get("recursive", True)  # scan folders inside each subfolder too
        if not isinstance(self.recursive, bool):
            raise BackupError('"recursive" in config.json must be true or false')
        if not self.staging_dir_set:
            self.staging_dir = self.default_staging(self.mode)
        self.lookback_days = raw.get("lookback_days", 2)
        self.date_range = raw.get("date_range")  # optional ["YYYY-MM-DD", "YYYY-MM-DD"]
        self.mochi = raw.get("mochi")  # None: found by check_mochi() (PATH, cargo's bin, a MOCHI checkout)
        self.email = raw.get("email")  # optional {"api_base_url": ..., "to": ...}
        # SQL Server login: "sql_server" in config.json, each value overridden by its DB_* variable.
        # A missing password is asked for by the front end (hidden) instead of being stored, unless the
        # desktop app saved it encrypted for this Windows user ("password_dpapi").
        sql, env = raw.get("sql_server") or {}, os.environ
        self.sql = {key: env.get(var) or sql.get(key) for key, var in (
            ("server", "DB_SERVER"), ("port", "DB_PORT"), ("database", "DB_NAME"),
            ("user", "DB_USER"), ("password", "DB_PASSWORD"), ("encryption", "DB_ENCRYPTION"))}
        if not self.sql["password"] and sql.get("password_dpapi"):
            self.sql["password"] = unprotect(sql["password_dpapi"])
        for name in ("source_dir", "output_dir", "staging_dir", "db_path"):
            if any(ord(c) < 32 for c in str(getattr(self, name))):  # "E:\backup" in JSON is E: + backspace + ackup
                raise BackupError(f"{name} in config.json has a broken path: write Windows paths with / (E:/backup)")
        for sub in self.subfolders:
            if not sub or sub in (".", "..") or any(c in sub for c in "/\\:"):
                raise BackupError(f"subfolder {sub!r} must be a single plain folder name")

    @property
    def on_network(self):
        if not hasattr(self, "_on_network"):
            self._on_network = is_network_path(self.source_dir)
        return self._on_network

    @property
    def threads(self):
        """Threads for work in source_dir: folder listings, copies, renames and deletes."""
        if self.file_threads is None:
            self.file_threads = NETWORK_FILE_THREADS if self.on_network else 1
        return self.file_threads

    @property
    def mode(self):
        """How files get into staging: "copy" (network drive by default) or "move"."""
        return self.staging_mode or ("copy" if self.on_network else "move")

    def default_staging(self, mode):
        """Move: next to the logs (a rename must stay on their drive). Copy: on a local disk, which is
        the archive folder's drive unless that is a network drive too; then this PC's own disk."""
        if mode == "move":
            return self.source_dir / ".staging"
        return local_data_dir() / "staging" if is_network_path(self.output_dir) else self.output_dir / ".staging"

    @property
    def next_staging_dir(self):
        """The second staging folder, next to the first: the next batch is staged there while the
        current one commits. A sibling, not inside: a batch's clean-up walks its whole folder."""
        return self.staging_dir.with_name(self.staging_dir.name + "-2")

    def staging_dirs(self):
        """Every folder an earlier run may have left a batch in: the configured one and its second
        folder, and with no staging_dir set, every default there has been (the mode or the defaults
        may have changed)."""
        firsts = [self.staging_dir]
        if not self.staging_dir_set:
            firsts += [d for d in (self.default_staging("move"), self.output_dir / ".staging",
                                   local_data_dir() / "staging") if d not in firsts]
        return [d for first in firsts for d in (first, first.with_name(first.name + "-2"))]

    def with_staging(self, folder):
        other = copy.copy(self)
        other.staging_dir = folder
        return other

    @property
    def manifest(self):
        return self.staging_dir / "manifest.jsonl"

    @property
    def journal(self):
        """Copy staging: one line per finished copy with the BLAKE3 of the bytes read from the source."""
        return self.staging_dir / "copied.jsonl"

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


def check_window(window):
    """A custom (from, to) window as two ISO dates, from <= to. Raises BackupError otherwise."""
    try:
        start, end = (dt.date.fromisoformat(str(d)) for d in window)
    except (TypeError, ValueError) as e:
        raise BackupError(f"the date range must be two dates YYYY-MM-DD: {window!r}") from e
    if start > end:
        raise BackupError(f"the date range starts after it ends: {start} .. {end}")
    return start.isoformat(), end.isoformat()


def query_window(db, cfg, window=None):
    """(from, to). A custom window (argument, else "date_range" in the settings) is used as is.
    Otherwise it continues: from = the last successful continuing run's to-date - 1 day (None on the
    first run), to = today - lookback_days. Custom-range runs never move where "continue" starts."""
    if window or cfg.date_range:
        return check_window(window or cfg.date_range)
    to = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=cfg.lookback_days)
    row = db.execute("SELECT query_to_date FROM scan_session s WHERE status = 'success'"
                     " AND query_to_date != 'resumed' AND NOT EXISTS (SELECT 1 FROM custom_window_session c"
                     " WHERE c.scan_session_id = s.id) ORDER BY id DESC LIMIT 1").fetchone()
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


def continue_window(cfg):
    """The window a "continue" run would use now, for display. Does not create the index."""
    if not cfg.db_path.is_file():  # first run: everything up to today - lookback_days
        db = sqlite3.connect(":memory:")
        db.executescript(SCHEMA)
    else:
        db = open_index(cfg.db_path)
    try:
        return query_window(db, cfg)
    finally:
        db.close()


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


def path_key(path):
    """A path compared without touching the disk (resolve() would cost a network round trip)."""
    return os.path.normcase(os.path.abspath(path))


def walk_files(top, recursive, skip, on_error, threads=1):
    """Yields (folder path below `top` ending in "/", or "", DirEntry) for each file in `top`, and with
    `recursive` in every folder below it. Up to `threads` folders are listed at once (on a network
    drive each listing mostly waits on the network); only folders being listed are held in memory.
    Symlinks are not followed and folders whose path_key is in `skip` are not entered. An unreadable
    folder is passed to on_error(folder, error) and skipped."""
    def listing(job):
        folder, rel = job
        files, folders = [], []
        try:
            with os.scandir(folder) as items:
                for item in items:
                    if item.is_dir(follow_symlinks=False):
                        if recursive and path_key(item.path) not in skip:
                            folders.append((Path(item.path), f"{rel}{item.name}/"))
                    elif item.is_file(follow_symlinks=False):
                        files.append(item)
        except OSError as e:
            return folder, rel, files, folders, e
        return folder, rel, files, folders, None

    with ThreadPoolExecutor(max(threads, 1)) as pool:
        pending = {pool.submit(listing, (Path(top), ""))}
        try:
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for job in done:
                    folder, rel, files, folders, error = job.result()
                    pending |= {pool.submit(listing, f) for f in folders}
                    if error:
                        on_error(folder, error)
                    for item in files:
                        yield rel, item
        finally:  # the caller stopped early (a stop, an error): list nothing more
            for job in pending:
                job.cancel()


def scan(cfg, dates, report=quiet, stop=None):
    """Matched files as manifest entries, plus error and scanned-file counts. Each subfolder is scanned
    at every depth (config "recursive", default on); the staging and output folders are never entered."""
    entries, errors, seen = [], 0, 0
    started = time.monotonic()
    skip = {path_key(p) for p in (*cfg.staging_dirs(), cfg.output_dir)}

    def unreadable(folder, e):
        nonlocal errors
        log.warning("skipping folder %s: %s", folder, e)
        errors += 1

    for sub in cfg.subfolders:
        for rel, item in walk_files(cfg.source_dir / sub, cfg.recursive, skip, unreadable, cfg.threads):
            seen += 1
            if seen % 200 == 0:
                check_stop(stop)
            if seen % 2000 == 0:
                report("scan", f"{len(entries):,} matches", seen)
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
            st = item.stat()  # on Windows from the folder listing: no extra round trip
            entries.append({"relative_path": f"{sub}/{rel}{item.name}", "source_subfolder": sub, "barcode": barcode,
                            "original_size": st.st_size, "mtime_ns": st.st_mtime_ns, "volume_name": month})
    report("scan", f"{len(entries):,} matches", seen, seen)
    log.info("scanned %d files, %d matches, in %s", seen, len(entries), seconds_text(time.monotonic() - started))
    return entries, errors, seen


def readonly_uri(path):
    """A SQLite URI that opens `path` read-only. Works for a UNC path (\\\\server\\share\\...): a plain
    f"file:{path.as_posix()}" gives file://server/share/..., which SQLite rejects ("invalid uri authority").
    Here the authority is left empty (file:////server/share/...), so SQLite hands //server/share/... to Windows.
    Characters such as space, #, ? and % are escaped."""
    text = str(path).replace("\\", "/")
    if not text.startswith("//") and not (len(text) > 1 and text[1] == ":") and not text.startswith("/"):
        text = Path(path).absolute().as_posix()  # a relative path
    if text.startswith("//"):
        text = "//" + text  # UNC: empty authority, then //server/share/...
    elif not text.startswith("/"):
        text = "/" + text  # C:/x -> /C:/x (SQLite drops the slash before a drive letter on Windows)
    return "file:" + urllib.parse.quote(text, safe="/:") + "?mode=ro"


def is_network_path(path):
    """True for a UNC path (//server/share) or a mapped network drive on Windows."""
    text = str(path).replace("\\", "/")
    if text.startswith("//"):
        return True
    if os.name != "nt":
        return False
    import ctypes
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return bool(drive) and ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE


def each(threads, fn, items):
    """fn over items, in order; on `threads` threads when that is more than one."""
    if threads <= 1:
        yield from map(fn, items)
        return
    with ThreadPoolExecutor(threads) as pool:
        yield from pool.map(fn, items)


def staged_path(cfg, entry):
    return cfg.staging_dir / entry["volume_name"] / entry["relative_path"]


# ---------------------------------------------------------------- free space

def free_space(path):
    """Free bytes on the drive of `path` (its nearest existing folder), or None if it cannot be read
    (a network drive not connected)."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def _drive(path):
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return os.stat(p).st_dev
    except OSError:
        return str(path)


def space_needed(cfg, entries):
    """Free space for archiving `entries`, checked before anything moves. A list of
    {what, path, free, need, required, ok}:

    - staging (copy staging only): the largest batch plus 10%. Required: without it nothing can
      be staged, and the run is refused.
    - archives: the files' full size. Not required: MOCHI compresses, so the archive grows by
      less; a shortfall is a warning.
    - index copy (index on a network drive): one more copy on this PC. Not required.
    Places on the same drive are counted together. `ok` is None when the free space cannot be read."""
    ordered = sorted(entries, key=lambda e: (e["volume_name"], e["relative_path"]))
    batch = max((sum(e["original_size"] for e in ordered[i:i + cfg.batch_files])
                 for i in range(0, len(ordered), cfg.batch_files)), default=0)
    rows = []
    if cfg.mode == "copy":
        rows.append({"what": "staging (this batch at a time)", "path": cfg.staging_dir,
                     "need": int(batch * STAGING_SPACE_MARGIN), "required": True})
    rows.append({"what": "archives (at most: MOCHI compresses)", "path": cfg.output_dir,
                 "need": sum(e["original_size"] for e in entries), "required": False})
    if cfg.db_path.is_file() and is_network_path(cfg.db_path):
        rows.append({"what": "index copy on this PC", "path": local_data_dir(),
                     "need": cfg.db_path.stat().st_size, "required": False})
    used = defaultdict(int)  # what the rows before need on the same drive
    for row in rows:
        drive = _drive(row["path"])
        row["free"] = free_space(row["path"])
        used[drive] += row["need"]
        row["ok"] = None if row["free"] is None else row["free"] >= used[drive]
    return rows


def space_text(row):
    gib = lambda n: f"{n / 2**30:.1f} GiB" if n >= 2**30 else f"{n / 2**20:.0f} MiB"  # noqa: E731
    free = "unknown free" if row["free"] is None else f"{gib(row['free'])} free"
    return f"{row['what']}: {row['path']}: {free}, needs {gib(row['need'])}"


def check_space(cfg, entries):
    """space_needed, logged; raises BackupError (before anything moves) if a required place is short."""
    rows = space_needed(cfg, entries)
    for row in rows:
        if row["ok"] is False and row["required"]:
            raise BackupError(f"not enough free space: {space_text(row)}. Nothing was moved. Free space there,"
                              " lower the batch size, or choose another staging folder (Settings)")
        level = logging.WARNING if row["ok"] is False else logging.INFO
        log.log(level, "free space %s%s", space_text(row), "  (may be too little)" if row["ok"] is False else "")
    return rows


# ---------------------------------------------------------------- stage

def stage(cfg, sid, entries, report=quiet, stop=None, skipped=None):
    """Puts files into staging/<YYYY-MM>/<SUB>/; the manifest lists each file before it is touched.

    cfg.mode "move": renamed there (staging is on the source drive); the staged file is the only copy.
    cfg.mode "copy": copied there (staging on a local disk); the original stays in the source until
    its archive is verified, then finish_staged deletes it if it did not change meanwhile.
    Returns the number of files not staged (they stay in the source; a later run picks them up).
    A set `stop` ends it early: files not staged yet stay in the source (and are added to `skipped`,
    a list, if given), the staged ones are finished by the next run (as a resumed batch).
    A file no longer in the source (archived or removed since the scan) is skipped quietly; if the
    whole log folder is gone (network down), it raises BackupError instead.
    Copy staging hashes what it reads (BLAKE3) and writes it to the journal, so finish_staged can
    check each local copy before it is committed (a copy damaged by a power cut is redone).
    Works on cfg.threads files at once: one on a local disk (measured, threads made renames slower
    there), several on a network drive, where each file mostly waits on the network."""
    errors, mode = 0, cfg.mode
    cfg.staging_dir.mkdir(parents=True, exist_ok=True)
    if mode == "move" and os.stat(cfg.staging_dir).st_dev != os.stat(cfg.source_dir).st_dev:
        raise BackupError(f"staging_dir {cfg.staging_dir} must be on the same drive as source_dir (files move by"
                          ' rename), or set "staging_mode": "copy"')
    if mode == "copy":
        need = sum(e["original_size"] for e in entries) * STAGING_SPACE_MARGIN
        free = shutil.disk_usage(cfg.staging_dir).free
        if free < need:
            raise BackupError(f"staging_dir {cfg.staging_dir} has {free / 2**30:.1f} GiB free; copying this batch"
                              f" needs {need / 2**30:.1f} GiB. Free space there, or set staging_dir to a bigger disk.")
    for d in {staged_path(cfg, e).parent for e in entries}:
        d.mkdir(parents=True, exist_ok=True)

    copied = []  # (relative_path, blake3) of finished copies, journaled after each batch

    def gone(src):
        if not cfg.source_dir.is_dir():
            raise BackupError(f"the log folder {cfg.source_dir} cannot be reached any more; stopped."
                              " Nothing is lost: run again when it is back")
        log.info("no longer in the source (skipped): %s", src)
        return 0

    def move(e):
        src, dst = cfg.source_dir / e["relative_path"], staged_path(cfg, e)
        try:
            # Never overwrite: it may be the only copy. Windows' rename already refuses an
            # existing target, so the extra check (a network round trip) is only needed elsewhere.
            if os.name != "nt" and dst.exists():
                raise FileExistsError(dst)
            os.rename(src, dst)
            return 0
        except FileNotFoundError:
            return gone(src)
        except OSError as err:  # stays in the source; a later run picks it up
            log.warning("not staged %s: %s", src, err)
            return 1

    def copy_in(e):
        src, dst = cfg.source_dir / e["relative_path"], staged_path(cfg, e)
        tmp = dst.with_name(dst.name + ".partial")  # a staged file is always whole
        try:
            if dst.exists():  # local: cheap
                raise FileExistsError(dst)
            digest, size = blake3.blake3(), 0
            with open(src, "rb") as f, open(tmp, "wb") as out:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
            if size != e["original_size"]:  # changed since the scan: a later run takes it
                raise OSError(f"changed since the scan ({size} bytes, scanned {e['original_size']})")
            os.utime(tmp, ns=(e["mtime_ns"], e["mtime_ns"]))  # the archive keeps it; from the scan, no round trip
            os.replace(tmp, dst)
            copied.append((e["relative_path"], digest.hexdigest()))
            return 0
        except FileNotFoundError as err:
            with contextlib.suppress(OSError):
                tmp.unlink()
            if err.filename and Path(err.filename) == src:
                return gone(src)
            log.warning("not staged %s: %s", src, err)
            return 1
        except OSError as err:  # the original was not touched
            log.warning("not staged %s: %s", src, err)
            with contextlib.suppress(OSError):
                tmp.unlink()
            return 1

    journal = open(cfg.journal, "a", encoding="utf-8") if mode == "copy" else None
    with open(cfg.manifest, "w", encoding="utf-8") as manifest, journal or contextlib.nullcontext():
        manifest.write(json.dumps({"scan_session_id": sid, "mode": mode, "journal": mode == "copy"}) + "\n")
        for i in range(0, len(entries), MANIFEST_BATCH):
            batch = entries[i:i + MANIFEST_BATCH]
            manifest.writelines(json.dumps(e) + "\n" for e in batch)
            manifest.flush()
            os.fsync(manifest.fileno())  # the batch is listed on disk before any of it moves
            put = copy_in if mode == "copy" else move

            def attempt(e):
                if stop is not None and stop.is_set():
                    if skipped is not None:
                        skipped.append(e)
                    return 0
                return put(e)

            try:
                for n, failed in enumerate(each(cfg.threads, attempt, batch), i + 1):
                    errors += failed
                    if n % PROGRESS_EVERY == 0 or n == i + len(batch):
                        report("stage", "copying to local staging" if mode == "copy" else "", n, len(entries))
            finally:  # journal what was copied, even when the batch ends with an error
                if journal:
                    journal.writelines(json.dumps({"p": p, "h": h}) + "\n" for p, h in copied)
                    journal.flush()
                    os.fsync(journal.fileno())
                    copied.clear()
            if stop is not None and stop.is_set():
                if skipped is not None:
                    skipped.extend(entries[i + len(batch):])
                break
    return errors


def read_journal(cfg):
    """{relative_path: blake3} of the copies journaled in staging ({} if none). A truncated last line
    is skipped: that copy counts as unverified and is redone."""
    found = {}
    try:
        lines = cfg.journal.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return found
    for line in lines:
        try:
            row = json.loads(line)
            found[row["p"]] = row["h"]
        except (ValueError, KeyError, TypeError):
            pass
    return found


def read_manifest(cfg):
    """(session id, entries, mode, journaled) or None. A truncated last line is skipped. Manifests
    written before copy staging existed have no mode: they are "move"; before copies were journaled,
    journaled is False (their copies cannot be checked against the source)."""
    try:
        lines = cfg.manifest.read_text(encoding="utf-8").splitlines()
        head = json.loads(lines[0])
        sid, mode, journaled = head["scan_session_id"], head.get("mode", "move"), head.get("journal", False)
    except (OSError, IndexError, KeyError, ValueError):
        return None
    entries = []
    for line in lines[1:]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass
    return sid, entries, mode, journaled


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
            p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60, **NO_WINDOW)
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
    started = time.monotonic()
    try:
        p = subprocess.run([cfg.mochi, "--json", "--no-local-history", *map(str, args)],
                           capture_output=True, text=True, **NO_WINDOW)
    except OSError as e:
        raise BackupError(f"the mochi program could not be started ({cfg.mochi}): {e}") from e
    took = time.monotonic() - started
    if took >= 1:
        log.info("mochi %s %s: %s", args[0], Path(str(args[1])).name, seconds_text(took))
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
    if archive.exists():  # keeps the level the archive was created with
        out = mochi(cfg, "append", archive, *inputs)
    else:
        level = COMPRESSION_PRESETS[cfg.compression]
        out = mochi(cfg, "create", archive, *([f"--compression-level={level}"] if level is not None else []),
                    *inputs)
    if out.get("durability") != "durable":
        log.warning("%s: DEGRADED (not an error): %s", month, out.get("durability_note"))


def commit_month(cfg, month, entries, resume=False, stop=None, known=None, phase=None):
    """Commits a month's staged files (one commit), then checks every file and the archive.

    Touches neither SQLite nor staging. Returns (hashes, head). A set `stop` is checked before
    the commit starts; a running `mochi` is never interrupted (that could leave a broken tail).
    `known`: {path: blake3} of the staged files, already checked (copy staging): not hashed again.
    `phase(text)`, if given, is told what it is doing now (mochi runs can take minutes)."""
    phase = phase or (lambda text: None)
    check_stop(stop)
    archive, paths = cfg.archive(month), [e["relative_path"] for e in entries]
    with ThreadPoolExecutor(WORKERS) as pool:
        hashing = ([known[p] for p in paths] if known is not None
                   else hash_all(pool, [staged_path(cfg, e) for e in entries]))  # runs while mochi commits
        if resume:
            # A crash between commit and index leaves the batch already committed:
            # commit only if the head does not hold every file with the same hash.
            hashes = dict(zip(paths, hashing))
            phase("checking what the archive already holds")
            head, _ = head_files(cfg, archive)
            if any(head.get(p) != h for p, h in hashes.items()):
                phase("writing the archive (mochi)")
                commit(cfg, month)
        else:
            phase("writing the archive (mochi)")
            commit(cfg, month)
            hashes = dict(zip(paths, hashing))
    phase("checking every file against the archive")
    head, head_id = head_files(cfg, archive)
    bad = [p for p, h in hashes.items() if head.get(p) != h]
    if bad:
        raise BackupError(f"{month}: {len(bad)} staged files differ from the archive head, e.g. {bad[0]}")
    phase("verifying the archive (mochi)")
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


def release(cfg, e, mode):
    """After e's archive is verified and indexed: deletes its staged file, and with copy staging
    its original in the source, unless the original changed since the scan (then it stays, and a
    later run archives the new content). Returns 1 if an original was kept, else 0."""
    kept = 0
    if mode == "copy":
        src = cfg.source_dir / e["relative_path"]
        try:
            st = os.stat(src)
            if (st.st_size, st.st_mtime_ns) != (e["original_size"], e.get("mtime_ns")):
                log.warning("kept %s in the source: it changed after it was copied", src)
                kept = 1
            else:
                os.unlink(src)
        except FileNotFoundError:
            pass  # already deleted (a resumed batch)
        except OSError as err:
            log.warning("kept %s in the source (it is archived): %s", src, err)
            kept = 1
    staged_path(cfg, e).unlink()
    return kept


def verify_copies(cfg, months, known):
    """Copy staging: each staged copy must hash to what was read from the source when it was copied
    (the journal). A copy that does not (damaged by a power cut, the PC going to sleep, a disk
    error) or that was never journaled is deleted and left out: its original is still in the
    source, so a later run copies it again. Fills `known` with the checked hashes; returns how
    many copies were redone."""
    journal = read_journal(cfg)

    def check(e):
        want = journal.get(e["relative_path"])
        return want if want and b3(staged_path(cfg, e)) == want else None

    redone = 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for month, es in months.items():
            good = []
            for e, h in zip(es, pool.map(check, es)):
                if h:
                    known[e["relative_path"]] = h
                    good.append(e)
                else:
                    log.warning("copy of %s could not be verified (damaged or unfinished); deleted, the original"
                                " stays in the source for the next run", e["relative_path"])
                    staged_path(cfg, e).unlink()
                    redone += 1
            months[month] = good
    return redone


def finish_staged(db, cfg, report=quiet, resume=False, stop=None):
    """Archives whatever the manifest lists in staging. Returns a summary dict (with "archived", the
    archived entries), or None if nothing is staged.

    Reports "commit" in files of this batch. A set `stop` lets months already committing finish
    (and be indexed and cleaned up), starts no other, keeps the manifest and raises Stopped:
    the next run resumes what is left."""
    manifest = read_manifest(cfg)
    if manifest is None:
        return None
    sid, listed, mode, journaled = manifest
    if not db.execute("SELECT 1 FROM scan_session WHERE id = ?", (sid,)).fetchone():
        log.warning("staging references missing session %s; recording into a new one", sid)
        sid = start_session(db, "resumed", "resumed")
    months = defaultdict(list)
    present = each(cfg.threads, lambda e: staged_path(cfg, e).is_file(), listed)
    for e, moved in zip(listed, present):
        if moved:  # listed but never moved: still in the source
            months[e["volume_name"]].append(e)

    archived, failures, errors, stopped = [], [], 0, False
    known = {}
    total = sum(len(es) for es in months.values())
    if mode == "copy" and journaled:
        report("commit", "checking the local copies", 0, total)
        errors += verify_copies(cfg, months, known)
        total = sum(len(es) for es in months.values())
    report("commit", "committing " + ", ".join(sorted(months)), 0, total)

    def phase(month):  # called from the commit threads: what each month is doing now
        return lambda text: report("commit", f"{month} {text}", len(archived), total)

    with ThreadPoolExecutor(min(WORKERS, max(len(months), 1))) as pool:
        jobs = {m: pool.submit(commit_month, cfg, m, es, resume, stop,
                               {e["relative_path"]: known[e["relative_path"]] for e in es} if known else None,
                               phase(m))
                for m, es in months.items() if es}
        for month, job in sorted(jobs.items()):  # SQLite and deletes stay on this thread
            try:
                hashes, head = job.result()
                report("commit", f"{month} saving to the index", len(archived), total)
                index_month(db, cfg, sid, month, months[month], hashes, head)
            except Stopped:
                stopped = True
                continue
            except (BackupError, OSError, sqlite3.Error) as e:
                log.error("%s kept in staging: %s", month, e)
                failures.append(e)
                continue
            report("commit", f"{month} cleaning up", len(archived), total)
            kept = 0
            for n, k in enumerate(each(cfg.threads, lambda e: release(cfg, e, mode), months[month]), 1):
                kept += k
                if n % PROGRESS_EVERY == 0:  # deleting on a network drive takes a while: show it moving
                    report("commit", f"{month} cleaning up", len(archived) + n, total)
            errors += kept
            archived += months[month]
            log.info("%s: %d files committed (LOCAL_COMMITTED), verified and indexed", month, len(months[month]))
            report("commit", month, len(archived), total)
    if failures:
        raise BackupError(f"{len(failures)} month(s) failed; staging kept for the next run: {failures[0]}")
    if stopped:
        log.info("stopped: %d files archived, the rest stays in staging for the next run", len(archived))
        raise Stopped

    cfg.manifest.unlink()
    with contextlib.suppress(FileNotFoundError):
        cfg.journal.unlink()
    leftovers = 0
    for d, _, files in sorted(os.walk(cfg.staging_dir), key=lambda w: -len(w[0])):
        for f in files:  # not in the manifest: never deleted
            if mode == "copy" and f.endswith(".partial"):  # a copy cut short; the original is in the source
                os.unlink(Path(d) / f)
                continue
            log.warning("left in staging (not listed in the manifest): %s", Path(d) / f)
            leftovers += 1
        if not os.listdir(d):
            os.rmdir(d)
    barcodes = len({e["barcode"] for e in archived})
    if resume:  # a batch left by an earlier run: add to that run's totals
        with db:
            db.execute("UPDATE scan_session SET completed_at = ?, total_files_moved = total_files_moved + ?,"
                       " total_errors = total_errors + ?, total_barcodes = CASE WHEN query_to_date = 'resumed'"
                       " THEN total_barcodes + ? ELSE total_barcodes END, status = CASE WHEN EXISTS (SELECT 1"
                       " FROM pending_run WHERE scan_session_id = ?) THEN 'failed' ELSE 'success' END"
                       " WHERE id = ?", (now(), len(archived), leftovers + errors, barcodes, sid, sid))
    return {"files_moved": len(archived), "barcodes": barcodes, "errors": leftovers + errors,
            "volumes": plan(archived), "archived": archived}


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


def run(cfg, candidates_fn=None, dry_run=False, report=quiet, confirm=None, window=None, stop=None,
        continue_stopped=False):
    """One backup run. Returns a summary dict whose "status" is "done", "dry_run", "cancelled" or "stopped".

    confirm(volumes, space) -> bool, if given, is asked after the scan and before any file moves
    (space: the free-space rows of space_needed);
    False stops the run with nothing moved and nothing recorded.
    candidates_fn(date_from, date_to) defaults to querying SQL Server (tests pass their own).
    window: a custom (from, to) date range; None continues from the last run (see query_window).
    stop: a threading.Event; once set, the run stops at the next safe point (between files while
    scanning or staging, before the next month's commit) and returns status "stopped". Nothing is
    lost, and the run's plan (the files it still has to archive) is kept in the index.
    continue_stopped: continue a stopped run (any day later, after a restart): no SQL query, no scan.
    True picks the most recent, a session id a given one (several can be kept). A new continuing
    run replaces stopped continuing runs once confirmed (it covers their dates); stopped
    custom-range runs are always kept until continued or discarded."""
    candidates_fn = candidates_fn or (lambda date_from, date_to: query_candidates(cfg.sql, date_from, date_to))
    if window:
        window = check_window(window)
    with lock_run(cfg), keep_awake():
        return _run(cfg, candidates_fn, dry_run, report, confirm, window, stop, continue_stopped)


def prepare_folders(cfg):
    """Creates the output, staging and index folders if they do not exist yet. The source folder is
    never created: a missing one is a wrong setting or a network drive that is not connected. With
    staging inside it (move staging), that stops the run; otherwise the scan finds nothing."""
    if not cfg.source_dir.is_dir() and path_key(cfg.staging_dir).startswith(path_key(cfg.source_dir) + os.sep):
        raise BackupError(f"the log folder {cfg.source_dir} cannot be reached (a network drive not connected?)."
                          " Nothing was changed; try again when it is available")
    for name, folder in (("output_dir", cfg.output_dir), ("staging_dir", cfg.staging_dir),
                         ("db_path's folder", cfg.db_path.parent)):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise BackupError(f"cannot create {name} {folder}: {e.strerror or e}") from e
    if not cfg.source_dir.is_dir():
        log.warning("source_dir %s does not exist: nothing to scan", cfg.source_dir)


def _run(cfg, candidates_fn, dry_run, report, confirm, window=None, stop=None, continue_stopped=False):
    # Checked before anything moves: without mochi, staged files could not be committed.
    try:
        log.info("using %s", check_mochi(cfg))
    except BackupError as e:
        if not dry_run:
            raise
        log.warning("a real run would stop here: %s", e)
    if not dry_run:
        prepare_folders(cfg)
    log.info("source_dir %s (%s): %s staging in %s, %d thread(s), batches of %d files", cfg.source_dir,
             "network drive" if cfg.on_network else "local disk", cfg.mode, cfg.staging_dir, cfg.threads,
             cfg.batch_files)
    if not dry_run:
        snapshot_index(cfg)
    db = open_index(cfg.db_path)
    try:
        return _run_with_index(db, cfg, candidates_fn, dry_run, report, confirm, window, stop, continue_stopped)
    finally:
        db.close()  # an open connection keeps index.db locked on Windows


INDEX_SNAPSHOTS = 5  # local copies of an index that is on a network drive


def snapshot_index(cfg):
    """An index on a network drive: before each run, copies it to this PC's disk (SQLite's backup
    method: a consistent copy) and checks the copy. A damaged index stops the run before anything
    moves, naming the newest good copy to restore from. Keeps the last INDEX_SNAPSHOTS copies.
    SQLite on a network share is safe with one writer (the run lock), but a dropped connection in
    the middle of a write can damage it. Returns the copy's path, or None (local index, or none yet)."""
    if not cfg.db_path.is_file() or not is_network_path(cfg.db_path):
        return None
    folder = local_data_dir() / "index-snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    earlier = sorted(folder.glob("index-*.db"))
    target = folder / f"index-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    try:
        source = sqlite3.connect(readonly_uri(cfg.db_path), uri=True)
        copy_db = sqlite3.connect(target)
        try:
            source.backup(copy_db)
            check = copy_db.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            copy_db.close()
            source.close()
    except sqlite3.Error as e:
        check = str(e)
    if check != "ok":
        with contextlib.suppress(OSError):
            target.unlink()
        good = earlier[-1] if earlier else None
        raise BackupError(f"the index {cfg.db_path} looks damaged ({check}). Nothing was changed. "
                          + (f"Restore it from the copy taken before the last run: {good}" if good
                             else "Restore it from your last export (Tools → Export)."))
    for old in earlier[:max(len(earlier) + 1 - INDEX_SNAPSHOTS, 0)]:
        with contextlib.suppress(OSError):
            old.unlink()
    log.info("index on a network drive: checked, and copied to %s", target)
    return target


def seconds_text(seconds):
    """1 h 05 min, 12 min 30 s, 8 s: for the log."""
    seconds = int(round(seconds))
    h, m, sec = seconds // 3600, seconds // 60 % 60, seconds % 60
    return f"{h} h {m:02d} min" if h else f"{m} min {sec:02d} s" if m else f"{sec} s"


PACE_RUNS = 5  # earlier runs whose pace estimates a new run before its first batch is done


def earlier_pace(db):
    """Seconds per file of the last PACE_RUNS runs (staging, commit, verify, index and clean-up;
    not the query or the scan), or None if there is no history yet. Runs from before this was
    recorded count by their start and end times, the median of them (a run continued another
    day would otherwise count its days off)."""
    rows = db.execute("SELECT files, seconds FROM run_pace WHERE files >= 100 ORDER BY id DESC LIMIT ?",
                      (PACE_RUNS,)).fetchall()
    if rows:
        return sum(r[1] for r in rows) / sum(r[0] for r in rows)
    paces = []
    for started, completed, files in db.execute(
            "SELECT started_at, completed_at, total_files_moved FROM scan_session WHERE status = 'success'"
            " AND total_files_moved >= 100 AND started_at LIKE 'epoch:%' AND completed_at LIKE 'epoch:%'"
            " ORDER BY id DESC LIMIT ?", (PACE_RUNS,)):
        seconds = int(completed[6:]) - int(started[6:])
        if seconds > 0:
            paces.append(seconds / files)
    return sorted(paces)[len(paces) // 2] if paces else None


class Pace:
    """When a run's archiving should end. Before its first batch is done it goes by earlier runs
    (`history`, seconds per file, or None: no estimate yet); then by this run's own batches. The
    running batch counts down, so the finish time holds still while mochi works."""

    def __init__(self, files, history=None):
        self.files, self.history = files, history
        self.start = self.batch_start = time.monotonic()
        self.done_files, self.done_seconds = 0, 0.0
        self.step_seconds = defaultdict(float)  # "stage", "commit": for the log and the summary

    def batch_done(self, files):
        t = time.monotonic()
        self.done_files += files
        self.done_seconds, self.batch_start = t - self.start, t

    def per_file(self):
        return self.done_seconds / self.done_files if self.done_files else self.history

    def finish_at(self):
        """time.time() when everything should be archived, or None (no pace yet)."""
        per = self.per_file()
        if not per:
            return None
        left = per * (self.files - self.done_files) - (time.monotonic() - self.batch_start)
        return time.time() + max(left, 0)

    def seconds(self):
        return time.monotonic() - self.start


def _offset(report, step, offset, total, label=""):
    """A report callback that shows one batch's progress as part of the whole run."""
    def batch_report(_step, message="", done=0, _total=None):
        report(step, " ".join(m for m in (label, message) if m), offset + (done or 0), total)
    return batch_report


def _stopped(info, resumed, archived, errors, sid=None, left=0):
    log.info("run stopped: %d files archived in this run, %d left; the next run continues", len(archived), left)
    return dict(info, status="stopped", resumed=resumed, files_moved=len(archived),
                barcodes=len({e["barcode"] for e in archived}), volumes=plan(archived), errors=errors,
                session_id=sid, unmatched=[], left=left)


# ---------------------------------------------------------------- the kept plan of a stopped run

def save_plan(db, sid, window, custom, entries):
    """Records the files a run is about to archive. Returns their row ids, in the same order."""
    with db:
        db.execute("INSERT INTO pending_run (scan_session_id, query_from, query_to, custom, total_files, created_at)"
                   " VALUES (?, ?, ?, ?, ?, ?)", (sid, window[0], window[1], int(custom), len(entries), now()))
        first = db.execute("SELECT COALESCE(MAX(id), 0) FROM pending_file").fetchone()[0] + 1
        db.executemany("INSERT INTO pending_file (id, scan_session_id, entry) VALUES (?, ?, ?)",
                       [(first + i, sid, json.dumps(e)) for i, e in enumerate(entries)])
    return list(range(first, first + len(entries)))


def load_plan(db, sid=None):
    """A stopped run's kept plan, or None: {sid, window, custom, total, stopped_at, created_at,
    entries, ids}. `sid` picks one; None the most recent. Several stopped runs can be kept."""
    row = db.execute("SELECT scan_session_id, query_from, query_to, custom, total_files, created_at, stopped_at"
                     " FROM pending_run" + (" WHERE scan_session_id = ?" if sid else "") +
                     " ORDER BY scan_session_id DESC LIMIT 1", (sid,) if sid else ()).fetchone()
    if not row:
        return None
    rows = db.execute("SELECT id, entry FROM pending_file WHERE scan_session_id = ? ORDER BY id", (row[0],)).fetchall()
    return {"sid": row[0], "window": (row[1], row[2]), "custom": bool(row[3]), "total": row[4],
            "created_at": row[5], "stopped_at": row[6], "ids": [r[0] for r in rows],
            "entries": [json.loads(r[1]) for r in rows]}


def drop_plan(db, sid):
    with db:
        db.execute("DELETE FROM pending_file WHERE scan_session_id = ?", (sid,))
        db.execute("DELETE FROM pending_run WHERE scan_session_id = ?", (sid,))


def _stopped_runs(db):
    rows = db.execute("SELECT r.scan_session_id, r.query_from, r.query_to, r.custom, r.total_files,"
                      " COALESCE(r.stopped_at, r.created_at), (SELECT COUNT(*) FROM pending_file f"
                      " WHERE f.scan_session_id = r.scan_session_id) FROM pending_run r"
                      " ORDER BY r.scan_session_id").fetchall()
    return [{"sid": r[0], "window": (r[1], r[2]), "custom": bool(r[3]), "total": r[4], "stopped_at": r[5],
             "left": r[6]} for r in rows]


def stopped_runs(cfg):
    """For display: every stopped run that can continue, oldest first, each {sid, window, custom,
    total, left, stopped_at}. Does not create the index."""
    if not cfg.db_path.is_file():
        return []
    db = open_index(cfg.db_path)
    try:
        return _stopped_runs(db)
    finally:
        db.close()


def stopped_run(cfg):
    """The most recent stopped run (see stopped_runs), or None."""
    runs = stopped_runs(cfg)
    return runs[-1] if runs else None


def discard_stopped_run(cfg, sid=None):
    """Forgets a stopped run's plan (`sid`, else the most recent). Its files stay in the source; a
    later run finds them. A batch already staged is still finished by the next run. Returns how
    many files it listed."""
    with lock_run(cfg):
        db = open_index(cfg.db_path)
        try:
            kept = load_plan(db, sid)
            if not kept:
                return 0
            drop_plan(db, kept["sid"])
            log.info("stopped run %s discarded (%d files left in the source)", kept["sid"], len(kept["entries"]))
            return len(kept["entries"])
        finally:
            db.close()


def _archive(db, cfg, sid, entries, ids, report, stop, info, done_before, total, barcodes, errors):
    """Archives a plan's files in batches of cfg.batch_files: stage, then commit, verify, index and
    clean up, batch after batch. A batch's plan rows are dropped once it is staged (the manifest
    tracks it from then on); files a stop skipped go back into the plan. Records the session's
    totals (added to what earlier days of the same run recorded)."""
    before = db.execute("SELECT total_files_moved, total_errors FROM scan_session WHERE id = ?", (sid,)).fetchone()
    moved_before, errors_before = (before or (0, 0))
    archived = []
    pace = Pace(len(entries), earlier_pace(db))
    done = [done_before]  # files archived so far, over the whole run

    def tracked(step, offset, label):
        """Reports a batch's step, then the whole run with its estimated finish."""
        inner = _offset(report, step, offset, total, label)

        def batch_report(_step, message="", n=0, _total=None):
            inner(_step, message, n, _total)
            if step == "commit":
                done[0] = max(done[0], offset + (n or 0))
            report("total", pace.finish_at(), done[0], total)
        return batch_report

    report("total", pace.finish_at(), done_before, total)
    starts = list(range(0, len(entries), cfg.batch_files))
    slots = [cfg, cfg.with_staging(cfg.next_staging_dir)]  # batches take turns: one stages while one commits

    def start_stage(k):
        """Stages batch k on a background thread, into slot k % 2."""
        i, slot = starts[k], slots[k % 2]
        job = {"k": k, "slot": slot, "batch": entries[i:i + cfg.batch_files], "ids": ids[i:i + cfg.batch_files],
               "skipped": [], "label": f"batch {k + 1}/{len(starts)}" if len(starts) > 1 else ""}

        def work():
            t = time.monotonic()
            failed = stage(slot, sid, job["batch"], tracked("stage", done_before + i, job["label"]), stop,
                           job["skipped"])
            took = time.monotonic() - t
            pace.step_seconds["stage"] += took
            log.info("%sstaged %d files in %s", f"{job['label']}: " if job["label"] else "",
                     len(job["batch"]) - len(job["skipped"]), seconds_text(took))
            return failed

        job["future"] = stager.submit(work)
        return job

    def staged(job):
        """Waits for a batch's staging. Its staged files are in its manifest now, so they leave the
        plan; files a stop skipped stay in it. (If staging raised, the whole batch stays: files
        already staged are then found gone, and skipped.)"""
        failed = job["future"].result()
        keep = {id(e) for e in job["skipped"]}
        with db:
            db.executemany("DELETE FROM pending_file WHERE id = ?",
                           [(pid,) for pid, e in zip(job["ids"], job["batch"]) if id(e) not in keep])
        return failed

    def room_for(k):
        """Copy staging: the next batch may be copied during this commit only if the staging drive
        has room for it beside the batch already there; otherwise it waits (as before)."""
        if cfg.mode != "copy":
            return True
        i = starts[k]
        need = sum(e["original_size"] for e in entries[i:i + cfg.batch_files]) * STAGING_SPACE_MARGIN
        free = free_space(cfg.staging_dir)
        return free is not None and free >= need

    try:
        stager = ThreadPoolExecutor(1, thread_name_prefix="stage")
        following = None  # the next batch, staging while this one commits
        try:
            check_stop(stop)
            current = start_stage(0) if starts else None
            for k in range(len(starts)):
                errors += staged(current)
                check_stop(stop)  # what was staged is finished by the next run
                following = None
                if k + 1 < len(starts) and not (stop is not None and stop.is_set()) and room_for(k + 1):
                    following = start_stage(k + 1)
                elif k + 1 < len(starts):
                    log.info("the next batch waits for this commit: not enough free space for two batches in %s",
                             cfg.staging_dir)
                t = time.monotonic()
                # A commit that has begun is finished, even if Stop is pressed meanwhile (while the next batch
                # stages): a stop waits for at most this batch's commit, as before.
                result = finish_staged(db, current["slot"], tracked("commit", done_before + starts[k], current["label"]))
                pace.step_seconds["commit"] += time.monotonic() - t
                errors += result["errors"]
                archived += result["archived"]
                pace.batch_done(len(current["batch"]))
                label = current["label"]
                log.info("%scommitted, verified and cleaned up %d files in %s; whole run so far %s, about %s left",
                         f"{label}: " if label else "", len(result["archived"]), seconds_text(time.monotonic() - t),
                         seconds_text(pace.seconds()),
                         seconds_text(max(pace.finish_at() - time.time(), 0)) if pace.finish_at() else "?")
                report("total", pace.finish_at(), done[0], total)
                if k + 1 < len(starts):
                    check_stop(stop)
                    current, following = following or start_stage(k + 1), None
        except BaseException:
            # A batch still staging in the background is waited for, so its plan rows match its manifest
            # (the next run finishes that batch from staging). A stop makes it end at its next file.
            if following is not None:
                try:
                    errors += staged(following)
                except Exception as e:
                    log.warning("staging the next batch also failed: %s", e)
            raise
        finally:
            stager.shutdown(wait=True)
    except Stopped:
        save_pace(db, sid, pace, archived)
        left = db.execute("SELECT COUNT(*) FROM pending_file WHERE scan_session_id = ?", (sid,)).fetchone()[0]
        with db:
            db.execute("UPDATE pending_run SET stopped_at = ? WHERE scan_session_id = ?", (now(), sid))
        complete_session(db, sid, "failed", barcodes, moved_before + len(archived), errors_before + errors)
        return _stopped(info, info.get("resumed", 0), archived, errors, sid, left)
    except Exception:
        with db:
            db.execute("UPDATE pending_run SET stopped_at = ? WHERE scan_session_id = ?", (now(), sid))
        complete_session(db, sid, "failed", barcodes, moved_before + len(archived), errors_before + errors + 1)
        raise
    save_pace(db, sid, pace, archived)
    drop_plan(db, sid)
    complete_session(db, sid, "success", barcodes, moved_before + len(archived), errors_before + errors)
    return {**info, "status": "done", "session_id": sid, "errors": errors, "left": 0,
            "files_moved": len(archived), "barcodes": len({e["barcode"] for e in archived}),
            "volumes": plan(archived), "seconds": dict(pace.step_seconds)}


def save_pace(db, sid, pace, archived):
    """Records how long this run took per file (also a stopped one), for the next run's estimate."""
    stage_s, commit_s = pace.step_seconds["stage"], pace.step_seconds["commit"]
    log.info("time: staging %s, commit + verify + index + clean-up %s, %d files archived (%.2f s per file)",
             seconds_text(stage_s), seconds_text(commit_s), len(archived),
             pace.seconds() / len(archived) if archived else 0)
    if archived:
        with db:
            db.execute("INSERT INTO run_pace (scan_session_id, files, seconds, recorded_at) VALUES (?, ?, ?, ?)",
                       (sid, len(archived), pace.seconds(), now()))


def _continue_plan(db, cfg, kept, report, stop, info):
    """Continues a stopped run from its kept plan: no SQL query and no scan."""
    if not cfg.source_dir.is_dir():
        raise BackupError(f"the log folder {cfg.source_dir} cannot be reached; the stopped run is kept."
                          " Try again when it is available")
    sid, entries, total = kept["sid"], kept["entries"], kept["total"]
    info["space"] = check_space(cfg, entries)  # refuses before anything moves; the plan stays
    date_from, date_to = kept["window"]
    log.info("continuing stopped run %s (%s .. %s): %d of %d files left", sid, date_from or "(start)", date_to,
             len(entries), total)
    with db:
        db.execute("UPDATE scan_session SET status = 'running', completed_at = NULL WHERE id = ?", (sid,))
    report("query", f"continuing the stopped run {date_from or '(start)'} .. {date_to}", 1, 1)
    report("scan", f"{len(entries):,} files left (kept from the first scan)", total, total)
    barcodes = db.execute("SELECT total_barcodes FROM scan_session WHERE id = ?", (sid,)).fetchone()
    info.update(window=(date_from, date_to), volumes=plan(entries), matched_files=len(entries),
                matched_barcodes=len({e["barcode"] for e in entries}), continued=True, unmatched=[])
    return _archive(db, cfg, sid, entries, kept["ids"], report, stop, info, total - len(entries), total,
                    barcodes[0] if barcodes else 0, 0)


def _run_with_index(db, cfg, candidates_fn, dry_run, report, confirm, window=None, stop=None,
                    continue_stopped=False):
    resumed, info = 0, {"window": (None, None), "candidates": 0, "scanned": 0, "volumes": {},
                        "matched_barcodes": 0, "matched_files": 0, "resumed": 0}
    try:
        for folder in cfg.staging_dirs() if not dry_run else ():
            leftover = cfg.with_staging(folder)
            if not leftover.manifest.exists():
                continue
            check_stop(stop)
            report("resume", "finishing the batch an earlier run left in staging")
            # The resumed batch's progress is shown on the "resume" row.
            finished = finish_staged(db, leftover, lambda _step, *args: report("resume", *args), resume=True,
                                     stop=stop)
            resumed += finished["files_moved"] if finished else 0
            report("resume", f"{resumed} files", 1, 1)
        check_stop(stop)
    except Stopped:
        return _stopped(info, resumed, [], 0, left=sum(r["left"] for r in _stopped_runs(db)))
    info["resumed"] = resumed

    if continue_stopped:  # True: the most recent stopped run; a number: that one (its session id)
        kept = load_plan(db, None if continue_stopped is True else continue_stopped)
        if kept:
            return _continue_plan(db, cfg, kept, report, stop, info)
        log.info("no stopped run to continue")
        return dict(info, status="done", files_moved=0, barcodes=0, errors=0, session_id=None, unmatched=[], left=0)

    custom = bool(window or cfg.date_range)
    date_from, date_to = query_window(db, cfg, window)
    info["window"] = (date_from, date_to)
    report("query", f"{date_from or '(start)'} .. {date_to}")
    log.info("querying candidates %s .. %s%s", date_from or "(start)", date_to, " (custom range)" if custom else "")
    candidates = candidates_fn(date_from, date_to)
    dates = dict(candidates)  # barcode -> last_update (the last row wins)
    report("query", f"{len(dates):,} candidate barcodes", 1, 1)
    try:
        entries, errors, seen = scan(cfg, dates, report, stop)
    except Stopped:
        return _stopped(dict(info, candidates=len(dates)), resumed, [], 0)
    volumes = plan(entries)
    matched = {e["barcode"] for e in entries}
    info.update(candidates=len(dates), scanned=seen, volumes=volumes,
                matched_barcodes=len(matched), matched_files=len(entries))
    space = check_space(cfg, entries) if entries and not dry_run else (space_needed(cfg, entries) if entries else [])
    info["space"] = space
    history = earlier_pace(db)
    info["expected_seconds"] = history * len(entries) if history and entries else None
    if entries and not dry_run:  # a preview has it in its summary
        if info["expected_seconds"]:
            log.info("expected to take about %s (the pace of earlier runs)", seconds_text(info["expected_seconds"]))
        report("total", time.time() + info["expected_seconds"] if info["expected_seconds"] else None, 0,
               len(entries))
    if dry_run:
        log.info("dry run: %d of %d candidate barcodes would be archived (%d files): %s",
                 len(matched), len(dates), len(entries), volumes)
        for row in space:
            log.info("free space %s", space_text(row))
        return dict(info, status="dry_run", files_moved=0, barcodes=0, errors=errors)
    if entries and confirm and not confirm(volumes, space):
        return dict(info, status="cancelled", files_moved=0, barcodes=0, errors=errors)
    if stop is not None and stop.is_set():
        return _stopped(info, resumed, [], errors)
    if not custom:  # a new continuing run covers the dates of a stopped continuing one: it replaces it
        for old in _stopped_runs(db):     # (stopped custom-range runs are kept: their dates are not covered)
            if not old["custom"]:
                log.info("stopped run %s (%d files left) is replaced by this continuing run, which covers its"
                         " dates", old["sid"], old["left"])
                drop_plan(db, old["sid"])

    sid = start_session(db, date_from or "", date_to)
    try:
        if custom:
            with db:
                db.execute("INSERT INTO custom_window_session (scan_session_id) VALUES (?)", (sid,))
        fresh = [(b, d) for b, d in candidates if not db.execute(
            "SELECT 1 FROM session_barcode WHERE barcode = ? AND last_update = ?", (b, d)).fetchone()]
        with db:
            db.executemany("INSERT OR IGNORE INTO session_barcode (scan_session_id, barcode, last_update, status)"
                           " VALUES (?, ?, ?, ?)",
                           [(sid, b, d, "matched" if b in matched else "no_files_found") for b, d in fresh])
        # Batches of cfg.batch_files, a month at a time where possible (fewer commits).
        entries.sort(key=lambda e: (e["volume_name"], e["relative_path"]))
        ids = save_plan(db, sid, (date_from, date_to), custom, entries)
    except Exception:
        complete_session(db, sid, "failed", errors=errors + 1)
        raise
    summary = _archive(db, cfg, sid, entries, ids, report, stop, info, 0, len(entries), len(fresh), errors)
    if summary["status"] == "done":
        summary["unmatched"] = sorted({b for b, _ in fresh} - matched)
    return summary


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
  3. If staging/ exists: copy its contents into <source_dir>/.staging before the first run,
     and staging-2/ (if there) into <source_dir>/.staging-2.
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
        jobs = [(p, f"archives/{p.relative_to(cfg.output_dir).as_posix()}", "archives") for p in archives]
        for folder, name in ((cfg.staging_dir, "staging"), (cfg.next_staging_dir, "staging-2")):
            staged = sorted(p for p in folder.rglob("*") if p.is_file()) if folder.exists() else []
            jobs += [(p, f"{name}/{p.relative_to(folder).as_posix()}", "staging") for p in staged]
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
    parser.add_argument("--new", action="store_true", help="start a new run even if a stopped one could continue")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config(find_config(args.config))
    if args.export:
        export(cfg, args.export)
        return
    stopped = [] if args.dry_run or args.new else stopped_runs(cfg)  # continued first, oldest first
    for kept in stopped or [None]:
        summary = run(cfg, dry_run=args.dry_run, continue_stopped=kept["sid"] if kept else False)
        log.info("%s: %s", summary["status"], json.dumps({k: v for k, v in summary.items() if k != "unmatched"}))
        if summary["status"] == "done" and cfg.email:
            send_email(cfg, summary)
        if summary["status"] == "stopped":
            break


if __name__ == "__main__":
    main()
