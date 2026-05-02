"""Core data models using Pydantic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path


class AudioFormat(Enum):
    FLAC = "flac"
    MP3 = "mp3"
    M4A = "m4a"
    OGG = "ogg"
    WAV = "wav"
    UNKNOWN = "unknown"


class HashStrategy(str, Enum):
    HEAD_ONLY = "head-only"      # first 1MB, default
    HEAD_TAIL = "head-tail"      # first 1MB + last 1MB
    FULL = "full"                # entire file
    NONE = "none"                # skip hashing


class TagQuality(Enum):
    COMPLETE = auto()
    MISSING_OPTIONAL = auto()
    MISSING_REQUIRED = auto()
    EMPTY = auto()


@dataclass(frozen=True, slots=True)
class AudioSignature:
    """Immutable audio fingerprinting data."""

    content_hash: str
    hash_algo: str = "xxhash3_64"
    acoustid_fingerprint: str | None = None
    acoustid_duration_ms: int | None = None
    chromaprint: str | None = None


@dataclass(slots=True)
class TrackTags:
    """All tag fields we care about."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    year: int | None = None
    genre: str | None = None
    isrc: str | None = None
    comment: str | None = None
    lyrics: str | None = None
    publisher: str | None = None
    composer: str | None = None
    # ReplayGain
    replaygain_track_gain: float | None = None
    replaygain_track_peak: float | None = None
    replaygain_album_gain: float | None = None
    replaygain_album_peak: float | None = None
    # Embedded cover info
    cover_mime_type: str | None = None
    cover_size_bytes: int | None = None
    cover_dimensions: tuple[int, int] | None = None

    def required_missing(self) -> list[str]:
        """Fields that should be present but aren't."""
        req = ["title", "artist", "album", "track_number"]
        return [f for f in req if getattr(self, f) is None]

    def optional_missing(self) -> list[str]:
        opt = [
            "album_artist",
            "year",
            "genre",
            "isrc",
            "cover_mime_type",
        ]
        return [f for f in opt if getattr(self, f) is None]

    def completeness_score(self) -> float:
        """0.0 to 1.0 tag completeness."""
        req = 4 - len(self.required_missing())
        opt = 6 - len(self.optional_missing())
        return (req + opt * 0.5) / 7.0


@dataclass(slots=True)
class FileInfo:
    """Everything we know about a single audio file on disk."""

    # File system
    path: Path
    size_bytes: int
    mtime_ns: int
    inode: int | None = None

    # Format / codec
    format: AudioFormat = AudioFormat.UNKNOWN
    sample_rate_hz: int | None = None
    bit_depth: int | None = None
    channels: int | None = None
    bitrate_kbps: float | None = None
    duration_seconds: float | None = None
    lossless: bool = False

    # Tags
    tags: TrackTags = field(default_factory=TrackTags)

    # Content signature
    signature: AudioSignature | None = None

    # Scan tracking
    first_seen: datetime | None = None
    last_scanned: datetime | None = None
    scan_id: int | None = None

    # Analysis flags (populated by analyzers)
    is_corrupt: bool = False
    is_transcode: bool = False
    is_duplicate: bool = False
    duplicate_group_id: int | None = None
    transcode_confidence: float = 0.0
    corruption_reason: str | None = None

    @property
    def primary_artist(self) -> str | None:
        return self.tags.album_artist or self.tags.artist

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def relpath(self) -> str:
        return str(self.path)

    def mtime_dt(self) -> datetime:
        return datetime.fromtimestamp(self.mtime_ns / 1e9)
