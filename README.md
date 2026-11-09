# Finisar Logs Backup

Moves log files whose names contain a barcode from SQL Server into monthly
[MOCHI](https://github.com/immerh8/MOCHI) archives, and indexes every file in
SQLite so it can be found again by barcode. It is a small Python rewrite of
[Finisar_move_log_files](https://github.com/Sepiolina/Finisar_move_log_files)
and leaves the same result on disk.

```
resume leftovers -> query SQL Server -> scan -> stage -> commit -> verify -> index -> delete staged copy
```

A file is always in the source folder, staging, or a verified archive (on a
network drive the original stays in the source until its archive is verified). A run can stop at any point. The next run continues where it
stopped, without losing or duplicating anything.

## Quick start: desktop app

```bash
pip install -r requirements.txt
```

```bash
python gui.py
```

On Windows, double-click `run_app.bat`, or the built `FinisarLogsBackup.exe` (see below).

The app is in **Thai** by default; **Settings → Language** switches it to English (from the next start).

1. **Settings** page: the log folder, the archive folder, the SQL Server login, then **Save**.
   Everything is saved for the Windows user in `%APPDATA%\FinisarLogsBackup\settings.json`;
   no config file to edit. The password can be remembered, encrypted for that Windows user.
   An existing `config.json` is imported on the first start (or with **Import config.json…**).
2. **Tools → Run check**: finds `mochi` and makes one SQL Server connection.
3. **Backup** page, two separate ways to run:
   - **A · Continue from the last run**: the regular backup. It picks up where the last
     successful continuing run ended, up to today minus the lookback days.
   - **B · Custom date range**: back-fill or redo any range. It never changes where A starts.

   **Preview** shows what would be archived and moves nothing. **Start backup** asks before any
   file moves. **Stop** ends the run at the next safe point (never inside a MOCHI commit). You can
   then close the program or shut down the PC: the run's plan is kept in the index, and the next
   day a **Stopped run** card offers **Continue**, which picks up exactly there without asking
   SQL Server or scanning the log folders again. Every stopped run keeps its own card: a stopped
   custom-range (B) run stays until you continue or discard it, whatever runs in between. Files are archived in batches (10,000 by
   default), so a stop waits for at most one batch's commit. A power cut or a crash is handled
   the same way.
4. **History** lists every run; **Tools → Export** copies a restorable set to another drive.

## Terminal and scheduled tasks

`backup_ui.py` is the terminal version. It uses the same settings as the app (or a `config.json`
in the current folder or next to the program, which wins). Ctrl+C stops it safely, like Stop.
If stopped runs can continue, it continues them, oldest first (so a scheduled task picks up
where the last run stopped); `--new` starts a new run instead.

**Everything on a NAS** (logs, archives, index) works: copy staging then uses this PC's own disk
(`%LOCALAPPDATA%\FinisarLogsBackup\staging`), and the index is checked and copied to this PC
before each run. See [operations.md](docs/operations.md#everything-on-a-nas).

```bash
python backup_ui.py --check-sql
```

```bash
python backup_ui.py --dry-run
```

```bash
python backup_ui.py --yes
```

```bash
python backup_ui.py --export E:/finisar_export
```

To build `.exe` files that run on a PC without Python, run `build_exe.bat`. It sets up a virtual
environment in `.venv`, runs the tests, and writes `dist\FinisarLogsBackup.exe` (the desktop app)
and `dist\FinisarLogsBackupCLI.exe` (the terminal version, for scheduled tasks). See
[Single-file .exe](docs/operations.md#single-file-exe-windows). Full steps are in
[docs/operations.md](docs/operations.md).

## Documentation

| Page | For |
|---|---|
| [docs/html/index.html](docs/html/index.html) | **Interactive developer guide** (open in a browser, works offline): architecture, how the Python code finds and talks to the MOCHI engine (Rust), a step-by-step run with a crash simulator, every function and test, and a reviewer checklist |
| [docs/how-it-works.md](docs/how-it-works.md) | The flow, the date window, matching rules, what is on disk (archives, index, manifest), and why a crash loses nothing |
| [docs/code-walkthrough.md](docs/code-walkthrough.md) | Reviewing the code: every function in run order, the logic/UI contract, the tests |
| [docs/operations.md](docs/operations.md) | Install, config reference, running, getting files back, moving to another PC, troubleshooting |
| [docs/performance.md](docs/performance.md) | Benchmarks (300,000 files in about 2.5 minutes), where the time goes, tuning |

## Files

| File | Role |
|---|---|
| `logs_backup.py` | **All backup logic**, in run order, no display code. `python logs_backup.py` runs it with plain log lines (for scheduled tasks). |
| `gui.py` | **Desktop app** (PySide6): settings, the two run modes, progress with Stop, history, check and export. Makes no backup decisions. |
| `gui_text.py` | The app's text in Thai (default) and English: `tr()`, plus Thai for progress lines and error headlines from `logs_backup.py` |
| `backup_ui.py` | Terminal UI (`rich`): progress, a confirm prompt before files move, a summary, Ctrl+C to stop. Makes no backup decisions. |
| `test_logs_backup.py`, `test_gui.py` | Tests: `MOCHI_BIN=/path/to/mochi python -m unittest -v` (the app's tests run without a screen) |
| `bench.py` | Benchmark on generated files |
| `config.example.json`, `run_app.bat`, `run_backup.bat`, `requirements.txt` | Settings template for `config.json`, Windows launchers for the app and the terminal version (use `.venv` if present), Python libraries (`blake3`, `pymssql`, `rich`, `PySide6-Essentials`) |
| `build_exe.bat` | Windows: builds `dist\FinisarLogsBackup.exe` (desktop app) and `dist\FinisarLogsBackupCLI.exe` (terminal), one file each (PyInstaller), in a `.venv` virtual environment |

## Same result as Finisar_move_log_files

| Output | Layout |
|---|---|
| Archives | `<output_dir>/<YYYY>/<YYYY-MM>.mochi`, month from the barcode's `last_update` |
| Paths in an archive | `<SUBFOLDER>/<filename>`, for example `BURNIN/unit1_BC001_log.txt`; files in deeper folders keep them (`BURNIN/line2/unit1_BC001_log.txt`) |
| SQLite index | Same 4 tables and `user_version = 2`, with a BLAKE3 `content_hash` and `commit_seq` per file |
| Manifest | `manifest.jsonl` in the same line format |

Deliberate differences:

- **Terminal UI instead of a desktop app.** The SQL login is in `config.json` (the password can be asked each run) or `DB_*` environment variables, not an encrypted settings file.
- **MOCHI through its command line** (`mochi --json`), not the Rust library. Tested with MOCHI 0.9.16.
- **Added: compression presets.** Settings → Compression (or `"compression"` in config.json) picks one of MOCHI 0.9.16's seven presets, Turbo to Extreme, for new monthly archives; Balanced is the default. An existing archive keeps the level it was created with.
- **One commit per month per run**, not commits split at 1 GiB or 20,000 files. The archive contents are the same.
- **Verify checks the whole month each run**, not only the new commit. This makes appends to big months a few seconds slower (see [performance](docs/performance.md)).
- **Two kinds of staging.** On a local disk files move into `source_dir/.staging` by rename (same drive). On a network drive they are copied to `output_dir/.staging` on the local disk, and the originals are deleted only after their archive is verified (`"staging_mode"` in config.json).
- **Added: a run lock**, so two runs started at once cannot share staging.
- **Added: Stop and batches.** A run archives in batches (10,000 files by default) and can be stopped between files or before a commit; the next run continues.
- **Added: custom date ranges** that do not move where the regular run continues (recorded in an extra `custom_window_session` table; the original tables are unchanged).
- **Scans folders inside each subfolder too** (`"recursive": true`, the default), keeping them in the archive paths. The original read only files directly in each subfolder; `"recursive": false` does the same.

A run ends **committed (LOCAL_COMMITTED)**. That is not a backup on its own:
keep an independent copy of the output folder and of the SQLite index.
