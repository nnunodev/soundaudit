"""Tests for tag-normalization analyzer."""

from __future__ import annotations

from pathlib import Path

from soundaudit.analyzer.normalize import (
    _majority_value,
    _normalize_value,
    _tag_value,
    analyze_folder,
    scan_folders,
)
from soundaudit.models import TrackTags


def _make_tags(**kwargs) -> TrackTags:
    t = TrackTags()
    for k, v in kwargs.items():
        setattr(t, k, v)
    return t


class TestTagValue:
    def test_extract_existing(self):
        t = _make_tags(album="Test Album", year=2024)
        assert _tag_value(t, "album") == "Test Album"
        assert _tag_value(t, "year") == 2024

    def test_extract_missing(self):
        t = TrackTags()
        assert _tag_value(t, "album") is None
        assert _tag_value(t, "year") is None

    def test_year_must_be_int(self):
        t = TrackTags()
        t.year = "not an int"  # type: ignore[assignment]
        assert _tag_value(t, "year") is None


class TestNormalizeValue:
    def test_none(self):
        assert _normalize_value("album", None) is None

    def test_whitespace_collapse(self):
        assert _normalize_value("album", "  Dark\tSide  ") == "Dark Side"

    def test_year_int_preserved(self):
        assert _normalize_value("year", 1999) == 1999

    def test_year_non_int_rejected(self):
        assert _normalize_value("year", "nineteen") is None


class TestMajorityValue:
    def test_clear_majority(self):
        vals = ["A", "A", "B", None]
        assert _majority_value(vals) == "A"

    def test_below_min_agree(self):
        vals = ["A", "B", None]
        assert _majority_value(vals, min_agree=2) is None

    def test_below_threshold(self):
        vals = ["A", "B", "C", "D"]
        assert _majority_value(vals, threshold=0.5) is None

    def test_all_none(self):
        assert _majority_value([None, None]) is None


class TestAnalyzeFolder:
    def test_empty_folder_returns_none(self, tmp_path: Path):
        assert analyze_folder(tmp_path) is None

    def test_consistent_tags_returns_none(self, tmp_path: Path):
        f1 = tmp_path / "01.flac"
        f2 = tmp_path / "02.flac"
        f1.write_text("dummy")
        f2.write_text("dummy")

        def reader(path: Path):
            return _make_tags(album="Album", album_artist="Artist", year=2024, artist="Artist")

        assert analyze_folder(tmp_path, tag_reader=reader) is None

    def test_detects_inconsistency(self, tmp_path: Path):
        f1 = tmp_path / "01.flac"
        f2 = tmp_path / "02.flac"
        f3 = tmp_path / "03.flac"
        f1.write_text("dummy")
        f2.write_text("dummy")
        f3.write_text("dummy")

        tags = {
            f1: _make_tags(album="Album", album_artist="Artist", year=2024, artist="Artist"),
            f2: _make_tags(album="Album", album_artist="Artist", year=2024, artist="Artist"),
            f3: _make_tags(album="Albumm", album_artist="Artist", year=2024, artist="Artist"),
        }

        def reader(path: Path):
            return tags[path]

        result = analyze_folder(tmp_path, tag_reader=reader)
        assert result is not None
        assert any(fix.field == "album" for fix in result.fixes)

    def test_minority_file_gets_fix(self, tmp_path: Path):
        f1 = tmp_path / "01.flac"
        f2 = tmp_path / "02.flac"
        f3 = tmp_path / "03.flac"
        for f in (f1, f2, f3):
            f.write_text("dummy")

        tags = {
            f1: _make_tags(album="Album", album_artist="Artist", year=2024, artist="A"),
            f2: _make_tags(album="Album", album_artist="Artist", year=2024, artist="A"),
            f3: _make_tags(album="Album", album_artist="Artist", year=2024, artist="B"),
        }

        def reader(path: Path):
            return tags[path]

        result = analyze_folder(tmp_path, fields=("artist",), tag_reader=reader)
        assert result is not None
        assert len(result.fixes) == 1
        fix = result.fixes[0]
        assert fix.path == f3
        assert fix.current == "B"
        assert fix.proposed == "A"

    def test_below_min_files(self, tmp_path: Path):
        f1 = tmp_path / "01.flac"
        f1.write_text("dummy")

        def reader(path: Path):
            return _make_tags(album="Album")

        assert analyze_folder(tmp_path, min_files=2, tag_reader=reader) is None

    def test_ignores_non_audio(self, tmp_path: Path):
        (tmp_path / "01.flac").write_text("dummy")
        (tmp_path / "02.flac").write_text("dummy")
        (tmp_path / "cover.jpg").write_text("dummy")

        def reader(path: Path):
            return _make_tags(album="A")

        result = analyze_folder(tmp_path, tag_reader=reader)
        assert result is None  # all consistent

    def test_missing_majority_no_fixes(self, tmp_path: Path):
        f1 = tmp_path / "01.flac"
        f2 = tmp_path / "02.flac"
        f3 = tmp_path / "03.flac"
        for f in (f1, f2, f3):
            f.write_text("dummy")

        tags = {
            f1: _make_tags(album="A"),
            f2: _make_tags(album="B"),
            f3: _make_tags(album="C"),
        }

        def reader(path: Path):
            return tags[path]

        result = analyze_folder(tmp_path, fields=("album",), tag_reader=reader)
        assert result is None


class TestScanFolders:
    def test_recursive_scan(self, tmp_path: Path):
        album = tmp_path / "Artist" / "Album"
        album.mkdir(parents=True)
        f1 = album / "01.flac"
        f2 = album / "02.flac"
        f3 = album / "03.flac"
        f1.write_text("x")
        f2.write_text("x")
        f3.write_text("x")

        tags = {
            f1: _make_tags(album="Album", album_artist="Artist", year=2024, artist="A"),
            f2: _make_tags(album="Album", album_artist="Artist", year=2024, artist="A"),
            f3: _make_tags(album="Album", album_artist="Artist", year=2024, artist="B"),
        }

        def reader(path: Path):
            return tags[path]

        results = scan_folders([tmp_path], fields=("artist",), min_files=2, tag_reader=reader)
        assert len(results) == 1
        assert results[0].folder == album

    def test_no_results_for_consistent_library(self, tmp_path: Path):
        album = tmp_path / "Artist" / "Album"
        album.mkdir(parents=True)
        for name in ("01.flac", "02.flac"):
            (album / name).write_text("x")

        def reader(path: Path):
            return _make_tags(album="A", album_artist="B", year=1, artist="C")

        results = scan_folders([tmp_path], min_files=2, tag_reader=reader)
        assert results == []

    def test_file_path_becomes_parent(self, tmp_path: Path):
        f1 = tmp_path / "01.flac"
        f2 = tmp_path / "02.flac"
        f3 = tmp_path / "03.flac"
        f1.write_text("x")
        f2.write_text("x")
        f3.write_text("x")

        tags = {
            f1: _make_tags(album="A", album_artist="B", year=1, artist="C"),
            f2: _make_tags(album="A", album_artist="B", year=1, artist="C"),
            f3: _make_tags(album="A", album_artist="B", year=1, artist="D"),
        }

        def reader(path: Path):
            return tags[path]

        # scanning a single file path should treat its parent as the folder
        results = scan_folders([f1], fields=("artist",), min_files=2, tag_reader=reader)
        assert any(r.folder == tmp_path for r in results)
