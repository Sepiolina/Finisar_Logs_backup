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
import sqlite3
import subprocess
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

class Config:
    def __init__(self, path):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.source_dir = Path(raw["source_dir"])
        self.output_dir = Path(raw["output_dir"])
        # Staging must be on the source drive: files are moved there by rename.
        self.staging_dir = Path(raw.get("staging_dir") or self.source_dir / ".staging")
        self.db_path = Path(raw["db_path"])
        self.subfolders = raw.get("subfolders", ["BURNIN", "ESS", "JTAG", "STM", "Xray"])
        self.extensions = {e.lstrip(".").lower() for e in raw.get("extensions", ["txt", "zip"])}
        self.lookback_days = raw.get("lookback_days", 2)
        self.date_range = raw.get("date_range")  # optional ["YYYY-MM-DD", "YYYY-MM-DD"]
        self.mochi = raw.get("mochi", "mochi")
        self.email = raw.get("email")  # optional {"api_base_url": ..., "to": ...}
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
    db = sqlite3.connect(path)
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

def query_candidates(date_from, date_to):
    """[(barcode, 'YYYY-MM-DD HH:MM:SS')] for the window. Connection settings come from DB_* env vars."""
    import pymssql  # imported here so the rest of the module works without it

    where, params = "sft.last_update < %s", [date_to]
    if date_from:
        where, params = "sft.last_update >= %s AND " + where, [date_from, date_to]
    sql = ("SELECT DISTINCT sfs.serial_no, CONVERT(VARCHAR(50), sft.last_update, 120)"
           " FROM ForthUtilities.dbo.ShipmentFinisarSerial sfs WITH (NOLOCK)"
           " JOIN ForthUtilities.dbo.ShipmentFinisarTrace sft WITH (NOLOCK) ON sfs.trace_id = sft.trace_id"
           f" WHERE {where}")
    env = os.environ
    missing = [k for k in ("DB_SERVER", "DB_NAME", "DB_USER", "DB_PASSWORD") if not env.get(k)]
    if missing:
        raise BackupError(f"missing environment variable(s): {', '.join(missing)}")
    retries, delay = int(env.get("DB_RETRY_ATTEMPTS", 3)), int(env.get("DB_RETRY_BASE_DELAY_MS", 2000)) / 1000
    for attempt in range(retries + 1):
        try:
            with pymssql.connect(server=env["DB_SERVER"], port=env.get("DB_PORT", "1433"),
                                 database=env["DB_NAME"], user=env["DB_USER"], password=env["DB_PASSWORD"],
                                 timeout=int(env.get("DB_REQUEST_TIMEOUT_MS", 30000)) // 1000) as conn:
                cur = conn.cursor()
                cur.execute(sql, tuple(params))
                return [(str(b).strip(), str(d)) for b, d in cur.fetchall()]
        except pymssql.Error as e:
            if attempt == retries:
                raise BackupError(f"SQL Server query failed: {e}") from e
            log.warning("SQL Server query failed (%s), retrying in %.0fs", e, delay * 2 ** attempt)
            time.sleep(delay * 2 ** attempt)


# ---------------------------------------------------------------- scan

def find_barcodes(name, barcodes):
    """Barcodes in `name` that start and end at a delimiter (_ . -) or at the name's start/end."""
    starts = [0] + [i + 1 for i, c in enumerate(name) if c in DELIMITERS]
    ends = [i for i, c in enumerate(name) if c in DELIMITERS] + [len(name)]
    return {name[s:e] for s in starts for e in ends if e > s and name[s:e] in barcodes}


def has_extension(name, extensions):
    return "." in name and name.rsplit(".", 1)[1].lower() in extensions


def scan(cfg, dates, report=quiet):
    """Matched files as manifest entries, plus error and scanned-file counts. Not recursive."""
    entries, errors, seen = [], 0, 0
    for sub in cfg.subfolders:
        try:
            listing = os.scandir(cfg.source_dir / sub)  # read as it goes, never held whole in memory
        except OSError as e:
            log.warning("skipping subfolder %s: %s", sub, e)
            errors += 1
            continue
        with listing:
            for item in listing:
                seen += 1
                if seen % 2000 == 0:
                    report("scan", f"{len(entries)} matches", seen)
                if not item.is_file(follow_symlinks=False) or not has_extension(item.name, cfg.extensions):
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
                entries.append({"relative_path": f"{sub}/{item.name}", "source_subfolder": sub,
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

def mochi(cfg, *args):
    """Runs `mochi --json ARGS`. Exit 0 and 2 (degraded, data committed / checked) pass; others raise."""
    p = subprocess.run([cfg.mochi, "--json", "--no-local-history", *map(str, args)],
                       capture_output=True, text=True)
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

    archived, failures, breakdown = [], [], {}
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
            breakdown[month] = (len(months[month]), sum(e["original_size"] for e in months[month]))
            log.info("%s: %d files committed (LOCAL_COMMITTED), verified and indexed", month, len(months[month]))
            report("commit", month, len(breakdown), len(months))
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
    return {"files_moved": len(archived), "errors": leftovers, "volumes": breakdown}


# ---------------------------------------------------------------- run

def lock_run(cfg):
    """Holds <output_dir>/.backup.lock so two runs never share staging. Returns the open lock file.

    The OS releases the lock when the process ends, even after a crash, so it never goes stale."""
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
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
    """{month: (files, bytes)} of matched files, sorted by month."""
    volumes = defaultdict(lambda: (0, 0))
    for e in entries:
        n, size = volumes[e["volume_name"]]
        volumes[e["volume_name"]] = (n + 1, size + e["original_size"])
    return dict(sorted(volumes.items()))


def run(cfg, candidates_fn=query_candidates, dry_run=False, report=quiet, confirm=None):
    """One backup run. Returns a summary dict whose "status" is "done", "dry_run" or "cancelled".

    confirm(volumes) -> bool, if given, is asked after the scan and before any file moves;
    False stops the run with nothing moved and nothing recorded."""
    with lock_run(cfg):
        return _run(cfg, candidates_fn, dry_run, report, confirm)


def _run(cfg, candidates_fn, dry_run, report, confirm):
    db = open_index(cfg.db_path)
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
    report("query", f"{len(candidates)} candidates", 1, 1)
    dates = dict(candidates)  # barcode -> last_update (the last row wins)
    entries, errors, seen = scan(cfg, dates, report)
    volumes = plan(entries)
    info = {"window": window, "candidates": len(candidates), "scanned": seen, "volumes": volumes, "resumed": resumed}
    if dry_run:
        log.info("dry run: %d files would be archived: %s", len(entries), volumes)
        return dict(info, status="dry_run", files_moved=0, errors=errors)
    if entries and confirm and not confirm(volumes):
        return dict(info, status="cancelled", files_moved=0, errors=errors)

    sid = start_session(db, date_from or "", date_to)
    try:
        matched = {e["barcode"] for e in entries}
        fresh = [(b, d) for b, d in candidates if not db.execute(
            "SELECT 1 FROM session_barcode WHERE barcode = ? AND last_update = ?", (b, d)).fetchone()]
        with db:
            db.executemany("INSERT OR IGNORE INTO session_barcode (scan_session_id, barcode, last_update, status)"
                           " VALUES (?, ?, ?, ?)",
                           [(sid, b, d, "matched" if b in matched else "no_files_found") for b, d in fresh])
        result = {"files_moved": 0, "errors": 0, "volumes": {}}
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


def send_email(cfg, summary):
    """Best effort: POSTs an HTML summary to {api_base_url}/api/email/send."""
    rows = "".join(f"<tr><td>{m}</td><td>{n}</td><td>{b}</td></tr>" for m, (n, b) in summary["volumes"].items())
    unmatched = ", ".join(html.escape(b) for b in summary["unmatched"][:2000])
    body = (f"<h3>Finisar log backup, session {summary['session_id']}</h3>"
            f"<p>Window {summary['window'][0] or '(start)'} .. {summary['window'][1]}<br>"
            f"Candidates {summary['candidates']}, files scanned {summary['scanned']}, "
            f"files committed (LOCAL_COMMITTED) {summary['files_moved']}, errors {summary['errors']}</p>"
            f"<table border=1><tr><th>Archive</th><th>Files</th><th>Bytes</th></tr>{rows}</table>"
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
    parser.add_argument("config", help="path to config.json")
    parser.add_argument("--dry-run", action="store_true", help="query and scan only; move nothing")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config(args.config)
    summary = run(cfg, dry_run=args.dry_run)
    log.info("%s: %s", summary["status"], json.dumps({k: v for k, v in summary.items() if k != "unmatched"}))
    if summary["status"] == "done" and cfg.email:
        send_email(cfg, summary)


if __name__ == "__main__":
    main()
