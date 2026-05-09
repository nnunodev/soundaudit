"""Textual TUI screens."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    ProgressBar,
    RichLog,
    Static,
)

from soundaudit._version import __version__
from soundaudit.actuator.tags import (
    TagWriteError,
    resolved_metadata_to_tags,
    snapshot_tags,
    validate_fields,
    write_tags,
)
from soundaudit.analyzer.acoustid import (
    AcoustidDuplicateAnalyzer,
    DupType,
    analyze_acoustid_keepers,
    find_acoustid_groups,
    write_acoustid_groups,
)
from soundaudit.analyzer.duplicates import (
    DuplicateGroupResult,
    KeeperVerdict,
    _human_size,
    analyze_keepers,
    find_duplicate_groups,
    write_duplicate_groups,
)
from soundaudit.analyzer.transcode import analyze_library_transcodes
from soundaudit.db.store import AcoustidGroup, Database
from soundaudit.models import FileInfo, TrackTags
from soundaudit.resolver.musicbrainz import MusicBrainzResolver
from soundaudit.scanner.walker import discover_files


class DashboardScreen(Screen[None]):
    """Main dashboard with library overview."""

    BINDINGS: ClassVar[Sequence[tuple[str, str, str]]] = [  # type: ignore[assignment]
        ("q", "quit", "Quit"),
        ("s", "scan", "Scan"),
        ("r", "report", "Report"),
        ("f", "fix", "Fix Tags (MB)"),
        ("t", "repair", "Repair Tags"),
        ("o", "organize", "Navidrome Org"),
        ("R", "reset", "Reset DB"),
    ]

    NAV_IDS: ClassVar[list[str]] = [
        "btn-scan", "btn-report", "btn-fix", "btn-repair", "btn-organize", "btn-reset", "btn-quit"
    ]
    _nav_index: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        with Container(id="dashboard"):
            yield Static(
                f"[b]SoundAudit[/b] [dim]v{__version__}[/dim]",
                id="title",
            )
            yield Static("Loading stats...", id="stats")
            yield Static("─" * 40, id="dash-sep")
            with Horizontal(id="actions-row"):
                yield Static("▸ Scan        [dim]s[/dim]", id="btn-scan", classes="nav-item")
                yield Static("▸ Reports     [dim]r[/dim]", id="btn-report", classes="nav-item")
                yield Static("▸ Fix (MB)    [dim]f[/dim]", id="btn-fix", classes="nav-item")
                yield Static("▸ Repair      [dim]t[/dim]", id="btn-repair", classes="nav-item")
                yield Static("▸ Navidrome   [dim]o[/dim]", id="btn-organize", classes="nav-item")
                yield Static("▸ Reset DB    [dim]R[/dim]", id="btn-reset", classes="nav-item")
                yield Static("▸ Quit        [dim]q[/dim]", id="btn-quit", classes="nav-item")
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
        elif wid == "btn-fix":
            self.action_fix()
        elif wid == "btn-repair":
            self.action_repair()
        elif wid == "btn-organize":
            self.action_organize()
        elif wid == "btn-reset":
            self._confirm_reset()
        elif wid == "btn-quit":
            self.action_quit()

    def _refresh_stats(self) -> None:
        stats_widget = self.query_one("#stats", Static)
        app: Any = self.app
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
                    s.query(func.count(DuplicateGroup.id))
                    .filter_by(ignored=0)
                    .scalar()
                    or 0
                )
                dup_groups_ignored = (
                    s.query(func.count(DuplicateGroup.id))
                    .filter_by(ignored=1)
                    .scalar()
                    or 0
                )
                dup_files = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.duplicate_group_id.is_not(None))
                    .scalar()
                    or 0
                )
                acoustid_groups = (
                    s.query(func.count(AcoustidGroup.id))
                    .filter_by(ignored=0)
                    .scalar()
                    or 0
                )
                acoustid_groups_ignored = (
                    s.query(func.count(AcoustidGroup.id))
                    .filter_by(ignored=1)
                    .scalar()
                    or 0
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
                resolved = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.mb_recording_id.is_not(None))
                    .scalar()
                    or 0
                )
                pending = (
                    s.query(func.count(DBFile.id))
                    .filter(
                        DBFile.mb_recording_id.is_not(None),
                        DBFile.tag_fix_date.is_(None),
                    )
                    .scalar()
                    or 0
                )
                fixed = (
                    s.query(func.count(DBFile.id))
                    .filter(DBFile.tag_fix_date.is_not(None))
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
            if dup_groups_ignored:
                lines.append(
                    f"[dim]Ignored[/dim] : {dup_groups_ignored} dup groups"
                )
            if acoustid_groups:
                lines.append(
                    f"[b]AcoustID[/b]: {acoustid_groups} groups, {acoustid_files} files"
                )
            if acoustid_groups_ignored:
                lines.append(
                    f"[dim]Ignored[/dim] : {acoustid_groups_ignored} AcoustID groups"
                )
            if transcode_count:
                style = "[red]" if transcode_high > 0 else "[yellow]"
                lines.append(
                    f"[b]Transc[/b]  : {style}{transcode_count} suspects ({transcode_high} high)[/]"
                )
            if resolved:
                lines.append(
                    f"[b]Resolved[/b]: {resolved} files with MusicBrainz data"
                )
            if pending:
                lines.append(
                    f"[b]Pending[/b] : {pending} files awaiting tag write"
                )
            if fixed:
                lines.append(
                    f"[b]Fixed[/b]   : {fixed} files updated"
                )
            stats_widget.update("\n".join(lines))
            # highlight first nav item on stats load
            self.query_one(f"#{self._nav_sel()}", Static).add_class("focused-nav")
        except Exception as exc:
            import logging
            logging.getLogger("soundaudit.tui").warning("Dashboard stats load failed: %s", exc, exc_info=True)
            stats_widget.update("[red]Error loading database stats.[/red]")

    def on_click(self, event) -> None:
        widget_id = getattr(getattr(event, "control", None), "id", None)
        if widget_id == "btn-scan":
            self.action_scan()
        elif widget_id == "btn-report":
            self.action_report()
        elif widget_id == "btn-fix":
            self.action_fix()
        elif widget_id == "btn-normalize":
            self.action_normalize()
        elif widget_id == "btn-standardize":
            self.action_standardize()
        elif widget_id == "btn-organize":
            self.action_organize()
        elif widget_id == "btn-reset":
            self._confirm_reset()
        elif widget_id == "btn-quit":
            self.action_quit()

    def _confirm_reset(self) -> None:
        self.app.push_screen(ResetConfirmScreen(), self._do_reset)

    def _do_reset(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        app: Any = self.app
        db_path = Path(app.get_db_path())
        if not db_path.exists():
            self.notify("No database found to reset.", severity="warning", timeout=3)
            self._refresh_stats()
            return
        try:
            from soundaudit.db.store import reset_database
            reset_database(str(db_path))
            self.notify("Database reset successfully.", severity="information", timeout=3)
        except PermissionError as exc:
            self.notify(
                f"Database is locked — stop any running scan and try again. ({exc})",
                severity="error",
                timeout=5,
            )
        except Exception as exc:
            self.notify(f"Failed to reset database: {exc}", severity="error", timeout=5)
        self._refresh_stats()

    def action_scan(self) -> None:
        self.app.push_screen("scan")

    def action_report(self) -> None:
        self.app.push_screen("report")

    def action_fix(self) -> None:
        self.app.push_screen(FixTagsScreen())

    def action_repair(self) -> None:
        self.app.push_screen(RepairTagsScreen())

    def action_organize(self) -> None:
        self.app.push_screen("organize")

    def action_reset(self) -> None:
        self._confirm_reset()

    def action_quit(self) -> None:
        self.app.exit()


class ScanScreen(Screen[None]):
    """Scan screen with loading and progress indicators."""

    BINDINGS: ClassVar[Sequence[tuple[str, str, str]]] = [  # type: ignore[assignment]
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
        with Container(id="scan-screen"):
            yield Static("[b]Scan[/b]  [dim]esc = back[/dim]", id="scan-title")
            with Vertical(id="path-list"):
                yield Static("[dim]Paths[/dim]", id="path-label")
            with Horizontal(id="stats-row"):
                yield Static("Found 0", id="stat-found")
                yield Static("Scanned 0", id="stat-scanned")
                yield Static("Skipped 0", id="stat-skipped")
                yield Static("Saved 0", id="stat-saved")
            with Horizontal(id="discovery-row"):
                yield Static("Discovery", id="discovery-label")
                yield ProgressBar(total=100, id="discovery-bar", show_eta=False)
            with Horizontal(id="scanning-row"):
                yield Static("Scanning", id="scanning-label")
                yield ProgressBar(total=100, id="scan-bar", show_eta=False)
            yield Static("", id="current-file")
            yield RichLog(id="scan-log", markup=True)
            with Horizontal(id="scan-actions"):
                yield Static("Start", id="btn-start", classes="scan-link")
                yield Static("Stop", id="btn-stop", classes="scan-link dimmed")
                yield Static("Back", id="btn-back", classes="scan-link")
        yield Footer()

    def on_mount(self) -> None:
        app: Any = self.app
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
        for idx, (full, short) in enumerate(zip(full_paths, short_paths, strict=False)):
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
        # ── Scan mode presets ──
        path_list.mount(Static("", id="sep-presets"))
        preset_ids = ["preset-quick", "preset-standard", "preset-deep"]
        path_list.mount(
            Static(
                "[green]▸[/green] Quick    [dim]dups only[/dim]",
                id="preset-quick",
                classes="path-item checked",
            )
        )
        path_list.mount(
            Static(
                "[dim]▸[/dim] Standard [dim]+ MusicBrainz[/dim]",
                id="preset-standard",
                classes="path-item unchecked",
            )
        )
        path_list.mount(
            Static(
                "[dim]▸[/dim] Deep     [dim]+ AcoustID + transcodes[/dim]",
                id="preset-deep",
                classes="path-item unchecked",
            )
        )
        self._preset_ids = preset_ids
        self._selected_preset = "preset-quick"
        # Append presets to focusable list
        self._path_ids.extend(preset_ids)
        # strip focus from everything non-interactive except log (scrollable)
        self.query_one("#scan-log", RichLog).can_focus = True
        # Use scan-title as keyboard focus anchor (not inside scrollable container)
        self.query_one("#scan-title", Static).can_focus = True
        for sid in ("stat-found", "stat-scanned", "stat-skipped", "stat-saved",
                    "discovery-label", "scanning-label", "current-file", "path-label", "sep-presets"):
            node = self.query_one(f"#{sid}", Static)
            if node:
                node.can_focus = False
        # Make interactive items non-focusable so arrow keys bubble to screen handler
        for pid in self._path_ids:
            self.query_one(f"#{pid}", Static).can_focus = False
        for bid in ("btn-start", "btn-stop", "btn-back"):
            self.query_one(f"#{bid}", Static).can_focus = False
        self.query_one("#scan-title", Static).focus()
        self._draw_focus()
        log = self.query_one("#scan-log", RichLog)
        log.write("Ready. ↑↓ move, Enter/Space toggle, Tab = actions, g = focus log")
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
        pid = self._path_ids[self._path_focus_idx]
        node = self.query_one(f"#{pid}", Static)
        self.query_one("#path-list", Vertical).scroll_to_widget(node)

    def _refocus_action(self, delta: int) -> None:
        self._action_focus_idx = (self._action_focus_idx + delta) % 3
        self._scan_focus_group = "actions"
        self._draw_focus()

    def _toggle_path(self, pid: str) -> None:
        if pid.startswith("preset-"):
            self._select_preset(pid)
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

    def _select_preset(self, pid: str) -> None:
        """Single-select preset: clear others, highlight chosen."""
        if self._selected_preset == pid:
            return
        self._selected_preset = pid
        labels = {
            "preset-quick": "Quick    [dim]dups only[/dim]",
            "preset-standard": "Standard [dim]+ MusicBrainz[/dim]",
            "preset-deep": "Deep     [dim]+ AcoustID + transcodes[/dim]",
        }
        for preset_id in self._preset_ids:
            node = self.query_one(f"#{preset_id}", Static)
            is_active = preset_id == pid
            if is_active:
                node.update(f"[green]▸[/green] {labels[preset_id]}")
                node.add_class("checked")
                node.remove_class("unchecked")
            else:
                node.update(f"[dim]▸[/dim] {labels[preset_id]}")
                node.remove_class("checked")
                node.add_class("unchecked")

    def on_key(self, event) -> None:
        key = event.key
        focused_wid = self.focused.id if self.focused else None
        is_log_focused = focused_wid == "scan-log"

        if is_log_focused and key in ("up", "down", "pageup", "pagedown"):
            log = self.query_one("#scan-log", RichLog)
            if key == "up":
                log.scroll_up()
            elif key == "down":
                log.scroll_down()
            elif key == "pageup":
                log.scroll_page_up()
            elif key == "pagedown":
                log.scroll_page_down()
            event.stop()
            return

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
        elif key == "g":
            self.query_one("#scan-log", RichLog).focus()
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
        if bid == "btn-start" and "dimmed" not in self.query_one("#btn-start", Static).classes:
            self._start_scan()
        elif bid == "btn-stop" and "dimmed" not in self.query_one("#btn-stop", Static).classes:
            self.action_cancel()
        elif bid == "btn-back" and "dimmed" not in self.query_one("#btn-back", Static).classes and not self.is_scanning:
            self.app.pop_screen()

    def watch_files_found(self, value: int) -> None:
        self.query_one("#stat-found", Static).update(f"Found {value:,}")

    def watch_files_scanned(self, value: int) -> None:
        total = max(self.files_found, 1)
        self.query_one("#stat-scanned", Static).update(f"Scanned {value:,}/{total:,}")

    def watch_files_skipped(self, value: int) -> None:
        self.query_one("#stat-skipped", Static).update(f"Skipped {value:,}")

    def watch_files_saved(self, value: int) -> None:
        self.query_one("#stat-saved", Static).update(f"Saved {value:,}")

    def watch_current_file(self, value: str) -> None:
        if not value:
            self.query_one("#current-file", Static).update("")
            return
        # If the entire string is wrapped in a single markup tag, re-wrap
        # the whole line (arrow + text) so the arrow inherits the colour.
        if value.startswith("[") and value.endswith("]") and "[/" in value:
            tag_end = value.index("]")
            tag_name = value[1:tag_end].split()[0]
            close_tag = f"[/{tag_name}]"
            if value.endswith(close_tag):
                inner = value[tag_end + 1 : -len(close_tag)]
                self.query_one("#current-file", Static).update(
                    f"[{tag_name}]▸ {inner}[/{tag_name}]"
                )
                return
        # Plain text: escape brackets so they display literally
        safe = value.replace("[", "\\[").replace("]", "\\]")
        self.query_one("#current-file", Static).update(f"▸ {safe}")

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
            self.query_one("#scan-log", RichLog).write("Cancelling...")
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
            self.query_one("#scan-log", RichLog).write(
                "No paths selected. Toggle at least one."
            )
            return
        # Map selected preset to analysis choices
        preset = getattr(self, "_selected_preset", "preset-quick")
        self._analysis_choices: dict[str, bool] = {
            "preset-quick": {"duplicates": True, "acoustid": False, "transcodes": False, "resolve": False},
            "preset-standard": {"duplicates": True, "acoustid": False, "transcodes": False, "resolve": True},
            "preset-deep": {"duplicates": True, "acoustid": True, "transcodes": True, "resolve": True},
        }.get(preset, {"duplicates": True, "acoustid": False, "transcodes": False, "resolve": False})
        self._selected_paths = selected
        self.files_found = 0
        self.files_scanned = 0
        self.files_skipped = 0
        self.files_saved = 0
        self.current_file = ""
        self.is_scanning = True
        self.query_one("#discovery-bar", ProgressBar).update(progress=0)
        self.query_one("#scan-bar", ProgressBar).update(progress=0)
        log = self.query_one("#scan-log", RichLog)
        log.clear()
        log.write(f"Starting scan of {len(selected)} path(s)...")
        enabled = [k for k, v in self._analysis_choices.items() if v]
        if enabled:
            log.write(f"After-scan analyses: {', '.join(enabled)}")
        self.run_worker(
            self._scan_worker, exclusive=True, thread=True
        )

    def _scan_worker(self) -> None:
        """Background worker that performs the scan."""
        app: Any = self.app
        cfg = app.get_config()
        db_path = app.get_db_path()
        selected_paths: list[str] = getattr(self, "_selected_paths", cfg.scan.paths)
        workers = cfg.scan.workers
        extensions = set(cfg.scan.extensions)

        database = Database(db_path)
        existing = database.get_existing_paths()

        log = self.query_one("#scan-log", RichLog)
        discovery_bar = self.query_one("#discovery-bar", ProgressBar)
        scan_bar = self.query_one("#scan-bar", ProgressBar)

        all_files: list[Path] = []
        skipped = 0

        # Discovery phase
        app.call_from_thread(
            log.write, f"Discovering files in {len(selected_paths)} selected path(s)..."
        )
        total_estimate = 0
        for root_path in selected_paths:
            root = Path(root_path).expanduser().resolve()
            if not root.exists():
                app.call_from_thread(
                    log.write, f"Skipping missing path: {root}"
                )
                continue
            try:
                for p in discover_files(root, extensions):
                    if not self.is_scanning:
                        app.call_from_thread(log.write, "Cancelled during discovery.")
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
                app.call_from_thread(log.write, f"Error in {root}: {exc}")

        app.call_from_thread(setattr, self, "files_found", len(all_files))
        app.call_from_thread(
            discovery_bar.update,
            total=max(len(all_files), 1),
            progress=max(len(all_files), 1),
        )
        app.call_from_thread(
            log.write,
            f"Discovered {len(all_files):,} file(s) total.",
        )

        # Clean up stale DB entries (files removed/moved since last scan)
        discovered_paths = {str(p) for p in all_files}
        stale_paths = set(existing.keys()) - discovered_paths
        if stale_paths:
            removed = database.delete_by_paths(stale_paths)
            app.call_from_thread(
                log.write,
                f"Removed {removed:,} stale database entr{'ies' if removed != 1 else 'y'} (no longer on disk).",
            )

        # Filter unchanged files – compare mtime with 1-second tolerance
        files_to_scan: list[Path] = []
        for p in all_files:
            try:
                file_mtime = os.path.getmtime(p)
                if str(p) in existing and abs(file_mtime - existing[str(p)]) <= 1.0:
                    skipped += 1
                    continue
                files_to_scan.append(p)
            except OSError as exc:
                app.call_from_thread(
                    log.write,
                    f"[WARN] Could not stat {p}: {exc}"
                )

        app.call_from_thread(setattr, self, "files_skipped", skipped)
        app.call_from_thread(
            log.write,
            f"Skipped {skipped:,} unchanged file{'' if skipped == 1 else 's'}.",
        )

        total_to_scan = len(files_to_scan)
        choices: dict[str, bool] = getattr(self, "_analysis_choices", {"duplicates": True})
        has_resolve = choices.get("resolve", False)
        other_phases = [k for k, v in choices.items() if v and k != "resolve"]
        total_phases = total_to_scan + len(other_phases)

        if total_to_scan == 0 and not other_phases and not has_resolve:
            app.call_from_thread(log.write, "No new or changed files to scan.")
            app.call_from_thread(setattr, self, "is_scanning", False)
            app.call_from_thread(self.post_message, self.ScanComplete(saved=0))
            return

        app.call_from_thread(log.write, f"Scanning {total_to_scan:,} file(s)...")
        app.call_from_thread(scan_bar.update, total=max(total_phases, 1))

        saved = 0
        corrupt_count = 0

        def process(p: Path) -> FileInfo:
            from soundaudit.scanner.extractor import extract_file_info
            return extract_file_info(
                p,
                fingerprint=False,
                fpcalc_path=cfg.fingerprinting.fpcalc_path,
            )

        with ThreadPoolExecutor(max_workers=min(workers, 16)) as pool:
            log_batch: list[str] = []
            for i, info in enumerate(pool.map(process, files_to_scan)):
                if not self.is_scanning:
                    if log_batch:
                        app.call_from_thread(self._write_log_lines, log_batch[:])
                        log_batch.clear()
                    app.call_from_thread(log.write, "Cancelled.")
                    break
                database.upsert_file(info)
                saved += 1
                app.call_from_thread(setattr, self, "files_saved", saved)
                if info.is_corrupt:
                    corrupt_count += 1
                    app.call_from_thread(
                        log.write,
                        f"[CORRUPT] {info.path}",
                    )
                    if info.corruption_reason:
                        app.call_from_thread(
                            log.write,
                            f"  reason: {info.corruption_reason}",
                        )
                else:
                    log_batch.append(Path(info.path).name)
                    if len(log_batch) >= 20:
                        app.call_from_thread(self._write_log_lines, log_batch[:])
                        log_batch.clear()
                app.call_from_thread(setattr, self, "files_scanned", i + 1)
                app.call_from_thread(scan_bar.advance, 1)
                app.call_from_thread(setattr, self, "current_file", Path(info.path).name)

            if log_batch:
                app.call_from_thread(self._write_log_lines, log_batch[:])
                log_batch.clear()

        app.call_from_thread(
            log.write,
            f"Done. Saved {saved:,} file(s) — {saved - corrupt_count:,} valid, {corrupt_count:,} corrupt.",
        )

        # Run duplicate analysis after scan
        if choices.get("duplicates", True):
            try:
                app.call_from_thread(setattr, self, "current_file", "Analyzing content-hash duplicates...")
                app.call_from_thread(log.write, "▸ Analyzing content-hash duplicates...")
                groups = find_duplicate_groups(database)
                if groups:
                    write_duplicate_groups(database, groups)
                    total_wasted = sum(r.wasted_bytes for r in groups)
                    app.call_from_thread(
                        log.write,
                        f"  Found {len(groups)} duplicate groups ({sum(r.file_count for r in groups)} files). "
                        f"Wasted: {_human_size(total_wasted)}.",
                    )
                else:
                    app.call_from_thread(log.write, "  No content-hash duplicates found.")
            except Exception as exc:
                app.call_from_thread(log.write, f"ERROR: Duplicate analysis failed: {exc}")
            finally:
                app.call_from_thread(scan_bar.advance, 1)

        if choices.get("acoustid"):
            try:
                app.call_from_thread(setattr, self, "current_file", "Analyzing AcoustID duplicates...")
                app.call_from_thread(log.write, "▸ Analyzing AcoustID duplicates...")
                ac_groups = find_acoustid_groups(database)
                if ac_groups:
                    write_acoustid_groups(database, ac_groups)
                    total_wasted = sum(r.wasted_bytes for r in ac_groups)
                    app.call_from_thread(
                        log.write,
                        f"  Found {len(ac_groups)} AcoustID groups ({sum(r.file_count for r in ac_groups)} files). "
                        f"Wasted: {_human_size(total_wasted)}.",
                    )
                else:
                    app.call_from_thread(log.write, "  No AcoustID duplicates found.")
            except Exception as exc:
                app.call_from_thread(log.write, f"ERROR: AcoustID analysis failed: {exc}")
            finally:
                app.call_from_thread(scan_bar.advance, 1)

        if choices.get("transcodes"):
            try:
                app.call_from_thread(setattr, self, "current_file", "Analyzing transcodes...")
                app.call_from_thread(log.write, "▸ Analyzing transcodes...")
                from soundaudit.analyzer.transcode import analyze_library_transcodes
                def _tc_log(msg: str) -> None:
                    app.call_from_thread(log.write, f"  {msg}")

                def _on_tc_total(total: int) -> None:
                    app.call_from_thread(
                        self.query_one("#scanning-label", Static).update,
                        f"Transcodes {total:,}"
                    )
                    app.call_from_thread(scan_bar.update, total=max(total, 1), progress=0)

                analyze_library_transcodes(
                    database,
                    workers=4,
                    log_callback=_tc_log,
                    on_total_known=_on_tc_total,
                    per_item_callback=lambda: app.call_from_thread(scan_bar.advance, 1),
                )
            except Exception as exc:
                app.call_from_thread(log.write, f"ERROR: Transcode analysis failed: {exc}")
            finally:
                app.call_from_thread(
                    self.query_one("#scanning-label", Static).update,
                    "Scanning"
                )

        if choices.get("resolve"):
            try:
                app.call_from_thread(log.write, "▸ Resolving MusicBrainz metadata...")
                app.call_from_thread(
                    setattr, self, "current_file", "Resolving MusicBrainz metadata..."
                )
                resolver = MusicBrainzResolver(
                    database,
                    cfg.resolvers,
                    cfg.fingerprinting,
                )

                def _resolve_progress(msg: str) -> None:
                    if msg.startswith("  "):
                        # Failure or summary line → log only
                        app.call_from_thread(log.write, msg)
                    elif msg.startswith("Resolving ") or msg.startswith("Done."):
                        # Header / footer → log only
                        app.call_from_thread(log.write, f"  {msg}")
                    else:
                        # Plain filename → yellow label only (no log spam)
                        short = msg if len(msg) <= 55 else msg[:52] + "..."
                        app.call_from_thread(setattr, self, "current_file", short)

                def _on_resolve_total(total: int) -> None:
                    self._resolve_total = total
                    self._resolve_done = 0
                    app.call_from_thread(
                        self.query_one("#scanning-label", Static).update,
                        f"Resolving 0/{total:,}"
                    )
                    app.call_from_thread(scan_bar.update, total=max(total, 1), progress=0)

                def _per_item() -> None:
                    self._resolve_done += 1
                    total = getattr(self, "_resolve_total", 1)
                    app.call_from_thread(
                        self.query_one("#scanning-label", Static).update,
                        f"Resolving {self._resolve_done}/{total:,}"
                    )
                    app.call_from_thread(scan_bar.advance, 1)

                results = resolver.resolve_library(
                    dry_run=False,
                    force=False,
                    progress_callback=_resolve_progress,
                    on_total_known=_on_resolve_total,
                    per_item_callback=_per_item,
                )
                if results:
                    app.call_from_thread(
                        log.write,
                        f"  Resolved {len(results)} file(s) via MusicBrainz.",
                    )
                else:
                    app.call_from_thread(log.write, "  No new files resolved.")
                app.call_from_thread(setattr, self, "current_file", "")
            except Exception as exc:
                app.call_from_thread(log.write, f"ERROR: MusicBrainz resolution failed: {exc}")
                app.call_from_thread(setattr, self, "current_file", "")
            finally:
                app.call_from_thread(
                    self.query_one("#scanning-label", Static).update,
                    "Scanning"
                )

        app.call_from_thread(setattr, self, "current_file", "[green]Scan complete[/green]")
        app.call_from_thread(setattr, self, "is_scanning", False)
        app.call_from_thread(self.post_message, self.ScanComplete(saved=saved))

    def _write_log_lines(self, lines: list[str]) -> None:
        log = self.query_one("#scan-log", RichLog)
        for line in lines:
            log.write(line)

    def on_scan_screen_scan_complete(self, event: ScanComplete) -> None:
        if event.saved > 0:
            self.query_one("#scan-log", RichLog).write(
                f"Scan complete. {event.saved:,} file(s) saved."
            )
        else:
            self.query_one("#scan-log", RichLog).write(
                "Scan complete — nothing new to save."
            )


class ReportScreen(Screen[None]):
    """Report screen showing detailed reports."""

    BINDINGS: ClassVar[Sequence[tuple[str, str, str]]] = [  # type: ignore[assignment]
        ("q", "quit", "Quit"),
        ("escape", "back", "Back"),
        ("s", "export", "Export"),
        ("d", "delete_current", "Delete group"),
        ("D", "delete_all", "Delete all marked"),
        ("i", "ignore_group", "Ignore group"),
    ]

    TAB_IDS: ClassVar[list[str]] = [
        "tab-summary", "tab-missing", "tab-tagstatus", "tab-duplicates", "tab-acoustid", "tab-transcodes", "tab-corrupt"
    ]
    _tab_index: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        with Container(id="report-screen"):
            yield Static("[b]Reports[/b]", id="report-title")
            with Horizontal(id="report-tabs"):
                yield Static("Summary", id="tab-summary", classes="tab-link active-tab")
                yield Static("Missing Tags", id="tab-missing", classes="tab-link")
                yield Static("Tag Status", id="tab-tagstatus", classes="tab-link")
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

        if key == "tab":
            if is_table_focused:
                self._tab_focus()
            else:
                self.query_one("#report-table", DataTable).focus()
            event.stop()
            return

        if key in ("left", "right", "h", "l"):
            self._tab_move(-1 if key in ("left", "h") else 1)
        elif key in ("up", "down", "k", "j") and not is_table_focused:
            self._tab_move(-1 if key in ("up", "k") else 1)
        elif key in ("enter", "space"):
            if is_table_focused:
                return  # let DataTable handle it
            self._tab_activate()
        elif key == "d":
            if self.TAB_IDS[self._tab_index] == "tab-corrupt":
                self._delete_corrupt_tab()
            elif self.TAB_IDS[self._tab_index] in ("tab-duplicates", "tab-acoustid"):
                self._delete_current_group()
            event.stop()
            return
        elif key == "D":
            if self.TAB_IDS[self._tab_index] in ("tab-duplicates", "tab-acoustid"):
                self._delete_all_duplicates()
            event.stop()
            return
        elif key == "i":
            self._ignore_current_group()
            event.stop()
            return
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
        elif tid == "tab-tagstatus":
            self._activate_tab("tab-tagstatus")
            self._load_tag_status()
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
            elif widget_id == "tab-tagstatus":
                self._load_tag_status()
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
        app: Any = self.app
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
        app: Any = self.app
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
            for f, sp in zip(files, short_paths, strict=False):
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

    def _load_tag_status(self) -> None:
        table = self.query_one("#report-table", DataTable)
        table.clear(columns=True)
        table.add_columns("File", "MB Title", "Current Title", "Status")
        app: Any = self.app
        try:
            database = Database(app.get_db_path())
            from soundaudit.db.store import DBFile
            with database.session() as s:
                files = (
                    s.query(DBFile)
                    .filter(DBFile.mb_recording_id.is_not(None))
                    .order_by(DBFile.path)
                    .limit(200)
                    .all()
                )
            paths = [f.path for f in files]
            short_paths = self._shorten_paths(paths)
            for f, sp in zip(files, short_paths, strict=False):
                status = "Fixed" if f.tag_fix_date else "Pending"
                style = "[green]" if f.tag_fix_date else "[yellow]"
                table.add_row(
                    sp,
                    f.mb_title or "—",
                    f.title or "—",
                    f"{style}{status}[/]",
                )
        except Exception:
            table.add_row("Error", "Could not load database", "", "")

    def _delete_corrupt_tab(self) -> None:
        app: Any = self.app
        try:
            db = Database(app.get_db_path())
            from soundaudit.db.store import DBFile
            with db.session() as s:
                files = s.query(DBFile).filter(DBFile.is_corrupt == 1).all()
            if not files:
                self.notify("No corrupt files to delete.", severity="warning", timeout=3)
                return
            deleted = 0
            errors = 0
            for f in files:
                p = Path(f.path)
                try:
                    p.unlink()
                    deleted += 1
                except OSError:
                    errors += 1
            db.delete_by_paths({f.path for f in files})
            self._load_corrupt()
            self.notify(f"Deleted {deleted} corrupt file(s).", severity="information", timeout=3)
        except Exception as exc:
            self.notify(f"Failed to delete corrupt files: {exc}", severity="error", timeout=5)

    def _load_corrupt(self) -> None:
        table = self.query_one("#report-table", DataTable)
        table.clear(columns=True)
        table.add_columns("File", "Reason")
        app: Any = self.app
        try:
            database = Database(app.get_db_path())
            from soundaudit.db.store import DBFile
            with database.session() as s:
                files = s.query(DBFile).filter(DBFile.is_corrupt == 1).all()
            if files:
                self.notify(f"{len(files)} corrupt file(s). Press [b]d[/b] to delete.", timeout=3)
            paths = [f.path for f in files]
            short_paths = self._shorten_paths(paths)
            for f, sp in zip(files, short_paths, strict=False):
                table.add_row(sp, f.corruption_reason or "unknown")

            if not files:
                table.add_row("No corrupt files found", "")
        except Exception:
            table.add_row("Error", "Could not load database")

    def _load_duplicates(self) -> None:
        table = self.query_one("#report-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Verdict", "File", "Album", "Tech", "Why")
        self._dup_group_rows: dict[int, int] = {}
        app: Any = self.app
        try:
            database = Database(app.get_db_path())
            from soundaudit.db.store import DBFile, DuplicateGroup
            with database.session() as s:
                groups = s.query(DuplicateGroup).filter_by(ignored=0).all()
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
                row_idx = table.row_count
                self._dup_group_rows[row_idx] = db_group.id
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
                    )
        except Exception:
            table.add_row("Error", "Could not load duplicates", "", "", "")

    def _load_acoustid(self) -> None:
        table = self.query_one("#report-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Verdict", "Type", "File", "Album", "Why")
        self._acoustid_group_rows: dict[int, int] = {}
        app: Any = self.app
        try:
            database = Database(app.get_db_path())
            from soundaudit.db.store import AcoustidGroup, DBFile
            with database.session() as s:
                groups = s.query(AcoustidGroup).filter_by(ignored=0).all()
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
                bfb_count = sum(1 for v in verdict.file_verdicts if v.dup_type == DupType.BIT_FOR_BIT.value)
                trans_count = len(verdict.file_verdicts) - bfb_count
                dup_summary_parts: list[str] = []
                if bfb_count > 1:
                    dup_summary_parts.append(f"{bfb_count} bit-for-bit")
                if trans_count > 0:
                    dup_summary_parts.append(f"{trans_count} transcode")

                row_idx = table.row_count
                self._acoustid_group_rows[row_idx] = db_group.id
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
                        DupType.BIT_FOR_BIT.value: "green",
                        DupType.TRANSCODE.value: "yellow",
                    }.get(fv.dup_type, "white")

                    path = str(Path(f.path).name) if len(files) > 3 else str(Path(f.path))
                    album = f.album or ("Single" if "single" in f.path.lower().split(os.sep) else "—")
                    table.add_row(
                        Text(str(fv.verdict), style=f"bold {row_style}"),
                        Text(str(fv.dup_type), style=type_style),
                        path,
                        album,
                        ", ".join(fv.reasons[:3]),
                    )
        except Exception:
            table.add_row("Error", "", "Could not load AcoustID duplicates", "", "")

    def _load_transcodes(self) -> None:
        table = self.query_one("#report-table", DataTable)
        table.clear(columns=True)
        table.add_columns("File", "Confidence", "Cutoff", "Reason")
        app: Any = self.app
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
            for f, sp in zip(files, short_paths, strict=False):
                conf_style = {
                    (0.7, 1.0): "[bold red]",
                    (0.4, 0.7): "[yellow]",
                }
                style = "[dim]"
                for (lo, hi), style_code in conf_style.items():
                    if lo <= f.transcode_confidence <= hi:
                        style = style_code
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

    def _ignore_current_group(self) -> None:
        """Mark the duplicate group under the cursor as ignored."""
        tab = self.TAB_IDS[self._tab_index]
        if tab not in ("tab-duplicates", "tab-acoustid"):
            self.notify("Ignore only works on Duplicates or AcoustID Dups tabs.", severity="warning", timeout=3)
            return

        table = self.query_one("#report-table", DataTable)
        cursor_row = table.cursor_coordinate.row
        mapping = self._dup_group_rows if tab == "tab-duplicates" else self._acoustid_group_rows

        group_id = None
        for row_idx, gid in sorted(mapping.items(), reverse=True):
            if row_idx <= cursor_row:
                group_id = gid
                break

        if group_id is None:
            self.notify("Move cursor to a duplicate group header and press [b]i[/b].", severity="warning", timeout=3)
            return

        app: Any = self.app
        db = Database(app.get_db_path())
        if tab == "tab-duplicates":
            db.ignore_duplicate_group(group_id)
            self._load_duplicates()
        else:
            db.ignore_acoustid_group(group_id)
            self._load_acoustid()

        self.notify(f"Group {group_id} ignored — both files will be kept.", severity="information", timeout=3)

    def _delete_current_group(self) -> None:
        """Delete files marked DELETE in the duplicate group under the cursor."""
        tab = self.TAB_IDS[self._tab_index]
        if tab not in ("tab-duplicates", "tab-acoustid"):
            self.notify("Delete only works on Duplicates or AcoustID Dups tabs.", severity="warning", timeout=3)
            return

        table = self.query_one("#report-table", DataTable)
        cursor_row = table.cursor_coordinate.row
        mapping = self._dup_group_rows if tab == "tab-duplicates" else self._acoustid_group_rows

        group_id = None
        for row_idx, gid in sorted(mapping.items(), reverse=True):
            if row_idx <= cursor_row:
                group_id = gid
                break

        if group_id is None:
            self.notify("Move cursor to a duplicate group and press [b]d[/b].", severity="warning", timeout=3)
            return

        # Dry-run to get preview
        from soundaudit.analyzer.acoustid import delete_acoustid_group_files
        from soundaudit.analyzer.duplicates import delete_duplicate_group_files

        app: Any = self.app
        db = Database(app.get_db_path())
        if tab == "tab-duplicates":
            to_delete, errors, _ = delete_duplicate_group_files(db, group_id, dry_run=True)
        else:
            to_delete, errors, _ = delete_acoustid_group_files(db, group_id, dry_run=True)

        if not to_delete:
            self.notify("No files marked DELETE in this group.", severity="information", timeout=3)
            return

        preview = "\n".join(f"  • {Path(p).name}" for p in to_delete[:5])
        if len(to_delete) > 5:
            preview += f"\n  ... and {len(to_delete) - 5} more"
        msg = f"Delete {len(to_delete)} file(s) from Group {group_id}?\n{preview}"

        def _do_delete(confirmed: bool | None) -> None:
            if not confirmed:
                return
            if tab == "tab-duplicates":
                deleted, errs, _ = delete_duplicate_group_files(db, group_id, dry_run=False)
                self._load_duplicates()
            else:
                deleted, errs, _ = delete_acoustid_group_files(db, group_id, dry_run=False)
                self._load_acoustid()
            self.notify(
                f"Deleted {len(deleted)} file(s). {len(errs)} errors.",
                severity="information" if not errs else "warning",
                timeout=3,
            )

        self.app.push_screen(ConfirmDialog("Delete Group Files", msg, confirm_label="Delete"), _do_delete)

    def _delete_all_duplicates(self) -> None:
        """Delete all files marked DELETE across every non-ignored group."""
        tab = self.TAB_IDS[self._tab_index]
        if tab not in ("tab-duplicates", "tab-acoustid"):
            return

        from soundaudit.analyzer.duplicates import delete_all_marked_group_files

        app: Any = self.app
        db = Database(app.get_db_path())
        to_delete, errors, total_bytes = delete_all_marked_group_files(
            db, dry_run=True, include_acoustid=True
        )

        if not to_delete:
            self.notify("No files marked DELETE across any groups.", severity="information", timeout=3)
            return

        def _do_delete_all(confirmed: bool | None) -> None:
            if not confirmed:
                return
            deleted, errs, _ = delete_all_marked_group_files(
                db, dry_run=False, include_acoustid=True
            )
            self._load_duplicates()
            self._load_acoustid()
            self.notify(
                f"Deleted {len(deleted)} file(s). {len(errs)} errors.",
                severity="information" if not errs else "warning",
                timeout=4,
            )

        msg = (
            f"Delete {len(to_delete)} file(s) across all groups?\n"
            f"This will free ~{_human_size(total_bytes)}."
        )
        self.app.push_screen(ConfirmDialog("Delete All Marked Duplicates", msg, confirm_label="Delete All"), _do_delete_all)

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
        app: Any = self.app
        db_path = app.get_db_path()
        try:
            from soundaudit.db.store import Database
            from soundaudit.reporter import ReportExporter, infer_format
            db = Database(db_path)
            fmt = infer_format(path)
            exporter = ReportExporter(path)

            if tab == "tab-summary":
                self._export_summary(db, exporter, fmt)
            elif tab == "tab-missing":
                self._export_missing_tags(db, exporter, fmt)
            elif tab == "tab-tagstatus":
                self._export_tag_status(db, exporter, fmt)
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

    def _export_tag_status(self, db: Database, exporter, fmt: str) -> None:
        from soundaudit.db.store import DBFile
        from soundaudit.reporter import MarkdownSection
        with db.session() as s:
            files = s.query(DBFile).filter(DBFile.mb_recording_id.is_not(None)).order_by(DBFile.path).all()
        rows = [{
            "file": f.path,
            "mb_title": f.mb_title or "",
            "current_title": f.title or "",
            "status": "Fixed" if f.tag_fix_date else "Pending",
        } for f in files]
        if fmt == "json":
            exporter.write_json({"report_type": "tag_status", "files": rows})
        elif fmt == "csv":
            exporter.write_csv(rows)
        else:
            md_rows = [[r["file"], r["mb_title"], r["current_title"], r["status"]] for r in rows] if rows else []
            exporter.write_markdown("Tag Status", [MarkdownSection("Tag Write Status", ["File", "MB Title", "Current Title", "Status"], md_rows)])

    def _export_duplicates(self, db: Database, exporter, fmt: str) -> None:
        from soundaudit.analyzer.duplicates import DuplicateGroupResult, analyze_keepers
        from soundaudit.db.store import DBFile, DuplicateGroup
        from soundaudit.reporter import MarkdownSection
        with db.session() as s:
            groups = s.query(DuplicateGroup).filter_by(ignored=0).all()
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
        from soundaudit.analyzer.acoustid import analyze_acoustid_keepers
        from soundaudit.analyzer.duplicates import DuplicateGroupResult
        from soundaudit.db.store import AcoustidGroup, DBFile
        from soundaudit.reporter import MarkdownSection

        with db.session() as s:
            groups = s.query(AcoustidGroup).filter_by(ignored=0).all()

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
                    "verdict": str(fv.verdict),
                    "dup_type": str(fv.dup_type),
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
                    str(fv.verdict),
                    str(fv.dup_type),
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
                    str(Path(cast(str, r["file"])).name),
                    f"{r['confidence']:.0%}",
                    f"{r['cutoff_hz']:,}Hz" if r["cutoff_hz"] else "—",
                    r["reason"] or "—",
                ]
                for r in rows
            ] if rows else []
            exporter.write_markdown(
                "Transcode Suspects",
                [MarkdownSection("Suspected Transcodes", ["File", "Confidence", "Cutoff", "Reason"], cast(list[list[str]], md_rows))],
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


class ConfirmDialog(ModalScreen[bool]):
    """Generic confirmation dialog with title, message, and Confirm/Cancel buttons."""

    NAV_IDS: ClassVar[list[str]] = ["btn-cancel", "btn-confirm"]
    _nav_index: reactive[int] = reactive(0)

    def __init__(self, title: str, message: str, *, confirm_label: str = "Confirm") -> None:
        self._title = title
        self._message = message
        self._confirm_label = confirm_label
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Container(
            Static(f"[b]{self._title}[/b]", id="cd-title"),
            Static(self._message, id="cd-message"),
            Horizontal(
                Static("▸ Cancel", id="btn-cancel", classes="nav-item"),
                Static(f"▸ {self._confirm_label}", id="btn-confirm", classes="nav-item"),
                id="cd-actions",
            ),
            id="cd-dialog",
        )

    def on_mount(self) -> None:
        self._focus_nav()

    def _focus_nav(self) -> None:
        sel = self.NAV_IDS[self._nav_index]
        self.query_one(f"#{sel}", Static).add_class("focused-nav").focus()

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
        self.dismiss(sel == "btn-confirm")

    def on_click(self, event) -> None:
        widget_id = getattr(getattr(event, "control", None), "id", None)
        if widget_id == "btn-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)


class AnalyzerChooseScreen(Screen[dict[str, bool] | None]):
    """Choose which analysis passes to run."""

    BINDINGS: ClassVar[Sequence[tuple[str, str, str]]] = [  # type: ignore[assignment]
        ("q", "quit", "Quit"),
        ("escape", "cancel", "Back"),
    ]

    OPTION_IDS: ClassVar[list[str]] = ["opt-duplicates", "opt-acoustid", "opt-transcodes"]

    def compose(self) -> ComposeResult:
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
        for oid in self.OPTION_IDS:
            self.query_one(f"#{oid}", Static).can_focus = False
        for bid in ("btn-run", "btn-back"):
            self.query_one(f"#{bid}", Static).can_focus = False
        self.query_one("#scan-title", Static).can_focus = True
        self.query_one("#scan-title", Static).focus()
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

    BINDINGS: ClassVar[Sequence[tuple[str, str, str]]] = [  # type: ignore[assignment]
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
        with Container(id="scan-screen"):
            yield Static("[b]Running Analyses[/b]", id="scan-title")
            yield RichLog(id="scan-log", markup=True)
            with Horizontal(id="scan-actions"):
                yield Static("Back", id="btn-back", classes="scan-link")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#scan-log", RichLog).can_focus = False
        self.query_one("#btn-back", Static).can_focus = False
        self.query_one("#btn-back", Static).add_class("dimmed")
        self._running = True
        self.run_worker(self._worker, exclusive=True, thread=True)

    def _log(self, msg: str) -> None:
        app: Any = self.app
        app.call_from_thread(
            self.query_one("#scan-log", RichLog).write,
            msg,
        )

    def _worker(self) -> None:
        app: Any = self.app
        db_path = app.get_db_path()
        database = Database(db_path)

        if self._choices.get("duplicates"):
            self._log("")
            self._log("[cyan]━ Content-Hash Duplicates ━[/cyan]")
            try:
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
                ac_analyzer = AcoustidDuplicateAnalyzer(database)
                groups = ac_analyzer.run()
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
                analyze_library_transcodes(
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


class FixTagsScreen(Screen[None]):
    """Choose paths, fields, and run tag fixer."""

    BINDINGS: ClassVar[Sequence[tuple[str, str, str]]] = [  # type: ignore[assignment]
        ("q", "quit", "Quit"),
        ("escape", "cancel", "Back"),
    ]

    FIELD_MAP: ClassVar[dict[str, str]] = {
        "fld-title": "title",
        "fld-artist": "artist",
        "fld-album": "album",
        "fld-album_artist": "album_artist",
        "fld-year": "year",
        "fld-genre": "genre",
        "fld-track_number": "track_number",
        "fld-track_total": "track_total",
        "fld-disc_number": "disc_number",
        "fld-disc_total": "disc_total",
        "fld-isrc": "isrc",
    }

    def compose(self) -> ComposeResult:
        with Container(id="scan-screen"):
            yield Static("[b]Fix Tags[/b]  [dim]esc = back[/dim]", id="scan-title")
            yield Static("[dim]Select paths and fields to update[/dim]", id="path-label")
            yield Static("Resolved files in selection: —", id="fix-count")
            with Vertical(id="path-list"):
                pass  # paths mounted dynamically
            with Horizontal(id="scan-actions"):
                yield Static("Run", id="btn-run", classes="scan-link")
                yield Static("Back", id="btn-back", classes="scan-link")
        yield Footer()

    def on_mount(self) -> None:
        app: Any = self.app
        cfg = app.get_config()
        full_paths = [str(p) for p in cfg.scan.paths]
        short_paths = ReportScreen._shorten_paths(full_paths)

        self._path_map: dict[str, tuple[str, str]] = {}
        self._path_selected: dict[str, bool] = {}
        self._path_ids: list[str] = []

        path_list = self.query_one("#path-list", Vertical)

        # Mount paths
        for idx, (full, short) in enumerate(zip(full_paths, short_paths, strict=False)):
            pid = f"fix-path-{idx}"
            self._path_map[pid] = (full, short)
            self._path_selected[pid] = True
            self._path_ids.append(pid)
            path_list.mount(
                Static(
                    f"[green]✓[/green] {short}",
                    id=pid,
                    classes="path-item checked nav-item",
                )
            )

        # ── Tag preset modes ──
        path_list.mount(Static("", id="sep-presets"))
        self._preset_ids = ["preset-core", "preset-full", "preset-all"]
        self._selected_preset = "preset-core"
        path_list.mount(
            Static(
                "[green]▸[/green] Core  [dim]title, artist, album, year[/dim]",
                id="preset-core",
                classes="path-item checked nav-item",
            )
        )
        path_list.mount(
            Static(
                "[dim]▸[/dim] Full  [dim]+ track, disc, album artist[/dim]",
                id="preset-full",
                classes="path-item unchecked nav-item",
            )
        )
        path_list.mount(
            Static(
                "[dim]▸[/dim] All   [dim]+ genre, ISRC[/dim]",
                id="preset-all",
                classes="path-item unchecked nav-item",
            )
        )

        # Separator
        path_list.mount(Static("", id="sep-options"))

        # Options
        self._opt_ids = ["opt-dryrun", "opt-backup"]
        path_list.mount(
            Static(
                "[green]✓[/green] Dry-run  [dim]preview only[/dim]",
                id="opt-dryrun",
                classes="path-item checked nav-item",
            )
        )
        path_list.mount(
            Static(
                "[green]✓[/green] Backup   [dim]save originals to DB[/dim]",
                id="opt-backup",
                classes="path-item checked nav-item",
            )
        )

        self._focus_ids: list[str] = [*self._path_ids, *self._preset_ids, *self._opt_ids, "btn-run", "btn-back"]
        self._focus_idx: int = 0
        self._focus_group: str = "list"

        self._opt_selected: dict[str, bool] = {
            "opt-dryrun": True,
            "opt-backup": True,
        }

        for fid in self._focus_ids:
            self.query_one(f"#{fid}", Static).can_focus = False
        self.query_one("#scan-title", Static).can_focus = True
        self.query_one("#scan-title", Static).focus()
        self._draw_focus()
        self._refresh_count()

    def _refresh_count(self) -> None:
        app: Any = self.app
        selected_paths = [
            self._path_map[pid][0]
            for pid, sel in self._path_selected.items()
            if sel
        ]
        if not selected_paths:
            self.query_one("#fix-count", Static).update(
                "Resolved files in selection: [yellow]0[/yellow]"
            )
            return
        try:
            db = Database(app.get_db_path())
            from sqlalchemy import func

            from soundaudit.db.store import DBFile
            with db.session() as s:
                q = s.query(func.count(DBFile.id)).filter(
                    DBFile.mb_recording_id.is_not(None)
                )
                # build OR filters for path prefixes
                from sqlalchemy import or_
                q = q.filter(or_(*[DBFile.path.startswith(p) for p in selected_paths]))
                count = q.scalar() or 0
            self.query_one("#fix-count", Static).update(
                f"Resolved files in selection: [b]{count:,}[/b]"
            )
        except Exception:
            self.query_one("#fix-count", Static).update(
                "Resolved files in selection: [red]—[/red]"
            )

    def _draw_focus(self) -> None:
        for fid in self._focus_ids:
            self.query_one(f"#{fid}", Static).remove_class("focused-nav")
        current = self._focus_ids[self._focus_idx]
        self.query_one(f"#{current}", Static).add_class("focused-nav")
        self.query_one("#path-list", Vertical).scroll_to_widget(self.query_one(f"#{current}", Static))

    def _toggle_item(self, fid: str) -> None:
        if fid.startswith("fix-path-"):
            was = self._path_selected[fid]
            self._path_selected[fid] = not was
            checked = self._path_selected[fid]
            node = self.query_one(f"#{fid}", Static)
            _, short = self._path_map[fid]
            if checked:
                node.update(f"[green]✓[/green] [b]{short}[/b]")
                node.add_class("checked")
                node.remove_class("unchecked")
            else:
                node.update(f"[red]✗[/red] [dim]{short}[/dim]")
                node.remove_class("checked")
                node.add_class("unchecked")
            self._refresh_count()
            return

        if fid.startswith("preset-"):
            self._select_preset(fid)
            return

        # option toggle (dry-run / backup)
        was = self._opt_selected[fid]
        self._opt_selected[fid] = not was
        checked = self._opt_selected[fid]
        node = self.query_one(f"#{fid}", Static)
        label = {
            "opt-dryrun": "Dry-run  [dim]preview only[/dim]",
            "opt-backup": "Backup   [dim]save originals to DB[/dim]",
        }.get(fid, "")
        if checked:
            node.update(f"[green]✓[/green] {label}")
            node.add_class("checked")
            node.remove_class("unchecked")
        else:
            node.update(f"[red]✗[/red] {label}")
            node.remove_class("checked")
            node.add_class("unchecked")

    def _select_preset(self, pid: str) -> None:
        if self._selected_preset == pid:
            return
        self._selected_preset = pid
        labels = {
            "preset-core": "Core  [dim]title, artist, album, year[/dim]",
            "preset-full": "Full  [dim]+ track, disc, album artist[/dim]",
            "preset-all": "All   [dim]+ genre, ISRC[/dim]",
        }
        for preset_id in self._preset_ids:
            node = self.query_one(f"#{preset_id}", Static)
            is_active = preset_id == pid
            if is_active:
                node.update(f"[green]▸[/green] {labels[preset_id]}")
                node.add_class("checked")
                node.remove_class("unchecked")
            else:
                node.update(f"[dim]▸[/dim] {labels[preset_id]}")
                node.remove_class("checked")
                node.add_class("unchecked")

    def on_key(self, event) -> None:
        key = event.key
        list_len = len(self._focus_ids) - 2  # exclude buttons

        if key in ("up", "k"):
            if self._focus_group == "list":
                self._focus_idx = max(0, self._focus_idx - 1)
            else:
                self._focus_group = "list"
                self._focus_idx = list_len - 1
            self._draw_focus()
            event.stop()
        elif key in ("down", "j"):
            if self._focus_group == "list":
                self._focus_idx = min(len(self._focus_ids) - 1, self._focus_idx + 1)
                if self._focus_idx >= list_len:
                    self._focus_group = "actions"
            else:
                self._focus_group = "actions"
                self._focus_idx = list_len
            self._draw_focus()
            event.stop()
        elif key in ("right", "l"):
            if self._focus_group == "actions":
                self._focus_idx = min(len(self._focus_ids) - 1, self._focus_idx + 1)
            else:
                self._focus_group = "actions"
                self._focus_idx = list_len
            self._draw_focus()
            event.stop()
        elif key in ("left", "h"):
            if self._focus_group == "actions":
                self._focus_idx = max(list_len, self._focus_idx - 1)
            self._draw_focus()
            event.stop()
        elif key in ("enter", "space"):
            fid = self._focus_ids[self._focus_idx]
            if fid == "btn-run":
                self._run_fix()
            elif fid == "btn-back":
                self.app.pop_screen()
            else:
                self._toggle_item(fid)
            event.stop()
        elif key in ("escape", "q"):
            self.app.pop_screen()
            event.stop()

    def on_click(self, event) -> None:
        widget_id = getattr(getattr(event, "control", None), "id", None)
        if widget_id in self._focus_ids and widget_id not in ("btn-run", "btn-back"):
            self._toggle_item(widget_id)
        elif widget_id == "btn-run":
            self._run_fix()
        elif widget_id == "btn-back":
            self.app.pop_screen()

    def _run_fix(self) -> None:
        selected_paths = [
            self._path_map[pid][0]
            for pid, sel in self._path_selected.items()
            if sel
        ]
        if not selected_paths:
            self.notify("No paths selected.", severity="warning", timeout=3)
            return
        preset = getattr(self, "_selected_preset", "preset-core")
        preset_fields = {
            "preset-core": {"title", "artist", "album", "year"},
            "preset-full": {"title", "artist", "album", "year", "album_artist", "track_number", "track_total", "disc_number", "disc_total"},
            "preset-all": {"title", "artist", "album", "year", "album_artist", "track_number", "track_total", "disc_number", "disc_total", "genre", "isrc"},
        }
        fields = preset_fields.get(preset, preset_fields["preset-core"])
        try:
            fields = validate_fields(fields)
        except TagWriteError as exc:
            self.notify(str(exc), severity="error", timeout=3)
            return
        dry_run = self._opt_selected.get("opt-dryrun", True)
        backup = self._opt_selected.get("opt-backup", True)
        self.app.push_screen(
            FixTagsRunScreen(fields, selected_paths=selected_paths, dry_run=dry_run, backup=backup)
        )


class FixTagsRunScreen(Screen[None]):
    """Show progress while writing tags."""

    BINDINGS: ClassVar[Sequence[tuple[str, str, str]]] = [  # type: ignore[assignment]
        ("q", "quit", "Quit"),
        ("escape", "cancel", "Back"),
    ]

    def __init__(self, fields: set[str], *, selected_paths: list[str], dry_run: bool = True, backup: bool = True) -> None:
        self._fields = fields
        self._selected_paths = selected_paths
        self._dry_run = dry_run
        self._backup = backup
        self._running = False
        super().__init__()

    def compose(self) -> ComposeResult:
        with Container(id="scan-screen"):
            status = "Preview" if self._dry_run else "Writing"
            yield Static(f"[b]{status} Tags[/b]  [dim]esc = back[/dim]", id="scan-title")
            yield RichLog(id="scan-log", markup=True)
            with Horizontal(id="scan-actions"):
                yield Static("Stop", id="btn-stop", classes="scan-link")
                yield Static("Back", id="btn-back", classes="scan-link dimmed")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#scan-log", RichLog).can_focus = True
        self.query_one("#btn-stop", Static).can_focus = True
        self.query_one("#btn-back", Static).can_focus = True
        self.query_one("#scan-log", RichLog).focus()
        self._running = True
        self._action_focus_idx = 0
        self._focus_group = "log"
        self.run_worker(self._worker, exclusive=True, thread=True)

    def _log(self, msg: str) -> None:
        app: Any = self.app
        app.call_from_thread(
            self.query_one("#scan-log", RichLog).write,
            msg,
        )

    def _worker(self) -> None:
        app: Any = self.app
        try:
            db_path = app.get_db_path()
            database = Database(db_path)
            from sqlalchemy import or_

            from soundaudit.db.store import DBFile

            with database.session() as s:
                files = (
                    s.query(DBFile)
                    .filter(DBFile.mb_recording_id.is_not(None))
                    .filter(or_(*[DBFile.path.startswith(p) for p in self._selected_paths]))
                    .all()
                )
                s.expunge_all()

            if not files:
                self._log("No files with resolved MusicBrainz data found in selected paths.")
                self._running = False
                app.call_from_thread(
                    self.query_one("#btn-back", Static).remove_class, "dimmed"
                )
                return

            total = len(files)
            preview = "[dim](preview)[/dim]" if self._dry_run else ""
            self._log(f"Processing {total} file(s) {preview}")
            self._log(f"Paths: {len(self._selected_paths)} selected")
            self._log(f"Fields: {', '.join(sorted(self._fields))}")
            self._log("")

            fixed = 0
            skipped = 0
            errors = 0

            for idx, db_file in enumerate(files, 1):
                # Check cancellation every iteration
                if not self._running:
                    self._log("Cancelled.")
                    break

                tags = resolved_metadata_to_tags(
                    db_file.mb_title,
                    db_file.mb_artist,
                    db_file.mb_album,
                    db_file.mb_album_artist,
                    db_file.mb_year,
                    db_file.mb_genre,
                )
                path = Path(db_file.path)

                if not path.exists():
                    self._log(f"  [dim]{idx}/{total}[/dim]  [red]✗ missing on disk[/red]  {path.name}")
                    errors += 1
                    continue

                try:
                    original = snapshot_tags(path)
                except Exception as exc:
                    self._log(f"  [dim]{idx}/{total}[/dim]  [red]✗ cannot read tags: {exc}[/red]  {path.name}")
                    errors += 1
                    continue

                changes: list[tuple[str, str, str]] = []
                for field in sorted(self._fields):
                    old_val = str(original.get(field) or "")
                    new_val = str(getattr(tags, field) or "")
                    if new_val and old_val != new_val:
                        changes.append((field, old_val or "—", new_val))

                if not changes:
                    self._log(f"  [dim]{idx}/{total}[/dim]  [dim]— unchanged[/dim]  {path.name}")
                    skipped += 1
                    if not self._dry_run:
                        with contextlib.suppress(Exception):
                            database.save_written_tags(db_file.id, tags, self._fields)
                    continue

                # Show the changes
                self._log(f"  [dim]{idx}/{total}[/dim]  [cyan]{path.name}[/cyan]")
                for field, old_val, new_val in changes:
                    self._log(f"      {field}: {old_val} → {new_val}")

                if not self._dry_run:
                    try:
                        backup_snapshot = write_tags(path, tags, fields=self._fields, backup=self._backup)
                        if self._backup:
                            database.save_tag_backup(db_file.id, backup_snapshot)
                        database.save_written_tags(db_file.id, tags, self._fields)
                        fixed += 1
                    except TagWriteError as exc:
                        self._log(f"      [red]write failed: {exc}[/red]")
                        errors += 1
                else:
                    fixed += 1

            self._log("")
            if not self._running:
                self._log("[yellow]Stopped early.[/yellow]")
            elif self._dry_run:
                self._log(f"[bold]Preview complete.[/bold] {fixed} would change, {skipped} unchanged, {errors} errors.")
            else:
                self._log(f"[bold green]Done.[/bold green] {fixed} updated, {skipped} unchanged, {errors} errors.")

        except Exception as exc:
            import traceback
            self._log(f"[red]CRASH: {exc}[/red]")
            for line in traceback.format_exc().splitlines():
                self._log(f"[dim]{line}[/dim]")
        finally:
            self._running = False
            app.call_from_thread(
                self.query_one("#btn-back", Static).remove_class, "dimmed"
            )

    def _draw_focus(self) -> None:
        for bid in ("btn-stop", "btn-back"):
            self.query_one(f"#{bid}", Static).remove_class("focused-nav")
        if self._focus_group == "actions":
            bids = ["btn-stop", "btn-back"]
            bid = bids[self._action_focus_idx]
            node = self.query_one(f"#{bid}", Static)
            node.add_class("focused-nav")
            node.focus()

    def _refocus_action(self, delta: int) -> None:
        self._action_focus_idx = (self._action_focus_idx + delta) % 2
        self._focus_group = "actions"
        self._draw_focus()

    def on_key(self, event) -> None:
        key = event.key
        focused_wid = self.focused.id if self.focused else None
        is_log_focused = focused_wid == "scan-log"

        if is_log_focused and key in ("up", "down", "pageup", "pagedown"):
            log = self.query_one("#scan-log", RichLog)
            if key == "up":
                log.scroll_up()
            elif key == "down":
                log.scroll_down()
            elif key == "pageup":
                log.scroll_page_up()
            elif key == "pagedown":
                log.scroll_page_down()
            event.stop()
            return

        if key == "tab":
            if self._focus_group == "log":
                self._focus_group = "actions"
                self.query_one("#scan-log", RichLog).blur()
                self._draw_focus()
            else:
                self._focus_group = "log"
                for bid in ("btn-stop", "btn-back"):
                    self.query_one(f"#{bid}", Static).remove_class("focused-nav")
                self.query_one("#scan-log", RichLog).focus()
            event.stop()
            return

        if key in ("left", "h"):
            if self._focus_group == "actions":
                self._refocus_action(-1)
            event.stop()
        elif key in ("right", "l"):
            if self._focus_group == "actions":
                self._refocus_action(1)
            event.stop()
        elif key in ("enter", "space", "return"):
            if self._focus_group == "actions":
                bids = ["btn-stop", "btn-back"]
                bid = bids[self._action_focus_idx]
                if bid == "btn-stop" and self._running:
                    self.action_cancel()
                elif bid == "btn-back" and not self._running:
                    self.app.pop_screen()
            event.stop()
        else:
            return

    def on_click(self, event) -> None:
        widget_id = getattr(getattr(event, "control", None), "id", None)
        if widget_id == "btn-stop" and self._running:
            self._running = False
            self._log("Cancelling...")
        elif widget_id in ("btn-back", "btn-stop") and not self._running:
            self.app.pop_screen()

    def action_cancel(self) -> None:
        if self._running:
            self._running = False
            self._log("Cancelling...")
        else:
            self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()


class RepairTagsScreen(Screen[None]):
    """Merge of Normalize + Standardize — detect and repair tag issues.

    Modes:
    - normalize: fix inconsistent tags within album folders (majority vote)
    - standardize: structural fixes for Navidrome (uppercase keys, missing track nums)
    """

    BINDINGS: ClassVar[Sequence[tuple[str, str, str]]] = [  # type: ignore[assignment]
        ("q", "quit", "Quit"),
        ("escape", "back", "Back"),
        ("up", "nav_up", "Up"),
        ("down", "nav_down", "Down"),
        ("left", "nav_left", "Left"),
        ("right", "nav_right", "Right"),
        ("enter", "activate", "Activate"),
        ("space", "activate", "Toggle"),
        ("tab", "next_group", "Next Group"),
        ("m", "toggle_mode", "Toggle Mode"),
        ("g", "focus_log", "Focus Log"),
    ]

    FIELD_IDS: ClassVar[list[str]] = [
        "fld-album",
        "fld-album_artist",
        "fld-year",
        "fld-artist",
    ]

    _MODE_DESC: ClassVar[dict[str, str]] = {
        "normalize": (
            "Detects inconsistent tags (album, artist, year, album_artist) within the same album folder. "
            "Uses majority voting to propose fixes for outlier tracks."
        ),
        "standardize": (
            "Fixes structural tag issues for Navidrome compatibility: "
            "missing TRACKNUMBER/DISCNUMBER, lowercase Vorbis keys, redundant TOTALTRACKS/TOTALDISCS."
        ),
    }

    def compose(self) -> ComposeResult:
        with Container(id="repair-screen"):
            yield Static("[b]Repair Tags[/b]  [dim]esc = back  |  m = toggle mode[/dim]", id="repair-title")
            yield Static("[dim]Normalize inconsistencies or standardize structural tags[/dim]", id="repair-hint")
            # Mode toggle
            with Horizontal(id="repair-modes"):
                yield Static("[green]▸[/green] Normalize", id="mode-normalize", classes="mode-link active-mode")
                yield Static("[dim]▸[/dim] Standardize", id="mode-standardize", classes="mode-link")
            yield Static(self._MODE_DESC["normalize"], id="repair-desc")
            with Vertical(id="repair-path-list"):
                yield Static("[dim]Source paths[/dim]", id="repair-path-label")
            yield Static("", id="repair-current")
            yield RichLog(id="repair-log", markup=True)
            with Horizontal(id="repair-stats"):
                yield Static("Folders: 0", id="repair-stat-folders")
                yield Static("Files: 0", id="repair-stat-files")
                yield Static("Applied: 0", id="repair-stat-applied")
                yield Static("Errors: 0", id="repair-stat-errors")
            with Horizontal(id="repair-actions"):
                yield Static("Start", id="btn-repair-start", classes="scan-link")
                yield Static("Stop", id="btn-repair-stop", classes="scan-link dimmed")
                yield Static("Back", id="btn-repair-back", classes="scan-link")
        yield Footer()

    def on_mount(self) -> None:
        app: Any = self.app
        cfg = app.get_config()
        full_paths = [str(p) for p in cfg.scan.paths]
        short_paths = ReportScreen._shorten_paths(full_paths)
        self._path_map: dict[str, tuple[str, str]] = {}
        self._path_selected: dict[str, bool] = {}
        self._path_ids: list[str] = []
        self._path_focus_idx: int = 0
        self._action_focus_idx: int = 0
        self._focus_group: str = "paths"
        self._is_running = False
        self._mode: str = "normalize"  # "normalize" | "standardize"

        path_list = self.query_one("#repair-path-list", Vertical)
        for idx, (full, short) in enumerate(zip(full_paths, short_paths, strict=False)):
            pid = f"repair-path-{idx}"
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

        # Field toggles — only visible in normalize mode
        path_list.mount(Static("", id="repair-sep-fields"))
        path_list.mount(Static("[dim]Fields to check[/dim]", id="repair-fields-label"))
        self._field_selected: dict[str, bool] = {}
        for fid in self.FIELD_IDS:
            self._field_selected[fid] = True
            label = fid.replace("fld-", "").replace("_", " ")
            path_list.mount(
                Static(
                    f"[green]✓[/green] {label.title()}",
                    id=fid,
                    classes="path-item checked field-toggle",
                )
            )
            self._path_ids.append(fid)

        # Options
        path_list.mount(Static("", id="repair-sep-options"))
        self._opt_ids = ["repair-dryrun"]
        self._opt_selected: dict[str, bool] = {"repair-dryrun": True}
        path_list.mount(
            Static(
                "[green]✓[/green] Dry-run  [dim]preview only[/dim]",
                id="repair-dryrun",
                classes="path-item checked",
            )
        )
        self._path_ids.append("repair-dryrun")

        for sid in ("repair-path-label", "repair-fields-label", "repair-sep-fields", "repair-sep-options"):
            node = self.query_one(f"#{sid}", Static)
            if node:
                node.can_focus = False
        for pid in self._path_ids:
            self.query_one(f"#{pid}", Static).can_focus = False
        for bid in ("btn-repair-start", "btn-repair-stop", "btn-repair-back"):
            self.query_one(f"#{bid}", Static).can_focus = False
        self.query_one("#repair-log", RichLog).can_focus = True
        self.query_one("#repair-title", Static).can_focus = True
        self.query_one("#repair-title", Static).focus()
        self._draw_focus()
        log = self.query_one("#repair-log", RichLog)
        log.write("Repair Tags — choose Normalize or Standardize mode (press [b]m[/b] to toggle).")
        log.write("Normalize: detects inconsistent tags (album, artist, year, album_artist) within album folders via majority voting.")
        log.write("Standardize: fixes structural issues — missing TRACKNUMBER/DISCNUMBER, lowercase keys, redundant totals.")
        log.write("↑↓ move, Enter/Space toggle, Tab = actions, m = toggle mode, g = focus log.")

    def _draw_focus(self) -> None:
        for pid in self._path_ids:
            self.query_one(f"#{pid}", Static).remove_class("focused-nav")
        for bid in ("btn-repair-start", "btn-repair-stop", "btn-repair-back"):
            self.query_one(f"#{bid}", Static).remove_class("focused-nav")
        for mid in ("mode-normalize", "mode-standardize"):
            self.query_one(f"#{mid}", Static).remove_class("focused-nav")
        if self._focus_group == "paths":
            pid = self._path_ids[self._path_focus_idx]
            self.query_one(f"#{pid}", Static).add_class("focused-nav")
        elif self._focus_group == "modes":
            mid = "mode-normalize" if self._mode == "normalize" else "mode-standardize"
            self.query_one(f"#{mid}", Static).add_class("focused-nav")
        else:
            bids = ["btn-repair-start", "btn-repair-stop", "btn-repair-back"]
            bid = bids[self._action_focus_idx]
            self.query_one(f"#{bid}", Static).add_class("focused-nav")

    def _refocus_path(self, delta: int) -> None:
        self._path_focus_idx = max(0, min(len(self._path_ids) - 1, self._path_focus_idx + delta))
        self._focus_group = "paths"
        self._draw_focus()
        pid = self._path_ids[self._path_focus_idx]
        node = self.query_one(f"#{pid}", Static)
        self.query_one("#repair-path-list", Vertical).scroll_to_widget(node)

    def _refocus_action(self, delta: int) -> None:
        self._action_focus_idx = max(0, min(2, self._action_focus_idx + delta))
        self._focus_group = "actions"
        self._draw_focus()

    def _toggle_mode(self) -> None:
        self._mode = "standardize" if self._mode == "normalize" else "normalize"
        n_node = self.query_one("#mode-normalize", Static)
        s_node = self.query_one("#mode-standardize", Static)
        desc = self.query_one("#repair-desc", Static)
        if self._mode == "normalize":
            n_node.update("[green]▸[/green] Normalize")
            n_node.add_class("active-mode")
            s_node.update("[dim]▸[/dim] Standardize")
            s_node.remove_class("active-mode")
            desc.update(self._MODE_DESC["normalize"])
            self.query_one("#repair-fields-label", Static).styles.display = "block"
            for fid in self.FIELD_IDS:
                self.query_one(f"#{fid}", Static).styles.display = "block"
            self.query_one("#repair-sep-fields", Static).styles.display = "block"
        else:
            s_node.update("[green]▸[/green] Standardize")
            s_node.add_class("active-mode")
            n_node.update("[dim]▸[/dim] Normalize")
            n_node.remove_class("active-mode")
            desc.update(self._MODE_DESC["standardize"])
            self.query_one("#repair-fields-label", Static).styles.display = "none"
            for fid in self.FIELD_IDS:
                self.query_one(f"#{fid}", Static).styles.display = "none"
            self.query_one("#repair-sep-fields", Static).styles.display = "none"
        self._path_focus_idx = 0
        self._focus_group = "paths"
        self._draw_focus()

    def _toggle_path(self, pid: str) -> None:
        if pid in self.FIELD_IDS:
            was = self._field_selected[pid]
            self._field_selected[pid] = not was
            checked = self._field_selected[pid]
            label = pid.replace("fld-", "").replace("_", " ").title()
            node = self.query_one(f"#{pid}", Static)
            if checked:
                node.update(f"[green]✓[/green] {label}")
                node.add_class("checked")
                node.remove_class("unchecked")
            else:
                node.update(f"[red]✗[/red] {label}")
                node.remove_class("checked")
                node.add_class("unchecked")
            return
        if pid == "repair-dryrun":
            was = self._opt_selected[pid]
            self._opt_selected[pid] = not was
            checked = self._opt_selected[pid]
            node = self.query_one(f"#{pid}", Static)
            label = "Dry-run  [dim]preview only[/dim]"
            if checked:
                node.update(f"[green]✓[/green] {label}")
                node.add_class("checked")
                node.remove_class("unchecked")
            else:
                node.update(f"[red]✗[/red] {label}")
                node.remove_class("checked")
                node.add_class("unchecked")
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

    def on_key(self, event) -> None:
        key = event.key
        focused_wid = self.focused.id if self.focused else None
        is_log_focused = focused_wid == "repair-log"

        if is_log_focused and key in ("up", "down", "pageup", "pagedown"):
            log = self.query_one("#repair-log", RichLog)
            if key == "up":
                log.scroll_up()
            elif key == "down":
                log.scroll_down()
            elif key == "pageup":
                log.scroll_page_up()
            elif key == "pagedown":
                log.scroll_page_down()
            event.stop()
            return

        if key in ("up", "k"):
            if self._focus_group == "paths":
                self._refocus_path(-1)
            event.stop()
        elif key in ("down", "j"):
            if self._focus_group == "paths":
                self._refocus_path(1)
            event.stop()
        elif key == "tab":
            if self._focus_group == "paths":
                self._focus_group = "actions"
            elif self._focus_group == "actions":
                self._focus_group = "log"
                self.query_one("#repair-log", RichLog).focus()
            else:
                self._focus_group = "paths"
                self.query_one("#repair-log", RichLog).blur()
            self._draw_focus()
            event.stop()
            return
        elif key in ("left", "h"):
            if self._focus_group == "actions":
                self._refocus_action(-1)
            elif self._focus_group == "log":
                self._focus_group = "paths"
                self.query_one("#repair-log", RichLog).blur()
                self._draw_focus()
            event.stop()
        elif key in ("right", "l"):
            if self._focus_group == "actions":
                self._refocus_action(1)
            event.stop()
        elif key in ("enter", "space", "return"):
            if self._focus_group == "paths":
                pid = self._path_ids[self._path_focus_idx]
                self._toggle_path(pid)
            elif self._focus_group == "actions":
                bids = ["btn-repair-start", "btn-repair-stop", "btn-repair-back"]
                bid = bids[self._action_focus_idx]
                if bid == "btn-repair-start" and not self._is_running:
                    self._start_repair()
                elif bid == "btn-repair-stop" and self._is_running:
                    self._set_running(False)
                    self._log("Cancelling...")
                elif bid == "btn-repair-back" and not self._is_running:
                    self.app.pop_screen()
            event.stop()
        elif key == "m":
            self._toggle_mode()
            event.stop()
        else:
            return

    def on_click(self, event) -> None:
        widget_id = getattr(getattr(event, "control", None), "id", None)
        if widget_id == "btn-repair-start" and not self._is_running:
            self._start_repair()
        elif widget_id == "btn-repair-stop" and self._is_running:
            self._set_running(False)
            self._log("Cancelling...")
        elif widget_id == "btn-repair-back" and not self._is_running:
            self.app.pop_screen()
        elif widget_id in ("mode-normalize", "mode-standardize"):
            self._toggle_mode()
        elif widget_id and widget_id in self._path_ids:
            self._path_focus_idx = self._path_ids.index(widget_id)
            self._focus_group = "paths"
            self._draw_focus()
            self._toggle_path(widget_id)
            event.stop()

    def _set_running(self, running: bool) -> None:
        self._is_running = running
        start = self.query_one("#btn-repair-start", Static)
        stop = self.query_one("#btn-repair-stop", Static)
        back = self.query_one("#btn-repair-back", Static)
        if running:
            start.add_class("dimmed")
            stop.remove_class("dimmed")
            back.add_class("dimmed")
        else:
            start.remove_class("dimmed")
            stop.add_class("dimmed")
            back.remove_class("dimmed")

    def _log(self, msg: str) -> None:
        self.query_one("#repair-log", RichLog).write(msg)

    def _set_current(self, msg: str) -> None:
        self.query_one("#repair-current", Static).update(msg)

    def _start_repair(self) -> None:
        selected = [
            self._path_map[pid][0]
            for pid, sel in self._path_selected.items()
            if sel
        ]
        if not selected:
            self._log("[yellow]No source paths selected. Toggle at least one path.[/yellow]")
            return
        if self._mode == "normalize":
            active_fields = {
                fid.replace("fld-", "")
                for fid, sel in self._field_selected.items()
                if sel
            }
            if not active_fields:
                self._log("[yellow]No fields selected. Toggle at least one field.[/yellow]")
                return
        self._set_running(True)
        self.query_one("#repair-log", RichLog).clear()
        self._log(f"Mode: {self._mode.title()}")
        self._log(f"Scanning {len(selected)} path(s)…")
        dry = self._opt_selected.get("repair-dryrun", True)
        self._log(f"Mode: {'dry-run preview' if dry else 'write to files'}")
        self.run_worker(self._repair_worker, exclusive=True, thread=True)

    def _repair_worker(self) -> None:
        if self._mode == "normalize":
            self._normalize_worker()
        else:
            self._standardize_worker()

    def _normalize_worker(self) -> None:
        app: Any = self.app
        cfg = app.get_config()
        selected = [
            self._path_map[pid][0]
            for pid, sel in self._path_selected.items()
            if sel
        ]
        active_fields = tuple(
            fid.replace("fld-", "")
            for fid, sel in self._field_selected.items()
            if sel
        )
        dry_run = self._opt_selected.get("repair-dryrun", True)
        ext_set = {e.lower() for e in cfg.organize.extensions}

        try:
            from soundaudit.analyzer.normalize import scan_folders
            results = scan_folders(
                [Path(p) for p in selected],
                fields=active_fields,
                min_files=2,
                extensions=ext_set,
            )
            total_fixes = sum(len(r.fixes) for r in results)
            app.call_from_thread(
                self.query_one("#repair-stat-folders", Static).update,
                f"Folders: {len(results)}"
            )
            app.call_from_thread(
                self.query_one("#repair-stat-files", Static).update,
                f"Files: {total_fixes}"
            )

            if not results:
                app.call_from_thread(self._log, "[green]No inconsistencies found.[/green]")
                return

            for folder_result in results:
                if not self._is_running:
                    app.call_from_thread(self._log, "[yellow]Cancelled.[/yellow]")
                    break
                app.call_from_thread(
                    self._set_current,
                    f"{folder_result.folder.name}  ({len(folder_result.fixes)} fix(es))"
                )
                for fix in folder_result.fixes:
                    cur = str(fix.current) if fix.current is not None else "—"
                    prop = str(fix.proposed) if fix.proposed is not None else "—"
                    app.call_from_thread(
                        self._log,
                        f"  [cyan]{fix.path.name}[/cyan]  {fix.field}: {cur} → {prop}"
                    )

            if dry_run:
                app.call_from_thread(self._log, "")
                app.call_from_thread(
                    self._log,
                    f"[bold]Preview complete.[/bold] {total_fixes} fix(es) pending. Disable dry-run to write."
                )
                return

            applied = 0
            errors = 0
            for folder_result in results:
                if not self._is_running:
                    app.call_from_thread(self._log, "[yellow]Stopped early.[/yellow]")
                    break
                for fix in folder_result.fixes:
                    try:
                        tags = TrackTags()
                        setattr(tags, fix.field, fix.proposed)
                        write_tags(Path(fix.path), tags, fields={fix.field}, backup=True)
                        applied += 1
                    except TagWriteError as exc:
                        app.call_from_thread(self._log, f"  [red]✗ {fix.path.name}: {exc}[/red]")
                        errors += 1
                    except Exception as exc:
                        app.call_from_thread(self._log, f"  [red]✗ {fix.path.name}: {exc}[/red]")
                        errors += 1

            app.call_from_thread(
                self.query_one("#repair-stat-applied", Static).update,
                f"Applied: {applied}"
            )
            app.call_from_thread(
                self.query_one("#repair-stat-errors", Static).update,
                f"Errors: {errors}"
            )
            parts = []
            if applied:
                parts.append(f"[bold green]{applied} updated[/bold green]")
            if errors:
                parts.append(f"[red]{errors} errors[/red]")
            app.call_from_thread(self._log, "")
            app.call_from_thread(self._log, f"Done. {' | '.join(parts)}")
        except Exception as exc:
            import traceback
            app.call_from_thread(self._log, f"[red]CRASH: {exc}[/red]")
            for line in traceback.format_exc().splitlines():
                app.call_from_thread(self._log, f"[dim]{line}[/dim]")
        finally:
            app.call_from_thread(self._set_running, False)
            app.call_from_thread(self._set_current, "")

    def _standardize_worker(self) -> None:
        app: Any = self.app
        selected = [
            self._path_map[pid][0]
            for pid, sel in self._path_selected.items()
            if sel
        ]
        dry_run = self._opt_selected.get("repair-dryrun", True)

        from soundaudit.analyzer.standardize import (
            apply_standardize,
            scan_folders_for_standardize,
        )

        try:
            results = scan_folders_for_standardize(
                [Path(p) for p in selected],
                extensions={".flac"},
                min_files=1,
            )
            total_files = sum(len(r.fixes) for r in results)
            app.call_from_thread(
                self.query_one("#repair-stat-folders", Static).update,
                f"Folders: {len(results)}"
            )
            app.call_from_thread(
                self.query_one("#repair-stat-files", Static).update,
                f"Files: {total_files}"
            )

            if not results:
                app.call_from_thread(self._log, "[green]No standardization needed.[/green]")
                return

            for folder_result in results:
                if not self._is_running:
                    app.call_from_thread(self._log, "[yellow]Cancelled.[/yellow]")
                    break
                app.call_from_thread(
                    self._set_current,
                    f"{folder_result.folder.name}  ({len(folder_result.fixes)} fix(es))"
                )
                for fix in folder_result.fixes:
                    cur = str(fix.current) if fix.current is not None else "—"
                    app.call_from_thread(
                        self._log,
                        f"  [cyan]{fix.path.name}[/cyan]  {fix.field}: {cur} → {fix.proposed}"
                    )

            if dry_run:
                app.call_from_thread(self._log, "")
                app.call_from_thread(
                    self._log,
                    f"[bold]Preview complete.[/bold] {total_files} fix(es) pending. Disable dry-run to write."
                )
                return

            applied = 0
            errors = 0
            for folder_result in results:
                if not self._is_running:
                    app.call_from_thread(self._log, "[yellow]Stopped early.[/yellow]")
                    break
                for fix in folder_result.fixes:
                    try:
                        if apply_standardize(Path(fix.path), backup=True):
                            applied += 1
                    except Exception as exc:
                        app.call_from_thread(self._log, f"  [red]✗ {fix.path.name}: {exc}[/red]")
                        errors += 1

            app.call_from_thread(
                self.query_one("#repair-stat-applied", Static).update,
                f"Applied: {applied}"
            )
            app.call_from_thread(
                self.query_one("#repair-stat-errors", Static).update,
                f"Errors: {errors}"
            )
            parts = []
            if applied:
                parts.append(f"[bold green]{applied} updated[/bold green]")
            if errors:
                parts.append(f"[red]{errors} errors[/red]")
            app.call_from_thread(self._log, "")
            app.call_from_thread(self._log, f"Done. {' | '.join(parts)}")
        except Exception as exc:
            import traceback
            app.call_from_thread(self._log, f"[red]CRASH: {exc}[/red]")
            for line in traceback.format_exc().splitlines():
                app.call_from_thread(self._log, f"[dim]{line}[/dim]")
        finally:
            app.call_from_thread(self._set_running, False)
            app.call_from_thread(self._set_current, "")

    def action_nav_up(self) -> None:
        if self._focus_group == "paths":
            self._refocus_path(-1)

    def action_nav_down(self) -> None:
        if self._focus_group == "paths":
            self._refocus_path(1)

    def action_nav_left(self) -> None:
        if self._focus_group == "actions":
            self._refocus_action(-1)
        elif self._focus_group == "log":
            self._focus_group = "paths"
            self.query_one("#repair-log", RichLog).blur()
            self._draw_focus()

    def action_nav_right(self) -> None:
        if self._focus_group == "actions":
            self._refocus_action(1)

    def action_activate(self) -> None:
        if self._focus_group == "paths":
            pid = self._path_ids[self._path_focus_idx]
            self._toggle_path(pid)
        elif self._focus_group == "actions":
            bids = ["btn-repair-start", "btn-repair-stop", "btn-repair-back"]
            bid = bids[self._action_focus_idx]
            if bid == "btn-repair-start" and not self._is_running:
                self._start_repair()
            elif bid == "btn-repair-stop" and self._is_running:
                self._set_running(False)
                self._log("Cancelling...")
            elif bid == "btn-repair-back" and not self._is_running:
                self.app.pop_screen()

    def action_next_group(self) -> None:
        if self._focus_group == "paths":
            self._focus_group = "actions"
        elif self._focus_group == "actions":
            self._focus_group = "log"
            self.query_one("#repair-log", RichLog).focus()
        else:
            self._focus_group = "paths"
            self.query_one("#repair-log", RichLog).blur()
        self._draw_focus()

    def action_toggle_mode(self) -> None:
        self._toggle_mode()

    def action_focus_log(self) -> None:
        self._focus_group = "log"
        self.query_one("#repair-log", RichLog).focus()

    def action_back(self) -> None:
        if not self._is_running:
            self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()

class OrganizeScreen(Screen[None]):
    """Navidrome folder-structure organizer screen.

    Discovers files from selected filesystem paths (or from the DB) and moves
    them into the configured Navidrome root using embedded tags.
    """

    BINDINGS: ClassVar[Sequence[tuple[str, str, str]]] = [  # type: ignore[assignment]
        ("q", "quit", "Quit"),
        ("escape", "back", "Back"),
    ]

    def compose(self) -> ComposeResult:
        with Container(id="organize-screen"):
            yield Static("[b]Navidrome Organizer[/b]  [dim]esc = back[/dim]", id="organize-title")
            yield Static("", id="organize-output")
            with Vertical(id="org-path-list"):
                yield Static("[dim]Source paths[/dim]", id="org-path-label")
            yield Static("", id="organize-current")
            yield RichLog(id="organize-log", markup=True)
            with Horizontal(id="organize-stats"):
                yield Static("Files: 0", id="org-stat-files")
                yield Static("Moved: 0", id="org-stat-moved")
                yield Static("Skipped: 0", id="org-stat-skipped")
                yield Static("Errors: 0", id="org-stat-errors")
            with Horizontal(id="organize-actions"):
                yield Static("Start", id="btn-org-start", classes="scan-link")
                yield Static("Stop", id="btn-org-stop", classes="scan-link dimmed")
                yield Static("Back", id="btn-org-back", classes="scan-link")
        yield Footer()

    def on_mount(self) -> None:
        app: Any = self.app
        cfg = app.get_config()
        full_paths = [str(p) for p in cfg.scan.paths]
        short_paths = ReportScreen._shorten_paths(full_paths)
        self._path_map: dict[str, tuple[str, str]] = {}
        self._path_selected: dict[str, bool] = {}
        self._path_ids: list[str] = []
        self._path_focus_idx: int = 0
        self._action_focus_idx: int = 0
        self._focus_group: str = "paths"
        self._is_organizing = False
        self._set_organizing(False)

        path_list = self.query_one("#org-path-list", Vertical)
        for idx, (full, short) in enumerate(zip(full_paths, short_paths, strict=False)):
            pid = f"org-path-{idx}"
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
        # from-db toggle
        path_list.mount(Static("", id="org-sep-toggle"))
        self._path_ids.append("org-from-db")
        self._from_db = False
        path_list.mount(
            Static(
                "[dim]▸[/dim] Use database [dim](already scanned)[/dim]",
                id="org-from-db",
                classes="path-item unchecked",
            )
        )
        # non-interactive items
        for sid in ("org-path-label", "org-sep-toggle"):
            node = self.query_one(f"#{sid}", Static)
            if node:
                node.can_focus = False
        for pid in self._path_ids:
            self.query_one(f"#{pid}", Static).can_focus = False
        for bid in ("btn-org-start", "btn-org-stop", "btn-org-back"):
            self.query_one(f"#{bid}", Static).can_focus = False
        self.query_one("#organize-log", RichLog).can_focus = True
        self.query_one("#organize-title", Static).can_focus = True
        self.query_one("#organize-title", Static).focus()
        # Show output path
        cfg = app.get_config()
        out = cfg.organize.output_path or "[red]not set[/red]"
        self.query_one("#organize-output", Static).update(f"[dim]→ {out}[/dim]")
        self._draw_focus()
        log = self.query_one("#organize-log", RichLog)
        log.write("Navidrome folder organizer.")
        log.write("↑↓ move, Enter/Space toggle, Tab = actions, g = focus log.")
        log.write("Select source paths, then Start to move files into Navidrome structure.")

    def _draw_focus(self) -> None:
        for pid in self._path_ids:
            self.query_one(f"#{pid}", Static).remove_class("focused-nav")
        for bid in ("btn-org-start", "btn-org-stop", "btn-org-back"):
            self.query_one(f"#{bid}", Static).remove_class("focused-nav")
        if self._focus_group == "paths":
            pid = self._path_ids[self._path_focus_idx]
            self.query_one(f"#{pid}", Static).add_class("focused-nav")
        else:
            bids = ["btn-org-start", "btn-org-stop", "btn-org-back"]
            bid = bids[self._action_focus_idx]
            self.query_one(f"#{bid}", Static).add_class("focused-nav")

    def _refocus_path(self, delta: int) -> None:
        self._path_focus_idx = max(0, min(len(self._path_ids) - 1, self._path_focus_idx + delta))
        self._focus_group = "paths"
        self._draw_focus()
        pid = self._path_ids[self._path_focus_idx]
        node = self.query_one(f"#{pid}", Static)
        self.query_one("#org-path-list", Vertical).scroll_to_widget(node)

    def _refocus_action(self, delta: int) -> None:
        self._action_focus_idx = max(0, min(2, self._action_focus_idx + delta))
        self._focus_group = "actions"
        self._draw_focus()

    def _toggle_path(self, pid: str) -> None:
        if pid == "org-from-db":
            self._from_db = not self._from_db
            node = self.query_one("#org-from-db", Static)
            if self._from_db:
                node.update("[green]▸[/green] Use database [dim](already scanned)[/dim]")
                node.add_class("checked")
                node.remove_class("unchecked")
            else:
                node.update("[dim]▸[/dim] Use database [dim](already scanned)[/dim]")
                node.add_class("unchecked")
                node.remove_class("checked")
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

    def on_key(self, event) -> None:
        key = event.key
        focused_wid = self.focused.id if self.focused else None
        is_log_focused = focused_wid == "organize-log"

        if is_log_focused and key in ("up", "down", "pageup", "pagedown"):
            log = self.query_one("#organize-log", RichLog)
            if key == "up":
                log.scroll_up()
            elif key == "down":
                log.scroll_down()
            elif key == "pageup":
                log.scroll_page_up()
            elif key == "pagedown":
                log.scroll_page_down()
            event.stop()
            return

        if key in ("up", "k"):
            if self._focus_group == "paths":
                self._refocus_path(-1)
            event.stop()
        elif key in ("down", "j"):
            if self._focus_group == "paths":
                self._refocus_path(1)
            event.stop()
        elif key == "tab":
            if self._focus_group == "paths":
                self._focus_group = "actions"
            elif self._focus_group == "actions":
                self._focus_group = "log"
                self.query_one("#organize-log", RichLog).focus()
            else:
                self._focus_group = "paths"
                self.query_one("#organize-log", RichLog).blur()
            self._draw_focus()
            event.stop()
            return
        elif key in ("left", "h"):
            if self._focus_group == "actions":
                self._refocus_action(-1)
            elif self._focus_group == "log":
                self._focus_group = "paths"
                self.query_one("#organize-log", RichLog).blur()
                self._draw_focus()
            event.stop()
        elif key in ("right", "l"):
            if self._focus_group == "actions":
                self._refocus_action(1)
            event.stop()
        elif key in ("enter", "space", "return"):
            if self._focus_group == "paths":
                pid = self._path_ids[self._path_focus_idx]
                self._toggle_path(pid)
            elif self._focus_group == "actions":
                bids = ["btn-org-start", "btn-org-stop", "btn-org-back"]
                bid = bids[self._action_focus_idx]
                if bid == "btn-org-start" and not self._is_organizing:
                    self._start_organize()
                elif bid == "btn-org-stop" and self._is_organizing:
                    self._set_organizing(False)
                    self._log("Cancelling...")
                elif bid == "btn-org-back" and not self._is_organizing:
                    self.app.pop_screen()
            event.stop()
        else:
            return

    def on_click(self, event) -> None:
        widget_id = getattr(getattr(event, "control", None), "id", None)
        if widget_id == "btn-org-start" and not self._is_organizing:
            self._start_organize()
        elif widget_id == "btn-org-stop" and self._is_organizing:
            self._set_organizing(False)
            self._log("Cancelling...")
        elif widget_id == "btn-org-back" and not self._is_organizing:
            self.app.pop_screen()
        elif widget_id and widget_id in self._path_ids:
            self._path_focus_idx = self._path_ids.index(widget_id)
            self._focus_group = "paths"
            self._draw_focus()
            self._toggle_path(widget_id)
            event.stop()

    def _set_organizing(self, organizing: bool) -> None:
        """Update running state and button dimming manually."""
        self._is_organizing = organizing
        start = self.query_one("#btn-org-start", Static)
        stop = self.query_one("#btn-org-stop", Static)
        back = self.query_one("#btn-org-back", Static)
        if organizing:
            start.add_class("dimmed")
            stop.remove_class("dimmed")
            back.add_class("dimmed")
        else:
            start.remove_class("dimmed")
            stop.add_class("dimmed")
            back.remove_class("dimmed")

    def _log(self, msg: str) -> None:
        self.query_one("#organize-log", RichLog).write(msg)

    def _set_current(self, msg: str) -> None:
        self.query_one("#organize-current", Static).update(msg)

    def _start_organize(self) -> None:
        app: Any = self.app
        cfg = app.get_config()
        out_root = None
        if cfg.organize.output_path:
            try:
                out_root = Path(cfg.organize.output_path).expanduser().resolve()
            except OSError:
                out_root = Path(cfg.organize.output_path)  # already expanduser()'d by config validator
        if out_root is None:
            self._log("[red]organize.output_path is not set in config.[/red]")
            self._log("Set organize.output_path in your config.yaml (e.g. ~/Music/Navidrome)")
            return
        if not out_root.exists():
            self._log(f"[red]Output path does not exist or is unreachable: {out_root}[/red]")
            return

        # Validate at least one source is selected
        any_selected = any(sel for pid, sel in self._path_selected.items() if pid != "org-from-db")
        if not self._from_db and not any_selected:
            self._log("[yellow]No source paths selected. Toggle at least one path, or enable 'Use database'.[/yellow]")
            return

        self._set_organizing(True)
        self._log(f"Output root: {out_root}")
        if self._from_db:
            self._log("Source: database (already scanned files)")
        else:
            selected = [self._path_map[pid][0] for pid, sel in self._path_selected.items() if sel]
            self._log(f"Source paths: {', '.join(selected)}")
        self.run_worker(self._organize_worker, exclusive=True, thread=True)

    def _organize_worker(self) -> None:
        from soundaudit.db.store import DBFile
        from soundaudit.organizer import execute_organization, plan_organization
        from soundaudit.scanner.walker import discover_files
        app: Any = self.app
        cfg = app.get_config()
        out_root = Path(cfg.organize.output_path)  # already expanduser()'d by config validator
        tmpl = cfg.organize.template or "{album_artist}/{album} [{year}]/{disc_track}. {title}.{format}"
        extensions = set(cfg.organize.extensions)
        db_path = app.get_db_path()
        database = Database(db_path)

        try:
            source_paths: list[Path] = []

            if self._from_db:
                with database.session() as s:
                    files = (
                        s.query(DBFile)
                        .filter(DBFile.is_corrupt == 0)
                        .all()
                    )
                    s.expunge_all()
                for f in files:
                    p = Path(f.path)
                    if p.suffix.lower() in extensions:
                        source_paths.append(p)
                app.call_from_thread(self._log, f"Loaded {len(source_paths)} file(s) from database.")
            else:
                selected = [
                    self._path_map[pid][0]
                    for pid, sel in self._path_selected.items()
                    if sel and pid != "org-from-db"
                ]
                for root_path in selected:
                    root = Path(root_path).expanduser().resolve()
                    if not root.exists():
                        app.call_from_thread(self._log, f"[yellow]Skipping missing path: {root}[/yellow]")
                        continue
                    try:
                        for p in discover_files(root, extensions):
                            if not self._is_organizing:
                                app.call_from_thread(self._log, "Cancelled during discovery.")
                                return
                            source_paths.append(p)
                    except Exception as exc:
                        app.call_from_thread(self._log, f"[red]Error in {root}: {exc}[/red]")
                app.call_from_thread(self._log, f"Discovered {len(source_paths)} file(s) from filesystem.")

            if not source_paths:
                app.call_from_thread(self._log, "[yellow]No files to organize.[/yellow]")
                return

            min_tracks = cfg.organize.min_album_tracks
            plans = plan_organization(source_paths, out_root, template=tmpl, min_album_tracks=min_tracks)

            ok = sum(1 for pl in plans if pl.status != "error")
            bad = len(plans) - ok
            app.call_from_thread(self._log, f"Plan ready: {ok} ok, {bad} errors.")
            app.call_from_thread(
                self.query_one("#org-stat-files", Static).update, f"Files: {len(plans)}"
            )

            def _on_move(plan) -> None:
                app.call_from_thread(self._log, f"[green]Moved[/green] {plan.proposed.name}")

            executed = execute_organization(plans, dry_run=False, move=cfg.organize.move, skip_existing=True, on_move=_on_move)

            moved = copied = errors = already = skipped = 0
            updated_db = 0
            for plan in executed:
                if plan.status == "moved":
                    moved += 1
                elif plan.status == "copied":
                    copied += 1
                elif plan.status == "already":
                    already += 1
                elif plan.status == "skipped":
                    skipped += 1
                    if plan.skip_reason:
                        app.call_from_thread(
                            self._log,
                            f"[yellow]Skipped[/yellow] {plan.source.name}: {plan.skip_reason}"
                        )
                elif plan.status == "error":
                    errors += 1
                    if plan.error:
                        # truncate very long paths for readability
                        src = str(plan.source)
                        app.call_from_thread(self._log, f"[red]Failed[/red] {src}: {plan.error}")
                if plan.status in ("moved", "copied") and database.update_file_path(str(plan.source), str(plan.proposed)):
                    updated_db += 1

            parts = [f"[bold green]{moved} moved[/bold green]"]
            if copied:
                parts.append(f"[green]{copied} copied[/green]")
            if already:
                parts.append(f"[dim]{already} already organized[/dim]")
            if skipped:
                parts.append(f"[yellow]{skipped} skipped[/yellow]")
            if errors:
                parts.append(f"[red]{errors} errors[/red]")
            app.call_from_thread(self._log, f"Done. {' | '.join(parts)}")
            app.call_from_thread(self._log, f"[dim]Updated {updated_db} path(s) in database.[/dim]")
            app.call_from_thread(
                self.query_one("#org-stat-moved", Static).update, f"Moved: {moved}"
            )
            app.call_from_thread(
                self.query_one("#org-stat-skipped", Static).update, f"Skipped: {skipped}"
            )
            app.call_from_thread(
                self.query_one("#org-stat-errors", Static).update, f"Errors: {errors}"
            )
        except Exception as exc:
            import traceback
            app.call_from_thread(self._log, f"[red]CRASH: {exc}[/red]")
            for line in traceback.format_exc().splitlines():
                app.call_from_thread(self._log, f"[dim]{line}[/dim]")
        finally:
            app.call_from_thread(self._set_organizing, False)

    def action_back(self) -> None:
        if not self._is_organizing:
            self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()


