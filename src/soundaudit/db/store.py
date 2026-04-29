"""SQLite database layer with SQLAlchemy 2.0 — async-ready schema."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from soundaudit.models import AudioFormat, FileInfo, TrackTags


class Base(DeclarativeBase):
    pass


class DBFile(Base):  # type: ignore[valid-type,misc]
    __tablename__ = "files"

    id = Column(Integer, primary_key=True)
    path = Column(String, unique=True, nullable=False, index=True)
    size_bytes = Column(Integer, nullable=False)
    mtime = Column(DateTime, nullable=False)

    # Format info
    format = Column(String, nullable=False, default="UNKNOWN")
    sample_rate_hz = Column(Integer)
    bit_depth = Column(Integer)
    channels = Column(Integer)
    bitrate_kbps = Column(Float)
    duration_seconds = Column(Float)
    lossless = Column(Integer, default=0)  # SQLite bool

    # Tags
    title = Column(String)
    artist = Column(String, index=True)
    album = Column(String, index=True)
    album_artist = Column(String, index=True)
    track_number = Column(Integer)
    track_total = Column(Integer)
    disc_number = Column(Integer)
    disc_total = Column(Integer)
    year = Column(Integer)
    genre = Column(String)
    isrc = Column(String)
    comment = Column(Text)
    lyrics = Column(Text)
    publisher = Column(String)
    composer = Column(String)
    replaygain_track_gain = Column(Float)
    replaygain_track_peak = Column(Float)
    replaygain_album_gain = Column(Float)
    replaygain_album_peak = Column(Float)
    cover_mime_type = Column(String)
    cover_size = Column(Integer)

    # Signatures
    content_hash = Column(String, index=True)
    hash_algo = Column(String, default="xxhash3_64")
    acoustid_fingerprint = Column(Text)
    acoustid_duration_ms = Column(Integer)

    # Scan tracking
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_scanned = Column(DateTime, default=datetime.utcnow)
    scan_id = Column(Integer, default=0)

    # Analysis flags
    is_corrupt = Column(Integer, default=0)
    is_transcode = Column(Integer, default=0)
    transcode_confidence = Column(Float, default=0.0)
    corruption_reason = Column(Text)
    duplicate_group_id = Column(Integer)
    acoustid_group_id = Column(Integer, index=True)

    def __repr__(self) -> str:
        return f"<DBFile {self.path}>"


class DuplicateGroup(Base):  # type: ignore[valid-type,misc]
    __tablename__ = "duplicate_groups"

    id = Column(Integer, primary_key=True)
    acoustid = Column(String, index=True)
    group_type = Column(String, default="content_hash")
    created = Column(DateTime, default=datetime.utcnow)


class AcoustidGroup(Base):  # type: ignore[valid-type,misc]
    """Fuzzy duplicate groups created from identical chromaprint fingerprints."""

    __tablename__ = "acoustid_groups"

    id = Column(Integer, primary_key=True)
    fingerprint = Column(String, index=True, nullable=False)
    created = Column(DateTime, default=datetime.utcnow)


class ScanHistory(Base):  # type: ignore[valid-type,misc]
    __tablename__ = "scan_history"

    id = Column(Integer, primary_key=True)
    started = Column(DateTime, default=datetime.utcnow)
    finished = Column(DateTime)
    files_found = Column(Integer, default=0)
    files_changed = Column(Integer, default=0)
    duration_seconds = Column(Float)


def _migrate_table(engine, table_name: str, model_cls) -> None:
    """Add any columns present in the model but missing from the SQLite table."""
    inspector = inspect(engine)
    if not inspector.has_table(table_name):
        return
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    for col in model_cls.__table__.columns:
        if col.name not in existing:
            # SQLite ALTER TABLE ADD COLUMN cannot add PRIMARY KEY / UNIQUE / NOT NULL without default.
            # All our new columns are nullable, so this is safe.
            with engine.begin() as conn:
                conn.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col.type}")
                )


class Database:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        # Auto-migrate tables to add any new columns since last version
        _migrate_table(self.engine, "files", DBFile)
        _migrate_table(self.engine, "duplicate_groups", DuplicateGroup)
        _migrate_table(self.engine, "scan_history", ScanHistory)
        # Enable WAL for better concurrent reads
        event.listen(self.engine, "connect", _enable_wal)

    def session(self) -> Session:
        return self.Session()

    def get_existing_paths(self) -> dict[str, float]:
        """Return {path: mtime_timestamp} for incremental scanning."""
        with self.session() as s:
            rows = s.query(DBFile.path, DBFile.mtime).all()
            return {row.path: row.mtime.timestamp() for row in rows}

    def delete_by_paths(self, paths: set[str]) -> int:
        """Remove DB rows for given paths. Returns number deleted."""
        if not paths:
            return 0
        BATCH = 500
        deleted = 0
        with self.session() as s:
            path_list = list(paths)
            for i in range(0, len(path_list), BATCH):
                batch = path_list[i : i + BATCH]
                result = s.query(DBFile).filter(DBFile.path.in_(batch)).delete(
                    synchronize_session=False
                )
                deleted += result
            s.commit()
        return deleted

    def upsert_file(self, info: FileInfo, session: Optional[Session] = None) -> None:
        """Insert new or update existing."""
        own_session = session is None
        s = session or self.session()
        try:
            existing = s.query(DBFile).filter_by(path=str(info.path)).first()
            if existing:
                self._update_file(existing, info)
            else:
                db_file = DBFile(
                    path=str(info.path),
                    size_bytes=info.size_bytes,
                    mtime=info.mtime_dt(),
                )
                self._update_file(db_file, info)
                s.add(db_file)
            s.commit()
        finally:
            if own_session:
                s.close()

    @staticmethod
    def _update_file(db_file: DBFile, info: FileInfo) -> None:
        db_file.size_bytes = info.size_bytes
        db_file.mtime = info.mtime_dt()
        db_file.format = info.format.value
        db_file.sample_rate_hz = info.sample_rate_hz
        db_file.bit_depth = info.bit_depth
        db_file.channels = info.channels
        db_file.bitrate_kbps = info.bitrate_kbps
        db_file.duration_seconds = info.duration_seconds
        db_file.lossless = int(info.lossless)

        tags = info.tags
        db_file.title = tags.title
        db_file.artist = tags.artist
        db_file.album = tags.album
        db_file.album_artist = tags.album_artist
        db_file.track_number = tags.track_number
        db_file.track_total = tags.track_total
        db_file.disc_number = tags.disc_number
        db_file.disc_total = tags.disc_total
        db_file.year = tags.year
        db_file.genre = tags.genre
        db_file.isrc = tags.isrc
        db_file.comment = tags.comment
        db_file.lyrics = tags.lyrics
        db_file.publisher = tags.publisher
        db_file.composer = tags.composer
        db_file.replaygain_track_gain = tags.replaygain_track_gain
        db_file.replaygain_track_peak = tags.replaygain_track_peak
        db_file.replaygain_album_gain = tags.replaygain_album_gain
        db_file.replaygain_album_peak = tags.replaygain_album_peak
        db_file.cover_mime_type = tags.cover_mime_type
        db_file.cover_size = tags.cover_size_bytes

        if info.signature:
            db_file.content_hash = info.signature.content_hash
            db_file.hash_algo = info.signature.hash_algo
            db_file.acoustid_fingerprint = info.signature.acoustid_fingerprint
            db_file.acoustid_duration_ms = info.signature.acoustid_duration_ms

        # Analysis flags
        db_file.is_corrupt = int(info.is_corrupt)
        db_file.is_transcode = int(info.is_transcode)
        db_file.transcode_confidence = info.transcode_confidence
        db_file.corruption_reason = info.corruption_reason
        db_file.duplicate_group_id = info.duplicate_group_id

        db_file.last_scanned = datetime.utcnow()


def _enable_wal(dbapi_conn, _connection_record):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA synchronous=NORMAL")
    dbapi_conn.execute("PRAGMA cache_size=-64000")  # 64MB
