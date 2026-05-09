"""Shared pytest fixtures for SoundAudit CLI integration tests.

Provides a temporary database fixture seeded with realistic rows for
report and CLI command tests.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import pytest

from soundaudit.db.store import Database, DBFile, DuplicateGroup
from soundaudit.models import AudioFormat, FileInfo, TrackTags


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Temporary SQLite path for a test database."""
    return tmp_path / "soundaudit.db"


@pytest.fixture
def seeded_db(db_path: Path) -> Generator[Database, None, None]:
    """Yield a Database instance backed by a pre-seeded temp file.

    The DB is closed on teardown so ``Database.close_all()`` is safe.
    """
    Database.close_all()
    db = Database(str(db_path))
    now = int(datetime.now().timestamp() * 1e9)

    files: list[FileInfo] = [
        FileInfo(
            path=Path("/music/Artist/Album/01 Song.flac"),
            size_bytes=25_000_000,
            mtime_ns=now,
            format=AudioFormat.FLAC,
            tags=TrackTags(title="Song", artist="Artist", album="Album", track_number=1, year=2023),
            sample_rate_hz=44100,
            bit_depth=16,
            lossless=True,
        ),
        FileInfo(
            path=Path("/music/Artist/Album/02 Track.mp3"),
            size_bytes=8_000_000,
            mtime_ns=now,
            format=AudioFormat.MP3,
            tags=TrackTags(title="Track", artist="Artist", album="Album", track_number=2, year=2023),
            sample_rate_hz=44100,
            bit_depth=None,
            lossless=False,
            bitrate_kbps=320.0,
        ),
        FileInfo(
            path=Path("/music/Bad/unknown_tags.flac"),
            size_bytes=10_000_000,
            mtime_ns=now,
            format=AudioFormat.FLAC,
            tags=TrackTags(),
            sample_rate_hz=44100,
            bit_depth=16,
            lossless=True,
        ),
        FileInfo(
            path=Path("/music/Bad/corrupt.mp3"),
            size_bytes=1_000,
            mtime_ns=now,
            format=AudioFormat.MP3,
            tags=TrackTags(title="Bad"),
            is_corrupt=True,
            corruption_reason="mutagen header error",
        ),
        FileInfo(
            path=Path("/music/Suspect/fake.flac"),
            size_bytes=30_000_000,
            mtime_ns=now,
            format=AudioFormat.FLAC,
            tags=TrackTags(title="Fake", artist="Artist"),
            sample_rate_hz=44100,
            bit_depth=16,
            lossless=True,
            is_transcode=True,
            transcode_confidence=0.95,
        ),
    ]
    for info in files:
        db.upsert_file(info)

    # Add a duplicate group so report duplicates has something to show
    with db.session() as s:
        g = DuplicateGroup(acoustid="samehash123", group_type="content_hash")
        s.add(g)
        s.commit()
        group_id = g.id
        for row in s.query(DBFile).filter(DBFile.path.like("%Artist/Album%")).all():
            row.content_hash = "samehash123"
            row.duplicate_group_id = group_id
        s.commit()

    yield db
    db.engine.dispose()
    Database.close_all()
