# Finisar Logs Backup

A small Python rewrite of [Finisar_move_log_files](https://github.com/Sepiolina/Finisar_move_log_files).
It produces the same result: log files whose names contain a barcode from SQL Server are moved into
monthly MOCHI archives and indexed in SQLite.

One file to review, `logs_backup.py`, about 480 lines (50 of them the SQL schema), read top to bottom in run order.

## What it produces (same as Finisar_move_log_files)

| Output | Layout |
|---|---|
| Archives | `<output_dir>/<YYYY>/<YYYY-MM>.mochi`. The month comes from the barcode's `last_update`. |
| Paths inside an archive | `<SUBFOLDER>/<filename>`, for example `BURNIN/unit1_BC001_log.txt` |
| SQLite index | The same 4 tables and `user_version = 2` (`scan_session`, `session_barcode`, `file_storage_volume`, `scanned_log_entry`), with a BLAKE3 `content_hash` and a `commit_seq` for each file |
| Staging | `<output_dir>/.staging` with `manifest.jsonl`, in the same line format. It is removed when the run finishes. |

## Flow

```
resume leftover staging -> query SQL Server -> scan -> stage -> commit -> verify -> index -> delete staged copy
```

1. **Resume**: if `staging/manifest.jsonl` exists, finish that batch first.
2. **Query**: get `(barcode, last_update)` rows for the window. The window runs from the last successful run's to-date minus 1 day up to today minus `lookback_days`, unless `date_range` is set.
3. **Scan**: list each subfolder (not recursive). A file matches when it has an allowed extension and its name contains exactly one barcode with `_ . -` or the start or end of the name on each side.
4. **Stage**: write the manifest lines and fsync them, then move the files to `staging/<YYYY-MM>/<SUB>/`. A file that already exists in staging is never overwritten.
5. **Commit**: run `mochi create` or `mochi append` once for each month, with that month's subfolder directories as input. If the archive head already holds every file with the same hash (a crash between commit and index), no new commit is made.
6. **Verify**: compare the BLAKE3 hash of every staged file with the hash in the archive head, then run `mochi verify --level content`.
7. **Index, then delete**: upsert the volume and entry rows in one transaction, then delete that month's staged copies.

Months are committed and verified in parallel. SQLite writes and deletes stay on the main thread.
A month that fails keeps its staged files, and the next run resumes it.
A file is always in exactly one place: the source tree, staging, or a verified archive.

## Setup and run

```bash
pip install -r requirements.txt          # blake3, pymssql
# put the mochi CLI (from immerh8/MOCHI, cargo build --release -p mochi-cli) on PATH, or set "mochi" in the config
cp config.example.json config.json       # edit paths
export DB_SERVER=... DB_NAME=... DB_USER=... DB_PASSWORD=...   # optional: DB_PORT, DB_RETRY_ATTEMPTS, DB_RETRY_BASE_DELAY_MS, DB_REQUEST_TIMEOUT_MS
python logs_backup.py config.json --dry-run   # query and scan only
python logs_backup.py config.json
```

To get an HTML statistics email after each run, set `"email": {"api_base_url": "http://host:port", "to": "..."}`.
The report is POSTed to the same `/api/email/send` relay. Sending is best effort and never fails the run.

## Tests

```bash
MOCHI_BIN=/path/to/mochi python -m unittest -v
```

The tests cover barcode and extension matching, the full flow plus an append run, resuming after a crash after staging, resuming after a crash after commit but before index (no duplicate commit), and a failed commit that keeps staging.

## Differences from Finisar_move_log_files (deliberate simplifications)

- **No desktop UI.** It is a CLI meant for a scheduled task or cron job. SQL settings come from `DB_*` environment variables, not the encrypted settings file.
- **MOCHI through its CLI** (`mochi --json`), not the Rust library.
- **One commit per month per run**, not commits split at 1 GiB or 20,000 files. The CLI streams files, so memory is not the limit.
- **Verification** runs `mochi verify --level content` over the whole archive each run, not only the new commits.
- **Barcode matching** tries every delimiter-bounded substring of the name against a set. This gives the same matches as the original's Aho-Corasick search, with no extra dependency.

A run ends **committed (LOCAL_COMMITTED)**. That is not a backup on its own: keep an independent copy of the output folder and of the SQLite index.
