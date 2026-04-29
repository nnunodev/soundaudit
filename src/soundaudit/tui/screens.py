"""Textual TUI screens."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Label,
    Log,
    ProgressBar,
    Static,
)

from soundaudit._version import __version__
from soundaudit.config import AppConfig
from soundaudit.db.store import Database
from soundaudit.models import FileInfo
from soundaudit.scanner.walker import discover_files


class DashboardScreen(Screen[None]):
    """Main dashboard with library overview."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("s", "scan", "Scan"),
        ("r", "report", "Report"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="dashboard"):
            with Vertical(id="stats-panel"):
                yield Static(
                    f"[b]SoundAudit[/b] [dim]v{__version__}[/dim]\n",
                    id="title",
                )
                yield Static("Loading stats...", id="stats")
            with Vertical(id="actions-panel"):
                yield Button("Scan Library", id="btn-scan", variant="primary")
                yield Button("Reports", id="btn-report", variant="success")
                yield Button("Reset DB", id="btn-reset", variant="warning")
                yield Button("Quit", id="btn-quit", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_stats()

    def on_screen_resume(self) -> None:
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        stats_widget = self.query_one("#stats", Static)
        app = self.app  # type: ignore[attr-defined]
        db_path = Path(app.get_db_path())
        if not db_path.exists():
            stats_widget.update(
                "[yellow]No database found.[/yellow]\n"
                "Use [b]Scan Library[/b] to get started."
            )
            return
        try:
            database = Database(str(db_path))
            from sqlalchemy import func
            from soundaudit.db.store import DBFile
            with database.session() as s:
                total = s.query(func.count(DBFile.id)).scalar() or 0
                flac = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.format == "flac")
                    .scalar()
                    or 0
                )
                mp3 = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.format == "mp3")
                    .scalar()
                    or 0
                )
                corrupt = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.is_corrupt == 1)
                    .scalar()
                    or 0
                )
                no_tags = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.title.is_(None))
                    .scalar()
                    or 0
                )
            lines = [
                f"[b]Database[/b]: {db_path}",
                "",
                "[b]Library Stats[/b]",
                f"  Total files  : {total:,}",
                f"  FLAC         : {flac:,}",
                f"  MP3          : {mp3:,}",
                f"  Corrupt      : { '[red]' + str(corrupt) + '[/red]' if corrupt else '0'}",
                f"  Missing tags : { '[yellow]' + str(no_tags) + '[/yellow]' if no_tags else '0'}",
            ]
            stats_widget.update("\n".join(lines))
        except Exception:
            stats_widget.update("[red]Error loading database stats.[/red]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-scan":
            self.action_scan()
        elif event.button.id == "btn-report":
            self.action_report()
        elif event.button.id == "btn-reset":
            self._confirm_reset()
        elif event.button.id == "btn-quit":
            self.app.exit()

    def _confirm_reset(self) -> None:
        self.app.push_screen(ResetConfirmScreen(), self._do_reset)

    def _do_reset(self, confirmed: bool) -> None:
        if not confirmed:
            return
        app = self.app  # type: ignore[attr-defined]
        db_path = Path(app.get_db_path())
        if db_path.exists():
            try:
                db_path.unlink()
                self.notify("Database reset successfully.", severity="information", timeout=3)
            except Exception as exc:
                self.notify(f"Failed to reset database: {exc}", severity="error", timeout=5)
        else:
            self.notify("No database found to reset.", severity="warning", timeout=3)
        self._refresh_stats()

    def action_scan(self) -> None:
        self.app.push_screen("scan")

    def action_report(self) -> None:
        self.app.push_screen("report")

    def action_quit(self) -> None:
        self.app.exit()


class ScanScreen(Screen[None]):
    """Scan screen with loading and progress indicators."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("escape", "cancel", "Cancel"),
    ]

    # Reactive state
    files_found: reactive[int] = reactive(0)
    files_scanned: reactive[int] = reactive(0)
    files_skipped: reactive[int] = reactive(0)
    files_saved: reactive[int] = reactive(0)
    current_file: reactive[str] = reactive("")
    is_scanning: reactive[bool] = reactive(False)

    class ScanComplete(Message):
        """Emitted when scanning finishes."""

        def __init__(self, saved: int) -> None:
            self.saved = saved
            super().__init__()

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="scan-screen"):
            yield Static("[b]Scan Library[/b]", id="scan-title")
            with Vertical(id="path-list"):
                yield Static("[dim]Select paths to scan:[/dim]", id="path-label")
            with Horizontal(id="progress-row"):
                with Vertical(id="progress-col"):
                    yield Label("Discovery")
                    yield ProgressBar(total=100, id="discovery-bar", show_eta=False)
                    yield Label("Scanning")
                    yield ProgressBar(total=100, id="scan-bar", show_eta=False)
                with Vertical(id="stats-col"):
                    yield Static("Files found: 0", id="stat-found")
                    yield Static("Scanned: 0", id="stat-scanned")
                    yield Static("Skipped: 0", id="stat-skipped")
                    yield Static("Saved: 0", id="stat-saved")
            yield Static("Waiting...", id="current-file")
            yield Log(id="scan-log")
            with Horizontal(id="scan-actions"):
                yield Button(
                    "Start Scan", id="btn-start", variant="primary", disabled=False
                )
                yield Button(
                    "Stop Scan", id="btn-stop", variant="error", disabled=True
                )
                yield Button(
                    "Back", id="btn-back", variant="default", disabled=False
                )
        yield Footer()

    def on_mount(self) -> None:
        app = self.app  # type: ignore[attr-defined]
        cfg = app.get_config()
        path_list = self.query_one("#path-list", Vertical)
        for p in cfg.scan.paths:
            path_list.mount(
                Checkbox(str(p), value=True, classes="path-checkbox")
            )
        # Focus the first checkbox so Enter/Space don't hit the button by accident
        checkboxes = list(self.query(".path-checkbox"))
        if checkboxes:
            checkboxes[0].focus()
        log = self.query_one("#scan-log", Log)
        log.write_line("Ready to scan. Press Start Scan.")
        self._update_progress_totals()

    def watch_files_found(self, value: int) -> None:
        self.query_one("#stat-found", Static).update(f"Files found: {value:,}")

    def watch_files_scanned(self, value: int) -> None:
        self.query_one("#stat-scanned", Static).update(f"Scanned: {value:,}")

    def watch_files_skipped(self, value: int) -> None:
        self.query_one("#stat-skipped", Static).update(f"Skipped: {value:,}")

    def watch_files_saved(self, value: int) -> None:
        self.query_one("#stat-saved", Static).update(f"Saved: {value:,}")

    def watch_current_file(self, value: str) -> None:
        display = Path(value).name if value else ""
        self.query_one("#current-file", Static).update(
            f"[bold]Scanning:[/bold] {display}" if display else ""
        )

    def watch_is_scanning(self, scanning: bool) -> None:
        self.query_one("#btn-start", Button).disabled = scanning
        self.query_one("#btn-stop", Button).disabled = not scanning
        self.query_one("#btn-back", Button).disabled = scanning

    def _update_progress_totals(self) -> None:
        scan_bar = self.query_one("#scan-bar", ProgressBar)
        scan_bar.total = max(self.files_found, 1)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-start":
            self._start_scan()
        elif event.button.id == "btn-stop":
            self.action_cancel()
        elif event.button.id == "btn-back":
            self.app.pop_screen()

    def action_cancel(self) -> None:
        if self.is_scanning:
            self.is_scanning = False
            self.query_one("#scan-log", Log).write_line("Cancelling...")
        else:
            self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()

    def _start_scan(self) -> None:
        # Collect selected paths
        selected: list[str] = []
        for cb in self.query(".path-checkbox"):
            if isinstance(cb, Checkbox) and cb.value:
                selected.append(str(cb.label))
        if not selected:
            self.query_one("#scan-log", Log).write_line(
                "No paths selected. Check at least one."
            )
            return
        self._selected_paths = selected
        self.files_found = 0
        self.files_scanned = 0
        self.files_skipped = 0
        self.files_saved = 0
        self.current_file = ""
        self.is_scanning = True
        log = self.query_one("#scan-log", Log)
        log.clear()
        log.write_line(f"Starting scan of {len(selected)} path(s)...")
        self.run_worker(
            self._scan_worker, exclusive=True, thread=True
        )

    def _scan_worker(self) -> None:
        """Background worker that performs the scan."""
        app = self.app  # type: ignore[attr-defined]
        cfg = app.get_config()
        db_path = app.get_db_path()
        selected_paths: list[str] = getattr(self, "_selected_paths", cfg.scan.paths)
        workers = cfg.scan.workers
        extensions = set(cfg.scan.extensions)

        database = Database(db_path)
        existing = database.get_existing_paths()

        log = self.query_one("#scan-log", Log)
        discovery_bar = self.query_one("#discovery-bar", ProgressBar)
        scan_bar = self.query_one("#scan-bar", ProgressBar)

        all_files: list[Path] = []
        skipped = 0

        # Discovery phase
        app.call_from_thread(
            log.write_line, f"Discovering files in {len(selected_paths)} selected path(s)..."
        )
        total_estimate = 0
        for root_path in selected_paths:
            root = Path(root_path).expanduser().resolve()
            if not root.exists():
                app.call_from_thread(
                    log.write_line, f"Skipping missing path: {root}"
                )
                continue
            try:
                for p in discover_files(root, extensions):
                    if not self.is_scanning:
                        app.call_from_thread(log.write_line, "Cancelled during discovery.")
                        return
                    all_files.append(p)
                    total_estimate += 1
                    # Throttle UI updates for performance
                    if total_estimate % 100 == 0:
                        app.call_from_thread(setattr, self, "files_found", total_estimate)
                        # total ahead by 500 so bar never reads 100% while still discovering
                        app.call_from_thread(
                            discovery_bar.update,
                            total=total_estimate + 500,
                            progress=total_estimate,
                        )
            except Exception as exc:
                app.call_from_thread(log.write_line, f"Error in {root}: {exc}")

        app.call_from_thread(setattr, self, "files_found", len(all_files))
        app.call_from_thread(
            discovery_bar.update,
            total=max(len(all_files), 1),
            progress=max(len(all_files), 1),
        )
        app.call_from_thread(
            log.write_line,
            f"Discovered {len(all_files):,} file(s) total.",
        )

        # Clean up stale DB entries (files removed/moved since last scan)
        discovered_paths = {str(p) for p in all_files}
        stale_paths = set(existing.keys()) - discovered_paths
        if stale_paths:
            removed = database.delete_by_paths(stale_paths)
            app.call_from_thread(
                log.write_line,
                f"Removed {removed:,} stale database entr{'ies' if removed != 1 else 'y'} (no longer on disk).",
            )

        # Filter unchanged files – compare mtime with 1-second tolerance
        files_to_scan: list[Path] = []
        for p in all_files:
            try:
                file_mtime = os.path.getmtime(p)
                if str(p) in existing:
                    if abs(file_mtime - existing[str(p)]) <= 1.0:
                        skipped += 1
                        continue
                files_to_scan.append(p)
            except OSError as exc:
                app.call_from_thread(
                    log.write_line,
                    f"[WARN] Could not stat {p}: {exc}"
                )

        app.call_from_thread(setattr, self, "files_skipped", skipped)
        app.call_from_thread(
            log.write_line,
            f"Skipped {skipped:,} unchanged file{'' if skipped == 1 else 's'}.",
        )

        total_to_scan = len(files_to_scan)
        if total_to_scan == 0:
            app.call_from_thread(log.write_line, "No new or changed files to scan.")
            app.call_from_thread(setattr, self, "is_scanning", False)
            app.call_from_thread(self.post_message, self.ScanComplete(saved=0))
            return

        app.call_from_thread(log.write_line, f"Scanning {total_to_scan:,} file(s)...")
        app.call_from_thread(scan_bar.update, total=max(total_to_scan, 1))

        saved = 0
        corrupt_count = 0

        def process(p: Path) -> FileInfo:
            from soundaudit.scanner.extractor import extract_file_info
            return extract_file_info(p)

        with ThreadPoolExecutor(max_workers=min(workers, 16)) as pool:
            log_batch: list[str] = []
            for i, info in enumerate(pool.map(process, files_to_scan)):
                if not self.is_scanning:
                    if log_batch:
                        app.call_from_thread(self._write_log_lines, log_batch[:])
                        log_batch.clear()
                    app.call_from_thread(log.write_line, "Cancelled.")
                    break
                database.upsert_file(info)
                saved += 1
                app.call_from_thread(setattr, self, "files_saved", saved)
                if info.is_corrupt:
                    corrupt_count += 1
                    app.call_from_thread(
                        log.write_line,
                        f"[CORRUPT] {info.path}",
                    )
                    if info.corruption_reason:
                        app.call_from_thread(
                            log.write_line,
                            f"  reason: {info.corruption_reason}",
                        )
                else:
                    log_batch.append(Path(info.path).name)
                    if len(log_batch) >= 20:
                        app.call_from_thread(self._write_log_lines, log_batch[:])
                        log_batch.clear()
                app.call_from_thread(setattr, self, "files_scanned", i + 1)
                app.call_from_thread(scan_bar.advance, 1)
                app.call_from_thread(setattr, self, "current_file", str(info.path))

            if log_batch:
                app.call_from_thread(self._write_log_lines, log_batch[:])
                log_batch.clear()

        app.call_from_thread(setattr, self, "is_scanning", False)
        app.call_from_thread(
            log.write_line,
            f"Done. Saved {saved:,} file(s) — {saved - corrupt_count:,} valid, {corrupt_count:,} corrupt.",
        )
        app.call_from_thread(self.post_message, self.ScanComplete(saved=saved))

    def _write_log_lines(self, lines: list[str]) -> None:
        log = self.query_one("#scan-log", Log)
        for line in lines:
            log.write_line(line)

    def on_scan_screen_scan_complete(self, event: ScanComplete) -> None:
        if event.saved > 0:
            self.query_one("#scan-log", Log).write_line(
                f"Scan complete. {event.saved:,} file(s) saved."
            )
        else:
            self.query_one("#scan-log", Log).write_line(
                "Scan complete — nothing new to save."
            )


class ReportScreen(Screen[None]):
    """Report screen showing detailed reports."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("escape", "back", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="report-screen"):
            yield Static("[b]Reports[/b]", id="report-title")
            with Horizontal(id="report-tabs"):
                yield Button("Summary", id="tab-summary", variant="primary")
                yield Button("Missing Tags", id="tab-missing", variant="default")
                yield Button("Corrupt", id="tab-corrupt", variant="default")
            yield DataTable(id="report-table")
        yield Footer()

    def on_mount(self) -> None:
        self._load_summary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tab-summary":
            self._load_summary()
        elif event.button.id == "tab-missing":
            self._load_missing_tags()
        elif event.button.id == "tab-corrupt":
            self._load_corrupt()

    def _load_summary(self) -> None:
        table = self.query_one("#report-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Metric", "Count")
        app = self.app  # type: ignore[attr-defined]
        try:
            database = Database(app.get_db_path())
            from sqlalchemy import func
            from soundaudit.db.store import DBFile
            with database.session() as s:
                total = s.query(func.count(DBFile.id)).scalar() or 0
                flac = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.format == "flac")
                    .scalar()
                    or 0
                )
                mp3 = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.format == "mp3")
                    .scalar()
                    or 0
                )
                corrupt = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.is_corrupt == 1)
                    .scalar()
                    or 0
                )
                no_tags = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.title.is_(None))
                    .scalar()
                    or 0
                )
            rows = [
                ("Total files", str(total)),
                ("FLAC", str(flac)),
                ("MP3", str(mp3)),
                ("Corrupt", str(corrupt)),
                ("Missing title", str(no_tags)),
            ]
            for row in rows:
                table.add_row(*row)
        except Exception:
            table.add_row("Error", "Could not load database")

    def _load_missing_tags(self) -> None:
        table = self.query_one("#report-table", DataTable)
        table.clear(columns=True)
        table.add_columns("File", "Missing")
        app = self.app  # type: ignore[attr-defined]
        try:
            database = Database(app.get_db_path())
            from soundaudit.db.store import DBFile
            with database.session() as s:
                files = (
                    s.query(DBFile)
                    .filter(
                        (DBFile.title.is_(None))
                        | (DBFile.artist.is_(None))
                        | (DBFile.album.is_(None))
                    )
                    .limit(100)
                    .all()
                )
            for f in files:
                missing = []
                if not f.title:
                    missing.append("title")
                if not f.artist:
                    missing.append("artist")
                if not f.album:
                    missing.append("album")
                table.add_row(f.path, ", ".join(missing))
        except Exception:
            table.add_row("Error", "Could not load database")

    def _load_corrupt(self) -> None:
        table = self.query_one("#report-table", DataTable)
        table.clear(columns=True)
        table.add_columns("File", "Reason")
        app = self.app  # type: ignore[attr-defined]
        try:
            database = Database(app.get_db_path())
            from soundaudit.db.store import DBFile
            with database.session() as s:
                files = s.query(DBFile).filter(DBFile.is_corrupt == 1).all()
            for f in files:
                table.add_row(f.path, f.corruption_reason or "unknown")
        except Exception:
            table.add_row("Error", "Could not load database")

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()


class ResetConfirmScreen(ModalScreen[bool]):
    """Modal dialog to confirm database reset."""

    def compose(self) -> ComposeResult:
        yield Container(
            Static("[b]Reset Database?[/b]", id="reset-title"),
            Static(
                "This will permanently delete the local SQLite database.\n"
                "Your music files will not be affected.",
                id="reset-message",
            ),
            Horizontal(
                Button("Cancel", id="btn-cancel", variant="default"),
                Button("Reset", id="btn-confirm", variant="error"),
                id="reset-actions",
            ),
            id="reset-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(False)
