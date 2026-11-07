# Code walkthrough

A guide for reviewing the code. Read [how-it-works.md](how-it-works.md) first
for the overall flow.

## Files

| File | Lines | Role |
|---|---|---|
| `logs_backup.py` | ~640 | **All backup logic.** No display code. Also runs alone: `python logs_backup.py`. |
| `gui.py` | ~900 | Desktop app with PySide6: settings, the two run modes, progress, Stop, history, check, export. **Makes no backup decisions.** |
| `backup_ui.py` | ~210 | Terminal UI with `rich`. Draws progress and asks before moving files; Ctrl+C stops. **Makes no backup decisions.** |
| `test_gui.py` | ~140 | The app without a screen: settings saved and used by the command line, preview, a custom-range backup in History, Stop then continue, widgets only touched on the window's thread. |
| `test_logs_backup.py` | ~190 | Tests: matching rules, config, full flow, crash recovery, cancel, the run lock. |
| `bench.py` | ~75 | Times a full run over generated files. See [performance.md](performance.md). |

For a safety audit, read `logs_backup.py` only. `gui.py` and `backup_ui.py` cannot
move, commit or delete anything: they only call `logs_backup.run()` (and `export`,
`check_mochi`, `check_sql`) and draw what they get back.

## The contract between logic and UI

`logs_backup.run(cfg, dry_run=False, report=quiet, confirm=None, window=None, stop=None)`
is the only entry point the UIs use to back up. `window` is a custom `(from, to)`
range (None: continue). `stop` is a `threading.Event`: once set, the run returns
`status="stopped"` at the next safe point. The UIs run it on a background thread.

| Callback | Called with | Purpose |
|---|---|---|
| `report(step, message, done, total)` | `step` is `resume`, `query`, `scan`, `stage` or `commit`. `total=None` means not known yet. `stage` and `commit` count files over the whole run; `message` names the batch (`batch 2/12`). | Progress only. The default, `quiet`, does nothing. Called on the run's thread. |
| `confirm(volumes) -> bool` | `{"2026-08": (barcodes, files, bytes), ...}` | Asked once, after the scan and **before any file moves**. `False` returns `status="cancelled"` with nothing moved and nothing written to the index. |

`run()` returns a summary dict:

| Key | Meaning |
|---|---|
| `status` | `done`, `dry_run`, `cancelled` or `stopped` |
| `window` | `(from, to)` dates queried. `from` is `None` on the first run. |
| `candidates` | Distinct barcodes returned by SQL Server |
| `matched_barcodes`, `matched_files` | Barcodes that have files in the source folders, and those files: what the run would archive |
| `barcodes`, `files_moved` | What was committed (0 for a dry run or a cancel) |
| `scanned`, `errors`, `resumed` | Counts |
| `volumes` | `{month: (barcodes, files, bytes)}`: committed months when `done`, otherwise what was found |
| `session_id`, `unmatched` | When `done` (and `session_id` when `stopped` after files were recorded) |

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

`find_config(given)` picks the file: the path passed on the command line if
any, else `config.json` in the current folder, else the one next to the program.
`Config(path)` reads it, fills defaults (`cfg.mode` is `"copy"` on a network
drive, else `"move"`; staging defaults to `output_dir/.staging` for copy and
`source_dir/.staging`, the same drive as the source, for move; `cfg.threads` is
16 on a network drive, else 1) and checks every subfolder
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

| Function | What it does |
|---|---|
| `query_candidates(sql_login, date_from, date_to)` | The same query as the original (`ShipmentFinisarSerial` joined to `ShipmentFinisarTrace`), with dates passed as query parameters, not pasted into the SQL. Strips spaces from barcodes. `run()` uses it unless the caller passes its own `candidates_fn` (the tests do). |
| `check_sql(sql_login)` | One connection, no retries, returns the server's version line. For `backup_ui.py --check-sql`. |
| `run_sql(sql_login, sql, params)` | Connects with `pymssql` using `cfg.sql` and runs one query, retrying with exponential backoff. Names any missing part of the login. A `HOST\NAME` server is connected by instance name, without a port. |
| `set_encryption(mode)` | FreeTDS (inside `pymssql`) takes the login encryption setting only from a `freetds.conf`, so this writes one to the temp folder and points `FREETDSCONF` at it: `request` (default), `require` or `off`. `off` logs a warning on every run. |
| `sql_error(e)` | Turns a `pymssql` error (a code plus a multi-line byte string) into one line with a hint for codes 20002, 20009, 18456 and 4060. The code is read from the text when it starts with `DB-Lib error message N`, because `pymssql` can keep the previous error's code. |

### Scan

| Function | What it does |
|---|---|
| `find_barcodes(name, barcodes)` | Lists every position a barcode may start (start of name, or after a delimiter) and may end (end of name, or before a delimiter), and checks each substring between them against the barcode set. Same results as the original's Aho-Corasick search, with no extra library. |
| `has_extension(name, extensions)` | Text after the last `.`, compared without case. |
| `walk_files(top, recursive, skip, on_error, threads)` | Yields each file under `top` with the folder path between them, listing up to `threads` folders at once with `os.scandir` (on a network drive each listing mostly waits on the network). With `recursive` it descends into every folder whose `path_key` is not in `skip`; links are not followed. `path_key` compares paths without touching the disk (`resolve()` would cost a round trip per folder). An unreadable folder goes to `on_error` and is skipped. |
| `scan(cfg, dates, report)` | Walks each subfolder (`cfg.recursive`, default on), never entering the staging or output folders. Keeps files with an allowed extension and exactly one barcode in the file name; `relative_path` keeps the folders (`BURNIN/line2/x_BC001.txt`), `source_subfolder` is the top folder. Returns manifest-ready dicts. A missing or unreadable folder or an ambiguous name counts as an error and does not stop the run. |

### Stage

`stage(cfg, sid, entries, report)` first checks, for move staging, that staging
and the source are on the same drive (a rename cannot cross drives), and for
copy staging, that staging has room for the batch plus 10%. It then writes the
manifest header (session and mode) and, for each batch of 2,000 entries:

1. writes their manifest lines, then `flush` and `fsync`, so the lines are on disk;
2. move: renames each file with `os.rename` into `staging/<YYYY-MM>/<SUB>/<file>`,
   refusing a path that already exists. Copy: copies it to `<file>.partial`,
   checks the size against the scan, sets the scan's modification time, and
   renames it into place; the original is not touched.

A file that cannot be staged (locked, permissions, changed since the scan) is
logged and counted, stays in the source folder, and a later run picks it up.
Files are staged on `cfg.threads` threads: one on a local disk (measured,
threads made renames slower there), 16 on a network drive, where each file
mostly waits on the network (`is_network_path`, overridable with
`file_threads`). Progress is reported every 100 files.

After a month is verified and indexed, `release(cfg, e, mode)` deletes each
staged file; with copy staging it first deletes the original, only if its size
and modification time still match the scan (otherwise it stays and counts as
an error, and the next run archives the new content).

`read_manifest(cfg)` reads it back. A broken first line means there is nothing
safe to resume. A broken last line (a crash mid-write) is skipped.

### MOCHI

| Function | What it does |
|---|---|
| `registry_mochi(reg)` | Windows only: the `mochi.exe` paths the MOCHI setup recorded: `App Paths\mochi.exe` (setup 0.9.15+, `HKCU` then `HKLM`), then MOCHI's install-location key plus `bin\mochi.exe` (setup 0.9.14). `reg` is `winreg`; tests pass a stand-in. |
| `mochi_places(configured)` | Where to look for `mochi`: only the configured path if one is set (`"mochi"` alone counts as unset), else the registry (`registry_mochi`), PATH, the setup's `bin` folder, cargo's bin folder, next to the program, and a `MOCHI` checkout beside it. |
| `check_mochi(cfg)` | Tries each place with `mochi --version` and sets `cfg.mochi` to the full path of the first that works. Called first in every run, before resuming or moving anything. A real run stops if none works, listing every place tried (and noting if only the desktop app is installed); a dry run logs a warning. Also shown by `--check-sql`. |
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
| `lock_run(cfg)` | Creates `output_dir` if needed, then takes an OS file lock on `output_dir/.backup.lock` (`msvcrt` on Windows, `fcntl` elsewhere). A second run at the same time gets a clear `BackupError`. |
| `prepare_folders(cfg)` | Real runs, after `mochi` is found: creates `output_dir`, `staging_dir` and the index's folder if missing, with a clear `BackupError` if one cannot be made. `source_dir` is never created (a missing one is a wrong setting): it only logs a warning. |
| `plan(entries)` | `{month: (barcodes, files, bytes)}`. One helper for both what a run would archive (confirm, dry run) and what it did archive (summary), so the two always count the same way. |
| `export(cfg, target, report)` | `--export`: under the run lock, copies the archives (skipping ones unchanged since the last export, per `export-manifest.json`), the index via SQLite's backup API, the config without its password, and staging if present. Writes `RESTORE.txt`. Refuses a target inside `source_dir` or `output_dir`. |
| `copy_checked(src, dst)` | Copies through a `.partial` file, hashing while reading. The copy is hashed again and must match before it replaces `dst`. |
| `run(...)` / `_run(...)` | The whole run under the lock: resume leftovers, query, scan, then dry run / confirm, then record barcodes (and `custom_window_session` for a custom range), then per batch of `cfg.batch_files` (sorted by month): stage, finish. Marks the session `failed` if anything raises after it started, or when stopped. |
| `save_plan`, `load_plan`, `drop_plan`, `stopped_run(cfg)`, `discard_stopped_run(cfg)` | The kept plan of a run (`pending_run`, `pending_file` in the index): written before anything moves, rows dropped as batches are staged, the whole plan dropped when the run ends `success` or a new run replaces it. `stopped_run` is what the app's Stopped run card shows. |
| `_archive(...)`, `_continue_plan(...)` | The batch loop shared by a new run and a continued one; totals add up over the days of one run. `_continue_plan` checks the log folder can be reached, then runs the plan without query or scan. |
| `verify_copies(cfg, months, known)` | Copy staging: hashes each staged copy and compares it with the journal (`read_journal`) written while copying; unverified copies are deleted and left out. The checked hashes go to `commit_month(known=...)`, so nothing is hashed twice. |
| `keep_awake()` | Windows `SetThreadExecutionState`: no sleep while a run works. |
| `check_stop(stop)`, `Stopped` | Raises `Stopped` once the stop event is set. Checked every 200 scanned files, per staged file (files not staged yet are skipped), between batches, and in `commit_month` before `mochi` starts. `finish_staged` lets months already committing finish and be indexed and cleaned up, then re-raises; the manifest stays for the next run. |
| `query_window(db, cfg, window)`, `continue_window(cfg)`, `check_window(window)` | The date range: a custom one as given (checked: two dates, from ≤ to), else continuing from the last successful run that is not in `custom_window_session`. `continue_window` is what the app shows on card A; it does not create the index. |
| `settings_dir()`, `load_settings()`, `save_settings()` | The app's settings file (`%APPDATA%\FinisarLogsBackup\settings.json`, or `$FINISAR_BACKUP_HOME`), written atomically. `find_config()` falls back to it after the `config.json` places. |
| `protect(text)`, `unprotect(token)` | The remembered SQL password, encrypted with Windows DPAPI for the current user (`password_dpapi` in `sql_server`). `Config` decrypts it. Windows only. |
| `send_email(cfg, summary)` | Optional HTML summary POSTed to `{api_base_url}/api/email/send`. Barcodes are HTML-escaped and the body is capped at 95,000 characters. Failure only logs a warning. |
| `main()` | Plain command line with log lines, no prompt. For scheduled tasks. |

## `backup_ui.py`

| Piece | What it does |
|---|---|
| `Steps` | The `report` callback. Adds one progress row per step the first time it reports, then updates it. `fail_running()` marks unfinished rows red when the run fails. |
| `Mark` | The status column: spinner while running, ✓ when done, ✗ when failed. |
| `ask_confirm(progress, yes)` | The `confirm` callback. Pauses the progress display, shows a table of months, asks `y/n` (default no). `--yes` skips it. |
| `run_stoppable(work)` | Runs the backup on a thread; Ctrl+C sets the stop event instead of killing the run mid-step. |
| `print_summary(summary)` | Summary table and panel. Uses the words "committed (LOCAL_COMMITTED)" and the reminder that this is not a backup on its own. |
| `main()` | Loads the config (a broken config shows a "Config problem" panel, not a traceback), asks for the SQL password hidden if `sql_server` has a user but no password, sets up logging (warnings on screen, everything in `backup.log`), runs, catches failures and shows a red panel that says nothing is lost and running again continues. |

## `gui.py`

| Piece | What it does |
|---|---|
| `Job` | Runs a function on a background thread with a stop event. Stays on the window's thread itself, so every signal it emits (progress, confirm request, result) is queued to the window's thread; widgets are never touched from the run. `confirm()` blocks the run until the user answers the dialog. |
| `BackupPage` | Cards A (continue, shows `continue_window`) and B (From/To dates), each with Preview and Start; a row per step (`StepRow`: progress, counts, time), Stop, the log, and a banner with the result. |
| `SettingsPage` | A form over the settings keys. **Save** builds a `Config` first (refuses a missing folder or a bad value), then `save_settings`. Remember the password stores `protect(password)`; otherwise the typed password is kept only for this session. |
| `HistoryPage` | Reads `scan_session` (and `custom_window_session`) read-only. |
| `ToolsPage` | Run check (`check_mochi`, `check_sql`, drive type and staging mode), Export (`lb.export` on a `Job`), open the log or settings folder. |
| `MainWindow` | Navigation, the password prompt, first-start import of a `config.json`, and closing during a run (asks, stops, closes when stopped). |

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
| `test_copy_staging_deletes_originals_only_once_archived` | Copy staging: files are archived, originals deleted, staging removed |
| `test_copy_staging_failed_commit_leaves_the_source_untouched` | Copy staging with a failing `mochi`: originals stay, the next run finishes the batch |
| `test_copy_staging_keeps_an_original_that_changed` | An original written to after its copy stays in the source; the next run archives the new content. Staged copies keep the modification time |
| `test_copy_staging_skips_a_file_that_changed_before_the_copy` | A file whose size changed since the scan is not staged, and no `.partial` file is left |
| `test_move_batch_left_by_an_older_run_is_resumed_in_copy_mode` | A move batch in `source_dir/.staging` is finished after switching to copy staging |
| `Staging.test_parallel_scan_finds_the_same_files` | Listing folders on 8 threads finds the same files as on 1, and skips staging |
| `Staging.test_staging_mode_setting` | `staging_mode` defaults, staging folder per mode, bad values refused |
| `test_batches_archive_everything_one_commit_each` | 252 files in batches of 100: all archived, one commit per batch, progress counted over the whole run |
| `test_stop_while_staging_then_continue` | Stop during the second batch: the first is archived, the session is `failed`, the next run finishes the rest |
| `test_stop_while_resuming_keeps_the_rest_for_later` | Stop before resuming: the leftover batch stays and the next run finishes it |
| `test_stop_while_scanning_moves_nothing` | Stop during the scan: nothing moved, nothing recorded |
| `test_custom_range_does_not_move_where_continue_starts` | A custom range between two continuing runs does not change the second one's start; a reversed range is refused |
| `Staging.test_settings_dict_and_saved_password` | `Config` from a dict, the remembered password decrypted, settings saved and read back, DPAPI refused off Windows |
| `Staging.test_check_window` | Custom ranges must be two valid dates, from ≤ to |
| `test_stopped_run_continues_next_day_without_a_new_scan` | Stop, a fresh `Config` (restart), Continue: no query, no scan, everything archived, one session with the totals of both days |
| `test_continue_skips_files_removed_meanwhile` | Plan files deleted before Continue are skipped without errors |
| `test_continue_with_the_log_folder_unreachable_keeps_the_plan` | Log folder missing: Continue refuses and the plan stays; once it is back, Continue finishes |
| `test_new_run_replaces_a_stopped_plan_and_finds_its_files` | A new run drops the plan; its scan finds the files again |
| `test_damaged_copy_is_redone_not_archived` | A staged copy damaged after copying is not archived; the original stays and the next run archives the right content |
| `test_unjournaled_copies_are_redone` | Copies whose journal is lost are redone, not trusted |
| `test_gui.Window.*` | The desktop app without a screen (see `test_gui.py` above) |
| `test_sql_login_from_config_with_env_override` | Login read from `config.json`, `DB_*` variables win, a missing part is named |
| `test_backspace_in_a_path_is_refused` | `"E:\backup"` in JSON is caught |
| `test_sql_errors_are_one_readable_line_with_a_hint` | Your error 20002, a stale code from `pymssql`, login failure, a bad `encryption` value |
| `test_export_copies_checked_and_skips_unchanged` | Export layout and hashes, password removed, a second export copies no archive, a target inside `output_dir` is refused |
| `test_mochi_path_from_the_registry` | App Paths read first, then the install key plus `bin`; nothing found; no registry off Windows; the registry comes before PATH and a set path still wins |
| `test_recursive_scan_keeps_folders_and_appends_into_them` | Nested files archived with their folders, the same name in two folders, the index rows; a later run adds to an existing nested folder and keeps the earlier file |
| `test_recursive_off_and_staging_inside_a_subfolder` | `"recursive": false` scans one level; a staging folder inside a subfolder is never scanned; a non-boolean `recursive` is refused |
| `test_missing_output_staging_and_index_folders_are_created` | Missing output, staging and index folders are created by a run; a folder that cannot be created gives a clear error |
| `test_mochi_is_found_without_a_config_path` | Finds `mochi` in a MOCHI checkout beside the program and in cargo's bin folder; lists the places when missing; notes a desktop-only install |
| `test_missing_mochi_moves_nothing` | No `mochi`: a real run stops before staging, a dry run warns |
| `test_config_is_found_without_an_argument` | Current folder first, then next to the program, a given path wins, a clear error when none exists |
| `test_index_folder_is_created` | `db_path` in a folder that does not exist yet |

The flow tests need the `mochi` program. Without it they are skipped, and only
the matching tests run.
