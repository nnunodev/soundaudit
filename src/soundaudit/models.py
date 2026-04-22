"""Core data models using Pydantic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Optional


class AudioFormat(Enum):
    FLAC = "flac"
    MP3 = "mp3"
    M4A = "m4a"
    OGG = "ogg"
    WAV = "wav"
    UNKNOWN = "unknown"


class TagQuality(Enum):
    COMPLETE = auto()
    MISSING_OPTIONAL = auto()
    MISSING_REQUIRED = auto()
    EMPTY = auto()


@dataclass(frozen=True, slots=True)
class AudioSignature:
    """Immutable audio fingerprinting data."""

    md5_content: str
    acoustid_fingerprint: Optional[str] = None
    acoustid_duration_ms: Optional[int] = None
    chromaprint: Optional[str] = None


@dataclass(slots=True)
class TrackTags:
    """All tag fields we care about."""

    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    album_artist: Optional[str] = None
    track_number: Optional[int] = None
    track_total: Optional[int] = None
    disc_number: Optional[int] = None
    disc_total: Optional[int] = None
    year: Optional[int] = None
    genre: Optional[str] = None
    isrc: Optional[str] = None
    comment: Optional[str] = None
    lyrics: Optional[str] = None
    publisher: Optional[str] = None
    composer: Optional[str] = None
    # ReplayGain
    replaygain_track_gain: Optional[float] = None
    replaygain_track_peak: Optional[float] = None
    replaygain_album_gain: Optional[float] = None
    replaygain_album_peak: Optional[float] = None
    # Embedded cover info
    cover_mime_type: Optional[str] = None
    cover_size_bytes: Optional[int] = None
    cover_dimensions: Optional[tuple[int, int]] = None

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
    inode: Optional[int] = None

    # Format / codec
    format: AudioFormat = AudioFormat.UNKNOWN
    sample_rate_hz: Optional[int] = None
    bit_depth: Optional[int] = None
    channels: Optional[int] = None
    bitrate_kbps: Optional[float] = None
    duration_seconds: Optional[float] = None
    lossless: bool = False

    # Tags
    tags: TrackTags = field(default_factory=TrackTags)

    # Content signature
    signature: Optional[AudioSignature] = None

    # Scan tracking
    first_seen: Optional[datetime] = None
    last_scanned: Optional[datetime] = None
    scan_id: Optional[int] = None

    # Analysis flags (populated by analyzers)
    is_corrupt: bool = False
    is_transcode: bool = False
    is_duplicate: bool = False
    duplicate_group_id: Optional[int] = None
    transcode_confidence: float = 0.0
    corruption_reason: Optional[str] = None

    @property
    def primary_artist(self) -> Optional[str]:
        return self.tags.album_artist or self.tags.artist

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def relpath(self) -> str:
        return str(self.path)

    def mtime_dt(self) -> datetime:
        return datetime.fromtimestamp(self.mtime_ns / 1e9)
