"""CLI command registration and integration tests."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from soundaudit.cli import app
from soundaudit.db.store import Database

runner = CliRunner()


class TestCLIRegistration:
    """Smoke-test that Typer registers commands correctly."""

    def test_app_import(self) -> None:
        assert app is not None

    def test_help_shows_commands(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "scan" in result.output
        assert "analyze" in result.output
        assert "resolve" in result.output
        assert "fix" in result.output
        assert "report" in result.output
        assert "clean-duplicates" in result.output
        assert "organize" in result.output
        assert "inspect-tags" in result.output
        assert "tui" in result.output
        assert "version" in result.output


class TestReportSubApp:
    """Smoke-test the report sub-command surface."""

    def test_report_sub_commands_exist(self) -> None:
        result = runner.invoke(app, ["report", "--help"])
        assert result.exit_code == 0
        assert "summary" in result.output
        assert "missing-tags" in result.output
        assert "duplicates" in result.output
        assert "transcodes" in result.output
        assert "corrupt" in result.output


class TestReportWithData:
    """CLI report commands backed by a seeded database."""

    def test_report_summary(self, seeded_db: Database) -> None:
        result = runner.invoke(app, ["report", "summary", "--db", str(seeded_db.engine.url).replace("sqlite:///", "")])
        assert result.exit_code == 0
        assert "5" in result.output or "files" in result.output.lower()

    def test_report_missing_tags(self, seeded_db: Database) -> None:
        db_path = str(seeded_db.engine.url).replace("sqlite:///", "")
        result = runner.invoke(app, ["report", "missing-tags", "--db", db_path])
        assert result.exit_code == 0
        assert "unknown_tags" in result.output

    def test_report_duplicates(self, seeded_db: Database) -> None:
        db_path = str(seeded_db.engine.url).replace("sqlite:///", "")
        result = runner.invoke(app, ["report", "duplicates", "--db", db_path])
        assert result.exit_code == 0
        assert "Group" in result.output or "KEEP" in result.output or "duplicate" in result.output.lower()

    def test_report_transcodes(self, seeded_db: Database) -> None:
        db_path = str(seeded_db.engine.url).replace("sqlite:///", "")
        result = runner.invoke(app, ["report", "transcodes", "--db", db_path])
        assert result.exit_code == 0
        assert "fake.flac" in result.output or "Transcode" in result.output

    def test_report_corrupt(self, seeded_db: Database) -> None:
        db_path = str(seeded_db.engine.url).replace("sqlite:///", "")
        result = runner.invoke(app, ["report", "corrupt", "--db", db_path])
        assert result.exit_code == 0
        assert "corrupt.mp3" in result.output

    def test_report_corrupt_delete_dry_run(self, seeded_db: Database) -> None:
        db_path = str(seeded_db.engine.url).replace("sqlite:///", "")
        result = runner.invoke(app, ["report", "corrupt", "--db", db_path, "--delete"])
        assert result.exit_code == 0
        # Should try deleting but fail on missing filesystem paths
        assert "Deleted" in result.output or "errors" in result.output or "corrupt" in result.output.lower()


class TestDeprecatedCommands:
    """Verify deprecated commands still exist but emit warnings."""

    def test_duplicates_emits_deprecation(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        result = runner.invoke(app, ["duplicates", "--db", db_path])
        assert result.exit_code == 0
        assert "deprecated" in result.output.lower()

    def test_normalize_tags_emits_deprecation(self, tmp_path: Path) -> None:
        # Provide a dummy path so Typer validation passes; deprecation prints
        dummy = tmp_path / "dummy.flac"
        dummy.write_bytes(b"")
        result = runner.invoke(app, ["normalize-tags", str(dummy)])
        assert result.exit_code in (0, 1)
        assert "deprecated" in result.output.lower()

    def test_standardize_tags_emits_deprecation(self, tmp_path: Path) -> None:
        dummy = tmp_path / "dummy.flac"
        dummy.write_bytes(b"")
        result = runner.invoke(app, ["standardize-tags", str(dummy)])
        assert result.exit_code in (0, 1)
        assert "deprecated" in result.output.lower()


class TestVersion:
    """Basic version flag / command checks."""

    def test_version_flag(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "SoundAudit" in result.output

    def test_version_command(self) -> None:
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "SoundAudit" in result.output
