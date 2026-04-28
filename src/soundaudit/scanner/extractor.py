"""Extract metadata from audio files using mutagen."""

from __future__ import annotations

from pathlib import Path

from mutagen import MutagenError
from mutagen.aac import AAC
from mutagen.aiff import AIFF
from mutagen.apev2 import APEv2File
from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from soundaudit.models import AudioFormat, AudioSignature, FileInfo, HashStrategy, TrackTags


try:
    import xxhash

    _XXHASH_AVAILABLE = True
except ImportError:
    _XXHASH_AVAILABLE = False


def extract_file_info(path: Path, *, hash_strategy: HashStrategy = HashStrategy.HEAD_ONLY) -> FileInfo:
    """Read all metadata from a single audio file.

    Every discovered file must end up in the database, even if unreadable.
    All I/O exceptions are caught and returned as a corrupt FileInfo.
    """
    # Try to stat first — if we can't even do that, save minimal info
    try:
        stat = path.stat()
        size_bytes = stat.st_size
        mtime_ns = stat.st_mtime_ns
    except OSError as exc:
        return FileInfo(
            path=path,
            size_bytes=0,
            mtime_ns=0,
            format=AudioFormat.UNKNOWN,
            tags=TrackTags(),
            is_corrupt=True,
            corruption_reason=f"Cannot stat file: {exc}",
        )

    suffix = path.suffix.lower()

    # Map extension to mutagen class and our enum
    class_and_format = _MUTAGEN_MAP.get(suffix)
    if class_and_format is None:
        return FileInfo(
            path=path,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            format=AudioFormat.UNKNOWN,
            tags=TrackTags(),
            is_corrupt=True,
            corruption_reason=f"Unknown extension: {suffix}",
        )

    mutagen_cls, fmt, lossless = class_and_format

    try:
        audio = mutagen_cls(str(path))
    except MutagenError as exc:
        return FileInfo(
            path=path,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            format=fmt,
            lossless=lossless,
            tags=TrackTags(),
            is_corrupt=True,
            corruption_reason=f"MutagenError: {exc}",
        )

    # Stream info
    info = audio.info
    file_info = FileInfo(
        path=path,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        format=fmt,
        sample_rate_hz=getattr(info, "sample_rate", None),
        bit_depth=getattr(info, "bits_per_sample", None),
        channels=getattr(info, "channels", None),
        bitrate_kbps=round(getattr(info, "bitrate", 0) / 1000) if hasattr(info, "bitrate") else None,
        duration_seconds=getattr(info, "length", None),
        lossless=lossless,
        tags=_extract_tags(audio),
    )

    # Cover art size (for FLAC)
    if isinstance(audio, FLAC):
        pics = audio.pictures
        if pics:
            pic = pics[0]
            file_info.tags.cover_mime_type = pic.mime
            file_info.tags.cover_size_bytes = len(pic.data)

    # Compute content hash according to strategy
    if hash_strategy != HashStrategy.NONE and _XXHASH_AVAILABLE:
        try:
            file_info.signature = _compute_signature(path, hash_strategy)
        except Exception as exc:
            file_info.corruption_reason = (
                f"{file_info.corruption_reason}\n" if file_info.corruption_reason else ""
            ) + f"Hash error ({type(exc).__name__}): {exc}"
    elif hash_strategy != HashStrategy.NONE and not _XXHASH_AVAILABLE:
        file_info.corruption_reason = (
            f"{file_info.corruption_reason}\n" if file_info.corruption_reason else ""
        ) + "xxhash not installed (pip install xxhash)"

    return file_info


def _extract_tags(audio) -> TrackTags:
    """Extract tags from a mutagen audio object."""
    tags = TrackTags()

    def _get(*keys: str) -> str | None:
        for k in keys:
            try:
                val = audio.get(k)
                if val:
                    return str(val[0]) if isinstance(val, list) else str(val)
            except (ValueError, KeyError, TypeError):
                continue
        return None

    # FLAC/Ogg uses "TITLE", MP3 uses "TIT2", MP4 uses "\xa9nam"
    tags.title = _get("TITLE", "TIT2", "\xa9nam")
    tags.artist = _get("ARTIST", "TPE1", "\xa9ART")
    tags.album = _get("ALBUM", "TALB", "\xa9alb")
    tags.album_artist = _get("ALBUMARTIST", "TPE2", "aART")
    tags.genre = _get("GENRE", "TCON", "\xa9gen")
    tags.year = _parse_int(_get("DATE", "TYER", "TDRC", "\xa9day"))
    tags.isrc = _get("ISRC", "TSRC")
    tags.comment = _get("COMMENT", "COMM", "\xa9cmt")
    tags.lyrics = _get("LYRICS", "USLT", "\xa9lyr")
    tags.publisher = _get("LABEL", "TPUB", "\xa9pub")
    tags.composer = _get("COMPOSER", "TCOM", "\xa9wrt")

    tags.track_number = _parse_track(_get("TRACKNUMBER", "TRCK"))
    tags.track_total = _parse_track_total(_get("TRACKNUMBER", "TRCK", "TRACKTOTAL"))
    tags.disc_number = _parse_track(_get("DISCNUMBER", "TPOS"))
    tags.disc_total = _parse_track_total(_get("DISCNUMBER", "TPOS", "DISCTOTAL"))

    # ReplayGain
    tags.replaygain_track_gain = _parse_rg(_get("REPLAYGAIN_TRACK_GAIN"))
    tags.replaygain_track_peak = _parse_rg(_get("REPLAYGAIN_TRACK_PEAK"))
    tags.replaygain_album_gain = _parse_rg(_get("REPLAYGAIN_ALBUM_GAIN"))
    tags.replaygain_album_peak = _parse_rg(_get("REPLAYGAIN_ALBUM_PEAK"))

    return tags


# Extension -> (mutagen_class, AudioFormat, is_lossless)
_MUTAGEN_MAP: dict[str, tuple] = {
    ".flac": (FLAC, AudioFormat.FLAC, True),
    ".mp3": (MP3, AudioFormat.MP3, False),
    ".m4a": (MP4, AudioFormat.M4A, False),
    ".ogg": (OggVorbis, AudioFormat.OGG, True),
    ".wav": (WAVE, AudioFormat.WAV, True),
    ".ape": (APEv2File, AudioFormat.UNKNOWN, True),
    ".wv": (APEv2File, AudioFormat.UNKNOWN, True),
    ".aiff": (AIFF, AudioFormat.UNKNOWN, True),
    ".aac": (AAC, AudioFormat.UNKNOWN, False),
}


def _compute_signature(path: Path, strategy: HashStrategy) -> AudioSignature:
    h = xxhash.xxh3_64()
    with open(path, "rb") as f:
        if strategy == HashStrategy.HEAD_ONLY:
            h.update(f.read(1048576))
        elif strategy == HashStrategy.HEAD_TAIL:
            h.update(f.read(1048576))
            f.seek(0, 2)
            size = f.tell()
            if size > 2097152:
                f.seek(size - 1048576)
                h.update(f.read(1048576))
        elif strategy == HashStrategy.FULL:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        else:
            raise ValueError(f"Unknown hash strategy: {strategy}")

    suffix = f"+{strategy.value}" if strategy != HashStrategy.FULL else ""
    return AudioSignature(
        content_hash=h.hexdigest(),
        hash_algo=f"xxhash3_64{suffix}",
    )


def _parse_int(v: str | None) -> int | None:
    if not v:
        return None
    try:
        return int(v.split("-")[0].split("/")[0])
    except ValueError:
        return None


def _parse_track(v: str | None) -> int | None:
    """Handle '3/12' form."""
    if not v:
        return None
    try:
        return int(v.split("/")[0])
    except ValueError:
        return None


def _parse_track_total(v: str | None) -> int | None:
    if not v:
        return None
    try:
        parts = v.split("/")
        return int(parts[1]) if len(parts) > 1 else None
    except (ValueError, IndexError):
        return None


def _parse_rg(v: str | None) -> float | None:
    if not v:
        return None
    try:
        # Strip ' dB'
        return float(v.replace(" dB", "").strip())
    except ValueError:
        return None
