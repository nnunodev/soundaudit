"""Tests for duplicate detection analyzer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from soundaudit.analyzer.duplicates import (
    DuplicateAnalyzer,
    KeeperVerdict,
    _human_size,
    analyze_keepers,
    find_duplicate_groups,
    write_duplicate_groups,
)
from soundaudit.db.store import DBFile, Database, DuplicateGroup


@pytest.fixture
def db(tmp_path: Path):
    path = str(tmp_path / "test.db")
    database = Database(path)
    yield database
    database.engine.dispose()


def _insert_files(database: Database, files_data: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    with database.session() as s:
        for data in files_data:
            f = DBFile(
                path=data["path"],
                size_bytes=data["size_bytes"],
                mtime=now,
                format=data.get("format", "flac"),
                content_hash=data.get("content_hash"),
                lossless=data.get("lossless", 0),
                bit_depth=data.get("bit_depth"),
                sample_rate_hz=data.get("sample_rate_hz"),
                bitrate_kbps=data.get("bitrate_kbps"),
                title=data.get("title"),
                artist=data.get("artist"),
                album=data.get("album"),
                album_artist=data.get("album_artist"),
                year=data.get("year"),
                genre=data.get("genre"),
                isrc=data.get("isrc"),
            )
            s.add(f)
        s.commit()


class TestFindDuplicateGroups:
    def test_no_duplicates(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {"path": "/a/1.flac", "size_bytes": 1000, "content_hash": "h1"},
                {"path": "/a/2.flac", "size_bytes": 2000, "content_hash": "h2"},
            ],
        )
        results = find_duplicate_groups(db)
        assert results == []

    def test_finds_duplicate_group(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {"path": "/a/1.flac", "size_bytes": 10_000_000, "content_hash": "dup"},
                {"path": "/a/2.flac", "size_bytes": 10_000_100, "content_hash": "dup"},
                {"path": "/b/3.flac", "size_bytes": 5_000_000, "content_hash": "uniq"},
            ],
        )
        results = find_duplicate_groups(db)
        assert len(results) == 1
        assert results[0].content_hash == "dup"
        assert results[0].file_count == 2
        assert results[0].total_size_bytes == 20_000_100
        # wasted = total - max = 20_000_100 - 10_000_100 = 10_000_000
        assert results[0].wasted_bytes == 10_000_000

    def test_ignores_null_hashes(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {"path": "/a/1.flac", "size_bytes": 1000, "content_hash": None},
                {"path": "/a/2.flac", "size_bytes": 1000, "content_hash": None},
            ],
        )
        results = find_duplicate_groups(db)
        assert results == []

    def test_sorted_by_wasted_desc(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {"path": "/a/1.flac", "size_bytes": 1_000, "content_hash": "small"},
                {"path": "/a/2.flac", "size_bytes": 1_000, "content_hash": "small"},
                {"path": "/b/1.flac", "size_bytes": 10_000_000, "content_hash": "big"},
                {"path": "/b/2.flac", "size_bytes": 10_000_000, "content_hash": "big"},
            ],
        )
        results = find_duplicate_groups(db)
        assert [r.content_hash for r in results] == ["big", "small"]


class TestWriteDuplicateGroups:
    def test_clears_old_and_writes_new(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {"path": "/a/1.flac", "size_bytes": 1000, "content_hash": "dup"},
                {"path": "/a/2.flac", "size_bytes": 1000, "content_hash": "dup"},
            ],
        )
        results = find_duplicate_groups(db)
        written = write_duplicate_groups(db, results)
        assert written == 1

        with db.session() as s:
            group = s.query(DuplicateGroup).first()
            assert group is not None
            assert group.acoustid == "dup"
            files = s.query(DBFile).filter_by(duplicate_group_id=group.id).all()
            assert len(files) == 2

    def test_overwrites_previous_groups(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {"path": "/a/1.flac", "size_bytes": 1000, "content_hash": "old"},
                {"path": "/a/2.flac", "size_bytes": 1000, "content_hash": "old"},
            ],
        )
        results = find_duplicate_groups(db)
        write_duplicate_groups(db, results)

        # Make hashes unique so no duplicates exist
        with db.session() as s:
            for f in s.query(DBFile).all():
                f.content_hash = f.path
            s.commit()

        results = find_duplicate_groups(db)
        write_duplicate_groups(db, results)

        with db.session() as s:
            assert s.query(DuplicateGroup).count() == 0
            assert s.query(DBFile).filter(DBFile.duplicate_group_id.is_not(None)).count() == 0


class TestDuplicateAnalyzer:
    def test_run_integration(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {"path": "/a/1.flac", "size_bytes": 10_000_000, "content_hash": "dup"},
                {"path": "/a/2.flac", "size_bytes": 10_000_100, "content_hash": "dup"},
                {"path": "/b/3.flac", "size_bytes": 5_000_000, "content_hash": "uniq"},
            ],
        )
        analyzer = DuplicateAnalyzer(db)
        results = analyzer.run()
        assert len(results) == 1
        assert results[0].content_hash == "dup"
        assert results[0].group_id is not None


class TestKeeperAnalysis:
    def test_prefers_album_over_single(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {
                    "path": "/music/Singles/Artist - Song.flac",
                    "size_bytes": 20_000_000,
                    "content_hash": "dup",
                    "lossless": 1,
                    "bit_depth": 16,
                    "sample_rate_hz": 44100,
                    "title": "Song",
                    "artist": "Artist",
                    "album": "Song",
                },
                {
                    "path": "/music/Albums/Artist - The Album/02 Song.flac",
                    "size_bytes": 25_000_000,
                    "content_hash": "dup",
                    "lossless": 1,
                    "bit_depth": 24,
                    "sample_rate_hz": 96000,
                    "title": "Song",
                    "artist": "Artist",
                    "album": "The Album",
                    "year": 2024,
                    "genre": "Rock",
                },
            ],
        )
        results = find_duplicate_groups(db)
        assert len(results) == 1
        verdict = analyze_keepers(results[0])

        assert len(verdict.file_verdicts) == 2
        keep = [v for v in verdict.file_verdicts if v.verdict == KeeperVerdict.KEEP][0]
        delete = [v for v in verdict.file_verdicts if v.verdict == KeeperVerdict.DELETE][0]

        assert "Albums" in keep.db_file.path
        assert "Singles" in delete.db_file.path
        assert keep.db_file.bit_depth == 24

    def test_prefers_lossless_over_lossy(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {
                    "path": "/a/1.flac",
                    "size_bytes": 20_000_000,
                    "content_hash": "dup",
                    "lossless": 1,
                    "bit_depth": 16,
                    "sample_rate_hz": 44100,
                    "title": "Song",
                    "artist": "Artist",
                    "album": "Album",
                },
                {
                    "path": "/a/1.mp3",
                    "size_bytes": 5_000_000,
                    "content_hash": "dup",
                    "lossless": 0,
                    "bitrate_kbps": 320,
                    "sample_rate_hz": 44100,
                    "title": "Song",
                    "artist": "Artist",
                    "album": "Album",
                },
            ],
        )
        results = find_duplicate_groups(db)
        verdict = analyze_keepers(results[0])

        keep = [v for v in verdict.file_verdicts if v.verdict == KeeperVerdict.KEEP][0]
        assert keep.db_file.format == "flac"

    def test_review_on_tie(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {
                    "path": "/a/1.flac",
                    "size_bytes": 10_000_000,
                    "content_hash": "dup",
                    "lossless": 1,
                    "bit_depth": 16,
                    "sample_rate_hz": 44100,
                    "title": "Song",
                    "artist": "Artist",
                    "album": "Album",
                },
                {
                    "path": "/b/1.flac",
                    "size_bytes": 10_000_000,
                    "content_hash": "dup",
                    "lossless": 1,
                    "bit_depth": 16,
                    "sample_rate_hz": 44100,
                    "title": "Song",
                    "artist": "Artist",
                    "album": "Album",
                },
            ],
        )
        results = find_duplicate_groups(db)
        verdict = analyze_keepers(results[0])

        # Both are virtually identical — one KEEP, one should be REVIEW (close score)
        verdicts = [v.verdict for v in verdict.file_verdicts]
        assert KeeperVerdict.KEEP in verdicts
        # At least one REVIEW or DELETE
        assert any(v in verdicts for v in (KeeperVerdict.REVIEW, KeeperVerdict.DELETE))


class TestHumanSize:
    def test_human_size(self) -> None:
        assert _human_size(512) == "512.0 B"
        assert _human_size(1024) == "1.0 KB"
        assert _human_size(1024 * 1024) == "1.0 MB"
        assert _human_size(1024 * 1024 * 1024) == "1.0 GB"
