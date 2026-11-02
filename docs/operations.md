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

   This gives you `target/release/mochi` (`mochi.exe` on Windows). Put its folder on
   PATH, or set its full path as `"mochi"` in the config.

## Configure

Copy `config.example.json` to `config.json`.

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
| `mochi` | `mochi` | Path to the `mochi` program |
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
  | `"require"` | Refuse to connect without TLS |
  | `"off"` | **No encryption at all**, the same as Finisar_move_log_files. The login and data travel in clear text on the network, so use it only on a trusted factory network. Each run logs a warning. |

  Use `"off"` only if `--check-sql` fails with **error 20002** and the port is right. That usually means the server only offers old TLS versions (SQL Server 2008–2014 without updates), which this program's TLS library refuses. The better fix is to have the server updated for TLS 1.2. Ask your database administrator.

Test the connection on its own, without scanning or moving anything:

```bash
python backup_ui.py config.json --check-sql
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
| Test the SQL Server login only | `python backup_ui.py config.json --check-sql` |
| See what would happen | `python backup_ui.py config.json --dry-run` |
| Run by hand | Double-click `run_backup.bat`, or `python backup_ui.py config.json` |
| Run without the question | `python backup_ui.py config.json --yes` |
| Plain log output (scheduled task) | `python logs_backup.py config.json` |

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

## Moving to another PC: what to copy

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
| `another backup run is in progress` | Another run holds the lock | Wait for it. If no run is open, a process may still be alive. Check Task Manager. |
| `not staged ...: [WinError 32] ...` | A file is open in another program | Nothing to do. It stays in the source and a later run takes it. |
| `mochi append ...: exit 3 {'code': 'LOCK_CONFLICT'...}` | Something else is writing that archive (for example a `mochi` command you started) | Close it and run again |
| `mochi ...: exit 3 {'code': 'UNCOMMITTED_TAIL'...}` | An earlier commit was interrupted mid-write | **A person must fix it once:** `mochi verify ARCHIVE`, then `mochi append ARCHIVE --truncate-tail` (it saves the tail to a `.mochiq` file first). Then run the backup again. Never automate this. |
| `... staged files differ from the archive head` | A staged file changed after it was committed, or the archive is not what was written | Staging is kept. Investigate before running again. |
| `left in staging (not listed in the manifest)` | A file in staging that no run recorded | It is never deleted. Check it and move it back to the source if it belongs there. |
| `DEGRADED (not an error)` | Committed, but the folder entry's durability is unconfirmed (always for a new archive on Windows) | Nothing. Data is committed. |
| `index of another format/version` | `db_path` points to an old tar.zst index | Move that file aside. A new index is created. |
