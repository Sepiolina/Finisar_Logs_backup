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


def volume_table(volumes, status):
    table = Table(title="Archives", title_justify="left")
    table.add_column("Month")
    table.add_column("Files", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Status")
    for month, (files, nbytes) in volumes.items():
        table.add_row(month, f"{files:,}", size(nbytes), status)
    return table


def ask_confirm(progress, yes):
    """The confirm callback: shows what would be archived and asks before anything moves."""
    def confirm(volumes):
        if yes:
            return True
        progress.stop()
        console.print(volume_table(volumes, "[yellow]to archive"))
        total = sum(files for files, _ in volumes.values())
        answer = Confirm.ask(f"Move {total:,} files into {len(volumes)} archive(s)?", default=False)
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
             f"Candidates  {summary['candidates']:,}",
             f"Scanned     {summary['scanned']:,} files",
             f"Committed   {summary['files_moved']:,} files" if status == "done" else "Dry run: nothing moved",
             f"Errors      {summary['errors']:,}"]
    if summary["resumed"]:
        lines.insert(4, f"Resumed     {summary['resumed']:,} files left in staging by an earlier run")
    if status == "done":
        lines.append("[dim]Committed is not a backup: keep an independent copy of the output folder and index.db.")
    style = "yellow" if summary["errors"] else ("green" if status == "done" else "cyan")
    console.print(Panel("\n".join(lines), title="Finisar log backup", border_style=style))


def main():
    parser = argparse.ArgumentParser(description="Finisar log backup (terminal UI)")
    parser.add_argument("config", help="path to config.json")
    parser.add_argument("--dry-run", action="store_true", help="query and scan only; move nothing")
    parser.add_argument("--yes", action="store_true", help="do not ask before moving files")
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
    if cfg.sql["user"] and not cfg.sql["password"]:  # asked each run, never stored
        cfg.sql["password"] = Prompt.ask(f"SQL Server password for {cfg.sql['user']}", password=True)

    columns = (Mark(finished_text="[green]✓"), TextColumn("{task.description}"),
               BarColumn(), MofNCompleteColumn(), TimeElapsedColumn())
    with Progress(*columns, console=console) as progress:
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
