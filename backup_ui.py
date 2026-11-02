"""Terminal front end for logs_backup.py, drawn with `rich`.

Display only: every backup decision is made in logs_backup.py. This file passes
two callbacks to logs_backup.run() (report for progress, confirm before files
move) and draws what it gets back.

    python backup_ui.py config.json [--dry-run] [--yes]
"""
import argparse
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

import logs_backup as lb

STEPS = {
    "resume": "Resume staging",
    "query": "Query SQL Server",
    "scan": "Scan folders",
    "stage": "Stage files",
    "commit": "Commit, verify, index",
    "export": "Export",
}

console = Console()


class Mark(SpinnerColumn):
    """Spinner while a step runs, ✓ when it is done, ✗ when it failed."""

    def render(self, task):
        return Text("✗", style="red") if task.fields.get("failed") else super().render(task)


def size(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class Steps:
    """The report callback: one progress row per step, added when the step first reports."""

    def __init__(self, progress):
        self.progress, self.rows = progress, {}

    def __call__(self, step, message="", done=0, total=None):
        if step not in self.rows:
            self.rows[step] = self.progress.add_task(STEPS[step], total=None)
        detail = f" [dim]{message}" if message else ""
        self.progress.update(self.rows[step], description=STEPS[step] + detail,
                             completed=done, total=total)

    def fail_running(self):
        for task in self.progress.tasks:
            if not task.finished:
                self.progress.update(task.id, description=f"[red]{task.description}", failed=True)


def totals(volumes):
    """(barcodes, files, bytes) over all months. A barcode's files all go to one month."""
    return tuple(sum(v[i] for v in volumes.values()) for i in range(3))


def volume_table(volumes, status):
    table = Table(title="Archives", title_justify="left", show_footer=len(volumes) > 1)
    barcodes, files, nbytes = totals(volumes)
    table.add_column("Month", footer="Total")
    table.add_column("Barcodes", justify="right", footer=f"{barcodes:,}")
    table.add_column("Files", justify="right", footer=f"{files:,}")
    table.add_column("Size", justify="right", footer=size(nbytes))
    table.add_column("Status")
    for month, (barcodes, files, nbytes) in volumes.items():
        table.add_row(month, f"{barcodes:,}", f"{files:,}", size(nbytes), status)
    return table


def ask_confirm(progress, yes):
    """The confirm callback: shows what would be archived and asks before anything moves."""
    def confirm(volumes):
        if yes:
            return True
        progress.stop()
        console.print(volume_table(volumes, "[yellow]to archive"))
        barcodes, files, nbytes = totals(volumes)
        answer = Confirm.ask(f"Archive {barcodes:,} barcodes ({files:,} files, {size(nbytes)})"
                             f" into {len(volumes)} monthly archive(s)?", default=False)
        progress.start()
        return answer
    return confirm


def print_summary(summary):
    status = summary["status"]
    if status == "cancelled":
        console.print(Panel("Cancelled. Nothing was moved or recorded.", border_style="yellow"))
        return
    label = {"done": "[green]committed (LOCAL_COMMITTED)", "dry_run": "[cyan]would archive"}[status]
    if summary["volumes"]:
        console.print(volume_table(summary["volumes"], label))
    date_from, date_to = summary["window"]
    lines = [f"Window      {date_from or '(start)'} .. {date_to}",
             f"Candidates  {summary['candidates']:,} barcodes",
             f"Scanned     {summary['scanned']:,} files",
             f"To archive  {summary['matched_files']:,} files ({summary['matched_barcodes']:,} barcodes)"]
    if summary["resumed"]:
        lines.append(f"Resumed     {summary['resumed']:,} files left in staging by an earlier run")
    if status == "done":
        lines.append(f"Committed   {summary['files_moved']:,} files ({summary['barcodes']:,} barcodes)")
    else:
        lines.append("Dry run: nothing moved")
    lines.append(f"Errors      {summary['errors']:,}")
    if status == "done":
        lines.append("[dim]Committed is not a backup: keep an independent copy (--export DIR to another drive).")
    style = "yellow" if summary["errors"] else ("green" if status == "done" else "cyan")
    console.print(Panel("\n".join(lines), title="Finisar log backup", border_style=style))


COLUMNS = (Mark(finished_text="[green]✓"), TextColumn("{task.description}"),
           BarColumn(), MofNCompleteColumn(), TimeElapsedColumn())


def export(cfg, target):
    """--export: copies archives, index and config to `target`, and shows what was copied."""
    with Progress(*COLUMNS, console=console) as progress:
        steps = Steps(progress)
        try:
            items = lb.export(cfg, target, report=steps)
        except Exception as e:
            steps.fail_running()
            progress.stop()
            logging.getLogger("logs_backup").exception("export failed")
            console.print(Panel(f"{e}\n\nNothing in the backup itself was changed. Details: backup.log",
                                title="Export failed", border_style="red"))
            sys.exit(1)
    table = Table(title="Exported", title_justify="left")
    for name, justify in (("Item", "left"), ("Files", "right"), ("Size", "right"), ("Copied now", "right")):
        table.add_column(name, justify=justify)
    for item, (files, nbytes, copied) in items.items():
        table.add_row(item, f"{files:,}", size(nbytes), f"{copied:,}" if copied else "[dim]unchanged")
    console.print(table)
    note = (f"Exported to {Path(target).resolve()}\nEvery copied file was checked against the original (BLAKE3)."
            " How to restore: RESTORE.txt in that folder.")
    if "staging" in items:
        note += ("\n[yellow]staging/ holds files from an interrupted run that are in no archive yet."
                 " Finish them with a normal run when you can.")
    console.print(Panel(note, border_style="green"))


def check_sql(cfg):
    """--check-sql: one connection with the configured login, nothing else."""
    sql = cfg.sql
    where = f"{sql['server']}" + ("" if "\\" in str(sql["server"]) else f":{sql['port'] or 1433}")
    with console.status(f"Connecting to {where} as {sql['user']} (encryption: {sql['encryption'] or 'request'})..."):
        try:
            version = lb.check_sql(sql)
        except lb.BackupError as e:
            console.print(Panel(str(e), title="SQL Server: no connection", border_style="red"))
            sys.exit(1)
    console.print(Panel(version, title="SQL Server: connected", border_style="green"))


def main():
    parser = argparse.ArgumentParser(description="Finisar log backup (terminal UI)")
    parser.add_argument("config", help="path to config.json")
    parser.add_argument("--dry-run", action="store_true", help="query and scan only; move nothing")
    parser.add_argument("--yes", action="store_true", help="do not ask before moving files")
    parser.add_argument("--check-sql", action="store_true", help="only test the SQL Server connection")
    parser.add_argument("--export", metavar="DIR", help="copy archives, index and config to DIR, then stop")
    args = parser.parse_args()

    # Warnings on screen; the full run log in backup.log next to the config, for audit.
    screen = RichHandler(console=console, show_path=False, level=logging.WARNING)
    screen.addFilter(lambda record: not record.exc_info)  # tracebacks go to backup.log only
    screen.setFormatter(logging.Formatter("%(message)s"))
    logfile = logging.FileHandler(Path(args.config).with_name("backup.log"), encoding="utf-8")
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[screen, logfile])

    try:
        cfg = lb.Config(args.config)
    except (OSError, ValueError, KeyError, lb.BackupError) as e:  # ValueError: invalid JSON
        detail = f"missing setting {e}" if isinstance(e, KeyError) else str(e)
        if "escape" in detail:
            detail += "\nWrite Windows paths with / (E:/archive) or \\\\ (E:\\\\archive)."
        console.print(Panel(f"{args.config}: {detail}", title="Config problem", border_style="red"))
        sys.exit(2)
    if args.export:
        export(cfg, args.export)
        return
    if cfg.sql["user"] and not cfg.sql["password"]:  # asked each run, never stored
        cfg.sql["password"] = Prompt.ask(f"SQL Server password for {cfg.sql['user']}", password=True)
    if args.check_sql:
        check_sql(cfg)
        return

    with Progress(*COLUMNS, console=console) as progress:
        steps = Steps(progress)
        try:
            summary = lb.run(cfg, dry_run=args.dry_run, report=steps, confirm=ask_confirm(progress, args.yes))
        except Exception as e:
            steps.fail_running()
            progress.stop()
            logging.getLogger("logs_backup").exception("run failed")
            console.print(Panel(f"{e}\n\nNothing is lost: files not yet committed are still in the source folders or in staging.\n"
                                "Run again to continue where this run stopped. Details: backup.log",
                                title="Run failed", border_style="red"))
            sys.exit(1)
    print_summary(summary)
    if summary["status"] == "done" and cfg.email:
        lb.send_email(cfg, summary)


if __name__ == "__main__":
    main()
