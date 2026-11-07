"""Tests. The flow tests need the `mochi` CLI (on PATH or in $MOCHI_BIN): python -m unittest -v"""
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

import logs_backup as lb

MOCHI = os.environ.get("MOCHI_BIN") or shutil.which("mochi")


def connect(test, path):
    """A SQLite connection closed when the test ends: an open one keeps the file locked on Windows,
    so the temp folder could not be deleted."""
    db = sqlite3.connect(path)
    test.addCleanup(db.close)
    return db


class Matching(unittest.TestCase):
    def test_barcode_needs_delimiters_on_both_sides(self):
        codes = {"123", "12345"}
        self.assertEqual(lb.find_barcodes("é123ü", codes), set())
        self.assertEqual(lb.find_barcodes("é_123-ü", codes), {"123"})
        self.assertEqual(lb.find_barcodes("123_12345", codes), {"123", "12345"})
        self.assertEqual(lb.find_barcodes("x12345", codes), set())
        self.assertEqual(lb.find_barcodes("unit_AB-7_log.txt", {"AB-7"}), {"AB-7"})

    def test_extension(self):
        exts = {"txt", "zip"}
        self.assertTrue(lb.has_extension("a.b.TXT", exts))
        self.assertFalse(lb.has_extension("noext", exts))
        self.assertFalse(lb.has_extension("a.txt.bak", exts))


class ConfigFile(unittest.TestCase):
    def config(self, text, **env):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "config.json").write_text(text)
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            for var in ("DB_SERVER", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"):
                if var not in env:
                    os.environ.pop(var, None)
            return lb.Config(tmp / "config.json"), tmp

    def test_sql_login_from_config_with_env_override(self):
        cfg, _ = self.config(json.dumps({"source_dir": "s", "output_dir": "o", "db_path": "i.db",
                                         "sql_server": {"server": "h", "database": "d", "user": "u"}}),
                             DB_PASSWORD="pw", DB_USER="env_user")
        self.assertEqual(cfg.sql, {"server": "h", "port": None, "database": "d", "user": "env_user", "password": "pw",
                                   "encryption": None})
        with self.assertRaisesRegex(lb.BackupError, "missing: server, database, user, password"):
            lb.query_candidates({}, None, "2026-01-01")

    def test_backspace_in_a_path_is_refused(self):
        with self.assertRaisesRegex(lb.BackupError, "db_path .* broken path"):
            self.config(r'{"source_dir": "s", "output_dir": "o", "db_path": "E:\backup.db"}')  # JSON \b = backspace

    def test_sql_errors_are_one_readable_line_with_a_hint(self):
        handshake = Exception((20002, b"DB-Lib error message 20002, severity 9:\nTDS server connection failed"
                                      b" (192.168.31.244)\nDB-Lib error message 20002, severity 9:\nTDS server"
                                      b" connection failed (192.168.31.244)\n"))
        self.assertTrue(lb.sql_error(handshake).startswith(
            "TDS server connection failed (192.168.31.244) (error 20002): the server answered but"))
        # pymssql can keep the previous server error's code (18456): the text's code wins.
        stale = Exception((18456, b"DB-Lib error message 20009, severity 9:\nUnable to connect: TDS server is"
                                  b" unavailable or does not exist (h)\n"))
        self.assertIn("(error 20009): the server cannot be reached", lb.sql_error(stale))
        login = Exception((18456, b"Login failed for user 'u'.DB-Lib error message 20018, severity 14:\n"))
        self.assertEqual(lb.sql_error(login), "Login failed for user 'u'. (error 18456): login failed."
                                              " Check the user and password")
        with self.assertRaisesRegex(lb.BackupError, '"encryption" must be'):
            lb.set_encryption("maybe")

    def test_encryption_values(self):
        for given, mode in [(None, "request"), ("request", "request"), (False, "off"), ("off", "off"),
                            ("false", "off"), ("0", "off"), (True, "require"), ("require", "require"), ("1", "require")]:
            if mode == "off":  # the warning is expected: checked here, not printed
                with self.assertLogs("logs_backup", "WARNING") as logs:
                    lb.set_encryption(given)
                self.assertIn("without encryption", logs.output[0])
            else:
                lb.set_encryption(given)
            conf = Path(os.environ["FREETDSCONF"]).read_text()
            self.assertIn(f"encryption = {mode}", conf, given)

    def test_config_is_found_without_an_argument(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        here, program = tmp / "here", tmp / "program"
        here.mkdir(), program.mkdir()
        old_cwd = os.getcwd()
        self.addCleanup(os.chdir, old_cwd)
        os.chdir(here)
        home = tmp / "app-settings"
        with unittest.mock.patch.object(lb, "__file__", str(program / "logs_backup.py")), \
                unittest.mock.patch.dict(os.environ, {"FINISAR_BACKUP_HOME": str(home)}):
            with self.assertRaisesRegex(lb.BackupError, "no settings found"):
                lb.find_config()
            lb.save_settings({"source_dir": "D:/logs"})  # what the desktop app saved: used last
            self.assertEqual(lb.find_config(), home / "settings.json")
            (program / "config.json").write_text("{}")
            self.assertEqual(lb.find_config().resolve(), (program / "config.json").resolve())  # next to the program
            (here / "config.json").write_text("{}")
            self.assertEqual(lb.find_config().resolve(), (here / "config.json").resolve())  # current folder first
            self.assertEqual(lb.find_config("x/other.json"), Path("x/other.json"))  # a given path wins
            (here / "config.json").unlink()
            with unittest.mock.patch.object(lb.sys, "frozen", True, create=True), \
                    unittest.mock.patch.object(lb.sys, "executable", str(tmp / "exe" / "app.exe")):
                (tmp / "exe").mkdir()
                (tmp / "exe" / "config.json").write_text("{}")
                self.assertEqual(lb.find_config().resolve(), (tmp / "exe" / "config.json").resolve())  # next to the .exe

    def test_mochi_path_from_the_registry(self):
        class FakeReg:  # stands in for winreg, which exists only on Windows
            HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKCU", "HKLM"

            def __init__(self, values):
                self.values = values

            def OpenKey(self, root, key):
                if (root, key) not in self.values:
                    raise FileNotFoundError(key)
                return contextlib.nullcontext((root, key))

            def QueryValueEx(self, k, name):
                return self.values[k], 1

        both = FakeReg({("HKCU", lb.APP_PATHS_KEY): r"C:\Users\u\AppData\Local\MOCHI\bin\mochi.exe",
                        ("HKCU", lb.MOCHI_INSTALL_KEY): r"C:\Users\u\AppData\Local\MOCHI"})
        found = lb.registry_mochi(both)
        self.assertEqual(found[0], r"C:\Users\u\AppData\Local\MOCHI\bin\mochi.exe")  # App Paths first (0.9.15)
        self.assertEqual(len(found), 2)
        self.assertTrue(found[1].endswith(lb.MOCHI_EXE) and "bin" in found[1])  # install key + bin (0.9.14)
        self.assertEqual(lb.registry_mochi(FakeReg({})), [])
        if os.name != "nt":
            self.assertEqual(lb.registry_mochi(), [])  # no registry off Windows
        with unittest.mock.patch.object(lb, "registry_mochi", lambda: ["/reg/mochi"]):
            self.assertEqual(lb.mochi_places(None)[0], "/reg/mochi")  # before PATH and the other places
            self.assertEqual(lb.mochi_places("C:/tools/mochi.exe"), ["C:/tools/mochi.exe"])  # a set path wins

    def test_index_folder_is_created(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        lb.open_index(tmp / "new" / "folder" / "index.db").close()
        self.assertTrue((tmp / "new" / "folder" / "index.db").is_file())


class Staging(unittest.TestCase):
    """stage() alone: no mochi needed."""

    def config(self, **extra):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        raw = {"source_dir": str(tmp / "src"), "output_dir": str(tmp / "out"), "db_path": str(tmp / "index.db")}
        (tmp / "config.json").write_text(json.dumps(dict(raw, **extra)))
        return lb.Config(tmp / "config.json")

    def test_file_threads_setting(self):
        self.assertEqual(self.config(file_threads=8).threads, 8)
        self.assertEqual(self.config().threads, 1)  # a local temp folder: one thread
        for bad in (0, 65, 2.5, True, "4"):
            with self.assertRaisesRegex(lb.BackupError, "file_threads"):
                self.config(file_threads=bad)

    def test_network_paths(self):
        self.assertTrue(lb.is_network_path("//server/share/logs"))
        self.assertTrue(lb.is_network_path(r"\\server\share\logs"))
        self.assertFalse(lb.is_network_path(tempfile.gettempdir()))

    def test_settings_dict_and_saved_password(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        raw = {"source_dir": str(tmp / "src"), "output_dir": str(tmp / "out"), "db_path": str(tmp / "i.db"),
               "sql_server": {"user": "u", "password_dpapi": "c2VjcmV0"}}
        with unittest.mock.patch.object(lb, "unprotect", lambda token: "pw:" + token):
            cfg = lb.Config(raw=raw)  # the desktop app: no file
        self.assertEqual(cfg.sql["password"], "pw:c2VjcmV0")
        self.assertEqual(cfg.batch_files, lb.BATCH_FILES)
        with self.assertRaises(KeyError):
            lb.Config(raw=dict(raw, source_dir=""))
        with self.assertRaisesRegex(lb.BackupError, "batch_files"):
            lb.Config(raw=dict(raw, batch_files=5, sql_server={}))
        path = lb.save_settings(dict(raw, sql_server={}), tmp / "app" / "settings.json")
        self.assertEqual(lb.load_settings(path)["db_path"], raw["db_path"])
        self.assertIsNone(lb.load_settings(tmp / "none.json"))
        if not lb.can_protect():
            with self.assertRaisesRegex(lb.BackupError, "only available on Windows"):
                lb.protect("secret")
        else:
            self.assertEqual(lb.unprotect(lb.protect("secret ü")), "secret ü")

    def test_check_window(self):
        self.assertEqual(lb.check_window(["2026-01-01", "2026-01-31"]), ("2026-01-01", "2026-01-31"))
        for bad in (["2026-01-31", "2026-01-01"], ["2026-13-01", "2026-12-31"], ["x"]):
            with self.assertRaises(lb.BackupError):
                lb.check_window(bad)

    def test_parallel_scan_finds_the_same_files(self):
        cfg = self.config()
        for n in range(300):
            p = cfg.source_dir / "ESS" / f"line{n % 7}" / f"sub{n % 3}" / f"u_BC{n:04}.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        (cfg.source_dir / ".staging" / "ESS").mkdir(parents=True)
        (cfg.source_dir / ".staging" / "ESS" / "u_BC0001.txt").write_text("staged")
        found = {}
        for threads in (1, 8):
            found[threads] = sorted(f"{rel}{item.name}" for rel, item in lb.walk_files(
                cfg.source_dir / "ESS", True, {lb.path_key(cfg.source_dir / ".staging")}, None, threads))
        self.assertEqual(len(found[1]), 300)
        self.assertEqual(found[1], found[8])

    def test_staging_mode_setting(self):
        self.assertEqual(self.config().mode, "move")  # a local temp folder
        cfg = self.config(staging_mode="copy")
        self.assertEqual(cfg.staging_dir, cfg.output_dir / ".staging")
        self.assertEqual(cfg.staging_dirs(), [cfg.output_dir / ".staging", cfg.source_dir / ".staging"])
        with self.assertRaisesRegex(lb.BackupError, "staging_mode"):
            self.config(staging_mode="rename")

    def test_threaded_stage_moves_every_file_and_never_overwrites(self):
        cfg = self.config(file_threads=8)
        entries = []
        for n in range(2500):  # more than one manifest batch
            rel = f"ESS/unit_BC{n:04}.txt"
            (cfg.source_dir / "ESS").mkdir(parents=True, exist_ok=True)
            (cfg.source_dir / rel).write_text(str(n))
            entries.append({"relative_path": rel, "source_subfolder": "ESS", "barcode": f"BC{n:04}",
                            "original_size": len(str(n)), "volume_name": "2026-08"})
        taken = lb.staged_path(cfg, entries[7])
        taken.parent.mkdir(parents=True)
        taken.write_text("already staged")
        reports = []
        logger = logging.getLogger("logs_backup")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.ERROR)
        errors = lb.stage(cfg, 1, entries, lambda *args: reports.append(args))
        self.assertEqual(errors, 1)
        self.assertEqual(taken.read_text(), "already staged")  # not overwritten
        self.assertTrue((cfg.source_dir / entries[7]["relative_path"]).is_file())  # left in the source
        for n, e in enumerate(entries):
            if n != 7:
                self.assertEqual(lb.staged_path(cfg, e).read_text(), str(n))
        self.assertEqual(reports[-1], ("stage", "", 2500, 2500))
        self.assertGreater(len(reports), 20)  # updates every 100 files, not every 2,000
        self.assertEqual(len(lb.read_manifest(cfg)[1]), 2500)


@unittest.skipUnless(MOCHI, "mochi CLI not found (set MOCHI_BIN)")
class Flow(unittest.TestCase):
    CANDIDATES = [("BC001", "2026-08-03 10:00:00"), ("BC002", "2026-07-30 09:00:00"), ("BC404", "2026-08-01 00:00:00")]

    def setUp(self):
        # resolve(): on Windows the temp folder can come as a short 8.3 name (C:\Users\IT_113~1\...),
        # while check_mochi reports the full name (C:\Users\IT_113280\...).
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        # On Windows every commit warns "DEGRADED (not an error)": expected, so not printed here.
        logger = logging.getLogger("logs_backup")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.ERROR)
        cfg = {"source_dir": str(self.tmp / "src"), "output_dir": str(self.tmp / "out"),
               "db_path": str(self.tmp / "index.db"), "subfolders": ["BURNIN", "ESS"],
               "date_range": ["2026-07-01", "2026-09-01"], "mochi": MOCHI}
        (self.tmp / "config.json").write_text(json.dumps(cfg))
        self.cfg = lb.Config(self.tmp / "config.json")
        self.write("BURNIN/unit1_BC001_log.txt", b"burnin one")
        self.write("ESS/unit2_BC002.txt", b"ess two")
        self.write("ESS/BC0011_other.txt", b"no match")
        self.write("ESS/unit_BC001.csv", b"wrong extension")

    def write(self, rel, data):
        p = self.cfg.source_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def run_backup(self):
        return lb.run(self.cfg, lambda *_: self.CANDIDATES)

    def head(self, month):
        files, (seq, _) = lb.head_files(self.cfg, self.cfg.archive(month))
        return files, seq

    def test_full_flow_then_append(self):
        summary = self.run_backup()
        self.assertEqual(summary["files_moved"], 2)
        self.assertEqual(summary["unmatched"], ["BC404"])
        files, seq = self.head("2026-08")
        self.assertEqual(files, {"BURNIN/unit1_BC001_log.txt": lb.blake3.blake3(b"burnin one").hexdigest()})
        self.assertEqual(seq, 0)
        self.assertIn("ESS/unit2_BC002.txt", self.head("2026-07")[0])
        self.assertFalse((self.cfg.source_dir / "BURNIN/unit1_BC001_log.txt").exists())
        self.assertTrue((self.cfg.source_dir / "ESS/BC0011_other.txt").exists())
        self.assertFalse(self.cfg.staging_dir.exists())

        db = connect(self, self.cfg.db_path)
        rows = db.execute("SELECT barcode, relative_path, source_subfolder, content_hash, commit_seq"
                          " FROM scanned_log_entry ORDER BY barcode").fetchall()
        self.assertEqual(rows[0], ("BC001", "BURNIN/unit1_BC001_log.txt", "BURNIN",
                                   files["BURNIN/unit1_BC001_log.txt"], 0))
        self.assertEqual(db.execute("SELECT status, total_files_moved FROM scan_session").fetchall(), [("success", 2)])
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

        self.write("ESS/late_BC001.txt", b"arrived later")
        self.assertEqual(self.run_backup()["files_moved"], 1)
        files, seq = self.head("2026-08")
        self.assertEqual(seq, 1)
        self.assertEqual(sorted(files), ["BURNIN/unit1_BC001_log.txt", "ESS/late_BC001.txt"])
        self.assertEqual(db.execute("SELECT entry_count, head_seq FROM file_storage_volume"
                                    " WHERE volume_name = '2026-08'").fetchone(), (2, 1))

    def test_crash_after_staging_is_resumed(self):
        db = lb.open_index(self.cfg.db_path)
        self.addCleanup(db.close)
        entries, _, _ = lb.scan(self.cfg, dict(self.CANDIDATES))
        lb.stage(self.cfg, lb.start_session(db, "", "x"), entries)
        self.CANDIDATES = []
        self.run_backup()
        self.assertEqual(len(self.head("2026-08")[0]), 1)
        self.assertFalse(self.cfg.staging_dir.exists())

    def test_crash_after_commit_before_index_makes_no_new_commit(self):
        db = lb.open_index(self.cfg.db_path)
        self.addCleanup(db.close)
        entries, _, _ = lb.scan(self.cfg, dict(self.CANDIDATES))
        lb.stage(self.cfg, lb.start_session(db, "", "x"), entries)
        lb.commit_month(self.cfg, "2026-08", [e for e in entries if e["volume_name"] == "2026-08"])
        self.CANDIDATES = []
        self.run_backup()
        self.assertEqual(self.head("2026-08")[1], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM scanned_log_entry").fetchone()[0], 2)

    def copy_mode(self):
        """Switches this test's config to copy staging (what a network drive gets by default)."""
        raw = json.loads((self.tmp / "config.json").read_text())
        (self.tmp / "config.json").write_text(json.dumps(dict(raw, staging_mode="copy", file_threads=4)))
        self.cfg = lb.Config(self.tmp / "config.json")
        self.assertEqual(self.cfg.staging_dir, self.cfg.output_dir / ".staging")

    def test_copy_staging_deletes_originals_only_once_archived(self):
        self.copy_mode()
        summary = self.run_backup()
        self.assertEqual((summary["files_moved"], summary["errors"]), (2, 0))
        self.assertEqual(self.head("2026-08")[0],
                         {"BURNIN/unit1_BC001_log.txt": lb.blake3.blake3(b"burnin one").hexdigest()})
        self.assertFalse((self.cfg.source_dir / "BURNIN/unit1_BC001_log.txt").exists())
        self.assertFalse((self.cfg.source_dir / "ESS/unit2_BC002.txt").exists())
        self.assertTrue((self.cfg.source_dir / "ESS/BC0011_other.txt").exists())
        self.assertFalse(self.cfg.staging_dir.exists())

    @unittest.skipIf(os.name == "nt", "uses a shell script as a fake mochi")
    def test_copy_staging_failed_commit_leaves_the_source_untouched(self):
        self.copy_mode()
        fake = self.tmp / "failing-mochi"  # starts, but every commit fails
        fake.write_text('#!/bin/sh\n[ "$1" = --version ] && echo "mochi 0.0" && exit 0\n'
                        'echo \'{"error":{"code":"IO_ERROR","message":"test"}}\'; exit 3\n')
        fake.chmod(0o755)
        self.cfg.mochi = str(fake)
        with self.assertRaisesRegex(lb.BackupError, "IO_ERROR"):
            self.run_backup()
        self.assertTrue((self.cfg.source_dir / "BURNIN/unit1_BC001_log.txt").is_file())  # original kept
        self.assertTrue((self.cfg.staging_dir / "2026-08/BURNIN/unit1_BC001_log.txt").is_file())
        self.cfg.mochi = MOCHI
        self.CANDIDATES = []
        self.assertEqual(self.run_backup()["resumed"], 2)
        self.assertFalse((self.cfg.source_dir / "BURNIN/unit1_BC001_log.txt").exists())
        self.assertFalse(self.cfg.staging_dir.exists())

    def test_copy_staging_keeps_an_original_that_changed(self):
        self.copy_mode()
        db = lb.open_index(self.cfg.db_path)
        self.addCleanup(db.close)
        entries, _, _ = lb.scan(self.cfg, dict(self.CANDIDATES))
        self.assertEqual(lb.stage(self.cfg, lb.start_session(db, "", "x"), entries), 0)
        changed = self.cfg.source_dir / "BURNIN/unit1_BC001_log.txt"
        staged = self.cfg.staging_dir / "2026-08/BURNIN/unit1_BC001_log.txt"
        self.assertEqual(staged.stat().st_mtime_ns, changed.stat().st_mtime_ns)  # kept, as a rename would
        changed.write_bytes(b"burnin one, more lines")  # written to after it was copied
        result = lb.finish_staged(db, self.cfg)
        self.assertEqual((result["files_moved"], result["errors"]), (2, 1))
        self.assertTrue(changed.is_file())  # kept: the archive holds the older content
        self.assertFalse((self.cfg.source_dir / "ESS/unit2_BC002.txt").exists())
        self.CANDIDATES = [("BC001", "2026-08-03 10:00:00")]
        self.run_backup()  # the next run archives the new content
        self.assertFalse(changed.exists())
        self.assertEqual(self.head("2026-08")[0]["BURNIN/unit1_BC001_log.txt"],
                         lb.blake3.blake3(b"burnin one, more lines").hexdigest())

    def test_copy_staging_skips_a_file_that_changed_before_the_copy(self):
        self.copy_mode()
        db = lb.open_index(self.cfg.db_path)
        self.addCleanup(db.close)
        entries, _, _ = lb.scan(self.cfg, dict(self.CANDIDATES))
        self.write("BURNIN/unit1_BC001_log.txt", b"grew after the scan")
        with self.assertLogs("logs_backup", "WARNING"):
            self.assertEqual(lb.stage(self.cfg, lb.start_session(db, "", "x"), entries), 1)
        self.assertFalse((self.cfg.staging_dir / "2026-08/BURNIN/unit1_BC001_log.txt").exists())
        self.assertEqual(list(self.cfg.staging_dir.rglob("*.partial")), [])

    def test_move_batch_left_by_an_older_run_is_resumed_in_copy_mode(self):
        db = lb.open_index(self.cfg.db_path)
        self.addCleanup(db.close)
        entries, _, _ = lb.scan(self.cfg, dict(self.CANDIDATES))
        lb.stage(self.cfg, lb.start_session(db, "", "x"), entries)  # move mode, in <source>/.staging
        moved_to = self.cfg.staging_dir
        self.copy_mode()
        self.CANDIDATES = []
        self.assertEqual(self.run_backup()["resumed"], 2)
        self.assertEqual(len(self.head("2026-08")[0]), 1)
        self.assertFalse(moved_to.exists())

    def many(self, n, month_of=lambda i: "2026-08"):
        """n more matching files, BC1000.. ; returns their candidates."""
        rows = []
        for i in range(n):
            self.write(f"ESS/line{i % 5}/unit_BC{1000 + i}.txt", f"log {i}".encode())
            rows.append((f"BC{1000 + i}", f"{month_of(i)}-10 08:00:00"))
        return rows

    def settings(self, **extra):
        raw = json.loads((self.tmp / "config.json").read_text())
        (self.tmp / "config.json").write_text(json.dumps(dict(raw, **extra)))
        mochi = self.cfg.mochi
        self.cfg = lb.Config(self.tmp / "config.json")
        self.cfg.mochi = mochi

    def test_batches_archive_everything_one_commit_each(self):
        self.settings(batch_files=100)
        self.CANDIDATES = self.CANDIDATES + self.many(250)
        reports = []
        summary = lb.run(self.cfg, lambda *_: self.CANDIDATES, report=lambda *a: reports.append(a))
        self.assertEqual(summary["files_moved"], 252)
        self.assertEqual(len(self.head("2026-08")[0]), 251)
        self.assertEqual(self.head("2026-08")[1], 2)  # 2026-07 is one file; 251 in 2026-08: 3 batches, 3 commits
        stage = [r for r in reports if r[0] == "stage"]
        self.assertEqual(stage[-1][2:], (252, 252))  # counted over the whole run, not per batch
        self.assertIn("batch 3/3", stage[-1][1])
        self.assertEqual(connect(self, self.cfg.db_path).execute(
            "SELECT status, total_files_moved FROM scan_session").fetchall(), [("success", 252)])

    def test_stop_while_staging_then_continue(self):
        self.settings(batch_files=100)
        self.CANDIDATES = self.CANDIDATES + self.many(250)
        stop = threading.Event()

        def report(step, message="", done=0, total=None):
            if step == "stage" and done >= 150:
                stop.set()  # the Stop button, during the second batch

        summary = lb.run(self.cfg, lambda *_: self.CANDIDATES, report=report, stop=stop)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["files_moved"], 100)  # the first batch was finished
        self.assertTrue(self.cfg.manifest.exists())  # the second batch waits in staging
        db = connect(self, self.cfg.db_path)
        self.assertEqual(db.execute("SELECT status FROM scan_session").fetchall(), [("failed",)])
        summary = self.run_backup()  # continue: resumes the staged part, scans for the rest
        self.assertEqual(summary["status"], "done")
        self.assertEqual(summary["resumed"] + summary["files_moved"] + 100, 252)
        self.assertEqual(len(self.head("2026-08")[0]), 251)
        self.assertFalse(self.cfg.staging_dir.exists())
        left = [p for p in self.cfg.source_dir.rglob("*_BC1*")]
        self.assertEqual(left, [])

    def test_stop_while_resuming_keeps_the_rest_for_later(self):
        db = lb.open_index(self.cfg.db_path)
        self.addCleanup(db.close)
        entries, _, _ = lb.scan(self.cfg, dict(self.CANDIDATES))
        lb.stage(self.cfg, lb.start_session(db, "", "x"), entries)  # an interrupted run's batch: 2 months
        stop = threading.Event()
        stop.set()
        summary = lb.run(self.cfg, lambda *_: self.CANDIDATES, stop=stop)
        self.assertEqual((summary["status"], summary["resumed"]), ("stopped", 0))
        self.assertTrue(self.cfg.manifest.exists())
        self.assertTrue((self.cfg.staging_dir / "2026-08/BURNIN/unit1_BC001_log.txt").is_file())
        self.assertEqual(self.run_backup()["resumed"], 2)
        self.assertFalse(self.cfg.staging_dir.exists())

    def test_stop_while_scanning_moves_nothing(self):
        self.CANDIDATES = self.CANDIDATES + self.many(500)
        stop = threading.Event()

        def report(step, *_):
            if step == "query":
                stop.set()

        summary = lb.run(self.cfg, lambda *_: self.CANDIDATES, report=report, stop=stop)
        self.assertEqual((summary["status"], summary["files_moved"]), ("stopped", 0))
        self.assertTrue((self.cfg.source_dir / "BURNIN/unit1_BC001_log.txt").is_file())
        self.assertFalse(self.cfg.manifest.exists())
        self.assertEqual(connect(self, self.cfg.db_path).execute("SELECT COUNT(*) FROM scan_session").fetchone()[0], 0)

    def test_custom_range_does_not_move_where_continue_starts(self):
        self.settings(date_range=None, lookback_days=2)
        asked = []

        def candidates(date_from, date_to):
            asked.append((date_from, date_to))
            return self.CANDIDATES

        lb.run(self.cfg, candidates)  # continue, first run: everything up to today - 2 days
        to = (lb.dt.datetime.now(lb.dt.timezone.utc).date() - lb.dt.timedelta(days=2)).isoformat()
        self.assertEqual(asked[-1], (None, to))
        lb.run(self.cfg, candidates, window=("2025-01-01", "2025-01-31"))  # back-fill an old month
        self.assertEqual(asked[-1], ("2025-01-01", "2025-01-31"))
        start = (lb.dt.date.fromisoformat(to) - lb.dt.timedelta(days=1)).isoformat()
        self.assertEqual(lb.continue_window(self.cfg), (start, to))  # from the last continuing run
        lb.run(self.cfg, candidates)
        self.assertEqual(asked[-1], (start, to))
        with self.assertRaisesRegex(lb.BackupError, "starts after it ends"):
            lb.run(self.cfg, candidates, window=("2025-02-01", "2025-01-01"))

    def test_callbacks_report_steps_and_cancel_moves_nothing(self):
        steps = []
        summary = lb.run(self.cfg, lambda *_: self.CANDIDATES, report=lambda step, *_: steps.append(step),
                         confirm=lambda volumes: False)
        self.assertEqual(summary["status"], "cancelled")
        self.assertEqual(summary["volumes"], {"2026-07": (1, 1, 7), "2026-08": (1, 1, 10)})  # barcodes, files, bytes
        self.assertEqual((summary["candidates"], summary["matched_barcodes"], summary["matched_files"]), (3, 2, 2))
        self.assertEqual(sorted(set(steps)), ["query", "scan"])
        self.assertTrue((self.cfg.source_dir / "BURNIN/unit1_BC001_log.txt").exists())
        self.assertEqual(connect(self, self.cfg.db_path).execute("SELECT COUNT(*) FROM scan_session").fetchone()[0], 0)

        summary = lb.run(self.cfg, lambda *_: self.CANDIDATES, report=lambda step, *_: steps.append(step),
                         confirm=lambda volumes: True)
        self.assertEqual((summary["files_moved"], summary["barcodes"]), (2, 2))
        self.assertIn("commit", steps)

    def test_second_run_at_the_same_time_is_refused(self):
        with lb.lock_run(self.cfg):
            with self.assertRaisesRegex(lb.BackupError, "another backup run"):
                self.run_backup()
        self.assertTrue((self.cfg.source_dir / "BURNIN/unit1_BC001_log.txt").exists())
        self.assertEqual(self.run_backup()["files_moved"], 2)

    def test_export_copies_checked_and_skips_unchanged(self):
        raw = json.loads(self.cfg.path.read_text())
        raw["sql_server"] = {"server": "h", "database": "d", "user": "u", "password": "SECRET"}
        self.cfg.path.write_text(json.dumps(raw))
        self.run_backup()
        target = self.tmp / "export"
        items = lb.export(lb.Config(self.cfg.path), target)
        self.assertEqual(items["archives"], (2, items["archives"][1], 2))
        manifest = json.loads((target / "export-manifest.json").read_text())["files"]
        for rel in ("archives/2026/2026-08.mochi", "archives/2026/2026-07.mochi", "index.db"):
            self.assertEqual(manifest[rel]["blake3"], lb.b3(target / rel))
        self.assertEqual(lb.b3(target / "archives/2026/2026-08.mochi"), lb.b3(self.cfg.archive("2026-08")))
        self.assertIsNone(json.loads((target / "config.json").read_text())["sql_server"]["password"])
        self.assertTrue((target / "RESTORE.txt").is_file())
        self.assertEqual(lb.export(lb.Config(self.cfg.path), target)["archives"][2], 0)  # nothing copied again
        with self.assertRaisesRegex(lb.BackupError, "must not be inside"):
            lb.export(self.cfg, self.cfg.output_dir / "export")

    def test_mochi_is_found_without_a_config_path(self):
        github, home = self.tmp / "GitHub", self.tmp / "home"
        program = github / "Finisar_Logs_backup"
        program.mkdir(parents=True)
        checkout = github / "MOCHI" / "target" / "release"
        checkout.mkdir(parents=True)
        cfg = lb.Config(self.cfg.path)
        env = {"PATH": str(self.tmp / "empty"), "HOME": str(home), "USERPROFILE": str(home),
               "LOCALAPPDATA": str(home / "AppData"), "ProgramFiles": str(home / "Program Files")}
        # registry_mochi is off too: on a PC with the MOCHI setup installed it would find the real mochi.exe.
        with unittest.mock.patch.object(lb, "__file__", str(program / "logs_backup.py")), \
                unittest.mock.patch.object(lb, "registry_mochi", lambda: []), \
                unittest.mock.patch.dict(os.environ, env):
            for configured in (None, "mochi"):  # "mochi" was the old example config's value
                cfg.mochi = configured
                with self.assertRaisesRegex(lb.BackupError, "(?s)not found. Looked at:.*GitHub.MOCHI.target.release") as e:
                    lb.check_mochi(cfg)
                self.assertNotIn("desktop app", str(e.exception))
            shutil.copy(MOCHI, checkout / lb.MOCHI_EXE)  # a MOCHI checkout beside this program
            cfg.mochi = None
            self.assertIn(str(checkout), lb.check_mochi(cfg))
            self.assertEqual(cfg.mochi, str(checkout / lb.MOCHI_EXE))
            (checkout / lb.MOCHI_EXE).unlink()
            (home / ".cargo" / "bin").mkdir(parents=True)  # cargo install puts it here
            shutil.copy(MOCHI, home / ".cargo" / "bin" / lb.MOCHI_EXE)
            cfg.mochi = None
            self.assertIn(".cargo", lb.check_mochi(cfg))
            (home / ".cargo" / "bin" / lb.MOCHI_EXE).unlink()
            setup_bin = home / "AppData" / "MOCHI" / "bin"  # the MOCHI setup that includes the CLI
            setup_bin.mkdir(parents=True)
            shutil.copy(MOCHI, setup_bin / lb.MOCHI_EXE)
            cfg.mochi = None
            self.assertIn(str(setup_bin), lb.check_mochi(cfg))
            (setup_bin / lb.MOCHI_EXE).unlink()
            desktop = home / "AppData" / "MOCHI"  # an older setup: only the desktop app installed
            (desktop / "mochi-desktop.exe").write_text("")
            cfg.mochi = None
            with self.assertRaisesRegex(lb.BackupError, "desktop app is installed, but this version of its setup does not include"):
                lb.check_mochi(cfg)

    def test_recursive_scan_keeps_folders_and_appends_into_them(self):
        self.write("BURNIN/line2/2026-08/unit3_BC001_log.txt", b"nested")
        self.write("BURNIN/line3/unit1_BC001_log.txt", b"same name, other folder")
        self.write("BURNIN/line3/deep/er/x_BC002.txt", b"deeper")
        self.run_backup()
        files, _ = self.head("2026-08")
        self.assertEqual(sorted(files), ["BURNIN/line2/2026-08/unit3_BC001_log.txt",
                                         "BURNIN/line3/unit1_BC001_log.txt", "BURNIN/unit1_BC001_log.txt"])
        self.assertIn("BURNIN/line3/deep/er/x_BC002.txt", self.head("2026-07")[0])
        rows = connect(self, self.cfg.db_path).execute(
            "SELECT relative_path, source_subfolder FROM scanned_log_entry WHERE relative_path LIKE '%line2%'").fetchall()
        self.assertEqual(rows, [("BURNIN/line2/2026-08/unit3_BC001_log.txt", "BURNIN")])
        self.assertFalse(self.cfg.staging_dir.exists())
        # A later file in an existing folder merges into it: the earlier ones stay in the head.
        self.write("BURNIN/line2/2026-08/unit4_BC001_log.txt", b"later")
        self.run_backup()
        files, seq = self.head("2026-08")
        self.assertEqual(seq, 1)
        self.assertIn("BURNIN/line2/2026-08/unit3_BC001_log.txt", files)
        self.assertIn("BURNIN/line2/2026-08/unit4_BC001_log.txt", files)

    def test_recursive_off_and_staging_inside_a_subfolder(self):
        raw = json.loads(self.cfg.path.read_text())
        raw.update(recursive=False, staging_dir=str(self.cfg.source_dir / "BURNIN" / "staging"))
        self.cfg.path.write_text(json.dumps(raw))
        cfg = lb.Config(self.cfg.path)
        self.write("BURNIN/line2/unit3_BC001_log.txt", b"nested")
        self.write("BURNIN/staging/stray_BC001.txt", b"inside staging")
        entries, _, _ = lb.scan(cfg, dict(self.CANDIDATES))
        self.assertEqual(sorted(e["relative_path"] for e in entries), ["BURNIN/unit1_BC001_log.txt", "ESS/unit2_BC002.txt"])
        raw["recursive"] = True
        self.cfg.path.write_text(json.dumps(raw))
        entries, _, _ = lb.scan(lb.Config(self.cfg.path), dict(self.CANDIDATES))
        paths = sorted(e["relative_path"] for e in entries)
        self.assertIn("BURNIN/line2/unit3_BC001_log.txt", paths)
        self.assertNotIn("BURNIN/staging/stray_BC001.txt", paths)  # the staging folder is never scanned
        raw["recursive"] = "yes"
        self.cfg.path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(lb.BackupError, '"recursive"'):
            lb.Config(self.cfg.path)

    def test_missing_output_staging_and_index_folders_are_created(self):
        raw = json.loads(self.cfg.path.read_text())
        raw.update(output_dir=str(self.tmp / "new" / "out"), db_path=str(self.tmp / "new" / "db" / "index.db"),
                   staging_dir=str(self.tmp / "src" / "new-staging"))
        self.cfg.path.write_text(json.dumps(raw))
        cfg = lb.Config(self.cfg.path)
        self.assertEqual(lb.run(cfg, lambda *_: self.CANDIDATES)["files_moved"], 2)
        self.assertTrue((self.tmp / "new" / "out" / "2026" / "2026-08.mochi").is_file())
        self.assertTrue((self.tmp / "new" / "db" / "index.db").is_file())
        (self.tmp / "blocker").write_text("a file, not a folder")
        raw["output_dir"] = str(self.tmp / "blocker" / "out")
        self.cfg.path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(lb.BackupError, "cannot create output_dir"):
            lb.run(lb.Config(self.cfg.path), lambda *_: self.CANDIDATES)

    def test_missing_mochi_moves_nothing(self):
        self.cfg.mochi = str(self.tmp / "missing-mochi")
        with self.assertRaisesRegex(lb.BackupError, "mochi program was not found"):
            self.run_backup()
        self.assertTrue((self.cfg.source_dir / "BURNIN/unit1_BC001_log.txt").is_file())
        self.assertFalse(self.cfg.staging_dir.exists())
        with self.assertLogs("logs_backup", "WARNING") as logs:  # a dry run only warns
            self.assertEqual(lb.run(self.cfg, lambda *_: self.CANDIDATES, dry_run=True)["matched_files"], 2)
        self.assertIn("a real run would stop here", logs.output[0])

    @unittest.skipIf(os.name == "nt", "uses a shell script as a fake mochi")
    def test_failed_commit_keeps_staging(self):
        fake = self.tmp / "failing-mochi"  # starts, but every commit fails
        fake.write_text('#!/bin/sh\n[ "$1" = --version ] && echo "mochi 0.0" && exit 0\n'
                        'echo \'{"error":{"code":"IO_ERROR","message":"test"}}\'; exit 3\n')
        fake.chmod(0o755)
        self.cfg.mochi = str(fake)
        with self.assertRaisesRegex(lb.BackupError, "IO_ERROR"):
            self.run_backup()
        self.assertTrue((self.cfg.staging_dir / "2026-08/BURNIN/unit1_BC001_log.txt").is_file())
        self.cfg.mochi = MOCHI
        self.CANDIDATES = []
        self.run_backup()
        self.assertEqual(len(self.head("2026-08")[0]), 1)
        self.assertFalse(self.cfg.staging_dir.exists())


if __name__ == "__main__":
    unittest.main()
