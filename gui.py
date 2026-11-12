"""Finisar log backup: desktop app (PySide6).

    python gui.py            (or FinisarLogsBackup.exe, built by build_exe.bat)

Everything is set up in the window (Settings page) and saved per Windows user in
%APPDATA%\\FinisarLogsBackup\\settings.json; no config file is needed. The SQL Server
password can be remembered, encrypted for the current Windows user (DPAPI).

Two ways to run, each with its own card on the Backup page:
  A. Continue: from where the last successful continuing run ended, up to today - lookback days.
  B. Custom date range: any range; it never changes where "Continue" starts.

A run can be stopped at any time with Stop: it ends at the next safe point (never in the
middle of a MOCHI commit), and the next run continues where it stopped.

All the backup logic is in logs_backup.py; this file only shows it.
"""
import datetime as dt
import logging
import os
import sqlite3
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QDate, QElapsedTimer, QLocale, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

import gui_text
import logs_backup as lb
from gui_text import tr, tr_error, tr_progress

log = logging.getLogger("logs_backup")

STEPS = [("resume", "Finish the previous run"), ("query", "Ask SQL Server for barcodes"),
         ("scan", "Scan the log folders"), ("stage", "Stage files"), ("commit", "Commit, verify, index")]
# The English texts are keys for gui_text.tr(): Thai (default) or English, per Settings → Language.

STYLE = """
* { font-size: 10pt; }
QMainWindow, QWidget#page { background: #f4f5f7; }
QListWidget#nav { background: #1e293b; border: none; padding-top: 12px; outline: 0; }
QListWidget#nav::item { color: #cbd5e1; padding: 11px 18px; border-left: 3px solid transparent; }
QListWidget#nav::item:selected { background: #334155; color: white; border-left: 3px solid #60a5fa; }
QListWidget#nav::item:hover:!selected { background: #273449; }
QLabel#brand { color: white; font-size: 13pt; font-weight: 600; padding: 18px 18px 4px 18px; background: #1e293b; }
QLabel#brandSub { color: #94a3b8; padding: 0 18px 8px 18px; background: #1e293b; }
QLabel#title { font-size: 17pt; font-weight: 600; color: #0f172a; }
QLabel#subtitle { color: #64748b; }
QFrame#card { background: white; border: 1px solid #e2e8f0; border-radius: 10px; }
QLabel#cardTitle { font-size: 12pt; font-weight: 600; color: #0f172a; }
QLabel#muted { color: #64748b; }
QLabel#window { font-size: 12pt; color: #1d4ed8; font-weight: 600; }
QLabel#badge { background: #e0e7ff; color: #3730a3; border-radius: 9px; padding: 2px 9px; font-size: 9pt; }
QPushButton { background: white; border: 1px solid #cbd5e1; border-radius: 6px; padding: 7px 14px; color: #0f172a; }
QPushButton:hover { background: #f1f5f9; }
QPushButton:disabled { color: #94a3b8; background: #f8fafc; }
QPushButton#primary { background: #2563eb; border-color: #2563eb; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #1d4ed8; }
QPushButton#primary:disabled { background: #93c5fd; border-color: #93c5fd; }
QPushButton#danger { background: white; border-color: #dc2626; color: #dc2626; font-weight: 600; }
QPushButton#danger:hover { background: #fef2f2; }
QPushButton#danger:disabled { border-color: #fecaca; color: #fca5a5; }
QLineEdit, QSpinBox, QComboBox, QDateEdit { background: white; border: 1px solid #cbd5e1; border-radius: 6px;
    padding: 5px 8px; min-height: 18px; }
QSpinBox::up-button, QSpinBox::down-button { width: 18px; border: none; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QDateEdit:focus { border-color: #2563eb; }
QProgressBar { background: #e2e8f0; border: none; border-radius: 4px; height: 8px; text-align: center; }
QProgressBar::chunk { background: #2563eb; border-radius: 4px; }
QPlainTextEdit { background: #0f172a; color: #e2e8f0; border-radius: 8px; padding: 6px;
    font-family: Consolas, "Cascadia Mono", monospace; font-size: 9pt; }
QTableWidget { background: white; border: 1px solid #e2e8f0; border-radius: 8px; gridline-color: #f1f5f9; }
QHeaderView::section { background: #f8fafc; border: none; border-bottom: 1px solid #e2e8f0; padding: 6px;
    font-weight: 600; color: #334155; }
QScrollArea { border: none; background: transparent; }
QLabel#modeBadge { background: #2563eb; color: white; font-weight: 700; font-size: 12pt; border-radius: 15px;
    min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; qproperty-alignment: AlignCenter; }
QLabel#modeBadgeB { background: #7c3aed; color: white; font-weight: 700; font-size: 12pt; border-radius: 15px;
    min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; qproperty-alignment: AlignCenter; }
QLabel#fieldHint { color: #64748b; font-size: 9pt; }
QLabel#empty { color: #64748b; padding: 18px; background: #f8fafc; border: 1px dashed #cbd5e1; border-radius: 8px; }
QFrame#stat { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; }
QLabel#statValue { font-size: 16pt; font-weight: 700; color: #0f172a; }
QLabel#statLabel { color: #64748b; font-size: 9pt; }
QLabel#resultTitle { font-size: 13pt; font-weight: 700; }
QFrame#stopped { background: #fffbeb; border: 1px solid #fcd34d; border-left: 5px solid #f59e0b; border-radius: 10px; }
QProgressBar#amber::chunk { background: #f59e0b; border-radius: 4px; }
QPushButton#link { background: transparent; border: none; color: #2563eb; padding: 4px 0; text-align: left; }
QPushButton#link:hover { color: #1d4ed8; text-decoration: underline; }
QFrame#checkRow { background: #f8fafc; border-radius: 6px; }
QTableWidget { alternate-background-color: #f8fafc; }
"""

CHECK_LOOK = {"ok": ("✓", "#16a34a"), "fail": ("✕", "#dc2626"), "warn": ("!", "#d97706"), "info": ("•", "#64748b")}

# Step status icons: (symbol, text color, background, border)
STATUS_LOOK = {"waiting": ("", "#94a3b8", "white", "#cbd5e1"), "running": ("●", "white", "#2563eb", "#2563eb"),
               "done": ("✓", "white", "#16a34a", "#16a34a"), "failed": ("✕", "white", "#dc2626", "#dc2626"),
               "stopped": ("■", "white", "#64748b", "#64748b")}


def when_text(stamp):
    """"epoch:1730000000" (as the index stores times) as local date and time."""
    if str(stamp or "").startswith("epoch:"):
        return dt.datetime.fromtimestamp(int(stamp[6:])).strftime("%Y-%m-%d %H:%M")
    return str(stamp or "")


def space_html(rows):
    """Free-space rows (lb.space_needed) as lines: ✓ enough, ⚠ maybe too little, ✗ too little."""
    lines = []
    for r in rows:
        mark, color = (("✓", "#16a34a") if r["ok"] else ("?", "#64748b") if r["ok"] is None
                       else ("✗", "#dc2626") if r["required"] else ("⚠", "#d97706"))
        free = tr("unknown") if r["free"] is None else size(r["free"])
        lines.append(f'<span style="color:{color}">{mark}</span> '
                     + tr("{what}: <b>{free}</b> free, needs {need}", what=tr(r["what"]), free=free,
                          need=size(r["need"]))
                     + f' <span style="color:#64748b">({r["path"]})</span>')
    return "<br>".join(lines)


def size(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def clock(ms):
    s = int(ms // 1000)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def card(title=None, subtitle=None):
    frame = QFrame(objectName="card")
    box = QVBoxLayout(frame)
    box.setContentsMargins(18, 16, 18, 16)
    box.setSpacing(10)
    if title:
        box.addWidget(QLabel(title, objectName="cardTitle"))
    if subtitle:
        label = QLabel(subtitle, objectName="muted")
        label.setWordWrap(True)
        box.addWidget(label)
    return frame, box


def page(title, subtitle):
    widget = QWidget(objectName="page")
    outer = QVBoxLayout(widget)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    inner = QWidget(objectName="page")
    box = QVBoxLayout(inner)
    box.setContentsMargins(28, 24, 28, 24)
    box.setSpacing(16)
    box.addWidget(QLabel(title, objectName="title"))
    if subtitle:
        label = QLabel(subtitle, objectName="subtitle")
        label.setWordWrap(True)
        box.addWidget(label)
    scroll.setWidget(inner)
    outer.addWidget(scroll)
    return widget, box


def ask(parent, title, text, yes="Yes", no="No"):
    """A yes/no question with the buttons in the app's language. True for yes."""
    box = QMessageBox(QMessageBox.Question, title, text, parent=parent)
    go = box.addButton(tr(yes), QMessageBox.YesRole)
    box.addButton(tr(no), QMessageBox.NoRole)
    box.exec()
    return box.clickedButton() == go


def warn(parent, title, text, info=False):
    box = QMessageBox(QMessageBox.Information if info else QMessageBox.Warning, title, text, parent=parent)
    box.addButton(tr("OK"), QMessageBox.AcceptRole)
    box.exec()


def button(text, kind=None):
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    b.setCursor(Qt.PointingHandCursor)
    return b


# ---------------------------------------------------------------- logging into the window

class LogBridge(QObject):
    line = Signal(str, int)


class WindowLog(logging.Handler):
    """Sends log records to the window (from any thread) through a Qt signal."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.bridge = LogBridge()
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.bridge.line.emit(self.format(record), record.levelno)
        except RuntimeError:  # the window is gone
            pass


# ---------------------------------------------------------------- background work

class Job(QObject):
    """Runs fn(job) on a background thread. Progress, confirm requests and the result come back as
    signals. The Job itself stays on the window's thread, so Qt queues every signal emitted by the
    worker to the window's thread: widgets are only ever touched there."""
    progress = Signal(str, str, object, object)
    confirm_needed = Signal(object)
    done = Signal(object)
    failed = Signal(str)
    running = []  # keeps each job alive until its thread has ended

    def __init__(self, fn):
        super().__init__()
        self.fn, self.stop = fn, threading.Event()
        self._answer, self._answered = False, threading.Event()

    def start(self):
        Job.running = [j for j in Job.running if j.worker.is_alive()] + [self]
        self.worker = threading.Thread(target=self._run, name="backup-job", daemon=True)
        self.worker.start()

    def _run(self):
        try:
            result = self.fn(self)
        except Exception as e:  # shown in the window; the traceback goes to the log file
            log.exception("failed")
            self.failed.emit(str(e))
        else:
            self.done.emit(result)

    def report(self, step, message="", done=0, total=None):
        self.progress.emit(step, message, done, total)

    def confirm(self, volumes, space=()):
        """Called on the worker thread: waits for the user's answer in the window."""
        self._answered.clear()
        self.confirm_needed.emit((volumes, space))
        self._answered.wait()
        return self._answer

    def answer(self, yes):
        self._answer = yes
        self._answered.set()


# ---------------------------------------------------------------- dialogs

class ConfirmDialog(QDialog):
    def __init__(self, volumes, space=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Archive these files?"))
        self.setMinimumWidth(520)
        box = QVBoxLayout(self)
        barcodes = sum(v[0] for v in volumes.values())
        files = sum(v[1] for v in volumes.values())
        nbytes = sum(v[2] for v in volumes.values())
        head = QLabel(tr("<b>{files} files</b> ({barcodes} barcodes, {size}) will be archived into {months} "
                         "monthly archive(s), then removed from the log folders.", files=f"{files:,}",
                         barcodes=f"{barcodes:,}", size=size(nbytes), months=len(volumes)))
        head.setWordWrap(True)
        box.addWidget(head)
        table = QTableWidget(len(volumes), 4)
        table.setHorizontalHeaderLabels([tr("Month"), tr("Barcodes"), tr("Files"), tr("Size")])
        table.verticalHeader().hide()
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for row, (month, (bc, n, b)) in enumerate(volumes.items()):
            for col, text in enumerate((month, f"{bc:,}", f"{n:,}", size(b))):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter if col else Qt.AlignLeft | Qt.AlignVCenter)
                table.setItem(row, col, item)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setMaximumHeight(min(320, 40 + 30 * len(volumes)))
        box.addWidget(table)
        if space:
            title = QLabel(f"<b>{tr('Free space')}</b>")
            box.addWidget(title)
            room = QLabel(space_html(space))
            room.setWordWrap(True)
            room.setTextFormat(Qt.RichText)
            box.addWidget(room)
        buttons = QDialogButtonBox()
        go = buttons.addButton(tr("Archive"), QDialogButtonBox.AcceptRole)
        go.setObjectName("primary")
        buttons.addButton(tr("Cancel"), QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)


# ---------------------------------------------------------------- Backup page

class StepRow:
    """One step of a run: a round status icon, the name, a bar, counts, time and a detail line."""

    def __init__(self, grid, row, name):
        self.status = QLabel()
        self.status.setFixedSize(22, 22)
        self.status.setAlignment(Qt.AlignCenter)
        self.name = QLabel(tr(name))
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.count = QLabel("")
        self.count.setObjectName("muted")
        self.count.setMinimumWidth(130)
        self.count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.time = QLabel("")
        self.time.setObjectName("muted")
        self.time.setMinimumWidth(56)
        self.time.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.detail = QLabel("")
        self.detail.setObjectName("fieldHint")
        for col, w in enumerate((self.status, self.name, self.bar, self.count, self.time)):
            grid.addWidget(w, row * 2, col)
        grid.addWidget(self.detail, row * 2 + 1, 1, 1, 4)
        grid.setRowMinimumHeight(row * 2 + 1, 18)
        self.timer = QElapsedTimer()
        self.active = False
        self.reset()

    def look(self, state):
        mark, color, background, border = STATUS_LOOK[state]
        self.status.setText(mark)
        self.status.setStyleSheet(f"color: {color}; background: {background}; border: 2px solid {border};"
                                  " border-radius: 11px; font-weight: 700;")
        self.name.setStyleSheet("font-weight: 700; color: #0f172a;" if state == "running"
                                else "color: #94a3b8;" if state == "waiting" else "color: #334155;")

    def reset(self):
        self.look("waiting")
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.count.setText("")
        self.time.setText("")
        self.detail.setText("")
        self.active = False
        self.timer.invalidate()

    def update(self, message, done, total):
        if not self.timer.isValid():
            self.timer.start()
        self.active = True
        self.look("running")
        if total:
            self.bar.setRange(0, max(int(total), 1))
            self.bar.setValue(min(int(done or 0), int(total)))
            self.count.setText(f"{int(done or 0):,} / {int(total):,}")
        else:
            self.bar.setRange(0, 0)  # busy
            self.count.setText(f"{int(done):,}" if done else "")
        self.detail.setText(tr_progress(message or ""))
        if total and done and done >= total:
            self.finish("done")

    def tick(self):
        if self.active and self.timer.isValid():
            self.time.setText(clock(self.timer.elapsed()))

    def finish(self, state):
        """state: "done", "failed" or "stopped"."""
        if self.timer.isValid():
            self.time.setText(clock(self.timer.elapsed()))
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 1)
            self.bar.setValue(1 if state == "done" else 0)
        self.look(state)
        self.active = False


def form_layout():
    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignRight | Qt.AlignTop)
    form.setVerticalSpacing(12)
    form.setHorizontalSpacing(16)
    return form


def required(label):
    """A form label marked as required."""
    return f"{label} <span style='color:#dc2626'>*</span>"


def with_hint(field, hint):
    """A form field with one line of help under it."""
    holder = QWidget()
    box = QVBoxLayout(holder)
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(3)
    box.addWidget(field)
    note = QLabel(hint, objectName="fieldHint")
    note.setWordWrap(True)
    box.addWidget(note)
    return holder


def stat_tile(value, label, color="#0f172a"):
    """A number with a small caption, for a run's result."""
    frame = QFrame(objectName="stat")
    box = QVBoxLayout(frame)
    box.setContentsMargins(14, 10, 14, 10)
    box.setSpacing(2)
    number = QLabel(value, objectName="statValue")
    number.setStyleSheet(f"color: {color};")
    box.addWidget(number)
    box.addWidget(QLabel(label, objectName="statLabel"))
    return frame


def mode_header(letter, title, subtitle):
    """A card header with a round mode badge (A or B), a title and one line of explanation."""
    frame = QFrame(objectName="card")
    box = QVBoxLayout(frame)
    box.setContentsMargins(18, 16, 18, 16)
    box.setSpacing(10)
    head = QHBoxLayout()
    head.setSpacing(10)
    head.addWidget(QLabel(letter, objectName="modeBadge" if letter == "A" else "modeBadgeB"), 0, Qt.AlignTop)
    titles = QVBoxLayout()
    titles.setSpacing(0)
    titles.addWidget(QLabel(title, objectName="cardTitle"))
    hint = QLabel(subtitle, objectName="muted")
    hint.setWordWrap(True)
    titles.addWidget(hint)
    head.addLayout(titles, 1)
    box.addLayout(head)
    return frame, box


class BackupPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app, self.job = app, None
        widget, box = page(tr("Backup"), tr("Archive test-station logs into monthly MOCHI archives."))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)

        # One card per stopped run (a continue run, custom-range runs): each can continue on its own.
        self.stopped_area = QWidget()
        self.stopped_layout = QVBoxLayout(self.stopped_area)
        self.stopped_layout.setContentsMargins(0, 0, 0, 0)
        self.stopped_layout.setSpacing(12)
        self.stopped, self.stopped_cards = [], []
        box.addWidget(self.stopped_area)

        modes = QHBoxLayout()
        modes.setSpacing(16)
        a, abox = mode_header("A", tr("Continue from the last run"),
                              tr("The regular backup: everything since the last successful run."))
        dates_a = QHBoxLayout()
        dates_a.addWidget(QLabel(tr("Dates"), objectName="muted"))
        self.continue_label = QLabel("…", objectName="window")
        dates_a.addWidget(self.continue_label, 1)
        abox.addLayout(dates_a)
        abox.addStretch(1)
        arow = QHBoxLayout()
        self.a_preview, self.a_start = button(tr("Preview")), button(tr("Start backup"), "primary")
        arow.addWidget(self.a_preview)
        arow.addStretch(1)
        arow.addWidget(self.a_start)
        abox.addLayout(arow)

        b, bbox = mode_header("B", tr("Custom date range"),
                              tr("Back-fill or redo any dates. Does not change where A starts."))
        dates = QHBoxLayout()
        today = QDate.currentDate()
        self.date_from, self.date_to = QDateEdit(today.addDays(-30)), QDateEdit(today.addDays(-2))
        for d in (self.date_from, self.date_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        dates.addWidget(QLabel(tr("From")))
        dates.addWidget(self.date_from, 1)
        dates.addWidget(QLabel(tr("to")))
        dates.addWidget(self.date_to, 1)
        bbox.addLayout(dates)
        bbox.addStretch(1)
        brow = QHBoxLayout()
        self.b_preview, self.b_start = button(tr("Preview")), button(tr("Start backup"), "primary")
        brow.addWidget(self.b_preview)
        brow.addStretch(1)
        brow.addWidget(self.b_start)
        bbox.addLayout(brow)
        modes.addWidget(a, 1)
        modes.addWidget(b, 1)
        box.addLayout(modes)

        progress, pbox = card()
        head = QHBoxLayout()
        self.run_title = QLabel(tr("Progress"), objectName="cardTitle")
        self.a_preview.setToolTip(tr("Shows what would be archived and checks free space. Moves nothing."))
        self.b_preview.setToolTip(tr("Shows what would be archived and checks free space. Moves nothing."))
        head.addWidget(self.run_title)
        head.addStretch(1)
        self.elapsed = QLabel("", objectName="muted")
        head.addWidget(self.elapsed)
        self.stop_button = button(tr("Stop"), "danger")
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip(tr("Stops at the next safe point. Nothing is lost: the next run continues."))
        head.addWidget(self.stop_button)
        pbox.addLayout(head)
        self.empty = QLabel(tr("Nothing running. Choose A or B above: Preview first to see what would be "
                               "archived, then Start backup."), objectName="empty")
        self.empty.setWordWrap(True)
        pbox.addWidget(self.empty)
        self.steps = QWidget()
        grid = QGridLayout(self.steps)
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(2, 1)
        self.rows = {key: StepRow(grid, i, name) for i, (key, name) in enumerate(STEPS)}
        self.steps.hide()
        pbox.addWidget(self.steps)
        # The result: a title, number tiles, then one message (self.banner).
        self.result = QFrame(objectName="result")
        rbox = QVBoxLayout(self.result)
        rbox.setContentsMargins(16, 14, 16, 14)
        rbox.setSpacing(10)
        self.result_title = QLabel("", objectName="resultTitle")
        rbox.addWidget(self.result_title)
        self.tiles = QHBoxLayout()
        self.tiles.setSpacing(10)
        rbox.addLayout(self.tiles)
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.RichText)
        rbox.addWidget(self.banner)
        self.result.hide()
        pbox.addWidget(self.result)
        box.addWidget(progress)

        # The technical log: folded away, it is for support. Opens by itself when a run fails.
        self.log_toggle = button(tr("Show technical log ▸"), "link")
        self.log_toggle.clicked.connect(lambda: self.show_log(self.log_view.isHidden()))
        box.addWidget(self.log_toggle)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setMinimumHeight(200)
        self.log_view.hide()
        box.addWidget(self.log_view, 1)
        box.addStretch(1)  # spare height goes below, not into the cards

        self.a_start.clicked.connect(lambda: self.start(dry_run=False, custom=False))
        self.a_preview.clicked.connect(lambda: self.start(dry_run=True, custom=False))
        self.b_start.clicked.connect(lambda: self.start(dry_run=False, custom=True))
        self.b_preview.clicked.connect(lambda: self.start(dry_run=True, custom=True))
        self.stop_button.clicked.connect(self.request_stop)
        self.clock = QTimer(self, interval=1000)
        self.clock.timeout.connect(self.tick)
        self.run_timer = QElapsedTimer()
        self.elapsed_text = "0:00:00"

    # -- display

    def add_log(self, text, level):
        color = "#fca5a5" if level >= logging.ERROR else "#fcd34d" if level >= logging.WARNING else "#e2e8f0"
        self.log_view.appendHtml(f'<span style="color:{color}">{text.replace("&", "&amp;").replace("<", "&lt;")}</span>')

    def refresh(self):
        """Shows the window a Continue run would use now, and every stopped run that can continue."""
        try:
            cfg = self.app.config(ask_password=False)
            date_from, date_to = lb.continue_window(cfg)
            stopped = lb.stopped_runs(cfg)
        except Exception as e:
            self.continue_label.setText(tr("Fill in Settings first") if isinstance(e, KeyError) else f"({e})")
            stopped = []
        else:
            self.continue_label.setText(f"{date_from or tr('the beginning')}  →  {date_to}")
        self.show_stopped(stopped)

    def show_stopped(self, stopped):
        for frame, *_ in self.stopped_cards:  # gone at once (deleteLater alone waits for the event loop)
            self.stopped_layout.removeWidget(frame)
            frame.setParent(None)
            frame.deleteLater()
        self.stopped, self.stopped_cards = stopped, []
        for s in reversed(stopped):  # newest first
            frame = QFrame(objectName="stopped")
            sbox = QHBoxLayout(frame)
            sbox.setContentsMargins(16, 12, 16, 12)
            sbox.setSpacing(14)
            sbox.addWidget(QLabel("B" if s["custom"] else "A", objectName="modeBadgeB" if s["custom"] else "modeBadge"))
            text = QVBoxLayout()
            text.setSpacing(4)
            text.addWidget(QLabel(f"<b>{tr('Stopped run')}</b> · {s['window'][0] or tr('(start)')} → {s['window'][1]}"))
            label = QLabel(tr("{left} of {total} files left · stopped {when}", left=f"{s['left']:,}",
                              total=f"{s['total']:,}", when=when_text(s["stopped_at"])), objectName="muted")
            label.setToolTip(tr("Continue picks up exactly there, without asking SQL Server or scanning the log "
                                "folders again. Files staged before the stop are checked and finished first."))
            text.addWidget(label)
            bar = QProgressBar(objectName="amber")
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            bar.setRange(0, max(s["total"], 1))
            bar.setValue(s["total"] - s["left"])
            text.addWidget(bar)
            sbox.addLayout(text, 1)
            row = QHBoxLayout()
            discard, go_on = button(tr("Discard")), button(tr("Continue"), "primary")
            discard.setToolTip(tr("Forget this run's plan. Its files stay in the log folders; a later run finds them."))
            discard.clicked.connect(lambda _=False, sid=s["sid"]: self.discard(sid))
            go_on.clicked.connect(lambda _=False, sid=s["sid"]: self.start(False, False, continue_stopped=sid))
            row.addWidget(discard)
            row.addWidget(go_on)
            sbox.addLayout(row)
            go_on.setEnabled(not self.app.busy)
            discard.setEnabled(not self.app.busy)
            self.stopped_layout.addWidget(frame)
            self.stopped_cards.append((frame, label, go_on, discard))
        self.stopped_area.setVisible(bool(stopped))

    def discard(self, sid):
        if not ask(self, tr("Discard the stopped run?"),
                   tr("Forget the plan of this stopped run? Nothing is deleted: its files stay in the log folders "
                      "and a later run finds them (after a new scan).")):
            return
        try:
            lb.discard_stopped_run(self.app.config(ask_password=False), sid)
        except Exception as e:
            warn(self, tr("Cannot discard"), tr_error(str(e)))
        self.refresh()
        self.app.history_page.refresh()

    def tick(self):
        for row in self.rows.values():
            row.tick()
        if self.run_timer.isValid():
            self.elapsed_text = clock(self.run_timer.elapsed())
            self.elapsed.setText(tr("Elapsed {time}", time=self.elapsed_text))

    def set_running(self, running):
        buttons = [self.a_start, self.a_preview, self.b_start, self.b_preview]
        for b in buttons + [w for _, _, go_on, discard in self.stopped_cards for w in (go_on, discard)]:
            b.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.stop_button.setText(tr("Stop"))
        self.app.set_busy(running)

    def show_banner(self, text, color, background, title="", tiles=()):
        """The result: a title, number tiles (value, caption, color), then the message."""
        self.result_title.setText(title)
        self.result_title.setStyleSheet(f"color: {color};")
        self.result_title.setVisible(bool(title))
        while self.tiles.count():
            item = self.tiles.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        for value, caption, tone in tiles:
            self.tiles.addWidget(stat_tile(value, caption, tone))
        self.tiles.addStretch(1)
        self.banner.setText(text)
        self.banner.setStyleSheet(f"color: {color};")
        self.result.setStyleSheet(f"QFrame#result {{ background: {background}; border-radius: 10px; }}"
                                  " QFrame#stat { background: rgba(255,255,255,0.8); border: none; }"
                                  " QLabel { background: transparent; }")
        self.result.show()

    def show_log(self, visible):
        self.log_view.setVisible(visible)
        self.log_toggle.setText(tr("Hide technical log ▾") if visible else tr("Show technical log ▸"))

    # -- running

    def start(self, dry_run, custom, continue_stopped=False):
        if self.app.settings_page.dirty and not self.app.settings_page.ask_save():
            return
        # A new A run covers the dates of a stopped A run and replaces it: offer to continue that
        # one instead (faster: no new scan). Stopped B runs are never replaced, so no question there.
        stopped_a = [s for s in self.stopped if not s["custom"]]
        if stopped_a and not dry_run and not continue_stopped and not custom:
            s = stopped_a[-1]
            box = QMessageBox(QMessageBox.Question, tr("A stopped run can continue"),
                              tr("The stopped continue run still has {left} files to archive.\n\nContinue it (no "
                                 "new scan), or start a new continue run? The new run covers its dates and replaces "
                                 "it (its files are found again by the new scan). Stopped custom-range runs are kept "
                                 "either way.", left=f"{s['left']:,}"), parent=self)
            go_on = box.addButton(tr("Continue stopped run"), QMessageBox.AcceptRole)
            new = box.addButton(tr("Start new run"), QMessageBox.DestructiveRole)
            box.addButton(tr("Cancel"), QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() == go_on:
                continue_stopped = s["sid"]
            elif box.clickedButton() != new:
                return
        try:
            cfg = self.app.config()
        except KeyError as e:
            warn(self, tr("Settings needed"), tr("Set {name} on the Settings page first.", name=e))
            self.app.go("settings")
            return
        except Exception as e:
            warn(self, tr("Settings problem"), tr_error(str(e)))
            return
        if cfg is None:  # password dialog cancelled
            return
        window = None
        if custom:
            window = (self.date_from.date().toString("yyyy-MM-dd"), self.date_to.date().toString("yyyy-MM-dd"))
            try:
                lb.check_window(window)
            except lb.BackupError as e:
                warn(self, tr("Date range"), tr_error(str(e)))
                return
        for row in self.rows.values():
            row.reset()
        self.result.hide()
        self.empty.hide()
        self.steps.show()
        what = "Preview" if dry_run else "Backup"
        kept = next((s for s in self.stopped if s["sid"] == continue_stopped), None)
        mode = (tr("continue the stopped run {start} → {end}", start=kept["window"][0] or tr("(start)"),
                   end=kept["window"][1]) if kept else f"{window[0]} → {window[1]}" if window else tr("continue"))
        self.run_title.setText(tr(what + ": {mode}", mode=mode))
        log.info("---- %s started (%s)", what.lower(),
                 f"stopped run {kept['sid']}" if kept else f"{window[0]} .. {window[1]}" if window else "continue")
        self.job = Job(lambda job: lb.run(cfg, dry_run=dry_run, report=job.report, confirm=job.confirm,
                                          window=window, stop=job.stop, continue_stopped=continue_stopped))
        self.job.progress.connect(self.on_progress)
        self.job.confirm_needed.connect(self.on_confirm)
        self.job.done.connect(self.on_done)
        self.job.failed.connect(self.on_failed)
        self.set_running(True)
        self.run_timer.start()
        self.clock.start()
        self.job.start()

    def request_stop(self):
        if self.job:
            self.job.stop.set()
            self.stop_button.setEnabled(False)
            self.stop_button.setText(tr("Stopping…"))
            log.info("stop requested: stopping at the next safe point (a commit in progress is finished first)")

    def on_progress(self, step, message, done, total):
        if step not in self.rows:
            return
        for key, row in self.rows.items():  # an earlier step still shown as running is over
            if key != step and row.active and row.bar.maximum() == 0:
                row.finish("done")
        self.rows[step].update(message, done, total)

    def on_confirm(self, request):
        volumes, space = request
        self.job.answer(ConfirmDialog(volumes, space, self).exec() == QDialog.Accepted)

    def end(self):
        self.clock.stop()
        self.tick()
        self.run_timer.invalidate()
        for row in self.rows.values():
            if row.active:
                row.finish("stopped")
        self.set_running(False)
        self.job = None
        self.refresh()
        self.app.history_page.refresh()

    def on_done(self, summary):
        status = summary["status"]
        self.end()  # its last tick sets elapsed_text
        took = self.elapsed_text
        errors = summary["errors"]
        tiles = [(f"{summary['files_moved']:,}", tr("files archived"), "#0f172a"),
                 (f"{summary['barcodes']:,}", tr("barcodes"), "#0f172a"),
                 (f"{errors:,}", tr("errors"), "#dc2626" if errors else "#0f172a")]
        if summary.get("resumed"):
            tiles.append((f"{summary['resumed']:,}", tr("from the previous run"), "#0f172a"))
        tiles.append((took, tr("time"), "#0f172a"))
        counts = tr("{files} files committed ({barcodes} barcodes) · {errors} errors",
                    files=f"{summary['files_moved']:,}", barcodes=f"{summary['barcodes']:,}", errors=f"{errors:,}")
        if status == "done":
            self.show_banner(tr("<b>Done.</b> {counts}. Committed is not a backup on its own: keep an export on "
                                "another drive (Tools).", counts=counts), "#14532d", "#dcfce7",
                             "✓ " + tr("Backup finished"), tiles)
        elif status == "stopped":
            tiles.insert(0, (f"{summary.get('left', 0):,}", tr("files left"), "#b45309"))
            self.show_banner(tr("<b>Stopped</b> with {left} files left. {counts}. Nothing is lost: you can close the "
                                "program or shut down the PC, and run other backups meanwhile. Continue it any time "
                                "with its <b>Continue</b> button above.", left=f"{summary.get('left', 0):,}",
                                counts=counts), "#78350f", "#fef3c7", "■ " + tr("Stopped safely"), tiles)
        elif status == "cancelled":
            self.show_banner(tr("Cancelled. Nothing was moved or recorded."), "#334155", "#e2e8f0",
                             tr("Cancelled"))
        else:  # dry run
            vols = summary["volumes"]
            files = sum(v[1] for v in vols.values())
            months = ", ".join(f"{m}: {v[1]:,}" for m, v in vols.items()) or tr("none")
            tiles = [(f"{files:,}", tr("files to archive"), "#1d4ed8"),
                     (f"{summary['matched_barcodes']:,}", tr("barcodes"), "#0f172a"),
                     (f"{summary['scanned']:,}", tr("files scanned"), "#0f172a"),
                     (str(len(vols)), tr("monthly archives"), "#0f172a")]
            self.show_banner(tr("<b>Preview.</b> {candidates} candidate barcodes, {scanned} files scanned, <b>{files} "
                                "files would be archived</b> ({months}). Nothing was moved.",
                                candidates=f"{summary['candidates']:,}", scanned=f"{summary['scanned']:,}",
                                files=f"{files:,}", months=months)
                             + (f"<br><br><b>{tr('Free space')}</b><br>{space_html(summary['space'])}"
                                if summary.get("space") else ""), "#1e3a8a", "#dbeafe",
                             tr("Preview: nothing was moved"), tiles)
        if status == "done" and self.app.cfg_email():
            lb.send_email(self.app.last_cfg, summary)

    def on_failed(self, message):
        for row in self.rows.values():
            if row.active:
                row.finish("failed")
        self.end()
        self.show_banner(tr("<b>Failed:</b> {message}<br>Nothing is lost: files not committed yet are still in the "
                            "log folders or in staging. Start again to continue. Details: Tools → Open log file.",
                            message=tr_error(message)), "#7f1d1d", "#fee2e2", "✕ " + tr("The run stopped with a problem"))
        self.show_log(True)  # the details are there


# ---------------------------------------------------------------- History page

class HistoryPage(QWidget):
    COLUMNS = ["#", "Started", "Status", "Mode", "Date range", "Barcodes", "Files", "Errors"]

    def __init__(self, app):
        super().__init__()
        self.app = app
        widget, box = page(tr("History"), tr("Every run recorded in the index, newest first."))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)
        top = QHBoxLayout()
        self.note = QLabel("", objectName="muted")
        top.addWidget(self.note)
        top.addStretch(1)
        again = button(tr("Refresh"))
        again.clicked.connect(self.refresh)
        top.addWidget(again)
        box.addLayout(top)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([tr(c) for c in self.COLUMNS])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)  # the date range
        self.table.setMinimumHeight(420)
        box.addWidget(self.table, 1)

    def refresh(self):
        self.table.setRowCount(0)
        try:
            path = Path(self.app.settings.get("db_path") or "")
        except AttributeError:
            return
        if not path.is_file():
            self.note.setText(tr("No runs yet."))
            return
        try:
            db = sqlite3.connect(lb.readonly_uri(path), uri=True)
            try:
                custom = {r[0] for r in db.execute("SELECT scan_session_id FROM custom_window_session")}
                waiting = {r[0] for r in db.execute("SELECT scan_session_id FROM pending_run")}
            except sqlite3.Error:
                custom, waiting = set(), set()
            rows = db.execute("SELECT id, started_at, status, query_from_date, query_to_date, total_barcodes,"
                              " total_files_moved, total_errors FROM scan_session ORDER BY id DESC LIMIT 500").fetchall()
            db.close()
        except sqlite3.Error as e:
            self.note.setText(tr("Cannot read the index: {error}", error=e))
            return
        self.note.setText(tr("{n} run(s) · {path}", n=len(rows), path=path))
        # Status: text color, cell tint. A stopped run that can continue is amber, not red: nothing is wrong.
        colors = {"success": ("#15803d", "#dcfce7"), "failed": ("#b91c1c", "#fee2e2"),
                  "running": ("#1d4ed8", "#dbeafe"), "waiting": ("#b45309", "#fef3c7")}
        for sid, started, status, start, end, barcodes, files, errors in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            mode = tr("resumed") if end == "resumed" else tr("custom") if sid in custom else tr("continue")
            if sid in waiting:
                mode += tr(" · can continue")
            when = started
            if str(started).startswith("epoch:"):
                when = dt.datetime.fromtimestamp(int(started[6:])).strftime("%Y-%m-%d %H:%M")
            values = [str(sid), when, tr(status), mode, "" if end == "resumed" else f"{start or tr('(start)')} → {end}",
                      f"{barcodes or 0:,}", f"{files or 0:,}", f"{errors or 0:,}"]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if c in (0, 5, 6, 7):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 2:
                    fg, bg = colors.get("waiting" if sid in waiting else status, ("#334155", "#f1f5f9"))
                    item.setForeground(QColor(fg))
                    item.setBackground(QColor(bg))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setTextAlignment(Qt.AlignCenter)
                if c == 7:
                    item.setForeground(QColor("#dc2626" if errors else "#94a3b8"))
                self.table.setItem(r, c, item)


# ---------------------------------------------------------------- Settings page

class PathField(QWidget):
    def __init__(self, placeholder="", folder=True, filter_=""):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        browse = button(tr("Browse…"))
        browse.clicked.connect(lambda: self.browse(folder, filter_))
        row.addWidget(self.edit, 1)
        row.addWidget(browse)

    def browse(self, folder, filter_):
        if folder:
            chosen = QFileDialog.getExistingDirectory(self, tr("Choose a folder"), self.edit.text())
        else:
            chosen = QFileDialog.getSaveFileName(self, tr("Choose a file"), self.edit.text(), filter_,
                                                 options=QFileDialog.DontConfirmOverwrite)[0] \
                if "index" in filter_.lower() else QFileDialog.getOpenFileName(self, tr("Choose a file"),
                                                                               self.edit.text(), filter_)[0]
        if chosen:
            self.edit.setText(chosen.replace("\\", "/"))

    def text(self):
        return self.edit.text().strip().replace("\\", "/")

    def setText(self, value):
        self.edit.setText(value or "")


class SettingsPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app, self.dirty = app, False
        widget, box = page(tr("Settings"), tr("Saved for this Windows user in {path}", path=lb.settings_path()))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)

        folders, fbox = card(tr("Folders"))
        form = form_layout()
        self.source = PathField(tr("D:/logs  or  //server/share/logs"))
        self.output = PathField("E:/archive")
        self.db = PathField(tr("default: <archive folder>/index.db"), folder=False, filter_="Index (*.db)")
        form.addRow(required(tr("Log folder (source)")), with_hint(
            self.source, tr("Where the test stations write their logs. A network share is fine.")))
        form.addRow(required(tr("Archive folder")), with_hint(
            self.output, tr("Where the monthly .mochi archives are kept.")))
        self.compression = QComboBox()
        for label, value in ((tr("Turbo (fastest, largest)"), "turbo"), (tr("Very fast"), "very_fast"),
                             (tr("Fast"), "fast"), (tr("Balanced (default)"), "balanced"),
                             (tr("Maximum"), "maximum"), (tr("Ultra"), "ultra"),
                             (tr("Extreme (slowest, smallest)"), "extreme")):
            self.compression.addItem(label, value)
        form.addRow(tr("Compression"), with_hint(
            self.compression, tr("For new monthly archives; an existing archive keeps the level it was "
                                 "created with. Ultra and Extreme are much slower and save little over Maximum.")))
        form.addRow(tr("Index file"), with_hint(
            self.db, tr("Finds files again by barcode. Empty: next to the archives.")))
        self.subfolders = QLineEdit()
        self.subfolders.setPlaceholderText("BURNIN, ESS, JTAG, STM, Xray")
        form.addRow(tr("Subfolders to scan"), self.subfolders)
        self.extensions = QLineEdit()
        self.extensions.setPlaceholderText("txt, zip")
        form.addRow(tr("File extensions"), self.extensions)
        self.recursive = QCheckBox(tr("Also scan folders inside each subfolder"))
        form.addRow("", self.recursive)
        self.lookback = QSpinBox()
        self.lookback.setRange(0, 365)
        self.lookback.setSuffix(tr(" days"))
        form.addRow(tr("Leave the newest"), with_hint(
            self.lookback, tr("Files of the last days stay for the next run (stations may still write them).")))
        fbox.addLayout(form)
        box.addWidget(folders)

        sql, sbox = card("SQL Server", tr("Read only: one query returns the barcodes of the date range."))
        form = form_layout()
        self.server = QLineEdit()
        self.server.setPlaceholderText(tr("sqlhost  or  HOST\\INSTANCE"))
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText(tr("1433 (default)"))
        self.database = QLineEdit()
        self.database.setPlaceholderText("ForthUtilities")
        self.user = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText(tr("asked when a run starts, unless remembered"))
        self.remember = QCheckBox(tr("Remember the password (encrypted for this Windows user)"))
        if not lb.can_protect():
            self.remember.setEnabled(False)
            self.remember.setToolTip(tr("Only available on Windows"))
        self.encryption = QComboBox()
        for label, value in (("Request (default)", "request"), ("Require", "require"),
                             ("Off (trusted network only)", "off")):
            self.encryption.addItem(tr(label), value)
        form.addRow(tr("Server"), self.server)
        form.addRow(tr("Port"), self.port)
        form.addRow(tr("Database"), self.database)
        form.addRow(tr("User"), self.user)
        form.addRow(tr("Password"), self.password)
        form.addRow("", self.remember)
        form.addRow(tr("Encryption"), self.encryption)
        sbox.addLayout(form)
        box.addWidget(sql)

        perf, pbox = card(tr("Network drive and speed"),
                          tr("Automatic suits most setups: on a network drive files are copied to local staging, "
                             "16 at a time; on a local disk they are moved one at a time."))
        form = form_layout()
        self.mode = QComboBox()
        for label, value in (("Automatic", None), ("Copy to local staging (network drive)", "copy"),
                             ("Move by rename (local disk)", "move")):
            self.mode.addItem(tr(label), value)
        self.staging = PathField(tr("automatic"))
        self.threads = QSpinBox()
        self.threads.setRange(0, 64)
        self.threads.setSpecialValueText(tr("Automatic"))
        self.batch = QSpinBox()
        self.batch.setRange(100, 1_000_000)
        self.batch.setSingleStep(1000)
        self.batch.setSuffix(tr(" files"))
        self.batch.setToolTip(tr("Stop waits for at most one batch's commit. Smaller: quicker stop, more commits."))
        form.addRow(tr("Staging"), self.mode)
        form.addRow(tr("Staging folder"), self.staging)
        form.addRow(tr("Files at once"), self.threads)
        form.addRow(tr("Batch size"), self.batch)
        pbox.addLayout(form)
        # Rarely changed: folded away under "Advanced settings".
        self.advanced = QWidget()
        adv = QVBoxLayout(self.advanced)
        adv.setContentsMargins(0, 0, 0, 0)
        adv.setSpacing(16)
        adv.addWidget(perf)

        other, obox = card(tr("Other"))
        form = form_layout()
        self.mochi = PathField(tr("found automatically"), folder=False, filter_="mochi (mochi.exe mochi)")
        self.email_url = QLineEdit()
        self.email_url.setPlaceholderText(tr("optional: http://host:port"))
        self.email_to = QLineEdit()
        self.email_to.setPlaceholderText(tr("optional: a@b.c"))
        self.language = QComboBox()
        for code, name in gui_text.LANGUAGES.items():
            self.language.addItem(name, code)
        self.language.setToolTip(tr("The language changes the next time the program starts."))
        form.addRow(tr("mochi program"), self.mochi)
        form.addRow(tr("Email report API"), self.email_url)
        form.addRow(tr("Email report to"), self.email_to)
        form.addRow(tr("Language") + (" / Language" if gui_text.language() != "en" else ""), self.language)
        obox.addLayout(form)
        adv.addWidget(other)
        self.advanced_toggle = button(tr("Advanced settings ▸"), "link")
        self.advanced_toggle.clicked.connect(lambda: self.show_advanced(self.advanced.isHidden()))
        box.addWidget(self.advanced_toggle)
        box.addWidget(self.advanced)
        self.advanced.hide()

        actions = QHBoxLayout()
        self.state = QLabel("", objectName="muted")
        actions.addWidget(self.state)
        actions.addStretch(1)
        importer = button(tr("Import config.json…"))
        importer.clicked.connect(self.import_file)
        actions.addWidget(importer)
        save = button(tr("Save settings"), "primary")
        save.clicked.connect(self.save)
        actions.addWidget(save)
        box.addLayout(actions)
        box.addStretch(1)

        for w in self.findChildren(QSpinBox):  # number fields: type, or arrow keys / mouse wheel
            w.setMaximumWidth(220)
        for w in self.findChildren(QLineEdit):
            w.textEdited.connect(self.touch)
        for w in self.findChildren(QSpinBox):
            w.valueChanged.connect(self.touch)
        for w in self.findChildren(QComboBox):
            w.currentIndexChanged.connect(self.touch)
        for w in self.findChildren(QCheckBox):
            w.toggled.connect(self.touch)

    def show_advanced(self, visible):
        self.advanced.setVisible(visible)
        self.advanced_toggle.setText(tr("Hide advanced settings ▾") if visible else tr("Advanced settings ▸"))

    def touch(self, *_):
        if not self.loading:
            self.dirty = True
            self.state.setText(tr("Unsaved changes"))

    loading = False

    def load(self, raw):
        self.loading = True
        raw = raw or {}
        sql = raw.get("sql_server") or {}
        self.source.setText(raw.get("source_dir"))
        self.output.setText(raw.get("output_dir"))
        self.db.setText(raw.get("db_path"))
        self.compression.setCurrentIndex(max(self.compression.findData(raw.get("compression") or "balanced"), 0))
        self.subfolders.setText(", ".join(raw.get("subfolders") or ["BURNIN", "ESS", "JTAG", "STM", "Xray"]))
        self.extensions.setText(", ".join(raw.get("extensions") or ["txt", "zip"]))
        self.recursive.setChecked(raw.get("recursive", True))
        self.lookback.setValue(int(raw.get("lookback_days", 2)))
        self.server.setText(sql.get("server") or "")
        self.port.setValue(int(sql.get("port") or 0))
        self.database.setText(sql.get("database") or "")
        self.user.setText(sql.get("user") or "")
        self.password.setText("")
        remembered = bool(sql.get("password_dpapi"))
        self.remember.setChecked(remembered)
        self.password.setPlaceholderText(tr("remembered (type to change)") if remembered
                                         else tr("asked when a run starts, unless remembered"))
        enc = str(sql.get("encryption") or "request").lower()
        enc = {"false": "off", "true": "require"}.get(enc, enc)
        self.encryption.setCurrentIndex(max(self.encryption.findData(enc), 0))
        self.mode.setCurrentIndex(max(self.mode.findData(raw.get("staging_mode")), 0))
        self.staging.setText(raw.get("staging_dir"))
        self.threads.setValue(int(raw.get("file_threads") or 0))
        self.batch.setValue(int(raw.get("batch_files") or lb.BATCH_FILES))
        self.mochi.setText(raw.get("mochi"))
        email = raw.get("email") or {}
        self.email_url.setText(email.get("api_base_url") or "")
        self.email_to.setText(email.get("to") or "")
        self.language.setCurrentIndex(max(self.language.findData(raw.get("language") or "th"), 0))
        self.loading, self.dirty = False, False
        self.state.setText("")

    def collect(self):
        """The settings as a dict (config.json keys), with the password handled per "remember"."""
        old_sql = (self.app.settings or {}).get("sql_server") or {}
        split = lambda text: [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]  # noqa: E731
        output = self.output.text()
        sql = {"server": self.server.text().strip() or None, "port": self.port.value() or None,
               "database": self.database.text().strip() or None, "user": self.user.text().strip() or None,
               "password": None, "encryption": self.encryption.currentData()}
        typed = self.password.text()
        if self.remember.isChecked():
            if typed:
                sql["password_dpapi"] = lb.protect(typed)
            elif old_sql.get("password_dpapi"):
                sql["password_dpapi"] = old_sql["password_dpapi"]
        if typed:
            self.app.session_password = typed  # used this session even if not remembered
        raw = {"source_dir": self.source.text(), "output_dir": output,
               "db_path": self.db.text() or (f"{output.rstrip('/')}/index.db" if output else ""),
               "compression": self.compression.currentData(),
               "sql_server": sql, "subfolders": split(self.subfolders.text()) or None,
               "extensions": split(self.extensions.text()) or None, "recursive": self.recursive.isChecked(),
               "lookback_days": self.lookback.value(), "staging_mode": self.mode.currentData(),
               "staging_dir": self.staging.text() or None, "file_threads": self.threads.value() or None,
               "batch_files": self.batch.value(), "mochi": self.mochi.text() or None, "date_range": None,
               "language": self.language.currentData(),
               "email": ({"api_base_url": self.email_url.text().strip(), "to": self.email_to.text().strip()}
                         if self.email_url.text().strip() and self.email_to.text().strip() else None)}
        return {k: v for k, v in raw.items() if v is not None or k in ("date_range",)}

    def save(self):
        try:
            raw = self.collect()
            lb.Config(raw=dict(raw, sql_server={k: v for k, v in raw["sql_server"].items() if k != "password_dpapi"}))
        except KeyError as e:
            names = {"source_dir": "the log folder", "output_dir": "the archive folder", "db_path": "the index file"}
            warn(self, tr("Missing setting"), tr("Please set {name}.", name=tr(names.get(e.args[0], e.args[0]))))
            return False
        except lb.BackupError as e:
            warn(self, tr("Settings problem"), tr_error(str(e)))
            return False
        try:
            lb.save_settings(raw)
        except OSError as e:
            warn(self, tr("Cannot save"), str(e))
            return False
        changed_language = raw.get("language") != (self.app.settings or {}).get("language", "th")
        self.app.settings = raw
        self.load(raw)
        self.state.setText(tr("Saved"))
        if changed_language:
            warn(self, tr("Language"), tr("The language changes the next time the program starts."), info=True)
        self.app.settings_saved()
        return True

    def ask_save(self):
        box = QMessageBox(QMessageBox.Question, tr("Unsaved settings"), tr("Save the changed settings before starting?"),
                          parent=self)
        save = box.addButton(tr("Save"), QMessageBox.AcceptRole)
        drop = box.addButton(tr("Don't save"), QMessageBox.DestructiveRole)
        box.addButton(tr("Cancel"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() == save:
            return self.save()
        if box.clickedButton() == drop:
            self.load(self.app.settings)
            return True
        return False

    def import_file(self):
        path = QFileDialog.getOpenFileName(self, tr("Import config.json"), str(lb.program_dir()), "JSON (*.json)")[0]
        if not path:
            return
        try:
            raw = lb.load_settings(path)
        except lb.BackupError as e:
            warn(self, tr("Cannot import"), str(e))
            return
        self.load(raw)
        password = (raw.get("sql_server") or {}).get("password")
        if password:
            self.password.setText(password)
        self.dirty = True
        self.state.setText(tr("Imported: check and save"))


# ---------------------------------------------------------------- Tools page

class ToolsPage(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app, self.job = app, None
        widget, box = page(tr("Tools"), None)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget)

        check, cbox = card(tr("Check the setup"),
                           tr("Finds the mochi program and makes one SQL Server connection. Changes nothing."))
        self.check_button = button(tr("Run check"), "primary")
        self.check_result = QLabel("", objectName="muted")
        self.check_result.setWordWrap(True)
        self.check_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row = QHBoxLayout()
        row.addWidget(self.check_button)
        row.addStretch(1)
        cbox.addLayout(row)
        cbox.addWidget(self.check_result)
        self.check_list = QVBoxLayout()  # one row per checked item (CHECK_LOOK)
        self.check_list.setSpacing(6)
        cbox.addLayout(self.check_list)
        box.addWidget(check)

        export, ebox = card(tr("Export a restorable copy"),
                            tr("Copies the archives, the index and the settings (without the password) to another "
                               "drive, every file checked. Archives unchanged since the last export are skipped."))
        row = QHBoxLayout()
        self.export_button = button(tr("Export to…"), "primary")
        self.export_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)  # button-sized, not full width
        row.addWidget(self.export_button)
        self.export_bar = QProgressBar()
        self.export_bar.setTextVisible(False)
        self.export_bar.setFixedHeight(8)
        self.export_bar.hide()
        row.addWidget(self.export_bar, 1)
        row.addStretch(0)
        ebox.addLayout(row)
        self.export_result = QLabel("", objectName="muted")
        self.export_result.setWordWrap(True)
        ebox.addWidget(self.export_result)
        box.addWidget(export)

        files, xbox = card(tr("Files "))
        row = QHBoxLayout()
        open_log = button(tr("Open log file"))
        open_log.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.app.log_path))))
        open_dir = button(tr("Open settings folder"))
        open_dir.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(lb.settings_dir()))))
        row.addWidget(open_log)
        row.addWidget(open_dir)
        row.addStretch(1)
        xbox.addLayout(row)
        box.addWidget(files)
        box.addStretch(1)

        self.check_button.clicked.connect(self.check)
        self.export_button.clicked.connect(self.export)

    def check(self):
        try:
            cfg = self.app.config()
        except Exception as e:
            self.check_result.setText(tr("Settings: {error}", error=e))
            return
        if cfg is None:
            return

        self.check_button.setEnabled(False)
        self.check_result.setText(tr("Checking…"))
        self.check_result.show()
        self.job = Job(lambda job: self.check_work(cfg))
        self.job.done.connect(self.check_done)
        self.job.failed.connect(self.check_done)
        self.job.start()

    @staticmethod
    def check_work(cfg):
        """[(state, label, value)]: state "ok", "fail", "warn" or "info"."""
        rows = []
        try:
            rows.append(("ok", "mochi", lb.check_mochi(cfg)))
        except lb.BackupError as e:
            rows.append(("fail", "mochi", tr_error(str(e))))
        try:
            rows.append(("ok", "SQL Server", lb.check_sql(cfg.sql)))
        except lb.BackupError as e:
            rows.append(("fail", "SQL Server", tr_error(str(e))))
        where = lambda p: tr("network drive") if lb.is_network_path(p) else tr("local disk")  # noqa: E731
        reach = lambda p: "info" if lb.free_space(p) is not None else "fail"  # noqa: E731
        rows.append((reach(cfg.source_dir), tr("Log folder"), tr("{path} ({where}), {threads} file(s) at once",
                     path=cfg.source_dir, where=where(cfg.source_dir), threads=cfg.threads)))
        rows.append(("info", tr("Staging"), tr("{mode}, in {path} ({where})", mode=tr(cfg.mode), path=cfg.staging_dir,
                                               where=where(cfg.staging_dir))))
        rows.append((reach(cfg.output_dir), tr("Archive folder"), f"{cfg.output_dir} ({where(cfg.output_dir)})"))
        rows.append(("info", tr("Index file"), f"{cfg.db_path} ({where(cfg.db_path)})"
                     + (tr("; checked and copied to {path} before each run", path=lb.local_data_dir() / "index-snapshots")
                        if lb.is_network_path(cfg.db_path) else "")))
        for name, path in (("staging", cfg.staging_dir), ("archives", cfg.output_dir), ("index", cfg.db_path)):
            free = lb.free_space(path)
            rows.append(("info" if free is not None else "warn", tr("Free space for {name}", name=tr(name)),
                         tr("unknown (not reachable)") if free is None else size(free)))
        return rows

    def check_done(self, rows):
        self.check_button.setEnabled(True)
        if isinstance(rows, str):  # the check itself failed
            rows = [("fail", tr("Check"), tr_error(rows))]
        self.check_result.setText("")
        self.check_result.hide()
        self.show_check(rows)

    def show_check(self, rows):
        while self.check_list.count():
            item = self.check_list.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        for state, label, value in rows:
            mark, color = CHECK_LOOK[state]
            frame = QFrame(objectName="checkRow")
            line = QHBoxLayout(frame)
            line.setContentsMargins(10, 6, 10, 6)
            line.setSpacing(10)
            icon = QLabel(mark)
            icon.setFixedWidth(18)
            icon.setStyleSheet(f"color: {color}; font-weight: 700;")
            line.addWidget(icon, 0, Qt.AlignTop)
            name = QLabel(f"<b>{label}</b>")
            name.setMinimumWidth(170)
            line.addWidget(name, 0, Qt.AlignTop)
            text = QLabel(str(value))
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            line.addWidget(text, 1)
            self.check_list.addWidget(frame)

    def export(self):
        if self.app.busy:
            warn(self, tr("Busy"), tr("Wait for the running backup to end first."), info=True)
            return
        try:
            cfg = self.app.config(ask_password=False)
        except Exception as e:
            self.export_result.setText(tr("Settings: {error}", error=e))
            return
        target = QFileDialog.getExistingDirectory(self, tr("Export to (another drive)"))
        if not target:
            return
        self.export_button.setEnabled(False)
        self.export_bar.show()
        self.export_result.setText(tr("Exporting…"))
        self.export_target = target
        self.job = Job(lambda job: lb.export(cfg, target, report=job.report))
        self.job.progress.connect(self.export_progress)
        self.job.done.connect(self.export_done)
        self.job.failed.connect(self.export_failed)
        self.job.start()

    def export_progress(self, _step, message, done, total):
        self.export_bar.setRange(0, int(total or 0))
        self.export_bar.setValue(int(done or 0))
        self.export_result.setText(message)

    def export_done(self, items):
        self.export_button.setEnabled(True)
        self.export_bar.hide()
        parts = [tr("{item}: {files} files, {size}, {copied} copied now", item=tr(item), files=f"{files:,}",
                    size=size(nbytes), copied=f"{copied:,}") for item, (files, nbytes, copied) in items.items()]
        self.export_result.setText(tr("Exported to {target}. {parts}. How to restore: RESTORE.txt in that folder.",
                                      target=self.export_target, parts=" · ".join(parts)))

    def export_failed(self, text):
        self.export_button.setEnabled(True)
        self.export_bar.hide()
        self.export_result.setText(tr("Export failed: {error}", error=tr_error(text)))


# ---------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    PAGES = [("backup", "Backup"), ("history", "History"), ("settings", "Settings"), ("tools", "Tools")]

    def __init__(self, log_handler, log_path):
        super().__init__()
        self.setWindowTitle(tr("Finisar Log Backup"))
        self.resize(1100, 820)
        self.busy, self.close_when_done = False, False
        self.session_password, self.last_cfg = None, None
        self.log_path = log_path
        try:
            self.settings = lb.load_settings()
        except lb.BackupError as e:
            warn(self, tr("Settings"), str(e))
            self.settings = None
        imported = False
        if self.settings is None:  # first start: take over a config.json if there is one
            for place in (Path.cwd() / "config.json", lb.program_dir() / "config.json"):
                if place.is_file():
                    try:
                        self.settings = lb.load_settings(place)
                        imported = True
                        break
                    except lb.BackupError:
                        pass
        self.settings = self.settings or {}

        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        side = QWidget()
        side.setStyleSheet("background: #1e293b;")
        side.setFixedWidth(200)
        sbox = QVBoxLayout(side)
        sbox.setContentsMargins(0, 0, 0, 0)
        sbox.setSpacing(0)
        brand = QLabel("Finisar Log Backup", objectName="brand")
        brand.setWordWrap(True)
        sbox.addWidget(brand)
        subtitle = QLabel(tr("MOCHI archives"), objectName="brandSub")
        subtitle.setWordWrap(True)
        sbox.addWidget(subtitle)
        self.nav = QListWidget(objectName="nav")
        for _, label in self.PAGES:
            self.nav.addItem(QListWidgetItem(tr(label)))
        sbox.addWidget(self.nav, 1)
        self.status = QLabel("", objectName="brandSub")
        self.status.setWordWrap(True)
        sbox.addWidget(self.status)
        outer.addWidget(side)

        self.stack = QStackedWidget()
        self.backup_page = BackupPage(self)
        self.history_page = HistoryPage(self)
        self.settings_page = SettingsPage(self)
        self.tools_page = ToolsPage(self)
        for w in (self.backup_page, self.history_page, self.settings_page, self.tools_page):
            self.stack.addWidget(w)
        outer.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.currentRowChanged.connect(self.page_shown)

        log_handler.bridge.line.connect(self.backup_page.add_log)
        self.settings_page.load(self.settings)
        if imported:
            self.settings_page.dirty = True
            self.settings_page.state.setText(tr("Imported from config.json: check and save"))
        complete = all(self.settings.get(k) for k in ("source_dir", "output_dir", "db_path"))
        self.go("backup" if complete and not imported else "settings")
        self.backup_page.refresh()
        self.history_page.refresh()
        self.update_status()

    def go(self, name):
        self.nav.setCurrentRow([p for p, _ in self.PAGES].index(name))

    def page_shown(self, index):
        name = self.PAGES[index][0]
        if name == "history":
            self.history_page.refresh()
        elif name == "backup":
            self.backup_page.refresh()

    def settings_saved(self):
        self.backup_page.refresh()
        self.history_page.refresh()
        self.update_status()

    def update_status(self):
        s = self.settings
        if s.get("source_dir"):
            self.status.setText(tr("Logs: {source}\nArchive: {output}", source=s.get("source_dir"),
                                   output=s.get("output_dir")))

    def set_busy(self, busy):
        self.busy = busy
        if not busy and self.close_when_done:
            QTimer.singleShot(0, self.close)

    def cfg_email(self):
        return bool(self.last_cfg and self.last_cfg.email)

    def config(self, ask_password=True):
        """A Config from the saved settings, with the SQL password filled in (asked if needed).
        Returns None if the password dialog was cancelled."""
        raw = dict(self.settings, date_range=None)  # the Backup page chooses the range
        cfg = lb.Config(raw=raw)
        if cfg.sql["user"] and not cfg.sql["password"]:
            if self.session_password:
                cfg.sql["password"] = self.session_password
            elif ask_password:
                dialog = QInputDialog(self)
                dialog.setWindowTitle(tr("SQL Server password"))
                dialog.setLabelText(tr("Password for {user} on {server}:", user=cfg.sql["user"],
                                       server=cfg.sql["server"]))
                dialog.setTextEchoMode(QLineEdit.Password)
                dialog.setOkButtonText(tr("OK"))
                dialog.setCancelButtonText(tr("Cancel"))
                if not dialog.exec():
                    return None
                self.session_password = cfg.sql["password"] = dialog.textValue()
        self.last_cfg = cfg
        return cfg

    def closeEvent(self, event):
        if not self.busy:
            event.accept()
            return
        if ask(self, tr("A backup is running"),
               tr("Stop the backup and close when it has stopped?\nNothing is lost: the next run continues where it "
                  "stopped.")):
            self.close_when_done = True
            self.backup_page.request_stop()
        event.ignore()


def setup_logging():
    """The run log in backup.log next to the settings (kept for audit), and in the window."""
    folder = lb.settings_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "backup.log"
    to_file = logging.FileHandler(path, encoding="utf-8")
    to_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    to_window = WindowLog()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(to_file)
    root.addHandler(to_window)
    return to_window, path


def style_app(app):
    """Fusion with a light palette and the app's style sheet, the same on every PC."""
    app.setApplicationName("Finisar Log Backup")
    app.setStyle("Fusion")
    palette = app.palette()  # a light theme, whatever the system's
    for role, color in ((QPalette.Window, "#f4f5f7"), (QPalette.Base, "#ffffff"), (QPalette.Text, "#0f172a"),
                        (QPalette.WindowText, "#0f172a"), (QPalette.Button, "#ffffff"),
                        (QPalette.ButtonText, "#0f172a"), (QPalette.Highlight, "#2563eb"),
                        (QPalette.AlternateBase, "#f8fafc"), (QPalette.ToolTipBase, "#0f172a"),
                        (QPalette.ToolTipText, "#ffffff")):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    thai = gui_text.language() == "th"  # Thai script reads better a little larger
    font = QFont("Segoe UI" if os.name == "nt" else app.font().family())
    font.setPointSizeF(10.5 if thai else 10)
    app.setFont(font)
    app.setStyleSheet(STYLE.replace("* { font-size: 10pt; }", "* { font-size: 10.5pt; }") if thai else STYLE)


def use_language(code):
    """Thai (default) or English for the whole app; Thai also gives Thai month names in the calendar."""
    gui_text.set_language(code)
    QLocale.setDefault(QLocale(QLocale.Thai, QLocale.Thailand) if gui_text.language() == "th"
                       else QLocale(QLocale.English, QLocale.UnitedStates))


def main(argv=None):
    app = QApplication(argv if argv is not None else sys.argv)
    try:
        use_language((lb.load_settings() or {}).get("language") or "th")
    except lb.BackupError:
        use_language("th")
    style_app(app)
    handler, path = setup_logging()
    window = MainWindow(handler, path)
    # Windows shutting down or logging off: stop the run at its next safe point (it continues next time).
    app.commitDataRequest.connect(lambda _manager: window.backup_page.request_stop())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
