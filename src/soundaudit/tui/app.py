"""Textual TUI application."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

from soundaudit.config import AppConfig
from soundaudit.tui.screens import (
    DashboardScreen,
    FixTagsScreen,
    OrganizeScreen,
    RepairTagsScreen,
    ReportScreen,
    ScanScreen,
)


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
        return AppConfig.from_yaml()

    def get_db_path(self) -> str:
        if self._db_path:
            return self._db_path
        return str(self.get_config().database.resolved())

    CSS_PATH = "soundaudit.tcss"

    SCREENS = {
        "dashboard": DashboardScreen,
        "scan": ScanScreen,
        "report": ReportScreen,
        "fix": FixTagsScreen,
        "repair": RepairTagsScreen,
        "organize": OrganizeScreen,
    }

    def on_mount(self) -> None:
        self.push_screen("dashboard")

    def action_quit(self) -> None:  # type: ignore[override]
        self.exit()
