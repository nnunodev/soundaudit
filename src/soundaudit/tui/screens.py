"""Textual TUI screens."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Log,
    ProgressBar,
    Static,
)

from soundaudit._version import __version__
from soundaudit.analyzer.acoustid import (
    AcoustidDuplicateAnalyzer,
    AcoustidGroupVerdict,
    DupType,
    analyze_acoustid_keepers,
    find_acoustid_groups,
    write_acoustid_groups,
)
from soundaudit.analyzer.duplicates import (
    DuplicateAnalyzer,
    DuplicateGroupResult,
    KeeperVerdict,
    _human_size,
    analyze_keepers,
    find_duplicate_groups,
    write_duplicate_groups,
)
from soundaudit.config import AppConfig
from soundaudit.db.store import AcoustidGroup, Database
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
            from soundaudit.db.store import DBFile, DuplicateGroup
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
                dup_groups = (
                    s.query(func.count(DuplicateGroup.id)).scalar()
                    or 0
                )
                dup_files = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.duplicate_group_id.is_not(None))
                    .scalar()
                    or 0
                )
                acoustid_groups = (
                    s.query(func.count(AcoustidGroup.id)).scalar() or 0
                )
                acoustid_files = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.acoustid_group_id.is_not(None))
                    .scalar()
                    or 0
                )
                transcode_count = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.is_transcode == 1)
                    .scalar()
                    or 0
                )
                transcode_high = (
                    s.query(func.count(DBFile.id))
                    .filter(
                        DBFile.is_transcode == 1,
                        DBFile.transcode_confidence >= 0.70,
                    )
                    .scalar()
                    or 0
                )
            lines = [
                f"[b]Database[/b]: {db_path.name}",
                f"[b]Total[/b]   : {total:,}",
                f"[b]FLAC[/b]    : {flac:,}  [b]MP3[/b]: {mp3:,}",
                f"[b]Corrupt[/b] : { '[red]' + str(corrupt) + '[/red]' if corrupt else '0'}  [b]Missing[/b]: { '[yellow]' + str(no_tags) + '[/yellow]' if no_tags else '0'}",
            ]
            if dup_groups:
                lines.append(
                    f"[b]Dups[/b]    : {dup_groups} groups, {dup_files} files"
                )
            if acoustid_groups:
                lines.append(
                    f"[b]AcoustID[/b]: {acoustid_groups} groups, {acoustid_files} files"
                )
            if transcode_count:
                style = "[red]" if transcode_high > 0 else "[yellow]"
                lines.append(
                    f"[b]Transc[/b]  : {style}{transcode_count} suspects ({transcode_high} high)[/]"
                )
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
        ("f", "toggle_fingerprint", "Fingerprint"),
    ]

    # Reactive state
    files_found: reactive[int] = reactive(0)
    files_scanned: reactive[int] = reactive(0)
    files_skipped: reactive[int] = reactive(0)
    files_saved: reactive[int] = reactive(0)
    current_file: reactive[str] = reactive("")
    is_scanning: reactive[bool] = reactive(False)
    fingerprint_enabled: reactive[bool] = reactive(False)

    class ScanComplete(Message):
        """Emitted when scanning finishes."""

        def __init__(self, saved: int) -> None:
            self.saved = saved
            super().__init__()

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="scan-screen"):
            yield Static("[b]Scan[/b]  [dim]esc = back  f = fingerprint[/dim]", id="scan-title")
            with Vertical(id="path-list"):
                yield Static("[dim]Paths[/dim]", id="path-label")
            with Horizontal(id="stats-row"):
                yield Static("Found 0", id="stat-found")
                yield Static("Scan 0", id="stat-scanned")
                yield Static("Skip 0", id="stat-skipped")
                yield Static("Save 0", id="stat-saved")
                yield Static("Fingerprint [dim]off[/dim]", id="stat-fingerprint")
            with Horizontal(id="discovery-row"):
                yield Static("Disc", id="discovery-label")
                yield ProgressBar(total=100, id="discovery-bar", show_eta=False)
            with Horizontal(id="scanning-row"):
                yield Static("Scan", id="scanning-label")
                yield ProgressBar(total=100, id="scan-bar", show_eta=False)
            yield Static("", id="current-file")
            yield Log(id="scan-log")
            with Horizontal(id="scan-actions"):
                yield Static("Start", id="btn-start", classes="scan-link")
                yield Static("Stop", id="btn-stop", classes="scan-link dimmed")
                yield Static("Back", id="btn-back", classes="scan-link")
        yield Footer()

    def on_mount(self) -> None:
        app = self.app  # type: ignore[attr-defined]
        cfg = app.get_config()
        full_paths = [str(p) for p in cfg.scan.paths]
        short_paths = ReportScreen._shorten_paths(full_paths)
        self._path_map: dict[str, tuple[str, str]] = {}
        self._path_selected: dict[str, bool] = {}
        self._path_ids: list[str] = []
        self._path_focus_idx: int = 0
        self._action_focus_idx: int = 0
        self._scan_focus_group: str = "paths"
        path_list = self.query_one("#path-list", Vertical)
        for idx, (full, short) in enumerate(zip(full_paths, short_paths)):
            pid = f"path-{idx}"
            self._path_map[pid] = (full, short)
            self._path_selected[pid] = True
            self._path_ids.append(pid)
            path_list.mount(
                Static(
                    f"[green]✓[/green] {short}",
                    id=pid,
                    classes="path-item checked",
                )
            )
        # ── Analysis options ──
        path_list.mount(Static("", id="sep-analyze"))
        path_list.mount(
            Static(
                "[green]✓[/green] Dup Groups     [dim]content-hash duplicates (auto)[/dim]",
                id="opt-duplicates",
                classes="path-item checked",
            )
        )
        path_list.mount(
            Static(
                "[red]✗[/red] AcoustID Dups  [dim]fingerprint duplicates[/dim]",
                id="opt-acoustid",
                classes="path-item unchecked",
            )
        )
        path_list.mount(
            Static(
                "[red]✗[/red] Transcodes     [dim]fake-FLAC detection (slow)[/dim]",
                id="opt-transcodes",
                classes="path-item unchecked",
            )
        )
        # strip focus from everything non-interactive
        self.query_one("#scan-log", Log).can_focus = False
        self.query_one("#scan-title", Static).can_focus = False
        for sid in ("stat-found", "stat-scanned", "stat-skipped", "stat-saved",
                    "discovery-label", "scanning-label", "current-file", "path-label", "sep-analyze"):
            node = self.query_one(f"#{sid}", Static)
            if node:
                node.can_focus = False
        self._draw_focus()
        log = self.query_one("#scan-log", Log)
        log.write_line("Ready. ↑↓ move, Enter/Space toggle, Tab = actions, f = fingerprint")
        self._update_progress_totals()

    def _draw_focus(self) -> None:
        for pid in self._path_ids:
            self.query_one(f"#{pid}", Static).remove_class("focused-nav")
        for bid in ("btn-start", "btn-stop", "btn-back"):
            self.query_one(f"#{bid}", Static).remove_class("focused-nav")
        if self._scan_focus_group == "paths":
            pid = self._path_ids[self._path_focus_idx]
            self.query_one(f"#{pid}", Static).add_class("focused-nav")
        else:
            bids = ["btn-start", "btn-stop", "btn-back"]
            bid = bids[self._action_focus_idx]
            self.query_one(f"#{bid}", Static).add_class("focused-nav")

    def _refocus_path(self, delta: int) -> None:
        self._path_focus_idx = max(
            0, min(len(self._path_ids) - 1, self._path_focus_idx + delta)
        )
        self._scan_focus_group = "paths"
        self._draw_focus()

    def _refocus_action(self, delta: int) -> None:
        self._action_focus_idx = (self._action_focus_idx + delta) % 3
        self._scan_focus_group = "actions"
        self._draw_focus()

    def _toggle_path(self, pid: str) -> None:
        if pid.startswith("opt-"):
            self._toggle_option_in_scan(pid)
            return
        full, short = self._path_map[pid]
        was = self._path_selected[pid]
        self._path_selected[pid] = not was
        checked = self._path_selected[pid]
        node = self.query_one(f"#{pid}", Static)
        if checked:
            node.update(f"[green]✓[/green] {short}")
            node.add_class("checked")
            node.remove_class("unchecked")
        else:
            node.update(f"[red]✗[/red] {short}")
            node.add_class("unchecked")
            node.remove_class("checked")

    def _toggle_option_in_scan(self, oid: str) -> None:
        node = self.query_one(f"#{oid}", Static)
        currently_checked = "checked" in node.classes
        base = {
            "opt-duplicates": "Dup Groups     [dim]content-hash duplicates (auto)[/dim]",
            "opt-acoustid": "AcoustID Dups  [dim]fingerprint duplicates[/dim]",
            "opt-transcodes": "Transcodes     [dim]fake-FLAC detection (slow)[/dim]",
        }[oid]
        if currently_checked:
            node.update(f"[red]✗[/red] {base}")
            node.remove_class("checked")
            node.add_class("unchecked")
        else:
            node.update(f"[green]✓[/green] {base}")
            node.add_class("checked")
            node.remove_class("unchecked")

    def on_key(self, event) -> None:
        key = event.key
        if key in ("up", "k"):
            if self._scan_focus_group == "paths":
                self._refocus_path(-1)
            event.stop()
        elif key in ("down", "j"):
            if self._scan_focus_group == "paths":
                self._refocus_path(1)
            event.stop()
        elif key in ("left", "h"):
            if self._scan_focus_group == "actions":
                self._refocus_action(-1)
            event.stop()
        elif key in ("right", "l"):
            if self._scan_focus_group == "actions":
                self._refocus_action(1)
            event.stop()
        elif key in ("tab",):
            if self._scan_focus_group == "paths":
                self._scan_focus_group = "actions"
            else:
                self._scan_focus_group = "paths"
            self._draw_focus()
            event.stop()
        elif key in ("enter", "space", "return"):
            if self._scan_focus_group == "paths":
                pid = self._path_ids[self._path_focus_idx]
                self._toggle_path(pid)
            else:
                bids = ["btn-start", "btn-stop", "btn-back"]
                self._activate_action(bids[self._action_focus_idx])
            event.stop()

    def _activate_action(self, bid: str) -> None:
        if bid == "btn-start":
            if "dimmed" not in self.query_one("#btn-start", Static).classes:
                self._start_scan()
        elif bid == "btn-stop":
            if "dimmed" not in self.query_one("#btn-stop", Static).classes:
                self.action_cancel()
        elif bid == "btn-back":
            if "dimmed" not in self.query_one("#btn-back", Static).classes:
                if not self.is_scanning:
                    self.app.pop_screen()

    def watch_files_found(self, value: int) -> None:
        self.query_one("#stat-found", Static).update(f"Found {value:,}")

    def watch_files_scanned(self, value: int) -> None:
        total = max(self.files_found, 1)
        self.query_one("#stat-scanned", Static).update(f"Scan {value:,}/{total:,}")

    def watch_files_skipped(self, value: int) -> None:
        self.query_one("#stat-skipped", Static).update(f"Skip {value:,}")

    def watch_files_saved(self, value: int) -> None:
        self.query_one("#stat-saved", Static).update(f"Save {value:,}")

    def watch_current_file(self, value: str) -> None:
        display = Path(value).name if value else ""
        self.query_one("#current-file", Static).update(
            f"▸ {display}" if display else ""
        )

    def watch_is_scanning(self, scanning: bool) -> None:
        start = self.query_one("#btn-start", Static)
        stop = self.query_one("#btn-stop", Static)
        back = self.query_one("#btn-back", Static)
        if scanning:
            start.add_class("dimmed")
            stop.remove_class("dimmed")
            back.add_class("dimmed")
        else:
            start.remove_class("dimmed")
            stop.add_class("dimmed")
            back.remove_class("dimmed")
        if self._scan_focus_group == "actions":
            bids = ["btn-start", "btn-stop", "btn-back"]
            cur = bids[self._action_focus_idx]
            dimmed = "dimmed" in self.query_one(f"#{cur}", Static).classes
            if dimmed:
                for i, b in enumerate(bids):
                    if "dimmed" not in self.query_one(f"#{b}", Static).classes:
                        self._action_focus_idx = i
                        break
                self._draw_focus()

    def watch_fingerprint_enabled(self, value: bool) -> None:
        node = self.query_one("#stat-fingerprint", Static)
        if value:
            node.update("Fingerprint [green]ON[/green]")
        else:
            node.update("Fingerprint [dim]off[/dim]")

    def _toggle_fingerprint(self) -> None:
        self.fingerprint_enabled = not self.fingerprint_enabled
        log = self.query_one("#scan-log", Log)
        log.write_line(
            f"Fingerprint {'enabled' if self.fingerprint_enabled else 'disabled'}."
        )

    def action_toggle_fingerprint(self) -> None:
        if not self.is_scanning:
            self._toggle_fingerprint()

    def _update_progress_totals(self) -> None:
        scan_bar = self.query_one("#scan-bar", ProgressBar)
        scan_bar.total = max(self.files_found, 1)

    def on_click(self, event) -> None:
        widget_id = getattr(getattr(event, "control", None), "id", None)
        if widget_id == "btn-start":
            self._start_scan()
        elif widget_id == "btn-stop":
            self.action_cancel()
        elif widget_id == "btn-back":
            if not self.is_scanning:
                self.app.pop_screen()
        elif widget_id and widget_id.startswith("path-"):
            if widget_id in self._path_ids:
                self._path_focus_idx = self._path_ids.index(widget_id)
                self._scan_focus_group = "paths"
                self._draw_focus()
            self._toggle_path(widget_id)
            event.stop()

    def action_cancel(self) -> None:
        if self.is_scanning:
            self.is_scanning = False
            self.query_one("#scan-log", Log).write_line("Cancelling...")
        else:
            self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()

    def _start_scan(self) -> None:
        selected = [
            self._path_map[pid][0]
            for pid, sel in self._path_selected.items()
            if sel
        ]
        if not selected:
            self.query_one("#scan-log", Log).write_line(
                "No paths selected. Toggle at least one."
            )
            return
        # Capture analysis choices from UI before worker starts
        self._analysis_choices: dict[str, bool] = {
            "duplicates": "checked" in self.query_one("#opt-duplicates", Static).classes,
            "acoustid": "checked" in self.query_one("#opt-acoustid", Static).classes,
            "transcodes": "checked" in self.query_one("#opt-transcodes", Static).classes,
        }
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
        enabled = [k for k, v in self._analysis_choices.items() if v]
        if enabled:
            log.write_line(f"After-scan analyses: {', '.join(enabled)}")
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
            return extract_file_info(
                p,
                fingerprint=getattr(self, "fingerprint_enabled", False),
                fpcalc_path=cfg.fingerprinting.fpcalc_path,
            )

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

        # Run duplicate analysis after scan
        choices: dict[str, bool] = getattr(self, "_analysis_choices", {"duplicates": True})

        if choices.get("duplicates", True):
            try:
                app.call_from_thread(log.write_line, "Analyzing content-hash duplicates...")
                groups = find_duplicate_groups(database)
                if groups:
                    write_duplicate_groups(database, groups)
                    total_wasted = sum(r.wasted_bytes for r in groups)
                    app.call_from_thread(
                        log.write_line,
                        f"Found {len(groups)} duplicate groups ({sum(r.file_count for r in groups)} files). "
                        f"Wasted: {_human_size(total_wasted)}.",
                    )
                else:
                    app.call_from_thread(log.write_line, "No content-hash duplicates found.")
            except Exception as exc:
                app.call_from_thread(log.write_line, f"Duplicate analysis failed: {exc}")

        if choices.get("acoustid"):
            try:
                app.call_from_thread(log.write_line, "Analyzing AcoustID duplicates...")
                ac_groups = find_acoustid_groups(database)
                if ac_groups:
                    write_acoustid_groups(database, ac_groups)
                    total_wasted = sum(r.wasted_bytes for r in ac_groups)
                    app.call_from_thread(
                        log.write_line,
                        f"Found {len(ac_groups)} AcoustID groups ({sum(r.file_count for r in ac_groups)} files). "
                        f"Wasted: {_human_size(total_wasted)}.",
                    )
                else:
                    app.call_from_thread(log.write_line, "No AcoustID duplicates found.")
            except Exception as exc:
                app.call_from_thread(log.write_line, f"AcoustID analysis failed: {exc}")

        if choices.get("transcodes"):
            try:
                app.call_from_thread(log.write_line, "Analyzing transcodes...")
                from soundaudit.analyzer.transcode import analyze_library_transcodes
                analyze_library_transcodes(
                    database,
                    workers=4,
                    log_callback=lambda msg: app.call_from_thread(log.write_line, msg),
                )
            except Exception as exc:
                app.call_from_thread(log.write_line, f"Transcode analysis failed: {exc}")

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
        ("s", "export", "Export"),
    ]

    TAB_IDS: ClassVar[list[str]] = [
        "tab-summary", "tab-missing", "tab-duplicates", "tab-acoustid", "tab-transcodes", "tab-corrupt"
    ]
    _tab_index: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="report-screen"):
            yield Static("[b]Reports[/b]", id="report-title")
            with Horizontal(id="report-tabs"):
                yield Static("Summary", id="tab-summary", classes="tab-link active-tab")
                yield Static("Missing Tags", id="tab-missing", classes="tab-link")
                yield Static("Duplicates", id="tab-duplicates", classes="tab-link")
                yield Static("AcoustID Dups", id="tab-acoustid", classes="tab-link")
                yield Static("Transcodes", id="tab-transcodes", classes="tab-link")
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
        elif tid == "tab-duplicates":
            self._activate_tab("tab-duplicates")
            self._load_duplicates()
        elif tid == "tab-acoustid":
            self._activate_tab("tab-acoustid")
            self._load_acoustid()
        elif tid == "tab-transcodes":
            self._activate_tab("tab-transcodes")
            self._load_transcodes()
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
            elif widget_id == "tab-duplicates":
                self._load_duplicates()
            elif widget_id == "tab-acoustid":
                self._load_acoustid()
            elif widget_id == "tab-transcodes":
                self._load_transcodes()
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

    def _load_duplicates(self) -> None:
        table = self.query_one("#report-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Verdict", "File", "Album", "Tech", "Why")
        app = self.app  # type: ignore[attr-defined]
        try:
            database = Database(app.get_db_path())
            from soundaudit.db.store import DBFile, DuplicateGroup
            with database.session() as s:
                groups = s.query(DuplicateGroup).all()
            if not groups:
                table.add_row("—", "No duplicates found", "", "", "")
                return

            for db_group in groups:
                with database.session() as s:
                    files = (
                        s.query(DBFile)
                        .filter_by(duplicate_group_id=db_group.id)
                        .order_by(DBFile.path)
                        .all()
                    )
                if not files or len(files) < 2:
                    continue

                group_result = DuplicateGroupResult(
                    content_hash=db_group.acoustid or "",
                    file_count=len(files),
                    total_size_bytes=sum(f.size_bytes for f in files),
                    files=files,
                    group_id=db_group.id,
                )
                verdict = analyze_keepers(group_result)

                # Group header
                header_style = "bold cyan"
                table.add_row(
                    Text(f"Grp {db_group.id}", style=header_style),
                    Text(f"{len(files)} files", style=header_style),
                    Text("", style=header_style),
                    Text(_human_size(verdict.wasted_bytes), style=header_style),
                    Text("wasted", style="yellow"),
                )

                for fv in verdict.file_verdicts:
                    f = fv.db_file
                    row_style = {
                        KeeperVerdict.KEEP: "green",
                        KeeperVerdict.DELETE: "red",
                        KeeperVerdict.REVIEW: "yellow",
                    }[fv.verdict]

                    path = str(Path(f.path).name) if len(files) > 3 else str(Path(f.path))
                    table.add_row(
                        Text(fv.verdict.value, style=f"bold {row_style}"),
                        path,
                        f.album or ("Single" if "single" in f.path.lower().split(os.sep) else "—"),
                        fv.tech_summary,
                        ", ".join(fv.reasons[:3]),
                        label=None,
                        style=row_style,
                    )
        except Exception:
            table.add_row("Error", "Could not load duplicates", "", "", "")

    def _load_acoustid(self) -> None:
        table = self.query_one("#report-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Verdict", "Type", "File", "Album", "Why")
        app = self.app  # type: ignore[attr-defined]
        try:
            database = Database(app.get_db_path())
            from soundaudit.db.store import AcoustidGroup, DBFile
            with database.session() as s:
                groups = s.query(AcoustidGroup).all()
            if not groups:
                table.add_row("—", "", "No AcoustID duplicates found", "", "")
                return

            for db_group in groups:
                with database.session() as s:
                    files = (
                        s.query(DBFile)
                        .filter_by(acoustid_group_id=db_group.id)
                        .order_by(DBFile.path)
                        .all()
                    )
                if not files or len(files) < 2:
                    continue

                group_result = DuplicateGroupResult(
                    content_hash=db_group.fingerprint,
                    file_count=len(files),
                    total_size_bytes=sum(f.size_bytes for f in files),
                    files=files,
                    group_id=db_group.id,
                )
                verdict = analyze_acoustid_keepers(group_result)

                header_style = "bold cyan"
                bfb_count = sum(1 for v in verdict.file_verdicts if v.dup_type == DupType.BIT_FOR_BIT)
                trans_count = len(verdict.file_verdicts) - bfb_count
                dup_summary_parts: list[str] = []
                if bfb_count > 1:
                    dup_summary_parts.append(f"{bfb_count} bit-for-bit")
                if trans_count > 0:
                    dup_summary_parts.append(f"{trans_count} transcode")

                table.add_row(
                    Text(f"Grp {db_group.id}", style=header_style),
                    Text("", style=header_style),
                    Text(f"{len(files)} files", style=header_style),
                    Text(", ".join(dup_summary_parts) if dup_summary_parts else "", style=header_style),
                    Text(_human_size(verdict.wasted_bytes), style="yellow"),
                )

                for fv in verdict.file_verdicts:
                    f = fv.db_file
                    row_style = {
                        KeeperVerdict.KEEP: "green",
                        KeeperVerdict.DELETE: "red",
                        KeeperVerdict.REVIEW: "yellow",
                    }[fv.verdict]
                    type_style = {
                        DupType.BIT_FOR_BIT: "green",
                        DupType.TRANSCODE: "yellow",
                    }[fv.dup_type]

                    path = str(Path(f.path).name) if len(files) > 3 else str(Path(f.path))
                    album = f.album or ("Single" if "single" in f.path.lower().split(os.sep) else "—")
                    table.add_row(
                        Text(fv.verdict.value, style=f"bold {row_style}"),
                        Text(fv.dup_type.value, style=type_style),
                        path,
                        album,
                        ", ".join(fv.reasons[:3]),
                        label=None,
                        style=row_style,
                    )
        except Exception:
            table.add_row("Error", "", "Could not load AcoustID duplicates", "", "")

    def _load_transcodes(self) -> None:
        table = self.query_one("#report-table", DataTable)
        table.clear(columns=True)
        table.add_columns("File", "Confidence", "Cutoff", "Reason")
        app = self.app  # type: ignore[attr-defined]
        try:
            database = Database(app.get_db_path())
            from soundaudit.db.store import DBFile
            with database.session() as s:
                files = (
                    s.query(DBFile)
                    .filter(DBFile.is_transcode == 1)
                    .order_by(DBFile.transcode_confidence.desc())
                    .limit(100)
                    .all()
                )
            if not files:
                table.add_row("—", "", "", "No transcode suspects found")
                return

            paths = [f.path for f in files]
            short_paths = self._shorten_paths(paths)
            for f, sp in zip(files, short_paths):
                conf_style = {
                    (0.7, 1.0): "[bold red]",
                    (0.4, 0.7): "[yellow]",
                }
                style = "[dim]"
                for (lo, hi), s in conf_style.items():
                    if lo <= f.transcode_confidence <= hi:
                        style = s
                        break
                cutoff = f"{f.spectral_cutoff_hz:,}Hz" if f.spectral_cutoff_hz else "—"
                table.add_row(
                    sp,
                    f"{style}{f.transcode_confidence:.0%}[/]",
                    cutoff,
                    f.transcode_reason or "—",
                )
        except Exception:
            table.add_row("Error", "", "", "Could not load transcodes")

    def _human_size(self, size_bytes: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(size_bytes) < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} PB"

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

    def action_export(self) -> None:
        tab = self.TAB_IDS[self._tab_index]
        default_name = f"soundaudit_{tab.removeprefix('tab-')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        self.app.push_screen(
            ExportDialog(default_name),
            lambda path: self._do_export(path, tab),
        )

    def _do_export(self, path: Path | None, tab: str) -> None:
        if path is None:
            return
        app = self.app  # type: ignore[attr-defined]
        db_path = app.get_db_path()
        try:
            from soundaudit.reporter import ReportExporter, infer_format
            from soundaudit.db.store import Database
            db = Database(db_path)
            fmt = infer_format(path)
            exporter = ReportExporter(path)

            if tab == "tab-summary":
                self._export_summary(db, exporter, fmt)
            elif tab == "tab-missing":
                self._export_missing_tags(db, exporter, fmt)
            elif tab == "tab-duplicates":
                self._export_duplicates(db, exporter, fmt)
            elif tab == "tab-acoustid":
                self._export_acoustid(db, exporter, fmt)
            elif tab == "tab-transcodes":
                self._export_transcodes(db, exporter, fmt)
            elif tab == "tab-corrupt":
                self._export_corrupt(db, exporter, fmt)

            self.notify(f"Saved to {path.name}", severity="information", timeout=4)
        except Exception as exc:
            self.notify(f"Export failed: {exc}", severity="error", timeout=5)

    def _export_summary(self, db: Database, exporter, fmt: str) -> None:
        from sqlalchemy import func
        from soundaudit.db.store import DBFile, DuplicateGroup
        with db.session() as s:
            total = s.query(func.count(DBFile.id)).scalar() or 0
            flac = s.query(func.count(DBFile.id)).filter(DBFile.format == "flac").scalar() or 0
            mp3 = s.query(func.count(DBFile.id)).filter(DBFile.format == "mp3").scalar() or 0
            corrupt = s.query(func.count(DBFile.id)).filter(DBFile.is_corrupt == 1).scalar() or 0
            no_tags = s.query(func.count(DBFile.id)).filter(DBFile.title.is_(None)).scalar() or 0
            dup_groups = s.query(func.count(DuplicateGroup.id)).scalar() or 0
        if fmt == "json":
            exporter.write_json({"report_type": "summary", "metrics": {
                "total": total, "flac": flac, "mp3": mp3, "corrupt": corrupt,
                "missing_title": no_tags, "duplicate_groups": dup_groups,
            }})
        elif fmt == "csv":
            exporter.write_csv([{"metric": k, "value": v} for k, v in {
                "total": total, "flac": flac, "mp3": mp3, "corrupt": corrupt,
                "missing_title": no_tags, "duplicate_groups": dup_groups,
            }.items()])
        else:
            from soundaudit.reporter import MarkdownSection
            rows = [[k, str(v)] for k, v in {
                "Total files": total, "FLAC": flac, "MP3": mp3,
                "Corrupt": corrupt, "Missing title": no_tags,
                "Duplicate groups": dup_groups,
            }.items()]
            exporter.write_markdown("Library Summary", [MarkdownSection("Summary", ["Metric", "Count"], rows)])

    def _export_missing_tags(self, db: Database, exporter, fmt: str) -> None:
        from soundaudit.db.store import DBFile
        from soundaudit.reporter import MarkdownSection
        with db.session() as s:
            files = s.query(DBFile).filter(
                (DBFile.title.is_(None)) | (DBFile.artist.is_(None)) | (DBFile.album.is_(None))
            ).all()
        rows = [{"file": f.path, "missing": ", ".join(
            t for t, cond in [("title", not f.title), ("artist", not f.artist), ("album", not f.album)] if cond
        )} for f in files]
        if fmt == "json":
            exporter.write_json({"report_type": "missing_tags", "files": rows})
        elif fmt == "csv":
            exporter.write_csv(rows)
        else:
            md_rows = [[r["file"], r["missing"]] for r in rows] if rows else []
            exporter.write_markdown("Missing Tags", [MarkdownSection("Files with Missing Tags", ["File", "Missing"], md_rows)])

    def _export_duplicates(self, db: Database, exporter, fmt: str) -> None:
        from soundaudit.db.store import DBFile, DuplicateGroup
        from soundaudit.reporter import MarkdownSection
        from soundaudit.analyzer.duplicates import DuplicateGroupResult, analyze_keepers
        with db.session() as s:
            groups = s.query(DuplicateGroup).all()
        group_data = []
        csv_rows = []
        md_sections = []
        for g in groups:
            with db.session() as s:
                files = s.query(DBFile).filter_by(duplicate_group_id=g.id).all()
            if len(files) < 2:
                continue
            result = DuplicateGroupResult(
                content_hash=g.acoustid or "", file_count=len(files),
                total_size_bytes=sum(f.size_bytes for f in files), files=files, group_id=g.id,
            )
            verdict = analyze_keepers(result)
            file_entries = []
            md_rows = []
            for fv in verdict.file_verdicts:
                f = fv.db_file
                entry = {
                    "path": f.path, "verdict": fv.verdict.value, "score": round(fv.score, 1),
                    "reasons": fv.reasons, "album": f.album or "", "format": f.format or "",
                    "bit_depth": f.bit_depth, "sample_rate_hz": f.sample_rate_hz,
                    "size_bytes": f.size_bytes, "lossless": bool(f.lossless),
                }
                file_entries.append(entry)
                csv_rows.append({"group_id": g.id, **entry})
                md_rows.append([fv.verdict.value, str(Path(f.path).name), f.album or "—", fv.tech_summary, ", ".join(fv.reasons[:3])])
            group_data.append({"group_id": g.id, "content_hash": g.acoustid or "", "total_files": len(files), "wasted_bytes": verdict.wasted_bytes, "files": file_entries})
            md_sections.append(MarkdownSection(f"Group {g.id} — {_human_size(verdict.wasted_bytes)} wasted", ["Verdict", "File", "Album", "Tech", "Why"], md_rows, f"Content hash: `{g.acoustid or 'n/a'}` | {len(files)} files"))
        if fmt == "json":
            exporter.write_json({"report_type": "duplicates", "groups": group_data})
        elif fmt == "csv":
            exporter.write_csv(csv_rows)
        else:
            exporter.write_markdown("Duplicate Groups", md_sections)

    def _export_acoustid(self, db: Database, exporter, fmt: str) -> None:
        from soundaudit.db.store import AcoustidGroup, DBFile
        from soundaudit.reporter import MarkdownSection
        from soundaudit.analyzer.duplicates import DuplicateGroupResult
        from soundaudit.analyzer.acoustid import analyze_acoustid_keepers

        with db.session() as s:
            groups = s.query(AcoustidGroup).all()

        group_data: list[dict] = []
        csv_rows: list[dict] = []
        md_sections: list[MarkdownSection] = []

        for g in groups:
            with db.session() as s:
                files = s.query(DBFile).filter_by(acoustid_group_id=g.id).all()
            if len(files) < 2:
                continue
            result = DuplicateGroupResult(
                content_hash=g.fingerprint,
                file_count=len(files),
                total_size_bytes=sum(f.size_bytes for f in files),
                files=files,
                group_id=g.id,
            )
            verdict = analyze_acoustid_keepers(result)
            file_entries = []
            md_rows = []
            for fv in verdict.file_verdicts:
                f = fv.db_file
                entry = {
                    "path": f.path,
                    "verdict": fv.verdict.value,
                    "dup_type": fv.dup_type.value,
                    "score": round(fv.score, 1),
                    "reasons": fv.reasons,
                    "album": f.album or "",
                    "format": f.format or "",
                    "bit_depth": f.bit_depth,
                    "sample_rate_hz": f.sample_rate_hz,
                    "size_bytes": f.size_bytes,
                    "lossless": bool(f.lossless),
                }
                file_entries.append(entry)
                csv_rows.append({"group_id": g.id, **entry})
                md_rows.append([
                    fv.verdict.value,
                    fv.dup_type.value,
                    str(Path(f.path).name),
                    f.album or "—",
                    ", ".join(fv.reasons[:3]),
                ])
            group_data.append({
                "group_id": g.id,
                "fingerprint": g.fingerprint[:64],
                "total_files": len(files),
                "wasted_bytes": verdict.wasted_bytes,
                "files": file_entries,
            })
            md_sections.append(
                MarkdownSection(
                    heading=f"Group {g.id} — {_human_size(verdict.wasted_bytes)} wasted",
                    headers=["Verdict", "Type", "File", "Album", "Why"],
                    rows=md_rows,
                    paragraph=f"Fingerprint: `{g.fingerprint[:32]}...`  |  {len(files)} files",
                )
            )

        if fmt == "json":
            exporter.write_json({
                "report_type": "acoustid_duplicates",
                "groups": group_data,
            })
        elif fmt == "csv":
            exporter.write_csv(csv_rows)
        else:
            exporter.write_markdown("AcoustID Duplicate Groups", md_sections)

    def _export_transcodes(self, db: Database, exporter, fmt: str) -> None:
        from soundaudit.db.store import DBFile
        from soundaudit.reporter import MarkdownSection
        with db.session() as s:
            files = (
                s.query(DBFile)
                .filter(DBFile.is_transcode == 1)
                .order_by(DBFile.transcode_confidence.desc())
                .all()
            )
        rows = [
            {
                "file": f.path,
                "confidence": f.transcode_confidence,
                "cutoff_hz": f.spectral_cutoff_hz,
                "reason": f.transcode_reason or "",
                "format": f.format,
                "bit_depth": f.bit_depth,
            }
            for f in files
        ]
        if fmt == "json":
            exporter.write_json({"report_type": "transcodes", "files": rows})
        elif fmt == "csv":
            exporter.write_csv(rows)
        else:
            md_rows = [
                [
                    str(Path(r["file"]).name),
                    f"{r['confidence']:.0%}",
                    f"{r['cutoff_hz']:,}Hz" if r["cutoff_hz"] else "—",
                    r["reason"] or "—",
                ]
                for r in rows
            ] if rows else []
            exporter.write_markdown(
                "Transcode Suspects",
                [MarkdownSection("Suspected Transcodes", ["File", "Confidence", "Cutoff", "Reason"], md_rows)],
            )

    def _export_corrupt(self, db: Database, exporter, fmt: str) -> None:
        from soundaudit.db.store import DBFile
        from soundaudit.reporter import MarkdownSection
        with db.session() as s:
            files = s.query(DBFile).filter(DBFile.is_corrupt == 1).all()
        rows = [{"file": f.path, "reason": f.corruption_reason or "unknown"} for f in files]
        if fmt == "json":
            exporter.write_json({"report_type": "corrupt", "files": rows})
        elif fmt == "csv":
            exporter.write_csv(rows)
        else:
            md_rows = [[r["file"], r["reason"]] for r in rows] if rows else []
            exporter.write_markdown("Corrupt Files", [MarkdownSection("Corrupt / Unreadable Files", ["File", "Reason"], md_rows)])

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


class ExportDialog(ModalScreen[Path | None]):
    """Modal dialog to choose export filename and format."""

    DEFAULT_CSS = """
    #export-dialog {
        width: auto;
        min-width: 50;
        max-width: 90vw;
        height: auto;
        border: solid $primary;
        padding: 0 1;
        background: $surface;
    }
    #export-title {
        text-align: center;
        padding-bottom: 0;
        height: auto;
    }
    #export-hint {
        text-align: center;
        height: auto;
        color: $text-muted;
    }
    #export-input {
        margin: 0 0;
    }
    #export-actions {
        align: center middle;
        height: auto;
        padding: 0 0;
    }
    #export-actions Static {
        width: auto;
        min-width: 10;
        text-align: center;
        padding: 0 1;
    }
    """

    FORMAT_SHORTCUTS: ClassVar[list[str]] = ["JSON", "CSV", "Markdown"]

    def __init__(self, default_name: str) -> None:
        self._default_name = default_name
        self._shortcut_idx = 0
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Container(
            Static("[b]Export Report[/b]", id="export-title"),
            Static("Type filename (.json, .csv, .md) or pick a format", id="export-hint"),
            Input(value=self._default_name, id="export-input"),
            Horizontal(
                Static("▸ JSON", id="fmt-json", classes="nav-item"),
                Static("▸ CSV", id="fmt-csv", classes="nav-item"),
                Static("▸ Markdown", id="fmt-md", classes="nav-item"),
                Static("▸ Save", id="btn-save", classes="nav-item"),
                Static("▸ Cancel", id="btn-cancel", classes="nav-item"),
                id="export-actions",
            ),
            id="export-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#export-input", Input).focus()
        self._focus_shortcut(True)

    def _shortcut_ids(self) -> list[str]:
        return ["fmt-json", "fmt-csv", "fmt-md", "btn-save", "btn-cancel"]

    def _focus_shortcut(self, first: bool = False) -> None:
        for sid in self._shortcut_ids():
            self.query_one(f"#{sid}", Static).remove_class("focused-nav")
        sid = self._shortcut_ids()[self._shortcut_idx]
        node = self.query_one(f"#{sid}", Static)
        node.add_class("focused-nav")
        if not first:
            node.focus()

    def _apply_shortcut(self) -> None:
        sid = self._shortcut_ids()[self._shortcut_idx]
        inp = self.query_one("#export-input", Input)
        if sid == "fmt-json":
            inp.value = self._replace_ext(inp.value, ".json")
        elif sid == "fmt-csv":
            inp.value = self._replace_ext(inp.value, ".csv")
        elif sid == "fmt-md":
            inp.value = self._replace_ext(inp.value, ".md")
        elif sid == "btn-save":
            self._do_save()
        elif sid == "btn-cancel":
            self.dismiss(None)

    @staticmethod
    def _replace_ext(name: str, ext: str) -> str:
        p = Path(name)
        return str(p.with_suffix(ext))

    def _do_save(self) -> None:
        name = self.query_one("#export-input", Input).value.strip()
        if not name:
            self.notify("Filename cannot be empty", severity="error", timeout=3)
            return
        path = Path(name)
        if path.exists():
            self.notify(f"{path.name} already exists", severity="warning", timeout=3)
            return
        self.dismiss(path)

    def on_key(self, event) -> None:
        key = event.key
        if key in ("left", "up", "h", "k"):
            self._shortcut_idx = max(0, self._shortcut_idx - 1)
            self._focus_shortcut()
            event.stop()
        elif key in ("right", "down", "l", "j"):
            self._shortcut_idx = min(len(self._shortcut_ids()) - 1, self._shortcut_idx + 1)
            self._focus_shortcut()
            event.stop()
        elif key in ("enter",):
            self._apply_shortcut()
            event.stop()
        elif key in ("escape", "q"):
            self.dismiss(None)
            event.stop()

    def on_click(self, event) -> None:
        widget_id = getattr(getattr(event, "control", None), "id", None)
        if widget_id in self._shortcut_ids():
            self._shortcut_idx = self._shortcut_ids().index(widget_id)
            self._apply_shortcut()
            event.stop()


class AnalyzerChooseScreen(Screen[dict[str, bool] | None]):
    """Choose which analysis passes to run."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("escape", "cancel", "Back"),
    ]

    OPTION_IDS: ClassVar[list[str]] = ["opt-duplicates", "opt-acoustid", "opt-transcodes"]

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="scan-screen"):
            yield Static("[b]Analyze[/b]  [dim]esc = back[/dim]", id="scan-title")
            yield Static("[dim]Toggle which analyses to run[/dim]", id="path-label")
            with Vertical(id="path-list"):
                yield Static(
                    "[green]✓[/green] Dup Groups     [dim]content-hash duplicates[/dim]",
                    id="opt-duplicates",
                    classes="path-item checked nav-item",
                )
                yield Static(
                    "[red]✗[/red] AcoustID Dups  [dim]fingerprint duplicates[/dim]",
                    id="opt-acoustid",
                    classes="path-item unchecked nav-item",
                )
                yield Static(
                    "[red]✗[/red] Transcodes     [dim]spectral fake-FLAC detection (slow)[/dim]",
                    id="opt-transcodes",
                    classes="path-item unchecked nav-item",
                )
            with Horizontal(id="scan-actions"):
                yield Static("Run", id="btn-run", classes="scan-link")
                yield Static("Back", id="btn-back", classes="scan-link")
        yield Footer()

    def on_mount(self) -> None:
        self._opt_selected: dict[str, bool] = {
            "opt-duplicates": True,
            "opt-acoustid": False,
            "opt-transcodes": False,
        }
        self._opt_focus_idx: int = 0
        self._action_focus_idx: int = 0
        self._focus_group: str = "options"
        self._draw_focus()

    def _draw_focus(self) -> None:
        for oid in self.OPTION_IDS:
            self.query_one(f"#{oid}", Static).remove_class("focused-nav")
        for bid in ("btn-run", "btn-back"):
            self.query_one(f"#{bid}", Static).remove_class("focused-nav")
        if self._focus_group == "options":
            oid = self.OPTION_IDS[self._opt_focus_idx]
            self.query_one(f"#{oid}", Static).add_class("focused-nav")
        else:
            bids = ["btn-run", "btn-back"]
            bid = bids[self._action_focus_idx]
            self.query_one(f"#{bid}", Static).add_class("focused-nav")

    def _toggle_option(self, oid: str) -> None:
        was = self._opt_selected[oid]
        self._opt_selected[oid] = not was
        node = self.query_one(f"#{oid}", Static)
        checked = self._opt_selected[oid]
        # Refresh text
        base = {
            "opt-duplicates": "Dup Groups     [dim]content-hash duplicates[/dim]",
            "opt-acoustid": "AcoustID Dups  [dim]fingerprint duplicates[/dim]",
            "opt-transcodes": "Transcodes     [dim]spectral fake-FLAC detection (slow)[/dim]",
        }[oid]
        if checked:
            node.update(f"[green]✓[/green] {base}")
            node.add_class("checked")
            node.remove_class("unchecked")
        else:
            node.update(f"[red]✗[/red] {base}")
            node.remove_class("checked")
            node.add_class("unchecked")

    def on_key(self, event) -> None:
        key = event.key
        if key in ("up", "k"):
            if self._focus_group == "options":
                self._opt_focus_idx = max(0, self._opt_focus_idx - 1)
            self._focus_group = "options"
            self._draw_focus()
            event.stop()
        elif key in ("down", "j"):
            if self._focus_group == "options":
                self._opt_focus_idx = min(len(self.OPTION_IDS) - 1, self._opt_focus_idx + 1)
            self._focus_group = "options"
            self._draw_focus()
            event.stop()
        elif key in ("tab",):
            self._focus_group = "actions" if self._focus_group == "options" else "options"
            self._draw_focus()
            event.stop()
        elif key in ("enter", "space"):
            if self._focus_group == "options":
                self._toggle_option(self.OPTION_IDS[self._opt_focus_idx])
            else:
                bids = ["btn-run", "btn-back"]
                self._activate_action(bids[self._action_focus_idx])
            event.stop()
        elif key in ("escape", "q"):
            self.app.pop_screen()
            event.stop()

    def _activate_action(self, bid: str) -> None:
        if bid == "btn-run":
            mapping = {
                "opt-duplicates": "duplicates",
                "opt-acoustid": "acoustid",
                "opt-transcodes": "transcodes",
            }
            result = {mapping[k]: v for k, v in self._opt_selected.items()}
            self.app.pop_screen()
            self.app.push_screen(AnalyzerRunScreen(result))
        elif bid == "btn-back":
            self.app.pop_screen()

    def on_click(self, event) -> None:
        widget_id = getattr(getattr(event, "control", None), "id", None)
        if widget_id in self.OPTION_IDS:
            self._toggle_option(widget_id)
        elif widget_id == "btn-run":
            self._activate_action("btn-run")
        elif widget_id == "btn-back":
            self._activate_action("btn-back")


class AnalyzerRunScreen(Screen[None]):
    """Show progress while running selected analysis passes."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("escape", "cancel", "Back"),
    ]

    class RunDone(Message):
        """Emitted when all passes complete."""

        def __init__(self) -> None:
            super().__init__()

    def __init__(self, choices: dict[str, bool]) -> None:
        self._choices = choices
        self._running: bool = False
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="scan-screen"):
            yield Static("[b]Running Analyses[/b]", id="scan-title")
            yield Log(id="scan-log")
            with Horizontal(id="scan-actions"):
                yield Static("Back", id="btn-back", classes="scan-link")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#scan-log", Log).can_focus = False
        self.query_one("#btn-back", Static).can_focus = False
        self.query_one("#btn-back", Static).add_class("dimmed")
        self._running = True
        self.run_worker(self._worker, exclusive=True, thread=True)

    def _log(self, msg: str) -> None:
        app = self.app  # type: ignore[attr-defined]
        app.call_from_thread(
            self.query_one("#scan-log", Log).write_line,
            msg,
        )

    def _worker(self) -> None:
        app = self.app  # type: ignore[attr-defined]
        db_path = app.get_db_path()
        database = Database(db_path)

        if self._choices.get("duplicates"):
            self._log("")
            self._log("[cyan]━ Content-Hash Duplicates ━[/cyan]")
            try:
                analyzer = DuplicateAnalyzer(database)
                groups = find_duplicate_groups(database)
                if groups:
                    write_duplicate_groups(database, groups)
                    total_wasted = sum(r.wasted_bytes for r in groups)
                    self._log(
                        f"Found {len(groups)} groups, "
                        f"{sum(r.file_count for r in groups)} files total. "
                        f"Wasted: {_human_size(total_wasted)}"
                    )
                else:
                    self._log("No duplicates found.")
            except Exception as exc:
                self._log(f"[red]Duplicate analysis failed: {exc}[/red]")

        if self._choices.get("acoustid"):
            self._log("")
            self._log("[cyan]━ AcoustID Duplicates ━[/cyan]")
            try:
                analyzer = AcoustidDuplicateAnalyzer(database)
                groups = analyzer.run()
                if groups:
                    total_wasted = sum(r.wasted_bytes for r in groups)
                    self._log(
                        f"Found {len(groups)} groups, "
                        f"{sum(r.file_count for r in groups)} files total. "
                        f"Wasted: {_human_size(total_wasted)}"
                    )
                else:
                    self._log("No AcoustID duplicates found.")
            except Exception as exc:
                self._log(f"[red]AcoustID analysis failed: {exc}[/red]")

        if self._choices.get("transcodes"):
            self._log("")
            self._log("[cyan]━ Transcode Detection ━[/cyan]")
            self._log("[yellow]This may take several minutes for large libraries...[/yellow]")
            try:
                results = analyze_library_transcodes(
                    database,
                    workers=4,
                    log_callback=self._log,
                )
            except Exception as exc:
                self._log(f"[red]Transcode analysis failed: {exc}[/red]")

        self._log("")
        self._log("[green]All selected analyses complete.[/green]")
        self._running = False
        app.call_from_thread(
            self.query_one("#btn-back", Static).remove_class, "dimmed"
        )

    def on_click(self, event) -> None:
        if not self._running:
            self.app.pop_screen()

    def action_cancel(self) -> None:
        if not self._running:
            self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()
