# Finisar Logs Backup

Moves log files whose names contain a barcode from SQL Server into monthly
[MOCHI](https://github.com/immerh8/MOCHI) archives, and indexes every file in
SQLite so it can be found again by barcode. It is a small Python rewrite of
[Finisar_move_log_files](https://github.com/Sepiolina/Finisar_move_log_files)
and leaves the same result on disk.

```
resume leftovers -> query SQL Server -> scan -> stage -> commit -> verify -> index -> delete staged copy
```

A file is always in exactly one place: the source folder, staging, or a
verified archive. A run can stop at any point. The next run continues where it
stopped, without losing or duplicating anything.

## Quick start

```bash
pip install -r requirements.txt
```

```bash
cp config.example.json config.json
```

Edit the paths in `config.json`. Put the `mochi` program on PATH (or set
`"mochi"` in the config), and set `DB_SERVER`, `DB_NAME`, `DB_USER` and
`DB_PASSWORD`. Then:

```bash
python backup_ui.py config.json --dry-run
```

```bash
python backup_ui.py config.json
```

On Windows, double-click `run_backup.bat`. Full steps are in [docs/operations.md](docs/operations.md).

## Documentation

| Page | For |
|---|---|
| [docs/how-it-works.md](docs/how-it-works.md) | The flow, the date window, matching rules, what is on disk (archives, index, manifest), and why a crash loses nothing |
| [docs/code-walkthrough.md](docs/code-walkthrough.md) | Reviewing the code: every function in run order, the logic/UI contract, the tests |
| [docs/operations.md](docs/operations.md) | Install, config reference, running, getting files back, moving to another PC, troubleshooting |
| [docs/performance.md](docs/performance.md) | Benchmarks (300,000 files in about 2.5 minutes), where the time goes, tuning |

## Files

| File | Role |
|---|---|
| `logs_backup.py` | **All backup logic**, in run order, no display code. `python logs_backup.py config.json` runs it with plain log lines (for scheduled tasks). |
| `backup_ui.py` | Terminal UI (`rich`): progress, a confirm prompt before files move, a summary. Makes no backup decisions. |
| `test_logs_backup.py` | Tests: `MOCHI_BIN=/path/to/mochi python -m unittest -v` |
| `bench.py` | Benchmark on generated files |
| `config.example.json`, `run_backup.bat`, `requirements.txt` | Config template, Windows launcher, Python libraries (`blake3`, `pymssql`, `rich`) |

## Same result as Finisar_move_log_files

| Output | Layout |
|---|---|
| Archives | `<output_dir>/<YYYY>/<YYYY-MM>.mochi`, month from the barcode's `last_update` |
| Paths in an archive | `<SUBFOLDER>/<filename>`, for example `BURNIN/unit1_BC001_log.txt` |
| SQLite index | Same 4 tables and `user_version = 2`, with a BLAKE3 `content_hash` and `commit_seq` per file |
| Manifest | `manifest.jsonl` in the same line format |

Deliberate differences:

- **Terminal UI instead of a desktop app.** The SQL login comes from `DB_*` environment variables, not an encrypted settings file.
- **MOCHI through its command line** (`mochi --json`), not the Rust library.
- **One commit per month per run**, not commits split at 1 GiB or 20,000 files. The archive contents are the same.
- **Verify checks the whole month each run**, not only the new commit. This makes appends to big months a few seconds slower (see [performance](docs/performance.md)).
- **Staging defaults to `source_dir/.staging`**, not `output_dir/.staging`. Files move into staging by rename, which only works on the same drive.
- **Added: a run lock**, so two runs started at once cannot share staging.

A run ends **committed (LOCAL_COMMITTED)**. That is not a backup on its own:
keep an independent copy of the output folder and of the SQLite index.
