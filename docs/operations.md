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

3. **The `mochi` program.** Build it from the MOCHI repository (needs Rust from rustup.rs):

   ```bash
   cargo build --release -p mochi-cli
   ```

   This gives you `target/release/mochi` (`mochi.exe` on Windows). The MOCHI
   **desktop app installer does not include it**: the backup needs this
   command-line program.

   With `"mochi": null` in the config (the default), the tool finds it on its own.
   It looks in this order:
   1. PATH
   2. `%USERPROFILE%\.cargo\bin` (where `cargo install --path crates/mochi-cli` puts it)
   3. next to `backup_ui.py`
   4. a MOCHI checkout beside this one, such as `D:/GitHub/MOCHI/target/release/mochi.exe`
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

## Configure

Copy `config.example.json` to `config.json`, next to the program.

The commands find the config on their own: `config.json` in the folder you run
them from, else the one next to `backup_ui.py`. To use another file, pass its
path last, for example `python backup_ui.py --dry-run D:/configs/line2.json`.

> **Windows paths:** write them with `/` (`E:/archive`) or doubled `\\`
> (`E:\\archive`). A single `\` breaks JSON: `"E:\backup"` reads as `E:` +
> backspace + `ackup`. The run stops with a "Config problem" message if this happens.

| Key | Default | Meaning |
|---|---|---|
| `source_dir` | required | Folder that holds the subfolders below |
| `output_dir` | required | Where `YYYY/YYYY-MM.mochi` archives go |
| `db_path` | required | SQLite index file. Its folder is created if missing. |
| `sql_server` | none | SQL Server login, see below |
| `staging_dir` | `source_dir/.staging` | Must be on the **same drive** as `source_dir`: files are moved there by rename. A run refuses to start staging otherwise. Not scanned, because only the subfolders are. |
| `subfolders` | `BURNIN, ESS, JTAG, STM, Xray` | Scanned one level deep |
| `extensions` | `txt, zip` | Allowed file extensions |
| `lookback_days` | `2` | The newest `N` days are left for the next run |
| `date_range` | none | `["YYYY-MM-DD", "YYYY-MM-DD"]` to back-fill a fixed window instead |
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
| Run by hand | Double-click `run_backup.bat`, or `python backup_ui.py` |
| Run without the question | `python backup_ui.py --yes` |
| Plain log output (scheduled task) | `python logs_backup.py` |

Every run continues where the last successful one ended, so running by hand
every few days or weeks is fine. If a run is stopped (window closed, power cut),
the next run finishes its leftover batch first. The UI writes a full log to
`backup.log` next to `config.json`.

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
| `staging/` | `staging_dir` | Only after an interrupted run. Those files are in no archive yet, so this is their only other copy. |
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
| `staging_dir` (default `source_dir/.staging`), **if it exists** | Only after an interrupted run. It holds files that are in no archive yet and no longer in the source. Copy as is. |
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
