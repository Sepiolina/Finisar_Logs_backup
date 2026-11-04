"""Tests. The flow tests need the `mochi` CLI (on PATH or in $MOCHI_BIN): python -m unittest -v"""
import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import logs_backup as lb

MOCHI = os.environ.get("MOCHI_BIN") or shutil.which("mochi")


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
        with unittest.mock.patch.object(lb, "__file__", str(program / "logs_backup.py")):
            with self.assertRaisesRegex(lb.BackupError, "no config.json found"):
                lb.find_config()
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


@unittest.skipUnless(MOCHI, "mochi CLI not found (set MOCHI_BIN)")
class Flow(unittest.TestCase):
    CANDIDATES = [("BC001", "2026-08-03 10:00:00"), ("BC002", "2026-07-30 09:00:00"), ("BC404", "2026-08-01 00:00:00")]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
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

        db = sqlite3.connect(self.cfg.db_path)
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
        entries, _, _ = lb.scan(self.cfg, dict(self.CANDIDATES))
        lb.stage(self.cfg, lb.start_session(db, "", "x"), entries)
        self.CANDIDATES = []
        self.run_backup()
        self.assertEqual(len(self.head("2026-08")[0]), 1)
        self.assertFalse(self.cfg.staging_dir.exists())

    def test_crash_after_commit_before_index_makes_no_new_commit(self):
        db = lb.open_index(self.cfg.db_path)
        entries, _, _ = lb.scan(self.cfg, dict(self.CANDIDATES))
        lb.stage(self.cfg, lb.start_session(db, "", "x"), entries)
        lb.commit_month(self.cfg, "2026-08", [e for e in entries if e["volume_name"] == "2026-08"])
        self.CANDIDATES = []
        self.run_backup()
        self.assertEqual(self.head("2026-08")[1], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM scanned_log_entry").fetchone()[0], 2)

    def test_callbacks_report_steps_and_cancel_moves_nothing(self):
        steps = []
        summary = lb.run(self.cfg, lambda *_: self.CANDIDATES, report=lambda step, *_: steps.append(step),
                         confirm=lambda volumes: False)
        self.assertEqual(summary["status"], "cancelled")
        self.assertEqual(summary["volumes"], {"2026-07": (1, 1, 7), "2026-08": (1, 1, 10)})  # barcodes, files, bytes
        self.assertEqual((summary["candidates"], summary["matched_barcodes"], summary["matched_files"]), (3, 2, 2))
        self.assertEqual(sorted(set(steps)), ["query", "scan"])
        self.assertTrue((self.cfg.source_dir / "BURNIN/unit1_BC001_log.txt").exists())
        self.assertEqual(sqlite3.connect(self.cfg.db_path).execute("SELECT COUNT(*) FROM scan_session").fetchone()[0], 0)

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
        with unittest.mock.patch.object(lb, "__file__", str(program / "logs_backup.py")), \
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
        rows = sqlite3.connect(self.cfg.db_path).execute(
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
