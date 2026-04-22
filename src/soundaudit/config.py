"""Pydantic-based configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScanConfig(BaseModel):
    paths: list[str] = Field(default_factory=lambda: ["/mnt/nas2/Music"])
    extensions: list[str] = Field(default_factory=lambda: [".flac", ".mp3", ".m4a"])
    workers: int = Field(default=4, ge=1, le=32)
    follow_symlinks: bool = False

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, v: list[str]) -> list[str]:
        resolved = []
        for p in v:
            path = Path(p).expanduser().resolve()
            if path.exists():
                resolved.append(str(path))
            else:
                raise ValueError(f"Scan path does not exist: {p}")
        return resolved


class DatabaseConfig(BaseModel):
    path: str = "~/.local/share/soundaudit/scan.db"

    def resolved(self) -> Path:
        return Path(self.path).expanduser().resolve()


class MusicBrainzConfig(BaseModel):
    rate_limit: float = Field(default=1.0, ge=0.1)
    retry_count: int = Field(default=3, ge=0)


class FingerprintConfig(BaseModel):
    enabled: bool = True
    fpcalc_path: str = "/usr/bin/fpcalc"
    cache_only: bool = False


class ReportingConfig(BaseModel):
    include_spectrograms: bool = False
    min_similarity_for_duplicates: float = Field(default=0.95, ge=0.0, le=1.0)


class ActuatorConfig(BaseModel):
    dry_run: bool = True
    backup_before_write: bool = True
    rename_template: str = "{artist}/{album} [{year}]/{disc:02d}-{track:02d}. {title}.flac"


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOUNDAUDIT_",
        env_nested_delimiter="__",
    )

    scan: ScanConfig = Field(default_factory=ScanConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    resolvers: MusicBrainzConfig = Field(default_factory=MusicBrainzConfig)
    fingerprinting: FingerprintConfig = Field(default_factory=FingerprintConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)
    actuator: ActuatorConfig = Field(default_factory=ActuatorConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AppConfig":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)

    def to_yaml(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(self.model_dump(), f, default_flow_style=False, sort_keys=False)
