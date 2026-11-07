# Operations guide

Install, configure, run, get files back, move to another PC, and fix problems.

## Install (once)

1. **Python 3.9 or newer.** On Windows, tick "Add to PATH" during install.
2. **The code and its libraries** (`blake3`, `pymssql`, `rich`):

   ```bash
   git clone https://github.com/Sepiolina/Finisar_Logs_backup
   ```

   ```bash
   cd Finisar_Logs_backup
   ```

   ```bash
   pip install -r requirements.txt
   ```

3. **The `mochi` program** (the command line, not only the desktop app). Either:
   - install MOCHI with a setup that includes it: it puts `mochi.exe` in
     `%LOCALAPPDATA%\MOCHI\bin` and adds that folder to your PATH (older setups installed
     only the desktop app), or
   - build it from the MOCHI repository (needs Rust from rustup.rs):

     ```bash
     cargo build --release -p mochi-cli
     ```

     This gives you `target/release/mochi` (`mochi.exe` on Windows).

   With `"mochi": null` in the config (the default), the tool finds it on its own.
   It looks in this order:
   1. **the Windows registry**, where the MOCHI setup records it: setup 0.9.15 and later write
      `HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths\mochi.exe` (also read from
      `HKLM`); for setup 0.9.14 the tool reads MOCHI's install folder
      (`HKCU\Software\The MOCHI Project contributors\MOCHI`) and adds `bin\mochi.exe`
   2. PATH
   3. `%LOCALAPPDATA%\MOCHI\bin` (the MOCHI setup; found even from a terminal opened
      before the install, which still has the old PATH)
   4. `%USERPROFILE%\.cargo\bin` (where `cargo install --path crates/mochi-cli` puts it)
   5. next to `backup_ui.py` (or the `.exe`)
   6. a MOCHI checkout beside this one, such as `D:/GitHub/MOCHI/target/release/mochi.exe`
      next to `D:/GitHub/Finisar_Logs_backup`

   Anywhere else, set the full path, for example
   `"mochi": "C:/tools/mochi.exe"`. Then that path is the only one used.

   Check what it finds (this also tests the SQL Server login):

   ```bash
   python backup_ui.py --check-sql
   ```

   Every real run checks this first and stops before moving any file if `mochi`
   cannot be found. A dry run only warns. A Python venv makes no difference: it
   changes which Python runs, not where programs are found.

## Desktop app

```bash
python gui.py
```

On Windows: double-click `run_app.bat`, or `FinisarLogsBackup.exe` once built (below).

| Page | What it does |
|---|---|
| **Backup** | Two separate ways to run. **A · Continue from the last run** (the regular backup) shows the date range it would use. **B · Custom date range** takes any From/To dates; it never changes where A starts. Each has **Preview** (moves nothing) and **Start backup** (asks before any file moves). While a run goes: a row per step with progress, counts and time, the log, and **Stop**. |
| **History** | Every run in the index: when, status, mode (continue, custom, resumed), date range, counts |
| **Settings** | Everything from the table under [Configure](#configure), with Browse buttons. **Save** checks the values first. |
| **Tools** | **Run check** (finds `mochi`, one SQL Server connection), **Export to…** (see [Export](#export-a-copy-of-everything-to-restore-from)), open the log file or the settings folder |

**Settings are saved per Windows user** in `%APPDATA%\FinisarLogsBackup\settings.json` (the same
keys as `config.json`). On the first start the app imports a `config.json` from the current folder
or next to the program, if there is one; **Import config.json…** does it later. The run log is
`backup.log` in the same folder.

**The SQL Server password** is asked when a run starts and kept until the app closes. With
**Remember the password** it is saved encrypted with Windows DPAPI for the current Windows user:
only that user, on that PC, can decrypt it. Another user, or a copied settings file, cannot.

**Stop** ends the run at the next safe point: between files while scanning or staging, or before
the next month's commit. A `mochi` commit that has started is finished first (stopping it could
leave a broken archive tail), so a stop can take as long as one batch's commit.

**Then you can close the program or shut down the PC.** The run's plan (the files it still has to
archive, its date range) is kept in the index. Next time, the Backup page shows a **Stopped run**
card with how many files are left and two buttons. Every stopped run has its own card: an A
run and any number of B (custom range) runs can wait side by side, and each continues on its own.

- **Continue**: first finishes the batch that was staged when it stopped (the **Finish the previous
  run** row), then archives the rest of the plan. No SQL query and no new scan, so on a slow NAS it
  starts right away. Files removed from the log folders in the meantime are skipped. If the log
  folder cannot be reached (NAS not connected yet), it stops without changing anything and the
  plan stays.
- **Discard**: forgets the plan. Nothing is deleted; the files stay in the log folders and a later
  run finds them with a new scan.

Running something else meanwhile is fine. A stopped **B** run is never replaced: it waits until you
continue or discard it. Starting a new **A** run while a stopped A run waits asks first, because the
new A run covers the same dates and replaces it (choose **Continue stopped run** to save the scan).
Files already staged when a run stopped are never dropped: whichever run comes next finishes them
first. The same holds for a crash, a power cut or Windows shutting down during
a run: the next run finishes what was staged and the plan continues. While a run works, the app
keeps Windows from going to sleep (the screen may still turn off). Closing the window during a run
asks, then stops the same way and closes.

**Integrity across the break.** With copy staging (network drive) every file's BLAKE3 is recorded
while it is copied from the NAS. Before a batch is committed, each local copy is hashed again and
must match; a copy that does not (damaged by a power cut before it reached the disk) or whose
record is missing is deleted and copied again later from the original, which is still on the NAS.
The originals are deleted only after the archive is verified and indexed, and only if they did not
change. So a stop, a shutdown or a power cut at any moment loses nothing and archives nothing damaged.

## Everything on a NAS

When the log folder, the archive folder and the index are all on network drives:

| What | Where | Why |
|---|---|---|
| Staging | `%LOCALAPPDATA%\FinisarLogsBackup\staging` on this PC (automatic when the archive folder is on a network drive) | Each file crosses the network once in (copied from the logs) and once out (written into the archive by `mochi`). The checksum check and `mochi` read the local copy. Holds one batch at a time; the run checks for its size plus 10% free first. |
| Index snapshots | `%LOCALAPPDATA%\FinisarLogsBackup\index-snapshots`, the last 5 | SQLite on a network share is safe with one writer at a time (the run lock), but a connection that drops in the middle of a write can damage it. Before each run the index is copied here (SQLite's backup method) and the copy is checked (`PRAGMA quick_check`). A damaged index stops the run before anything moves, with the path of the newest good copy to put back. |
| Run lock | `<archive folder>/.backup.lock` | Two PCs cannot run at the same time against the same NAS. |

Tools → **Run check** shows which folders are on a network drive and where staging and the
snapshots are. Keep an export on another drive too (Tools → Export).

If you start runs from two different PCs, each keeps its own staging. A batch staged on PC 1 and
then continued from PC 2 is safe: with copy staging the originals stay on the NAS until archived,
so PC 2 archives them, and PC 1 later finds them already in the archive and only cleans up.

## Single-file .exe (Windows)

`build_exe.bat` packs Python, the code and its libraries into `dist\FinisarLogsBackup.exe` (the desktop
app) and `dist\FinisarLogsBackupCLI.exe` (the terminal version), so the PC that runs the backup does
not need Python. Build it on any Windows PC with Python 3.9 or newer:

```bash
build_exe.bat
```

What it does:

1. creates a virtual environment in `.venv` the first time (with `py -3`, else `python`), so nothing
   goes into the system Python;
2. installs `requirements.txt` and PyInstaller into `.venv`;
3. runs the tests (`test_logs_backup`, and `test_gui` without a screen) and stops if they fail;
4. writes `dist\FinisarLogsBackup.exe` and `dist\FinisarLogsBackupCLI.exe`.

`build_exe.bat clean` deletes `.venv`, `build` and `dist` first and builds from scratch.

Double-click `FinisarLogsBackup.exe` and fill in Settings. `FinisarLogsBackupCLI.exe` takes the same
options as `backup_ui.py`; it uses a `config.json` in the current folder or the `.exe`'s folder if there
is one, else the settings the app saved, and writes `backup.log` next to whichever it used. For a
scheduled task, save the password in the app (Remember) as the same Windows user the task runs as:

```bash
FinisarLogsBackupCLI.exe --check-sql
```

```bash
FinisarLogsBackupCLI.exe --yes
```

The `.exe` does not contain `mochi.exe`. It finds it the same way as the Python version (step 3 of
[Install](#install-once)), and also looks for `mochi.exe` in the `.exe`'s own folder.

Once `.venv` exists, `run_backup.bat` uses it too.

## Configure

The desktop app's Settings page writes all of this for you. To use a file instead, copy
`config.example.json` to `config.json`, next to the program.

The commands find the settings on their own: `config.json` in the folder you run
them from, else the one next to `backup_ui.py`, else the settings the desktop app saved. To use another file, pass its
path last, for example `python backup_ui.py --dry-run D:/configs/line2.json`.

> **Windows paths:** write them with `/` (`E:/archive`) or doubled `\\`
> (`E:\\archive`). A single `\` breaks JSON: `"E:\backup"` reads as `E:` +
> backspace + `ackup`. The run stops with a "Config problem" message if this happens.

| Key | Default | Meaning |
|---|---|---|
| `source_dir` | required | Folder that holds the subfolders below |
| `output_dir` | required | Where `YYYY/YYYY-MM.mochi` archives go. Created if missing. |
| `db_path` | required | SQLite index file. Its folder is created if missing. |
| `sql_server` | none | SQL Server login, see below |
| `staging_mode` | `null`: automatic | How files get into staging. `"move"`: renamed into staging on the source drive. `"copy"`: copied to staging on a local disk, and each original deleted from the source only after its archive is verified (and only if it did not change after it was copied; a changed file stays for the next run). Automatic: **`"copy"` when `source_dir` is a network drive** (a mapped drive or `//server/share`), `"move"` on a local disk. |
| `staging_dir` | `source_dir/.staging` (move); `output_dir/.staging` (copy), or this PC's `%LOCALAPPDATA%\FinisarLogsBackup\staging` when the archive folder is on a network drive | With `"move"` it must be on the **same drive** as `source_dir` (files are moved there by rename); a run refuses to start staging otherwise. With `"copy"` put it on a **local disk** with room for one batch (a run checks for the batch's size plus 10% before copying). Created if missing. Never scanned, even inside a subfolder. |
| `subfolders` | `BURNIN, ESS, JTAG, STM, Xray` | The folders scanned under `source_dir` |
| `recursive` | `true` | `true`: also scan every folder inside each subfolder, at any depth, keeping those folders in the archive paths (`BURNIN/line2/unit1_BC001_log.txt`). `false`: only files directly in each subfolder. |
| `file_threads` | `null`: automatic | How many folders are listed, and files copied or renamed into staging and deleted after the commit, at once. Automatic: **16 when `source_dir` is a network drive** (a mapped drive or `//server/share`), where each call mostly waits on the network; 1 on a local disk, where one thread is fastest. Set 1–64 to override. |
| `extensions` | `txt, zip` | Allowed file extensions |
| `lookback_days` | `2` | The newest `N` days are left for the next run |
| `date_range` | none | `["YYYY-MM-DD", "YYYY-MM-DD"]` to back-fill a fixed window instead. Like the app's custom range, such runs do not move where a continuing run starts. |
| `batch_files` | `10000` | Files staged and committed per batch (100–1,000,000). A stop waits for at most one batch's commit. Each batch is one MOCHI commit per month it touches. |
| `mochi` | `null`: found automatically | Full path to `mochi.exe`, only needed if it is not in one of the places [Install](#install-once) step 3 lists |
| `email` | none | `{"api_base_url": "http://host:port", "to": "a@b.c"}` for a report after each run |

### SQL Server login

Put it in `config.json`:

```json
"sql_server": {
  "server": "sqlhost",
  "port": 1433,
  "database": "ForthUtilities",
  "user": "backup_reader",
  "password": null,
  "encryption": null
}
```

- `server`: a host name or IP. For a **named instance** write `HOST\\NAME` (two
  backslashes in JSON). The port is then found by name and `port` is ignored.
- `encryption`: how the login is protected:

  | Value | Meaning |
  |---|---|
  | `null` or `"request"` | Default. Uses TLS when the server offers it. |
  | `"require"` or `true` | Refuse to connect without TLS |
  | `"off"` or `false` | **No encryption at all**, the same as Finisar_move_log_files. The login and data travel in clear text on the network, so use it only on a trusted factory network. Each run logs a warning. |

  Use `"off"` only if `--check-sql` fails with **error 20002** and the port is right. That usually means the server only offers old TLS versions (SQL Server 2008–2014 without updates), which this program's TLS library refuses. The better fix is to have the server updated for TLS 1.2. Ask your database administrator.

If `"off"` is set, every run prints `SQL Server connection without encryption`.
If you don't see that line, the setting was not applied.

If a connection still fails, FreeTDS can write a protocol log to send to whoever
supports the tool. On Windows (PowerShell): `$env:TDSDUMP="C:/temp/tds.log"`, then
run `--check-sql`. The log does not contain the password, but it does contain the
server name and user.

Test the connection on its own, without scanning or moving anything:

```bash
python backup_ui.py --check-sql
```

It prints the server's version, or one line saying what failed with a hint.

Leave `"password": null` and `backup_ui.py` asks for it each run, hidden as you
type, so it is never saved anywhere. That suits manual runs. For a scheduled task
(`logs_backup.py`, which never asks), give the password through `DB_PASSWORD`
instead. You can also write it in the file, but then anyone who can read
`config.json` can read the password. `config.json` is already in `.gitignore`.

Each value can also come from an environment variable, which **wins** over the
file:

| Variable | Overrides | Default |
|---|---|---|
| `DB_SERVER`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `server`, `database`, `user`, `password` | |
| `DB_PORT` | `port` | 1433 |
| `DB_ENCRYPTION` | `encryption` | `request` |
| `DB_RETRY_ATTEMPTS` / `DB_RETRY_BASE_DELAY_MS` | (env only) | 3 / 2000 |
| `DB_REQUEST_TIMEOUT_MS` | (env only) | 30000 |

On Windows, set variables under System Properties → Environment Variables, so
the double-click launcher and scheduled tasks see them.

## Run

| Want | Do |
|---|---|
| Check that `mochi` is found and SQL Server answers | `python backup_ui.py --check-sql` |
| Copy everything needed to restore, to another drive | `python backup_ui.py --export E:/finisar_export` (see [Export](#export-a-copy-of-everything-to-restore-from)) |
| See what would happen | `python backup_ui.py --dry-run` |
| Run by hand | The desktop app (`run_app.bat` or `FinisarLogsBackup.exe`), or `run_backup.bat` / `python backup_ui.py` |
| Back-fill old dates | The app's **B · Custom date range** |
| Stop a running backup | **Stop** in the app, or Ctrl+C in the terminal. Shut down if you like; **Continue** (app) or the next terminal run picks up where it stopped |
| Start fresh although a run was stopped | **Discard** on its Stopped run card, or `python backup_ui.py --new` (stopped runs are kept for later) |
| Run without the question | `python backup_ui.py --yes` |
| Plain log output (scheduled task) | `python logs_backup.py` |

Every continuing run starts where the last successful continuing one ended, so
running by hand every few days or weeks is fine. If a run is stopped (Stop, window
closed, power cut), the next run finishes its leftover batch first. The app writes a
full log to `backup.log` in its settings folder; the terminal UI next to the settings
file it used.

## Get files back

1. Find a barcode's files in the index (`sqlite3` or DB Browser for SQLite):

   ```sql
   SELECT e.relative_path, v.path
   FROM scanned_log_entry e JOIN file_storage_volume v ON v.id = e.file_storage_volume_id
   WHERE e.barcode = 'BC001';
   ```

2. Extract one file. `mochi` checks it against its hash before writing it:

   ```bash
   mochi get E:/archive/2026/2026-08.mochi BURNIN/unit1_BC001_log.txt -C D:/restore
   ```

Without the index:
- `mochi list ARCHIVE` lists a month's files.
- `mochi search ARCHIVE BC001` finds files by name.
- `mochi get ARCHIVE -C DIR` extracts a whole month.

## Check old archives now and then

Each run verifies the archives it wrote. To re-check older months for disk
damage, for example monthly:

```bash
mochi verify E:/archive/2026/2026-01.mochi
```

Exit code 0 or 2 is fine. Exit code 1 means a problem: keep your independent
copy and see MOCHI's repair docs.

## Export: a copy of everything to restore from

One command copies what you need to move this setup to another PC, or to keep
as an independent copy:

```bash
python backup_ui.py --export E:/finisar_export
```

| In the target folder | From | Notes |
|---|---|---|
| `archives/YYYY/YYYY-MM.mochi` | `output_dir` | Every copy is checked against the original (BLAKE3) before it replaces anything. Archives unchanged since the last export to the same folder are skipped, so exporting again is quick. |
| `index.db` | `db_path` | Copied with SQLite's backup method, so the copy is consistent |
| `config.json` | your config | The SQL Server password is removed. It is asked again on the first run. |
| `staging/` | `staging_dir` | Only after an interrupted run. Those files are in no archive yet. With move staging this is their only other copy; with copy staging the originals are also still in the source. |
| `RESTORE.txt` | | The restore steps below |
| `export-manifest.json` | | Size and BLAKE3 of every exported file |

Export holds the same lock as a backup run, so a run cannot change an archive
while it is copied. It never deletes or moves anything in the backup itself.
The target must not be inside `source_dir` or `output_dir`. Use another drive,
ideally one you can unplug and keep elsewhere.

**To restore on another PC:**

1. Copy the export folder there.
2. Edit its `config.json`: `output_dir` = `<export folder>/archives`, `db_path` =
   `<export folder>/index.db` (or copy them where you want them and point there).
   Also set `source_dir` and `mochi`.
3. If `staging/` exists, copy its contents into `<source_dir>/.staging` before the first run.
4. Run `--check-sql`, then `--dry-run`, then a real run. The first real run
   finishes the staged files.

## Moving to another PC by hand: what to copy

`--export` does all of this. By hand:

| Copy | Why |
|---|---|
| `output_dir` (all `.mochi` files) | The archived logs |
| `db_path` (the index) | Lookup by barcode, and where the next run should start |
| `staging_dir` (default `source_dir/.staging`, or `output_dir/.staging` with copy staging), **if it exists** | Only after an interrupted run. It holds files that are in no archive yet (with move staging, no longer in the source either). Copy as is. |
| `config.json` | Paths and settings |
| Your SQL password, and any `DB_*` values | They are not in any file (unless you wrote the password in `config.json`) |

Not needed: the code, `mochi` (rebuild it), `.backup.lock`, `backup.log`.

Keep the same paths on the new PC if you can. The index stores each archive's
full path. If paths change, everything still works, but the stored path of a
month is only updated the next time that month gets new files.

## Troubleshooting

| Message | Meaning | What to do |
|---|---|---|
| `SQL Server login incomplete, missing: ...` | Part of the login is not set | Add it to `sql_server` in `config.json`, or set the `DB_*` variable (see [SQL Server login](#sql-server-login)) |
| `Config problem: Invalid \escape` | A single `\` in a path in `config.json` | Write paths with `/` (`E:/archive`) |
| `... has a broken path: write Windows paths with /` | A `\b`, `\t`, `\n`, ... in a path turned into a control character | Same fix |
| `cannot open the index ...` | `db_path` is on a drive that does not exist, or you lack permission | Fix `db_path` |
| `SQL Server query failed: ... (error 20009)` | Nothing answers at that address and port | Check `server`, `port`, the network and the firewall. Nothing was moved. |
| `SQL Server query failed: TDS server connection failed ... (error 20002)` | Something answered, but the SQL Server handshake failed | 1. Check `port`: a named instance is not on 1433, so write `HOST\\NAME` as `server`. 2. If the port is right, the server probably only offers old TLS. Ask for TLS 1.2 on the server, or set `"encryption": "off"` (see [SQL Server login](#sql-server-login)). Retry with `--check-sql`. |
| `SQL Server query failed: Login failed ... (error 18456)` | Wrong user or password, or the login is disabled | Check them. |
| `SQL Server query failed: ... (error 4060)` | The login cannot open `database` | Check `database` and the login's permissions |
| `the mochi program was not found. Looked at: ...` | `mochi.exe` is in none of the listed places | See [Install](#install-once) step 3. If it says only the desktop app is installed, build the command-line program. Files already in staging stay there and are finished by the next run. |
| `... kept in staging: [WinError 2] The system cannot find the file specified` (versions before this check) | Same as above | Same. Do not move staged files by hand. |
| `another backup run is in progress` | Another run holds the lock | Wait for it. If no run is open, a process may still be alive. Check Task Manager. |
| `not staged ...: [WinError 32] ...` | A file is open in another program | Nothing to do. It stays in the source and a later run takes it. |
| `mochi append ...: exit 3 {'code': 'LOCK_CONFLICT'...}` | Something else is writing that archive (for example a `mochi` command you started) | Close it and run again |
| `mochi ...: exit 3 {'code': 'UNCOMMITTED_TAIL'...}` | An earlier commit was interrupted mid-write | **A person must fix it once:** `mochi verify ARCHIVE`, then `mochi append ARCHIVE --truncate-tail` (it saves the tail to a `.mochiq` file first). Then run the backup again. Never automate this. |
| `... staged files differ from the archive head` | A staged file changed after it was committed, or the archive is not what was written | Staging is kept. Investigate before running again. |
| `left in staging (not listed in the manifest)` | A file in staging that no run recorded | It is never deleted. Check it and move it back to the source if it belongs there. |
| `DEGRADED (not an error)` | Committed, but the folder entry's durability is unconfirmed (always for a new archive on Windows) | Nothing. Data is committed. |
| `index of another format/version` | `db_path` points to an old tar.zst index | Move that file aside. A new index is created. |
