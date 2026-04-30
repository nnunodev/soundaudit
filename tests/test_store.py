"""Tests for database store helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from soundaudit.db.store import Database, _enable_wal, reset_database


class TestEnableWal:
    def test_sets_pragmas(self) -> None:
        calls: list[str] = []

        class FakeConn:
            def execute(self, sql):
                calls.append(sql)

        _enable_wal(FakeConn(), None)  # type: ignore[arg-type]
        assert calls == [
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA cache_size=-64000",
            "PRAGMA busy_timeout = 5000",
        ]


class TestResetDatabase:
    def test_deletes_db_and_sidecars(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        # Create DB so engine exists
        db = Database(str(db_path))
        db.engine.dispose()

        wal = tmp_path / "test.db-wal"
        shm = tmp_path / "test.db-shm"
        wal.write_text("wal")
        shm.write_text("shm")

        assert db_path.exists()
        assert wal.exists()
        assert shm.exists()

        reset_database(str(db_path))

        assert not db_path.exists()
        assert not wal.exists()
        assert not shm.exists()

    def test_noop_when_db_missing(self, tmp_path: Path) -> None:
        db_path = tmp_path / "missing.db"
        # Should not raise even if file does not exist
        reset_database(str(db_path))
        assert not db_path.exists()

    def test_disposes_engine_before_unlink(self, tmp_path: Path) -> None:
        db_path = tmp_path / "locked.db"
        db = Database(str(db_path))
        # On Windows, dispose the original engine first so its pooled
        # connections release the file handle.  Database.close_all() handles
        # this for every engine SoundAudit has opened.
        Database.close_all()
        reset_database(str(db_path))
        assert not db_path.exists()


class TestDatabaseUpsert:
    def test_upsert_new_file(self, tmp_path: Path) -> None:
        from datetime import datetime
        from soundaudit.models import AudioFormat, FileInfo, TrackTags

        db = Database(str(tmp_path / "test.db"))
        info = FileInfo(
            path=tmp_path / "song.flac",
            size_bytes=1_000,
            mtime_ns=int(datetime.now().timestamp() * 1e9),
            format=AudioFormat.FLAC,
            tags=TrackTags(title="Song", artist="Artist"),
        )
        db.upsert_file(info)

        with db.session() as s:
            from soundaudit.db.store import DBFile

            row = s.query(DBFile).filter_by(path=str(info.path)).first()
            assert row is not None
            assert row.title == "Song"
            assert row.artist == "Artist"
            assert row.format == "flac"
            assert row.size_bytes == 1_000

    def test_upsert_updates_existing(self, tmp_path: Path) -> None:
        from datetime import datetime
        from soundaudit.models import AudioFormat, FileInfo, TrackTags

        db = Database(str(tmp_path / "test.db"))
        path = tmp_path / "song.flac"
        info1 = FileInfo(
            path=path,
            size_bytes=1_000,
            mtime_ns=int(datetime.now().timestamp() * 1e9),
            format=AudioFormat.FLAC,
            tags=TrackTags(title="Old", artist="Artist"),
        )
        db.upsert_file(info1)

        info2 = FileInfo(
            path=path,
            size_bytes=2_000,
            mtime_ns=int(datetime.now().timestamp() * 1e9),
            format=AudioFormat.FLAC,
            tags=TrackTags(title="New", artist="Artist"),
        )
        db.upsert_file(info2)

        with db.session() as s:
            from soundaudit.db.store import DBFile

            row = s.query(DBFile).filter_by(path=str(path)).first()
            assert row is not None
            assert row.title == "New"
            assert row.size_bytes == 2_000

    def test_get_existing_paths(self, tmp_path: Path) -> None:
        from datetime import datetime
        from soundaudit.models import AudioFormat, FileInfo, TrackTags

        db = Database(str(tmp_path / "test.db"))
        info = FileInfo(
            path=tmp_path / "a.flac",
            size_bytes=1,
            mtime_ns=int(datetime.now().timestamp() * 1e9),
            format=AudioFormat.FLAC,
            tags=TrackTags(),
        )
        db.upsert_file(info)
        existing = db.get_existing_paths()
        assert str(info.path) in existing

    def test_delete_by_paths(self, tmp_path: Path) -> None:
        from datetime import datetime
        from soundaudit.models import AudioFormat, FileInfo, TrackTags

        db = Database(str(tmp_path / "test.db"))
        for name in ("a.flac", "b.flac", "c.flac"):
            db.upsert_file(
                FileInfo(
                    path=tmp_path / name,
                    size_bytes=1,
                    mtime_ns=int(datetime.now().timestamp() * 1e9),
                    format=AudioFormat.FLAC,
                    tags=TrackTags(),
                )
            )
        removed = db.delete_by_paths({str(tmp_path / "a.flac"), str(tmp_path / "c.flac")})
        assert removed == 2

        with db.session() as s:
            from soundaudit.db.store import DBFile

            remaining = {r.path for r in s.query(DBFile).all()}
        assert remaining == {str(tmp_path / "b.flac")}
