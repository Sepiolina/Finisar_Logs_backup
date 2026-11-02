# Code walkthrough

A guide for reviewing the code. Read [how-it-works.md](how-it-works.md) first
for the overall flow.

## Files

| File | Lines | Role |
|---|---|---|
| `logs_backup.py` | ~580 | **All backup logic.** No display code. Also runs alone: `python logs_backup.py config.json`. |
| `backup_ui.py` | ~160 | Terminal UI with `rich`. Draws progress and asks before moving files. **Makes no backup decisions.** |
| `test_logs_backup.py` | ~170 | Tests: matching rules, config, full flow, crash recovery, cancel, the run lock. |
| `bench.py` | ~75 | Times a full run over generated files. See [performance.md](performance.md). |

For a safety audit, read `logs_backup.py` only. `backup_ui.py` cannot move,
commit or delete anything: it only calls `logs_backup.run()` and draws what it
gets back.

## The contract between logic and UI

`logs_backup.run(cfg, dry_run=False, report=quiet, confirm=None)` is the only
entry point the UI uses.

| Callback | Called with | Purpose |
|---|---|---|
| `report(step, message, done, total)` | `step` is `resume`, `query`, `scan`, `stage` or `commit`. `total=None` means not known yet. | Progress only. The default, `quiet`, does nothing. |
| `confirm(volumes) -> bool` | `{"2026-08": (files, bytes), ...}` | Asked once, after the scan and **before any file moves**. `False` returns `status="cancelled"` with nothing moved and nothing written to the index. |

`run()` returns a summary dict:

| Key | Meaning |
|---|---|
| `status` | `done`, `dry_run` or `cancelled` |
| `window` | `(from, to)` dates queried. `from` is `None` on the first run. |
| `candidates`, `scanned`, `files_moved`, `errors`, `resumed` | Counts |
| `volumes` | `{month: (files, bytes)}`: committed months when `done`, otherwise what was found |
| `session_id`, `unmatched` | Only when `done` |

On failure `run()` raises (`BackupError` for expected failures). Nothing is
lost in that case (see [how-it-works.md](how-it-works.md#why-nothing-is-lost)).

## `logs_backup.py`, top to bottom

The file is laid out in run order and split by `# ----` section comments.

### Constants and schema

- `DELIMITERS = "_.-"`: what may surround a barcode in a filename.
- `MANIFEST_BATCH = 2000`: how many manifest lines are synced to disk at a time
  before their files move. Bigger means fewer `fsync` calls. Safety does not
  depend on it.
- `WORKERS`: CPU count, at most 8. Used for hashing threads and for how many
  months are committed side by side.
- `SCHEMA`: the SQLite tables, copied from Finisar_move_log_files.

### Config

`Config(path)` reads `config.json`, fills defaults (staging defaults to
`source_dir/.staging`, the same drive as the source) and checks every subfolder
is a single plain folder name (no `/`, `\`, `:`, `..`). It refuses a path that
contains a control character (what `"E:\backup"` turns into in JSON). `cfg.sql`
holds the SQL Server login: each value comes from the `DB_*` environment
variable if set, otherwise from `sql_server` in the file. `cfg.manifest` and
`cfg.archive(month)` build the two paths used everywhere else.

### SQLite index

| Function | What it does |
|---|---|
| `open_index(path)` | Creates the folder if needed, then opens or creates the database. Refuses a legacy or other-version index without changing it. |
| `start_session` / `complete_session` | Insert a `scan_session` row as `running`, then update it with status and totals. |
| `query_window(db, cfg)` | Works out the dates to query (see [how-it-works.md](how-it-works.md#the-date-window)). It ignores recovery sessions whose dates are the word `resumed`. |

### SQL Server

`query_candidates(sql_login, date_from, date_to)` runs the same query as the
original (`ShipmentFinisarSerial` joined to `ShipmentFinisarTrace`) with `pymssql`,
using `cfg.sql`. Dates are passed as query parameters, not pasted into the SQL.
It retries with exponential backoff and raises `BackupError` naming any missing
part of the login. `run()` uses it unless the caller passes its own
`candidates_fn` (the tests do).

### Scan

| Function | What it does |
|---|---|
| `find_barcodes(name, barcodes)` | Lists every position a barcode may start (start of name, or after a delimiter) and may end (end of name, or before a delimiter), and checks each substring between them against the barcode set. Same results as the original's Aho-Corasick search, with no extra library. |
| `has_extension(name, extensions)` | Text after the last `.`, compared without case. |
| `scan(cfg, dates, report)` | Reads each subfolder with `os.scandir` as it goes, so a huge folder is never held in memory whole (not recursive, symlinks not followed). Keeps files with an allowed extension and exactly one barcode. Returns manifest-ready dicts. A missing subfolder or an ambiguous name counts as an error and does not stop the run. |

### Stage

`stage(cfg, sid, entries, report)` first checks that staging and the source are
on the same drive (a rename cannot cross drives). It then writes the manifest
header and, for each batch of 2,000 entries:

1. writes their manifest lines, then `flush` and `fsync`, so the lines are on disk;
2. moves each file with `os.rename` into `staging/<YYYY-MM>/<SUB>/<file>`,
   refusing a path that already exists.

A file that cannot be moved (locked, permissions) is logged and counted, stays
in the source folder, and a later run picks it up. Renames run on one thread:
measured, threads made them slower.

`read_manifest(cfg)` reads it back. A broken first line means there is nothing
safe to resume. A broken last line (a crash mid-write) is skipped.

### MOCHI

| Function | What it does |
|---|---|
| `mochi(cfg, *args)` | Runs `mochi --json --no-local-history ARGS`. Exit 0 and 2 pass (2 = committed or checked, with a warning such as unconfirmed folder durability). Any other exit raises `BackupError` with MOCHI's error code. |
| `head_files(cfg, archive)` | `mochi list`: returns `{path: BLAKE3}` of the archive head, plus `(seq, commit_id)`. |
| `b3(path)` | BLAKE3 of a file, memory-mapped. BLAKE3 may use several threads for a big file. |
| `commit(cfg, month)` | One `mochi create` (new month) or `mochi append` with the `staging/<month>/<SUB>` folders as input. MOCHI merges each into the archive's `<SUB>/` folder. |
| `commit_month(cfg, month, entries, resume)` | Hashes every staged file on worker threads (`hash_all`, 500 files per task) **while** MOCHI commits. When resuming, first checks whether the head already holds every file with the same hash and, if so, skips the commit (no duplicate versions). Then compares each staged hash with the head and runs `mochi verify --level content`. Does not touch SQLite or staging, so it can run on a worker thread. |

### Index and clean up

| Function | What it does |
|---|---|
| `index_month(...)` | One SQLite transaction: insert or update the month's `file_storage_volume` row (path, size, head commit), upsert a `scanned_log_entry` per file, recount `entry_count`. |
| `finish_staged(db, cfg, report, resume)` | Reads the manifest, keeps entries whose staged file exists, groups them by month and runs `commit_month` for the months in parallel. On the main thread, as each month finishes: index it, **then** delete its staged copies. If any month failed, raises and leaves the manifest. Otherwise removes the manifest and empty folders, reports any unlisted leftover files (never deleting them) and completes the session. |

### Run

| Function | What it does |
|---|---|
| `lock_run(cfg)` | Takes an OS file lock on `output_dir/.backup.lock` (`msvcrt` on Windows, `fcntl` elsewhere). A second run at the same time gets a clear `BackupError`. |
| `plan(entries)` | `{month: (files, bytes)}` for the confirm prompt and the summary. |
| `run(...)` / `_run(...)` | The whole run under the lock: resume leftovers, query, scan, then dry run / confirm, then record barcodes, stage, finish. Marks the session `failed` if anything raises after it started. |
| `send_email(cfg, summary)` | Optional HTML summary POSTed to `{api_base_url}/api/email/send`. Barcodes are HTML-escaped and the body is capped at 95,000 characters. Failure only logs a warning. |
| `main()` | Plain command line with log lines, no prompt. For scheduled tasks. |

## `backup_ui.py`

| Piece | What it does |
|---|---|
| `Steps` | The `report` callback. Adds one progress row per step the first time it reports, then updates it. `fail_running()` marks unfinished rows red when the run fails. |
| `Mark` | The status column: spinner while running, ✓ when done, ✗ when failed. |
| `ask_confirm(progress, yes)` | The `confirm` callback. Pauses the progress display, shows a table of months, asks `y/n` (default no). `--yes` skips it. |
| `print_summary(summary)` | Summary table and panel. Uses the words "committed (LOCAL_COMMITTED)" and the reminder that this is not a backup on its own. |
| `main()` | Loads the config (a broken config shows a "Config problem" panel, not a traceback), asks for the SQL password hidden if `sql_server` has a user but no password, sets up logging (warnings on screen, everything in `backup.log`), runs, catches failures and shows a red panel that says nothing is lost and running again continues. |

## Tests

```bash
MOCHI_BIN=/path/to/mochi python -m unittest -v
```

| Test | Checks |
|---|---|
| `test_barcode_needs_delimiters_on_both_sides` | Boundary rule, overlapping barcodes, barcodes containing `-` |
| `test_extension` | Case, no extension, `.txt.bak` |
| `test_full_flow_then_append` | Archive layout and paths, hashes in the head and the index, source emptied, staging removed, second run appends commit 1 |
| `test_crash_after_staging_is_resumed` | A leftover batch is finished by the next run |
| `test_crash_after_commit_before_index_makes_no_new_commit` | No duplicate commit on resume, rows indexed |
| `test_callbacks_report_steps_and_cancel_moves_nothing` | Progress steps, and a declined confirm moves and records nothing |
| `test_second_run_at_the_same_time_is_refused` | Run lock |
| `test_failed_commit_keeps_staging` | A failing `mochi` keeps staging, and the next run completes it |
| `test_sql_login_from_config_with_env_override` | Login read from `config.json`, `DB_*` variables win, a missing part is named |
| `test_backspace_in_a_path_is_refused` | `"E:\backup"` in JSON is caught |
| `test_index_folder_is_created` | `db_path` in a folder that does not exist yet |

The flow tests need the `mochi` program. Without it they are skipped, and only
the matching tests run.
