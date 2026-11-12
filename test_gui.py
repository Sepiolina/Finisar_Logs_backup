"""Desktop app smoke tests, without a screen (QT_QPA_PLATFORM=offscreen). Need PySide6; the backup
runs also need the `mochi` CLI (on PATH or in $MOCHI_BIN): python -m unittest -v test_gui"""
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication, QDialog
    import gui
except ImportError:  # PySide6 not installed
    gui = None

import logs_backup as lb

MOCHI = os.environ.get("MOCHI_BIN") or shutil.which("mochi")


@unittest.skipUnless(gui, "PySide6 not installed")
class Window(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = unittest.mock.patch.dict(os.environ, {"FINISAR_BACKUP_HOME": str(self.tmp / "app")})
        patcher.start()
        self.addCleanup(patcher.stop)
        for rel in ("BURNIN/unit1_BC001_log.txt", "ESS/unit2_BC002.txt"):
            (self.tmp / "src" / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / "src" / rel).write_text(rel)
        self.candidates = [("BC001", "2026-08-03 10:00:00"), ("BC002", "2026-07-30 09:00:00")]
        query = unittest.mock.patch.object(lb, "query_candidates", lambda sql, a, b: self.candidates)
        query.start()
        self.addCleanup(query.stop)
        gui.use_language("en")  # the assertions below read the English texts
        self.addCleanup(gui.use_language, "th")
        self.handler = gui.WindowLog()
        self.window = gui.MainWindow(self.handler, self.tmp / "backup.log")
        self.addCleanup(self.window.close)

    def wait(self, condition, seconds=20):
        end = time.monotonic() + seconds
        while not condition():
            self.qt.processEvents()
            if time.monotonic() > end:
                self.fail("timed out")
            time.sleep(0.01)

    def fill_settings(self, **extra):
        page = self.window.settings_page
        page.source.setText(str(self.tmp / "src"))
        page.output.setText(str(self.tmp / "out"))
        page.subfolders.setText("BURNIN, ESS")
        page.server.setText("sqlhost")
        page.user.setText("")
        if MOCHI:
            page.mochi.setText(MOCHI)
        for name, value in extra.items():
            getattr(page, name).setValue(value)
        page.dirty = True
        self.assertTrue(page.save())

    def test_first_start_opens_settings_and_saves_them(self):
        self.assertEqual(self.window.stack.currentWidget(), self.window.settings_page)
        self.fill_settings(batch=500)
        saved = lb.load_settings()
        self.assertEqual(saved["db_path"], f"{self.tmp.as_posix()}/out/index.db")  # default next to the archives
        self.assertEqual(saved["subfolders"], ["BURNIN", "ESS"])
        self.assertEqual(saved["batch_files"], 500)
        self.assertNotIn("password_dpapi", saved["sql_server"])
        self.assertEqual(saved["compression"], "balanced")  # the default preset
        page = self.window.settings_page
        page.compression.setCurrentIndex(page.compression.findData("ultra"))
        self.assertTrue(page.save())
        self.assertEqual(lb.load_settings()["compression"], "ultra")
        page.load(lb.load_settings())
        self.assertEqual(page.compression.currentData(), "ultra")
        self.assertEqual(page.compression.count(), len(lb.COMPRESSION_PRESETS))
        cfg = lb.Config(lb.find_config())  # the command line uses the same settings
        self.assertEqual(cfg.source_dir, Path(saved["source_dir"]))
        self.assertIn("→", self.window.backup_page.continue_label.text())

    def test_missing_setting_is_refused(self):
        page = self.window.settings_page
        page.source.setText("")
        with unittest.mock.patch.object(gui, "warn") as warned:
            self.assertFalse(page.save())
        self.assertIn("log folder", warned.call_args[0][2])

    def test_preview_moves_nothing(self):
        self.fill_settings()
        page = self.window.backup_page
        threads, original = set(), gui.BackupPage.on_progress

        def on_progress(self, *args):  # widgets may only be touched on the window's thread
            threads.add(threading.current_thread())
            original(self, *args)

        with unittest.mock.patch.object(gui.BackupPage, "on_progress", on_progress):
            page.a_preview.click()
            self.wait(lambda: page.job is None)
        self.assertEqual(threads, {threading.main_thread()})
        self.assertIn("2 files would be archived", page.banner.text())
        self.assertIn("Free space", page.banner.text())
        self.assertTrue((self.tmp / "src/BURNIN/unit1_BC001_log.txt").exists())

    @unittest.skipUnless(MOCHI, "mochi CLI not found (set MOCHI_BIN)")
    def test_custom_range_backup_then_history(self):
        self.fill_settings()
        page = self.window.backup_page
        with unittest.mock.patch.object(gui.ConfirmDialog, "exec", lambda self: QDialog.Accepted):
            page.b_start.click()
            self.wait(lambda: page.job is None)
        self.assertIn("Done.", page.banner.text())
        self.assertFalse((self.tmp / "src/BURNIN/unit1_BC001_log.txt").exists())
        self.assertEqual(self.window.history_page.table.item(0, 3).text(), "custom")
        self.assertEqual(self.window.history_page.table.item(0, 2).text(), "success")

    @unittest.skipUnless(MOCHI, "mochi CLI not found (set MOCHI_BIN)")
    def test_stop_then_continue(self):
        for i in range(300):
            (self.tmp / "src/ESS" / f"u_BC{5000 + i}.txt").write_text(str(i))
            self.candidates.append((f"BC{5000 + i}", "2026-08-10 08:00:00"))
        self.fill_settings(batch=100)
        page = self.window.backup_page
        original = gui.BackupPage.on_progress

        def stop_during_staging(self, step, message, done, total):  # runs on the window's thread
            original(self, step, message, done, total)
            if step == "stage" and done and done >= 150 and self.job and not self.job.stop.is_set():
                self.request_stop()

        with unittest.mock.patch.object(gui.ConfirmDialog, "exec", lambda self: QDialog.Accepted):
            with unittest.mock.patch.object(gui.BackupPage, "on_progress", stop_during_staging):
                page.a_start.click()
                self.wait(lambda: page.job is None)
            self.assertEqual(len(page.stopped_cards), 1)
            page.refresh()
            page.refresh()  # refreshing again never shows a stopped run twice
            self.assertEqual(len(page.stopped_area.findChildren(gui.QFrame, "stopped")), 1)
            self.assertIn("Stopped</b> with", page.banner.text())
        # As after closing the program and restarting the PC: a new window, the stopped run offered.
        self.window.close()
        self.window = gui.MainWindow(self.handler, self.tmp / "backup.log")
        page = self.window.backup_page
        self.assertFalse(page.stopped_area.isHidden())
        frame, label, go_on, _ = page.stopped_cards[0]
        self.assertIn("files left", label.text())
        with unittest.mock.patch.object(lb, "query_candidates", side_effect=AssertionError("no new query")):
            go_on.click()
            self.wait(lambda: page.job is None)
        self.assertIn("Done.", page.banner.text())
        self.assertEqual(page.stopped_cards, [])
        self.assertTrue(page.stopped_area.isHidden())
        self.assertIn("continue", self.window.history_page.table.item(0, 3).text())
        self.assertEqual(list((self.tmp / "src").rglob("*_BC*")), [])
        files, _ = lb.head_files(lb.Config(lb.find_config()), self.tmp / "out/2026/2026-08.mochi")
        self.assertEqual(len(files), 301)


@unittest.skipUnless(gui, "PySide6 not installed")
class Thai(unittest.TestCase):
    def test_every_text_has_a_thai_translation(self):
        import ast
        import gui_text
        tree = ast.parse(Path(gui.__file__).read_text(encoding="utf-8"))
        keys = {node.args[0].value for node in ast.walk(tree)
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr"
                and node.args and isinstance(node.args[0], ast.Constant)}
        keys |= {name for _, name in gui.STEPS} | set(gui.HistoryPage.COLUMNS) | {"Backup: {mode}", "Preview: {mode}"}
        missing = sorted(k for k in keys if k not in gui_text.TH)
        self.assertEqual(missing, [])
        for key, thai in gui_text.TH.items():  # the same {placeholders} on both sides
            self.assertEqual(sorted(re.findall(r"{(\w+)}", key)), sorted(re.findall(r"{(\w+)}", thai)), key)

    def test_window_in_thai(self):
        qt = QApplication.instance() or QApplication([])
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "src/ESS").mkdir(parents=True)
        (tmp / "src/ESS/unit_BC1.txt").write_text("x")
        with unittest.mock.patch.dict(os.environ, {"FINISAR_BACKUP_HOME": str(tmp / "app")}), \
                unittest.mock.patch.object(lb, "query_candidates", lambda *_: [("BC1", "2026-08-01 00:00:00")]):
            lb.save_settings({"source_dir": str(tmp / "src"), "output_dir": str(tmp / "out"),
                              "db_path": str(tmp / "out/index.db"), "subfolders": ["ESS"], "language": "th"})
            gui.use_language(lb.load_settings()["language"])
            window = gui.MainWindow(gui.WindowLog(), tmp / "backup.log")
            self.addCleanup(window.close)
            self.assertEqual([window.nav.item(i).text() for i in range(4)],
                             ["สำรองข้อมูล", "ประวัติ", "การตั้งค่า", "เครื่องมือ"])
            self.assertEqual(window.settings_page.language.currentData(), "th")
            page = window.backup_page
            page.a_preview.click()
            end = time.monotonic() + 20
            while page.job is not None and time.monotonic() < end:
                qt.processEvents()
                time.sleep(0.01)
            self.assertIn("ตัวอย่าง", page.banner.text())
            self.assertIn("จะเก็บเข้าไฟล์เก็บถาวร 1 ไฟล์", page.banner.text())
            self.assertEqual(gui_text_tr_progress("batch 2/3 copying to local staging"),
                             "ชุดที่ 2/3 กำลังคัดลอกมาพักบนเครื่องนี้")


def gui_text_tr_progress(message):
    import gui_text
    return gui_text.tr_progress(message)


if __name__ == "__main__":
    unittest.main()
