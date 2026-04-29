"""Tests for reporter/export module."""

from __future__ import annotations

import json
from datetime import datetime
from io import StringIO
from pathlib import Path

import pytest

from soundaudit.reporter import (
    MarkdownSection,
    ReportExporter,
    _csv_value,
    infer_format,
)


class TestInferFormat:
    def test_json_extension(self) -> None:
        assert infer_format(Path("report.json")) == "json"

    def test_csv_extension(self) -> None:
        assert infer_format(Path("data.csv")) == "csv"

    def test_md_extension(self) -> None:
        assert infer_format(Path("notes.md")) == "markdown"

    def test_txt_extension(self) -> None:
        assert infer_format(Path("dump.txt")) == "markdown"

    def test_unknown_extension(self) -> None:
        assert infer_format(Path("output.bin")) == "markdown"


class TestCsvValue:
    def test_none(self) -> None:
        assert _csv_value(None) == ""

    def test_bool(self) -> None:
        assert _csv_value(True) == "true"
        assert _csv_value(False) == "false"

    def test_list(self) -> None:
        assert _csv_value(["a", "b"]) == "a; b"

    def test_string(self) -> None:
        assert _csv_value("hello") == "hello"


class TestReportExporterJson:
    def test_writes_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        exporter = ReportExporter(path)
        exporter.write_json({"key": "value", "num": 42})
        with open(path) as f:
            data = json.load(f)
        assert data == {"key": "value", "num": 42}

    def test_serializes_datetime(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        exporter = ReportExporter(path)
        exporter.write_json({"ts": datetime(2024, 1, 1, 12, 0, 0)})
        with open(path) as f:
            data = json.load(f)
        assert data["ts"].startswith("2024-01-01")

    def test_pretty_print(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"
        exporter = ReportExporter(path)
        exporter.write_json({"a": 1})
        text = path.read_text()
        assert '"a": 1' in text


class TestReportExporterCsv:
    def test_writes_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "out.csv"
        exporter = ReportExporter(path)
        exporter.write_csv([{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}])
        text = path.read_text()
        assert "name,age" in text
        assert "Alice,30" in text
        assert "Bob,25" in text

    def test_empty_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "out.csv"
        exporter = ReportExporter(path)
        exporter.write_csv([])
        assert path.read_text() == ""


class TestReportExporterMarkdown:
    def test_writes_sections(self, tmp_path: Path) -> None:
        path = tmp_path / "out.md"
        exporter = ReportExporter(path)
        sections = [
            MarkdownSection(
                heading="Summary",
                headers=["Metric", "Count"],
                rows=[["Files", "10"]],
            )
        ]
        exporter.write_markdown("Test Report", sections)
        text = path.read_text()
        assert "# Test Report" in text
        assert "## Summary" in text
        assert "| Metric | Count |" in text
        assert "| Files | 10 |" in text

    def test_escapes_pipe(self, tmp_path: Path) -> None:
        path = tmp_path / "out.md"
        exporter = ReportExporter(path)
        sections = [
            MarkdownSection(
                heading="Data",
                headers=["Col"],
                rows=[["a | b"]],
            )
        ]
        exporter.write_markdown("R", sections)
        text = path.read_text()
        assert "a \\| b" in text

    def test_empty_rows_shows_no_data(self, tmp_path: Path) -> None:
        path = tmp_path / "out.md"
        exporter = ReportExporter(path)
        sections = [
            MarkdownSection(
                heading="Empty",
                headers=["A"],
                rows=[],
            )
        ]
        exporter.write_markdown("R", sections)
        text = path.read_text()
        assert "No data." in text
