"""SQLite database layer with SQLAlchemy 2.0 — async-ready schema."""

from __future__ import annotations

import weakref
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from soundaudit.models import FileInfo, TrackTags


class Base(DeclarativeBase):
    pass


# Global weak registry of engines so we can force-close all connections
_engine_refs: list[weakref.ref] = []


class DBFile(Base):  # type: ignore[valid-type,misc]
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mtime: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Format info
    format: Mapped[str] = mapped_column(String, nullable=False, default="UNKNOWN")
    sample_rate_hz: Mapped[int | None] = mapped_column(Integer)
    bit_depth: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[int | None] = mapped_column(Integer)
    bitrate_kbps: Mapped[float | None] = mapped_column(Float)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    lossless: Mapped[int] = mapped_column(Integer, default=0)  # SQLite bool

    # Tags
    title: Mapped[str | None] = mapped_column(String)
    artist: Mapped[str | None] = mapped_column(String, index=True)
    album: Mapped[str | None] = mapped_column(String, index=True)
    album_artist: Mapped[str | None] = mapped_column(String, index=True)
    track_number: Mapped[int | None] = mapped_column(Integer)
    track_total: Mapped[int | None] = mapped_column(Integer)
    disc_number: Mapped[int | None] = mapped_column(Integer)
    disc_total: Mapped[int | None] = mapped_column(Integer)
    year: Mapped[int | None] = mapped_column(Integer)
    genre: Mapped[str | None] = mapped_column(String)
    isrc: Mapped[str | None] = mapped_column(String)
    comment: Mapped[str | None] = mapped_column(Text)
    lyrics: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String)
    composer: Mapped[str | None] = mapped_column(String)
    replaygain_track_gain: Mapped[float | None] = mapped_column(Float)
    replaygain_track_peak: Mapped[float | None] = mapped_column(Float)
    replaygain_album_gain: Mapped[float | None] = mapped_column(Float)
    replaygain_album_peak: Mapped[float | None] = mapped_column(Float)
    cover_mime_type: Mapped[str | None] = mapped_column(String)
    cover_size: Mapped[int | None] = mapped_column(Integer)

    # Signatures
    content_hash: Mapped[str | None] = mapped_column(String, index=True)
    hash_algo: Mapped[str | None] = mapped_column(String, default="xxhash3_64")
    acoustid_fingerprint: Mapped[str | None] = mapped_column(Text)
    acoustid_duration_ms: Mapped[int | None] = mapped_column(Integer)

    # Scan tracking
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_scanned: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    scan_id: Mapped[int] = mapped_column(Integer, default=0)

    # Analysis flags
    is_corrupt: Mapped[int] = mapped_column(Integer, default=0)
    is_transcode: Mapped[int] = mapped_column(Integer, default=0)
    transcode_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    corruption_reason: Mapped[str | None] = mapped_column(Text)
    spectral_cutoff_hz: Mapped[int | None] = mapped_column(Integer)
    transcode_reason: Mapped[str | None] = mapped_column(Text)

    # MusicBrainz resolution
    mb_recording_id: Mapped[str | None] = mapped_column(String, index=True)
    mb_release_id: Mapped[str | None] = mapped_column(String)
    mb_track_id: Mapped[str | None] = mapped_column(String)
    mb_score: Mapped[float] = mapped_column(Float, default=0.0)
    mb_match_date: Mapped[datetime | None] = mapped_column(DateTime)
    mb_title: Mapped[str | None] = mapped_column(String)
    mb_artist: Mapped[str | None] = mapped_column(String)
    mb_album: Mapped[str | None] = mapped_column(String)
    mb_album_artist: Mapped[str | None] = mapped_column(String)
    mb_year: Mapped[int | None] = mapped_column(Integer)
    mb_genre: Mapped[str | None] = mapped_column(String)

    duplicate_group_id: Mapped[int | None] = mapped_column(Integer)
    acoustid_group_id: Mapped[int | None] = mapped_column(Integer, index=True)

    # Phase 4 — tag writeback backup
    tag_backup_json: Mapped[str | None] = mapped_column(Text)
    tag_fix_date: Mapped[datetime | None] = mapped_column(DateTime)

    def __repr__(self) -> str:
        return f"<DBFile {self.path}>"


class DuplicateGroup(Base):  # type: ignore[valid-type,misc]
    __tablename__ = "duplicate_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    acoustid: Mapped[str | None] = mapped_column(String, index=True)
    group_type: Mapped[str] = mapped_column(String, default="content_hash")
    ignored: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AcoustidGroup(Base):  # type: ignore[valid-type,misc]
    """Fuzzy duplicate groups created from identical chromaprint fingerprints."""

    __tablename__ = "acoustid_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String, index=True, nullable=False)
    ignored: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ScanHistory(Base):  # type: ignore[valid-type,misc]
    __tablename__ = "scan_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    started: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished: Mapped[datetime | None] = mapped_column(DateTime)
    files_found: Mapped[int] = mapped_column(Integer, default=0)
    files_changed: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)


def _migrate_table(engine, table_name: str, model_cls: type[Base]) -> None:
    """Add any columns present in the model but missing from the SQLite table."""
    inspector = inspect(engine)
    if not inspector.has_table(table_name):
        return
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    for col in model_cls.__table__.columns:
        if col.name not in existing:
            # SQLite ALTER TABLE ADD COLUMN cannot add PRIMARY KEY / UNIQUE / NOT NULL without default.
            # All our new columns are nullable, so this is safe.
            col_type = col.type.compile(dialect=engine.dialect)
            with engine.begin() as conn:
                conn.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}")
                )


class Database:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{db_path}")
        _engine_refs.append(weakref.ref(self.engine))
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        # Auto-migrate tables to add any new columns since last version
        _migrate_table(self.engine, "files", DBFile)
        _migrate_table(self.engine, "duplicate_groups", DuplicateGroup)
        _migrate_table(self.engine, "acoustid_groups", AcoustidGroup)
        _migrate_table(self.engine, "scan_history", ScanHistory)
        # Enable WAL for better concurrent reads
        event.listen(self.engine, "connect", _enable_wal)

    @staticmethod
    def close_all() -> None:
        """Dispose every engine SoundAudit has opened.

        This releases file handles so Windows can delete the .db.
        """
        for ref in list(_engine_refs):
            engine = ref()
            if engine is not None:
                engine.dispose()
        _engine_refs.clear()

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
        batch_size = 500
        deleted = 0
        with self.session() as s:
            path_list = list(paths)
            for i in range(0, len(path_list), batch_size):
                batch = path_list[i : i + batch_size]
                result = s.query(DBFile).filter(DBFile.path.in_(batch)).delete(
                    synchronize_session=False
                )
                deleted += result
            s.commit()
        return deleted

    def upsert_file(self, info: FileInfo, session: Session | None = None) -> None:
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

        db_file.last_scanned = datetime.now(timezone.utc)

    def save_tag_backup(self, file_id: int, backup: dict) -> None:
        """Store original tags JSON before a write operation."""
        import json

        with self.session() as s:
            row = s.query(DBFile).filter_by(id=file_id).first()
            if row:
                row.tag_backup_json = json.dumps(backup, ensure_ascii=False)
                s.commit()

    def update_file_path(self, old_path: str, new_path: str) -> bool:
        """Update a file's path in the database after moving on disk."""
        with self.session() as s:
            row = s.query(DBFile).filter_by(path=old_path).first()
            if not row:
                return False
            row.path = new_path
            s.commit()
        return True

    def ignore_duplicate_group(self, group_id: int) -> None:
        """Mark a duplicate group as ignored (user wants to keep both files)."""
        with self.session() as s:
            row = s.query(DuplicateGroup).filter_by(id=group_id).first()
            if row:
                row.ignored = 1
                s.commit()

    def ignore_acoustid_group(self, group_id: int) -> None:
        """Mark an AcoustID group as ignored."""
        with self.session() as s:
            row = s.query(AcoustidGroup).filter_by(id=group_id).first()
            if row:
                row.ignored = 1
                s.commit()

    def save_written_tags(self, file_id: int, tags: TrackTags, fields: set[str]) -> None:
        """Update DB columns to reflect what was just written to disk.

        Also sets tag_fix_date so the TUI can distinguish fixed vs pending.
        """
        with self.session() as s:
            row = s.query(DBFile).filter_by(id=file_id).first()
            if not row:
                return
            if "title" in fields:
                row.title = tags.title
            if "artist" in fields:
                row.artist = tags.artist
            if "album" in fields:
                row.album = tags.album
            if "album_artist" in fields:
                row.album_artist = tags.album_artist
            if "track_number" in fields:
                row.track_number = tags.track_number
            if "track_total" in fields:
                row.track_total = tags.track_total
            if "disc_number" in fields:
                row.disc_number = tags.disc_number
            if "disc_total" in fields:
                row.disc_total = tags.disc_total
            if "year" in fields:
                row.year = tags.year
            if "genre" in fields:
                row.genre = tags.genre
            if "isrc" in fields:
                row.isrc = tags.isrc
            row.tag_fix_date = datetime.now(timezone.utc)
            s.commit()


def _enable_wal(dbapi_conn, _connection_record: object) -> None:
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA synchronous=NORMAL")
    dbapi_conn.execute("PRAGMA cache_size=-64000")  # 64MB
    dbapi_conn.execute("PRAGMA busy_timeout = 5000")  # wait 5s on lock


def reset_database(db_path: str | Path) -> None:
    """Delete the SQLite database and its WAL sidecars.

    Disposes any pooled connections first so Windows releases the file handle.
    Raises PermissionError if the file is still locked.
    """
    path = Path(db_path)
    # Dispose every engine SoundAudit has ever created so file handles are released
    Database.close_all()

    if path.exists():
        path.unlink()
    for sidecar in (path.with_suffix(".db-wal"), path.with_suffix(".db-shm")):
        if sidecar.exists():
            sidecar.unlink()
