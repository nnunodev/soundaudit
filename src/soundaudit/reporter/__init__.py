"""Export report data to JSON, CSV, or Markdown."""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import asdict
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, TextIO


class ReportExporter:
    """Write structured report data to disk in various formats."""

    def __init__(self, destination: Path | TextIO) -> None:
        self.dest = destination

    def _open(self) -> TextIO:
        if hasattr(self.dest, "write"):
            return self.dest  # type: ignore[return-value]
        return open(self.dest, "w", newline="", encoding="utf-8")

    def write_json(self, data: dict[str, Any]) -> None:
        """Pretty-printed JSON with datetime serialization."""
        with self._open() as fh:
            json.dump(data, fh, indent=2, default=_json_default, ensure_ascii=False)
            fh.write("\n")

    def write_csv(self, rows: list[dict[str, Any]]) -> None:
        """Write flat rows as CSV. Keys of the first row become headers."""
        if not rows:
            with self._open() as fh:
                fh.write("")
            return
        keys = list(rows[0].keys())
        with self._open() as fh:
            writer = csv.DictWriter(fh, fieldnames=keys)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: _csv_value(v) for k, v in row.items()})

    def write_markdown(self, title: str, sections: list[MarkdownSection]) -> None:
        """Write a Markdown document with tables."""
        lines: list[str] = [f"# {title}", ""]
        lines.append(f"_Generated: {datetime.now().isoformat()}_")
        lines.append("")
        for section in sections:
            lines.append(f"## {section.heading}")
            lines.append("")
            if section.paragraph:
                lines.append(section.paragraph)
                lines.append("")
            if section.rows:
                lines.append(_md_table(section.headers, section.rows))
                lines.append("")
            else:
                lines.append("*No data.*")
                lines.append("")
        with self._open() as fh:
            fh.write("\n".join(lines))


class MarkdownSection:
    def __init__(
        self,
        heading: str,
        headers: list[str],
        rows: list[list[str]],
        paragraph: str = "",
    ) -> None:
        self.heading = heading
        self.headers = headers
        self.rows = rows
        self.paragraph = paragraph


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return {k: getattr(obj, k) for k in obj.__dataclass_fields__}
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _csv_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x) for x in v)
    return str(v)


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    def esc(cell: str) -> str:
        return cell.replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(esc(h) for h in headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(esc(str(c)) for c in row) + " |")
    return "\n".join(lines)


def infer_format(path: Path) -> str:
    """Guess export format from file extension."""
    ext = path.suffix.lower()
    if ext in (".json",):
        return "json"
    if ext in (".csv",):
        return "csv"
    if ext in (".md", ".markdown", ".txt"):
        return "markdown"
    return "markdown"
