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
        ("R", "reset", "Reset DB"),
    ]

    NAV_IDS: ClassVar[list[str]] = [
        "btn-scan", "btn-report", "btn-reset", "btn-quit"
    ]
    _nav_index: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="dashboard"):
            yield Static(
                f"[b]SoundAudit[/b] [dim]v{__version__}[/dim]",
                id="title",
            )
            yield Static("Loading stats...", id="stats")
            yield Static("─" * 40, id="dash-sep")
            with Horizontal(id="actions-row"):
                yield Static("▸ Scan       [dim]s[/dim]", id="btn-scan", classes="nav-item")
                yield Static("▸ Reports    [dim]r[/dim]", id="btn-report", classes="nav-item")
                yield Static("▸ Reset DB   [dim]R[/dim]", id="btn-reset", classes="nav-item")
                yield Static("▸ Quit       [dim]q[/dim]", id="btn-quit", classes="nav-item")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_stats()
        self.query_one(f"#{self._nav_sel()}", Static).focus()

    def on_screen_resume(self) -> None:
        self._refresh_stats()

    def _nav_sel(self) -> str:
        return self.NAV_IDS[self._nav_index]

    def _nav_move(self, delta: int) -> None:
        old_id = self._nav_sel()
        self._nav_index = max(0, min(len(self.NAV_IDS) - 1, self._nav_index + delta))
        new_id = self._nav_sel()
        if old_id != new_id:
            self.query_one(f"#{old_id}", Static).remove_class("focused-nav")
            node = self.query_one(f"#{new_id}", Static)
            node.add_class("focused-nav")
            node.focus()

    def watch__nav_index(self, index: int) -> None:
        pass  # handled in _nav_move

    def on_key(self, event) -> None:
        key = event.key
        if key in ("down", "right", "j", "l"):
            self._nav_move(1)
        elif key in ("up", "left", "k", "h"):
            self._nav_move(-1)
        elif key in ("enter", "space"):
            self._nav_action()
        else:
            return
        event.stop()

    def _nav_action(self) -> None:
        wid = self._nav_sel()
        if wid == "btn-scan":
            self.action_scan()
        elif wid == "btn-report":
            self.action_report()
        elif wid == "btn-reset":
            self._confirm_reset()
        elif wid == "btn-quit":
            self.action_quit()

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
                f"[b]Database[/b]: {db_path.name}",
                f"[b]Total[/b]   : {total:,}",
                f"[b]FLAC[/b]    : {flac:,}  [b]MP3[/b]: {mp3:,}",
                f"[b]Corrupt[/b] : { '[red]' + str(corrupt) + '[/red]' if corrupt else '0'}  [b]Missing[/b]: { '[yellow]' + str(no_tags) + '[/yellow]' if no_tags else '0'}",
            ]
            stats_widget.update("\n".join(lines))
            # highlight first nav item on stats load
            self.query_one(f"#{self._nav_sel()}", Static).add_class("focused-nav")
        except Exception:
            stats_widget.update("[red]Error loading database stats.[/red]")

    def on_click(self, event) -> None:
        widget_id = getattr(getattr(event, "control", None), "id", None)
        if widget_id == "btn-scan":
            self.action_scan()
        elif widget_id == "btn-report":
            self.action_report()
        elif widget_id == "btn-reset":
            self._confirm_reset()
        elif widget_id == "btn-quit":
            self.action_quit()

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

    def action_reset(self) -> None:
        self._confirm_reset()

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

    TAB_IDS: ClassVar[list[str]] = ["tab-summary", "tab-missing", "tab-corrupt"]
    _tab_index: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="report-screen"):
            yield Static("[b]Reports[/b]", id="report-title")
            with Horizontal(id="report-tabs"):
                yield Static("Summary", id="tab-summary", classes="tab-link active-tab")
                yield Static("Missing Tags", id="tab-missing", classes="tab-link")
                yield Static("Corrupt", id="tab-corrupt", classes="tab-link")
            yield DataTable(id="report-table")
        yield Footer()

    def on_mount(self) -> None:
        self._load_summary()
        self._tab_focus()

    def _tab_focus(self) -> None:
        self.query_one(f"#{self.TAB_IDS[self._tab_index]}", Static).focus()

    def on_key(self, event) -> None:
        key = event.key
        focused_wid = self.focused.id if self.focused else None
        is_table_focused = focused_wid == "report-table"

        if key in ("left", "right", "h", "l"):
            self._tab_move(-1 if key in ("left", "h") else 1)
        elif key in ("up", "down", "k", "j") and not is_table_focused:
            self._tab_move(-1 if key in ("up", "k") else 1)
        elif key in ("enter", "space"):
            if is_table_focused:
                return  # let DataTable handle it
            self._tab_activate()
        else:
            return
        event.stop()

    def _tab_move(self, delta: int) -> None:
        self._tab_index = (self._tab_index + delta) % len(self.TAB_IDS)
        self._tab_focus()
        self._tab_activate()

    def _tab_activate(self) -> None:
        tid = self.TAB_IDS[self._tab_index]
        if tid == "tab-summary":
            self._activate_tab("tab-summary")
            self._load_summary()
        elif tid == "tab-missing":
            self._activate_tab("tab-missing")
            self._load_missing_tags()
        elif tid == "tab-corrupt":
            self._activate_tab("tab-corrupt")
            self._load_corrupt()

    def on_click(self, event) -> None:
        widget_id = getattr(getattr(event, "control", None), "id", None)
        if widget_id in self.TAB_IDS:
            self._tab_index = self.TAB_IDS.index(widget_id)
            self._tab_focus()
            self._activate_tab(widget_id)
            if widget_id == "tab-summary":
                self._load_summary()
            elif widget_id == "tab-missing":
                self._load_missing_tags()
            elif widget_id == "tab-corrupt":
                self._load_corrupt()

    def _activate_tab(self, active_id: str) -> None:
        for tid in self.TAB_IDS:
            widget = self.query_one(f"#{tid}", Static)
            if tid == active_id:
                widget.add_class("active-tab")
            else:
                widget.remove_class("active-tab")

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
            paths = [f.path for f in files]
            short_paths = self._shorten_paths(paths)
            for f, sp in zip(files, short_paths):
                missing = []
                if not f.title:
                    missing.append("title")
                if not f.artist:
                    missing.append("artist")
                if not f.album:
                    missing.append("album")
                table.add_row(sp, ", ".join(missing))
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
            paths = [f.path for f in files]
            short_paths = self._shorten_paths(paths)
            for f, sp in zip(files, short_paths):
                table.add_row(sp, f.corruption_reason or "unknown")
        except Exception:
            table.add_row("Error", "Could not load database")

    @staticmethod
    def _shorten_paths(paths: list[str]) -> list[str]:
        """Strip the longest common directory prefix from a list of paths."""
        if not paths or len(paths) == 1:
            return paths
        import os
        prefix = os.path.commonprefix(paths)
        # Truncate to last separator so we don't chop in the middle of a folder name
        sep = max(prefix.rfind("\\"), prefix.rfind("/"))
        if sep > 0:
            prefix = prefix[: sep + 1]
        return [p[len(prefix) :] if p.startswith(prefix) else p for p in paths]

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()


class ResetConfirmScreen(ModalScreen[bool]):
    """Modal dialog to confirm database reset."""

    NAV_IDS: ClassVar[list[str]] = ["btn-cancel", "btn-confirm"]
    _nav_index: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        yield Container(
            Static("[b]Reset Database?[/b]", id="reset-title"),
            Static(
                "This will permanently delete the local SQLite database.\n"
                "Your music files will not be affected.",
                id="reset-message",
            ),
            Horizontal(
                Static("▸ Cancel", id="btn-cancel", classes="nav-item"),
                Static("▸ Reset", id="btn-confirm", classes="nav-item"),
                id="reset-actions",
            ),
            id="reset-dialog",
        )

    def on_mount(self) -> None:
        self._focus_nav()

    def _focus_nav(self) -> None:
        sel = self.NAV_IDS[self._nav_index]
        node = self.query_one(f"#{sel}", Static)
        node.add_class("focused-nav")
        node.focus()

    def _nav_move(self, delta: int) -> None:
        old = self.NAV_IDS[self._nav_index]
        self._nav_index = max(0, min(len(self.NAV_IDS) - 1, self._nav_index + delta))
        new = self.NAV_IDS[self._nav_index]
        if old != new:
            self.query_one(f"#{old}", Static).remove_class("focused-nav")
            self.query_one(f"#{new}", Static).add_class("focused-nav").focus()

    def on_key(self, event) -> None:
        key = event.key
        if key in ("left", "up", "h", "k"):
            self._nav_move(-1)
        elif key in ("right", "down", "l", "j"):
            self._nav_move(1)
        elif key in ("enter", "space"):
            self._nav_action()
        elif key in ("escape", "q"):
            self.dismiss(False)
        else:
            return
        event.stop()

    def _nav_action(self) -> None:
        sel = self.NAV_IDS[self._nav_index]
        if sel == "btn-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def on_click(self, event) -> None:
        widget_id = getattr(getattr(event, "control", None), "id", None)
        if widget_id == "btn-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)
