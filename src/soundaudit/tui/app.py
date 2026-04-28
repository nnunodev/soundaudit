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
        width: 54;
        height: auto;
        border: solid $primary;
        padding: 1 1;
    }

    #title {
        text-align: center;
        padding-bottom: 1;
    }

    #stats-panel {
        width: 100%;
        height: auto;
        padding: 1 0;
    }

    #actions-panel {
        width: 100%;
        height: auto;
        align: center middle;
    }

    #actions-panel Button {
        width: 80%;
        margin: 0 0;
    }

    /* Scan screen */
    #scan-screen {
        width: 80;
        height: 28;
        border: solid $primary;
        padding: 1 1;
        align: center top;
    }

    #scan-title {
        height: auto;
        text-align: center;
        padding-bottom: 0;
    }

    #scan-paths {
        height: auto;
        text-align: center;
        padding: 1 0;
        color: $text-muted;
    }

    #path-list {
        height: auto;
        max-height: 10;
        overflow-y: auto;
        padding: 0 1;
        margin: 0 0;
        border: solid $surface-lighten-1;
    }

    #path-label {
        height: auto;
        padding: 0 0;
        text-align: center;
    }

    .path-checkbox {
        padding: 0 1;
        margin: 0;
        height: auto;
    }

    #progress-row {
        height: auto;
    }

    #progress-col {
        width: 60%;
        height: auto;
        padding: 0 1;
    }

    #stats-col {
        width: 40%;
        height: auto;
        padding: 0 1;
    }

    #scan-bar, #discovery-bar {
        margin: 1 0;
    }

    #current-file {
        height: auto;
        padding: 0 0;
        text-align: center;
    }

    #scan-log {
        height: 1fr;
        min-height: 3;
        max-height: 12;
        padding: 0 1;
    }

    #scan-actions {
        dock: bottom;
        height: auto;
        align: center middle;
        padding: 0 0;
    }

    #scan-actions Button {
        width: 30%;
        margin: 0 1;
    }

    /* Report screen */
    #report-screen {
        width: 80;
        height: 28;
        border: solid $primary;
        padding: 1 1;
    }

    #report-title {
        height: auto;
        text-align: center;
        padding-bottom: 0;
    }

    #report-tabs {
        height: auto;
        align: center middle;
        padding: 0 0;
    }

    #report-tabs Button {
        width: 30%;
        margin: 0 1;
    }

    #report-table {
        height: 1fr;
        width: 100%;
    }

    /* Reset dialog */
    #reset-dialog {
        width: 50;
        height: auto;
        border: solid $error;
        padding: 1 2;
        background: $surface;
    }

    #reset-title {
        text-align: center;
        padding-bottom: 1;
    }

    #reset-message {
        text-align: center;
        padding-bottom: 1;
    }

    #reset-actions {
        align: center middle;
        height: auto;
    }

    #reset-actions Button {
        width: 40%;
        margin: 0 1;
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
