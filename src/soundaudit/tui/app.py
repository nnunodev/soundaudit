"""Textual TUI application."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

from soundaudit.config import AppConfig
from soundaudit.tui.screens import DashboardScreen, ReportScreen, ScanScreen


class SoundAuditApp(App[None]):
    """Main TUI application for SoundAudit."""

    def __init__(
        self,
        db_path: str | None = None,
        config_path: Path | None = None,
    ) -> None:
        self._db_path = db_path
        self._config_path = config_path
        super().__init__()

    def get_config(self) -> AppConfig:
        if self._config_path and self._config_path.exists():
            return AppConfig.from_yaml(self._config_path)
        # Auto-detect config.yaml / config.yml in cwd
        for name in ("config.yaml", "config.yml"):
            candidate = Path(name)
            if candidate.exists():
                return AppConfig.from_yaml(candidate)
        return AppConfig()

    def get_db_path(self) -> str:
        if self._db_path:
            return self._db_path
        return str(self.get_config().database.resolved())

    CSS = """
    Screen {
        align: center middle;
    }

    #dashboard {
        width: auto;
        min-width: 44;
        max-width: 95vw;
        height: auto;
        border: solid $primary;
        padding: 0 1;
    }

    #title {
        text-align: center;
        height: auto;
        padding: 0 0;
    }

    #stats {
        height: auto;
        padding: 0 0;
    }

    #dash-sep {
        height: auto;
        color: $surface-lighten-2;
        text-align: center;
    }

    #actions-row {
        height: auto;
        align: center middle;
        padding: 0 0;
    }

    #actions-row Static {
        width: auto;
        height: auto;
        min-width: 12;
        min-height: 1;
        text-align: center;
        padding: 0 1;
    }

    .nav-item {
        text-style: none;
        color: $text;
    }
    .nav-item:hover {
        background: $surface-lighten-1;
        text-style: bold;
    }
    .nav-item.focused-nav {
        background: $primary-darken-2;
        text-style: bold;
    }

    /* Scan screen */
    #scan-screen {
        width: auto;
        min-width: 60;
        max-width: 95vw;
        height: auto;
        max-height: 95vh;
        padding: 0 1;
    }

    #scan-title {
        height: auto;
        text-align: center;
        padding: 0 0;
    }

    #path-list {
        height: auto;
        max-height: 6;
        overflow-y: auto;
        padding: 0 1;
        margin: 0 0;
    }

    #path-label {
        height: auto;
        padding: 0 0;
        text-align: center;
    }

    .path-item {
        height: auto;
        padding: 0 1;
        text-style: none;
        color: $text;
    }
    .path-item.focused-nav {
        background: $primary-darken-2;
    }
    .path-item.checked {
        text-style: none;
    }
    .path-item.unchecked {
        text-style: none;
        color: $text-muted;
    }

    #stats-row {
        height: auto;
        align: center middle;
        padding: 0 0;
    }

    #stats-row Static {
        width: 1fr;
        height: auto;
        text-align: center;
    }

    #discovery-row, #scanning-row {
        height: auto;
    }

    #discovery-label, #scanning-label {
        width: 4;
        height: auto;
    }

    #discovery-bar, #scan-bar {
        width: 1fr;
        margin: 0;
    }

    #current-file {
        height: auto;
        padding: 0 0;
        text-align: center;
        color: $warning;
    }

    #scan-log {
        height: 1fr;
        min-height: 4;
        padding: 0 1;
        border: solid $surface-lighten-1;
    }

    #scan-actions {
        dock: bottom;
        height: auto;
        align: center middle;
        padding: 0 0;
    }

    #scan-actions Static {
        width: auto;
        min-width: 6;
        text-align: center;
        padding: 0 1;
        height: auto;
    }

    .scan-link {
        text-style: none;
        color: $text;
    }
    .scan-link.dimmed {
        text-style: none;
        color: $text-muted;
    }
    .scan-link.focused-nav {
        background: $primary-darken-2;
        text-style: bold;
    }
    .scan-link:hover {
        background: $surface-lighten-1;
        text-style: bold;
    }

    /* Report screen */
    #report-screen {
        width: auto;
        min-width: 60;
        max-width: 95vw;
        height: auto;
        max-height: 95vh;
        border: solid $primary;
        padding: 0 1;
    }

    #report-title {
        height: auto;
        text-align: center;
        padding: 0 0;
    }

    #report-tabs {
        height: auto;
        align: center middle;
        padding: 0 0;
    }

    .tab-link {
        width: auto;
        min-width: 10;
        min-height: 1;
        text-style: none;
        color: $text;
        padding: 0 1;
        height: auto;
    }
    .tab-link.active-tab {
        text-style: bold underline;
        color: $primary;
    }
    .tab-link.focused-nav {
        background: $primary-darken-2;
    }

    #report-table {
        height: 1fr;
        width: 100%;
        min-height: 6;
    }

    /* Reset dialog */
    #reset-dialog {
        width: auto;
        min-width: 40;
        max-width: 90vw;
        height: auto;
        border: solid $error;
        padding: 0 1;
        background: $surface;
    }

    #reset-title {
        text-align: center;
        padding-bottom: 0;
        height: auto;
    }

    #reset-message {
        text-align: center;
        padding-bottom: 0;
        height: auto;
    }

    #reset-actions {
        align: center middle;
        height: auto;
        padding: 0 0;
    }

    #reset-actions Static {
        width: auto;
        min-width: 10;
        text-align: center;
        padding: 0 1;
    }
    """


    SCREENS = {
        "dashboard": DashboardScreen,
        "scan": ScanScreen,
        "report": ReportScreen,
    }

    def on_mount(self) -> None:
        self.push_screen("dashboard")

    def action_quit(self) -> None:
        self.exit()
