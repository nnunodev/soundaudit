"""Tests for AcoustID/chromaprint duplicate detection."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from soundaudit.analyzer.acoustid import (
    AcoustidDuplicateAnalyzer,
    DupType,
    _classify_dup_type,
    analyze_acoustid_keepers,
    find_acoustid_groups,
    write_acoustid_groups,
)
from soundaudit.analyzer.duplicates import DuplicateGroupResult
from soundaudit.db.store import DBFile, Database


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
                acoustid_fingerprint=data.get("acoustid_fingerprint"),
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


class TestFindAcoustidGroups:
    def test_no_groups_when_no_fingerprints(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {"path": "/a/1.flac", "size_bytes": 1000, "content_hash": "h1"},
                {"path": "/a/2.flac", "size_bytes": 2000, "content_hash": "h2"},
            ],
        )
        results = find_acoustid_groups(db)
        assert results == []

    def test_finds_acoustid_group(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {
                    "path": "/a/1.flac",
                    "size_bytes": 10_000_000,
                    "acoustid_fingerprint": "fp123",
                    "content_hash": "h1",
                },
                {
                    "path": "/a/2.mp3",
                    "size_bytes": 5_000_000,
                    "acoustid_fingerprint": "fp123",
                    "content_hash": "h2",
                },
                {
                    "path": "/b/3.flac",
                    "size_bytes": 5_000_000,
                    "acoustid_fingerprint": "fp999",
                    "content_hash": "h3",
                },
            ],
        )
        results = find_acoustid_groups(db)
        assert len(results) == 1
        assert results[0].content_hash == "fp123"
        assert results[0].file_count == 2
        assert results[0].total_size_bytes == 15_000_000

    def test_ignores_null_fingerprints(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {"path": "/a/1.flac", "size_bytes": 1000, "acoustid_fingerprint": None},
                {"path": "/a/2.flac", "size_bytes": 1000, "acoustid_fingerprint": None},
            ],
        )
        results = find_acoustid_groups(db)
        assert results == []

    def test_sorted_by_wasted_desc(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {
                    "path": "/a/1.flac",
                    "size_bytes": 1_000,
                    "acoustid_fingerprint": "small",
                },
                {
                    "path": "/a/2.flac",
                    "size_bytes": 1_000,
                    "acoustid_fingerprint": "small",
                },
                {
                    "path": "/b/1.flac",
                    "size_bytes": 10_000_000,
                    "acoustid_fingerprint": "big",
                },
                {
                    "path": "/b/2.flac",
                    "size_bytes": 10_000_000,
                    "acoustid_fingerprint": "big",
                },
            ],
        )
        results = find_acoustid_groups(db)
        assert [r.content_hash for r in results] == ["big", "small"]


class TestWriteAcoustidGroups:
    def test_clears_old_and_writes_new(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {
                    "path": "/a/1.flac",
                    "size_bytes": 1000,
                    "acoustid_fingerprint": "dup",
                },
                {
                    "path": "/a/2.flac",
                    "size_bytes": 1000,
                    "acoustid_fingerprint": "dup",
                },
            ],
        )
        results = find_acoustid_groups(db)
        written = write_acoustid_groups(db, results)
        assert written == 1

        with db.session() as s:
            from soundaudit.db.store import AcoustidGroup

            group = s.query(AcoustidGroup).first()
            assert group is not None
            assert group.fingerprint == "dup"
            files = s.query(DBFile).filter_by(acoustid_group_id=group.id).all()
            assert len(files) == 2

    def test_overwrites_previous_groups(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {
                    "path": "/a/1.flac",
                    "size_bytes": 1000,
                    "acoustid_fingerprint": "old",
                },
                {
                    "path": "/a/2.flac",
                    "size_bytes": 1000,
                    "acoustid_fingerprint": "old",
                },
            ],
        )
        results = find_acoustid_groups(db)
        write_acoustid_groups(db, results)

        # Make fingerprints unique so no groups exist
        with db.session() as s:
            for f in s.query(DBFile).all():
                f.acoustid_fingerprint = f.path
            s.commit()

        results = find_acoustid_groups(db)
        write_acoustid_groups(db, results)

        with db.session() as s:
            from soundaudit.db.store import AcoustidGroup

            assert s.query(AcoustidGroup).count() == 0
            assert s.query(DBFile).filter(DBFile.acoustid_group_id.is_not(None)).count() == 0


class TestDupTypeClassification:
    def test_bit_for_bit_when_shared_hash(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {"path": "/a/1.flac", "size_bytes": 1, "content_hash": "same"},
                {"path": "/a/2.flac", "size_bytes": 1, "content_hash": "same"},
            ],
        )
        with db.session() as s:
            files = s.query(DBFile).all()
        mapping = _classify_dup_type(files)
        assert mapping[files[0].id] == DupType.BIT_FOR_BIT
        assert mapping[files[1].id] == DupType.BIT_FOR_BIT

    def test_transcode_when_different_hash(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {"path": "/a/1.flac", "size_bytes": 1, "content_hash": "h1"},
                {"path": "/a/2.mp3", "size_bytes": 1, "content_hash": "h2"},
            ],
        )
        with db.session() as s:
            files = s.query(DBFile).all()
        mapping = _classify_dup_type(files)
        assert mapping[files[0].id] == DupType.TRANSCODE
        assert mapping[files[1].id] == DupType.TRANSCODE

    def test_mixed_group(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {"path": "/a/1.flac", "size_bytes": 1, "content_hash": "same"},
                {"path": "/a/2.flac", "size_bytes": 1, "content_hash": "same"},
                {"path": "/a/3.mp3", "size_bytes": 1, "content_hash": "different"},
            ],
        )
        with db.session() as s:
            files = s.query(DBFile).all()
        mapping = _classify_dup_type(files)
        assert mapping[files[0].id] == DupType.BIT_FOR_BIT
        assert mapping[files[1].id] == DupType.BIT_FOR_BIT
        assert mapping[files[2].id] == DupType.TRANSCODE


class TestAcoustidKeeperAnalysis:
    def test_transcode_flagged_in_reasons(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {
                    "path": "/a/1.flac",
                    "size_bytes": 20_000_000,
                    "acoustid_fingerprint": "fp",
                    "content_hash": "h1",
                    "lossless": 1,
                    "bit_depth": 24,
                    "sample_rate_hz": 96000,
                    "title": "Song",
                    "artist": "Artist",
                    "album": "Album",
                },
                {
                    "path": "/a/2.mp3",
                    "size_bytes": 5_000_000,
                    "acoustid_fingerprint": "fp",
                    "content_hash": "h2",
                    "lossless": 0,
                    "bitrate_kbps": 320,
                    "sample_rate_hz": 44100,
                    "title": "Song",
                    "artist": "Artist",
                    "album": "Album",
                },
            ],
        )
        results = find_acoustid_groups(db)
        verdict = analyze_acoustid_keepers(results[0])

        types = {fv.db_file.path: fv.dup_type for fv in verdict.file_verdicts}
        assert all(dt == DupType.TRANSCODE for dt in types.values())

        flac_verdict = [fv for fv in verdict.file_verdicts if fv.db_file.format == "flac"][0]
        assert "transcode" in flac_verdict.reasons

    def test_bit_for_bit_flagged(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {
                    "path": "/a/1.flac",
                    "size_bytes": 20_000_000,
                    "acoustid_fingerprint": "fp",
                    "content_hash": "same",
                    "lossless": 1,
                    "bit_depth": 24,
                    "sample_rate_hz": 96000,
                    "title": "Song",
                    "artist": "Artist",
                    "album": "Album",
                },
                {
                    "path": "/a/2.flac",
                    "size_bytes": 20_000_000,
                    "acoustid_fingerprint": "fp",
                    "content_hash": "same",
                    "lossless": 1,
                    "bit_depth": 24,
                    "sample_rate_hz": 96000,
                    "title": "Song",
                    "artist": "Artist",
                    "album": "Album",
                },
            ],
        )
        results = find_acoustid_groups(db)
        verdict = analyze_acoustid_keepers(results[0])

        assert all(fv.dup_type == DupType.BIT_FOR_BIT for fv in verdict.file_verdicts)
        assert all("bit-for-bit" in fv.reasons for fv in verdict.file_verdicts)


class TestAcoustidDuplicateAnalyzer:
    def test_run_integration(self, db: Database) -> None:
        _insert_files(
            db,
            [
                {
                    "path": "/a/1.flac",
                    "size_bytes": 10_000_000,
                    "acoustid_fingerprint": "fp",
                    "content_hash": "h1",
                },
                {
                    "path": "/a/2.mp3",
                    "size_bytes": 5_000_000,
                    "acoustid_fingerprint": "fp",
                    "content_hash": "h2",
                },
                {
                    "path": "/b/3.flac",
                    "size_bytes": 5_000_000,
                    "acoustid_fingerprint": "fp2",
                    "content_hash": "h3",
                },
            ],
        )
        analyzer = AcoustidDuplicateAnalyzer(db)
        results = analyzer.run()
        assert len(results) == 1
        assert results[0].content_hash == "fp"
        assert results[0].group_id is not None
