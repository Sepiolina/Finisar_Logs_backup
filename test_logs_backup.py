"""Tests. The flow tests need the `mochi` CLI (on PATH or in $MOCHI_BIN): python -m unittest -v"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
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

    def test_failed_commit_keeps_staging(self):
        self.cfg.mochi = str(self.tmp / "missing-mochi")
        with self.assertRaises(Exception):
            self.run_backup()
        self.assertTrue((self.cfg.staging_dir / "2026-08/BURNIN/unit1_BC001_log.txt").is_file())
        self.cfg.mochi = MOCHI
        self.CANDIDATES = []
        self.run_backup()
        self.assertEqual(len(self.head("2026-08")[0]), 1)
        self.assertFalse(self.cfg.staging_dir.exists())


if __name__ == "__main__":
    unittest.main()
