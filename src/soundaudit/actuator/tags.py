"""Tag writeback actuator — safely writes metadata to audio files using mutagen."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from mutagen import MutagenError
from mutagen.apev2 import APEv2File
from mutagen.flac import FLAC
from mutagen.id3 import (  # type: ignore[attr-defined]
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TSRC,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from soundaudit.models import TrackTags


class TagWriteError(Exception):
    """Raised when mutagen cannot write tags."""


_VALID_FIELDS = frozenset(
    {
        "title",
        "artist",
        "album",
        "album_artist",
        "track_number",
        "track_total",
        "disc_number",
        "disc_total",
        "year",
        "genre",
        "isrc",
    }
)


def validate_fields(fields: set[str] | frozenset[str]) -> set[str]:
    """Return intersection with valid fields, warn about unknown."""
    valid = set(fields) & _VALID_FIELDS
    invalid = set(fields) - _VALID_FIELDS
    if invalid:
        raise TagWriteError(f"Invalid fields: {', '.join(sorted(invalid))}")
    return valid


def _snapshot_tracknumber(audio) -> tuple[int | None, int | None]:
    """Extract track number / total from various tag formats."""
    raw = audio.get("TRACKNUMBER")
    if not raw:
        return None, None
    val = raw[0] if isinstance(raw, list) else str(raw)
    parts = str(val).split("/")
    try:
        num = int(parts[0])
    except ValueError:
        num = None
    try:
        total = int(parts[1]) if len(parts) > 1 else None
    except ValueError:
        total = None
    return num, total


def _snapshot_discnumber(audio) -> tuple[int | None, int | None]:
    raw = audio.get("DISCNUMBER") or audio.get("DISC")
    if not raw:
        return None, None
    val = raw[0] if isinstance(raw, list) else str(raw)
    parts = str(val).split("/")
    try:
        num = int(parts[0])
    except ValueError:
        num = None
    try:
        total = int(parts[1]) if len(parts) > 1 else None
    except ValueError:
        total = None
    return num, total


def _snapshot_flac_ogg(audio: FLAC | OggVorbis) -> dict[str, str | int | None]:
    """Read current tag values into a plain dict."""

    def _get(key: str) -> str | None:
        val = audio.get(key)
        return str(val[0]) if val else None

    track_num, track_total = _snapshot_tracknumber(audio)
    disc_num, disc_total = _snapshot_discnumber(audio)

    return {
        "title": _get("TITLE"),
        "artist": _get("ARTIST"),
        "album": _get("ALBUM"),
        "album_artist": _get("ALBUMARTIST"),
        "track_number": track_num,
        "track_total": track_total,
        "disc_number": disc_num,
        "disc_total": disc_total,
        "year": _get("DATE"),
        "genre": _get("GENRE"),
        "isrc": _get("ISRC"),
    }


def _snapshot_mp3(audio: MP3) -> dict[str, str | int | None]:
    if audio.tags is None:
        return dict.fromkeys(_VALID_FIELDS)
    tags = audio.tags

    def _text(frame_id: str) -> str | None:
        frame = tags.get(frame_id)
        if frame:
            return str(frame.text[0]) if frame.text else None
        return None

    track_text = _text("TRCK")
    track_num, track_total = None, None
    if track_text:
        parts = track_text.split("/")
        with contextlib.suppress(ValueError):
            track_num = int(parts[0])
        with contextlib.suppress(ValueError):
            track_total = int(parts[1]) if len(parts) > 1 else None

    disc_text = _text("TPOS")
    disc_num, disc_total = None, None
    if disc_text:
        parts = disc_text.split("/")
        with contextlib.suppress(ValueError):
            disc_num = int(parts[0])
        with contextlib.suppress(ValueError):
            disc_total = int(parts[1]) if len(parts) > 1 else None

    year_raw = _text("TDRC") or _text("TYER")
    year: int | str | None = None
    if year_raw:
        try:
            year = int(str(year_raw)[:4])
        except ValueError:
            year = str(year_raw)[:4] if year_raw else None

    return {
        "title": _text("TIT2"),
        "artist": _text("TPE1"),
        "album": _text("TALB"),
        "album_artist": _text("TPE2"),
        "track_number": track_num,
        "track_total": track_total,
        "disc_number": disc_num,
        "disc_total": disc_total,
        "year": year,
        "genre": _text("TCON"),
        "isrc": _text("TSRC"),
    }


def _snapshot_mp4(audio: MP4) -> dict[str, str | int | None]:
    def _get(key: str) -> str | None:
        val = audio.get(key)
        if isinstance(val, list) and val:
            return str(val[0])
        return None

    trkn = audio.get("trkn")
    track_num, track_total = None, None
    if isinstance(trkn, list) and trkn:
        track_num, track_total = trkn[0] if isinstance(trkn[0], tuple) else (trkn[0], 0)

    disk = audio.get("disk")
    disc_num, disc_total = None, None
    if isinstance(disk, list) and disk:
        disc_num, disc_total = disk[0] if isinstance(disk[0], tuple) else (disk[0], 0)

    year_raw = _get("\xa9day")
    year: int | None = None
    if year_raw:
        with contextlib.suppress(ValueError):
            year = int(str(year_raw)[:4])

    return {
        "title": _get("\xa9nam"),
        "artist": _get("\xa9ART"),
        "album": _get("\xa9alb"),
        "album_artist": _get("aART"),
        "track_number": track_num,
        "track_total": track_total,
        "disc_number": disc_num,
        "disc_total": disc_total,
        "year": year,
        "genre": _get("\xa9gen"),
        "isrc": None,  # no standard ISRC key for MP4
    }


def _snapshot_ape(audio: APEv2File) -> dict[str, str | int | None]:
    def _get(key: str) -> str | None:
        val = audio.get(key)
        return str(val) if val is not None else None

    track_text = _get("Track")
    track_num, track_total = None, None
    if track_text:
        parts = track_text.split("/")
        with contextlib.suppress(ValueError):
            track_num = int(parts[0])
        with contextlib.suppress(ValueError):
            track_total = int(parts[1]) if len(parts) > 1 else None

    disc_text = _get("Disc")
    disc_num, disc_total = None, None
    if disc_text:
        parts = disc_text.split("/")
        with contextlib.suppress(ValueError):
            disc_num = int(parts[0])
        with contextlib.suppress(ValueError):
            disc_total = int(parts[1]) if len(parts) > 1 else None

    year_raw = _get("Year")
    year: int | None = None
    if year_raw:
        with contextlib.suppress(ValueError):
            year = int(str(year_raw)[:4])

    return {
        "title": _get("Title"),
        "artist": _get("Artist"),
        "album": _get("Album"),
        "album_artist": _get("Album Artist"),
        "track_number": track_num,
        "track_total": track_total,
        "disc_number": disc_num,
        "disc_total": disc_total,
        "year": year,
        "genre": _get("Genre"),
        "isrc": _get("ISRC"),
    }


def _snapshot_wave(audio: WAVE) -> dict[str, str | int | None]:
    # WAVE has limited tag support in mutagen; we snapshot what we can.
    info: dict[str, str] = {}
    if audio.tags:
        for key in ("Title", "Artist", "Album", "Genre", "Date"):
            val = audio.tags.get(key)
            if val:
                info[key.lower()] = str(val)
    return {
        "title": info.get("title"),
        "artist": info.get("artist"),
        "album": info.get("album"),
        "album_artist": None,
        "track_number": None,
        "track_total": None,
        "disc_number": None,
        "disc_total": None,
        "year": info.get("date"),
        "genre": info.get("genre"),
        "isrc": None,
    }


def snapshot_tags(path: Path) -> dict[str, str | int | None]:
    """Return a JSON-safe dict of current tag values."""
    suffix = path.suffix.lower()
    audio: Any
    if suffix == ".flac":
        audio = FLAC(str(path))
        return _snapshot_flac_ogg(audio)
    if suffix == ".mp3":
        audio = MP3(str(path))
        return _snapshot_mp3(audio)
    if suffix == ".m4a":
        audio = MP4(str(path))
        return _snapshot_mp4(audio)
    if suffix == ".ogg":
        audio = OggVorbis(str(path))
        return _snapshot_flac_ogg(audio)
    if suffix == ".wav":
        audio = WAVE(str(path))
        return _snapshot_wave(audio)
    if suffix in (".ape", ".wv"):
        audio = APEv2File(str(path))
        return _snapshot_ape(audio)
    raise TagWriteError(f"Unsupported format for tag writing: {suffix}")


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _write_flac_ogg(
    audio: FLAC | OggVorbis,
    tags: TrackTags,
    fields: set[str],
) -> None:
    def _set(key: str, val: str | None) -> None:
        if val is not None:
            audio[key] = [val]
        elif key in audio:
            del audio[key]

    if "title" in fields and tags.title is not None:
        _set("TITLE", tags.title)
    if "artist" in fields and tags.artist is not None:
        _set("ARTIST", tags.artist)
    if "album" in fields and tags.album is not None:
        _set("ALBUM", tags.album)
    if "album_artist" in fields and tags.album_artist is not None:
        _set("ALBUMARTIST", tags.album_artist)
    if "genre" in fields and tags.genre is not None:
        _set("GENRE", tags.genre)
    if "year" in fields and tags.year is not None:
        _set("DATE", str(tags.year))
    if "isrc" in fields and tags.isrc is not None:
        _set("ISRC", tags.isrc)

    if "track_number" in fields or "track_total" in fields:
        parts: list[str] = []
        if tags.track_number is not None:
            parts.append(str(tags.track_number))
        if tags.track_total is not None:
            parts.append(str(tags.track_total))
        if parts:
            audio["TRACKNUMBER"] = ["/".join(parts)]
        elif "TRACKNUMBER" in audio:
            del audio["TRACKNUMBER"]

    if "disc_number" in fields or "disc_total" in fields:
        parts = []
        if tags.disc_number is not None:
            parts.append(str(tags.disc_number))
        if tags.disc_total is not None:
            parts.append(str(tags.disc_total))
        if parts:
            audio["DISCNUMBER"] = ["/".join(parts)]
        elif "DISCNUMBER" in audio:
            del audio["DISCNUMBER"]

    audio.save()


def _write_mp3(audio: MP3, tags: TrackTags, fields: set[str]) -> None:
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None

    def _set(frame_class, frame_id: str, val: str | None) -> None:
        if val is not None:
            audio.tags[frame_id] = frame_class(encoding=3, text=val)
        elif frame_id in audio.tags:
            del audio.tags[frame_id]

    if "title" in fields and tags.title is not None:
        _set(TIT2, "TIT2", tags.title)
    if "artist" in fields and tags.artist is not None:
        _set(TPE1, "TPE1", tags.artist)
    if "album" in fields and tags.album is not None:
        _set(TALB, "TALB", tags.album)
    if "album_artist" in fields and tags.album_artist is not None:
        _set(TPE2, "TPE2", tags.album_artist)
    if "genre" in fields and tags.genre is not None:
        _set(TCON, "TCON", tags.genre)
    if "year" in fields and tags.year is not None:
        _set(TDRC, "TDRC", str(tags.year))
    if "isrc" in fields and tags.isrc is not None:
        _set(TSRC, "TSRC", tags.isrc)

    if "track_number" in fields or "track_total" in fields:
        parts: list[str] = []
        if tags.track_number is not None:
            parts.append(str(tags.track_number))
        if tags.track_total is not None:
            parts.append(str(tags.track_total))
        if parts:
            audio.tags["TRCK"] = TRCK(encoding=3, text="/".join(parts))
        elif "TRCK" in audio.tags:
            del audio.tags["TRCK"]

    if "disc_number" in fields or "disc_total" in fields:
        parts = []
        if tags.disc_number is not None:
            parts.append(str(tags.disc_number))
        if tags.disc_total is not None:
            parts.append(str(tags.disc_total))
        if parts:
            audio.tags["TPOS"] = TPOS(encoding=3, text="/".join(parts))
        elif "TPOS" in audio.tags:
            del audio.tags["TPOS"]

    audio.save()


def _write_mp4(audio: MP4, tags: TrackTags, fields: set[str]) -> None:
    def _set(key: str, val: str | int | None) -> None:
        if val is not None:
            audio[key] = [val]
        elif key in audio:
            del audio[key]

    if "title" in fields and tags.title is not None:
        _set("\xa9nam", tags.title)
    if "artist" in fields and tags.artist is not None:
        _set("\xa9ART", tags.artist)
    if "album" in fields and tags.album is not None:
        _set("\xa9alb", tags.album)
    if "album_artist" in fields and tags.album_artist is not None:
        _set("aART", tags.album_artist)
    if "genre" in fields and tags.genre is not None:
        _set("\xa9gen", tags.genre)
    if "year" in fields and tags.year is not None:
        _set("\xa9day", str(tags.year))

    if "track_number" in fields or "track_total" in fields:
        track_num = tags.track_number or 0
        track_total = tags.track_total or 0
        if track_num or track_total:
            audio["trkn"] = [(track_num, track_total)]
        elif "trkn" in audio:
            del audio["trkn"]

    if "disc_number" in fields or "disc_total" in fields:
        disc_num = tags.disc_number or 0
        disc_total = tags.disc_total or 0
        if disc_num or disc_total:
            audio["disk"] = [(disc_num, disc_total)]
        elif "disk" in audio:
            del audio["disk"]

    audio.save()


def _write_ape(audio: APEv2File, tags: TrackTags, fields: set[str]) -> None:
    def _set(key: str, val: str | None) -> None:
        if val is not None:
            audio[key] = val
        elif key in audio:
            del audio[key]

    if "title" in fields and tags.title is not None:
        _set("Title", tags.title)
    if "artist" in fields and tags.artist is not None:
        _set("Artist", tags.artist)
    if "album" in fields and tags.album is not None:
        _set("Album", tags.album)
    if "album_artist" in fields and tags.album_artist is not None:
        _set("Album Artist", tags.album_artist)
    if "genre" in fields and tags.genre is not None:
        _set("Genre", tags.genre)
    if "year" in fields and tags.year is not None:
        _set("Year", str(tags.year))
    if "isrc" in fields and tags.isrc is not None:
        _set("ISRC", tags.isrc)

    if "track_number" in fields or "track_total" in fields:
        parts: list[str] = []
        if tags.track_number is not None:
            parts.append(str(tags.track_number))
        if tags.track_total is not None:
            parts.append(str(tags.track_total))
        if parts:
            audio["Track"] = "/".join(parts)
        elif "Track" in audio:
            del audio["Track"]

    if "disc_number" in fields or "disc_total" in fields:
        parts = []
        if tags.disc_number is not None:
            parts.append(str(tags.disc_number))
        if tags.disc_total is not None:
            parts.append(str(tags.disc_total))
        if parts:
            audio["Disc"] = "/".join(parts)
        elif "Disc" in audio:
            del audio["Disc"]

    audio.save()


def _write_wave(audio: WAVE, tags: TrackTags, fields: set[str]) -> None:
    if audio.tags is None:
        audio.add_tags()
    # WAVE only stores INFO-list chunks; mutation is best-effort.
    # We'll skip most numeric fields since INFO chunks are string-only.
    info_map = {
        "title": ("Title", tags.title),
        "artist": ("Artist", tags.artist),
        "album": ("Album", tags.album),
        "genre": ("Genre", tags.genre),
    }
    for field, (key, val) in info_map.items():
        if field in fields and val is not None:
            audio.tags[key] = val  # type: ignore[index]
    audio.save()


def write_tags(
    path: Path,
    tags: TrackTags,
    fields: set[str] | None = None,
    backup: bool = True,
) -> dict[str, str | int | None]:
    """Write tags to *path* and return the backup snapshot.

    If *backup* is True (default), the existing tag values are read before
    writing and returned as a JSON-safe dict.

    Raises TagWriteError on unsupported formats or mutagen failures.
    """
    path = Path(path)
    if not path.exists():
        raise TagWriteError(f"File not found: {path}")

    original: dict[str, str | int | None] = {}
    if backup:
        original = snapshot_tags(path)

    write_fields = validate_fields(fields or _VALID_FIELDS)

    suffix = path.suffix.lower()
    try:
        audio: Any
        if suffix == ".flac":
            audio = FLAC(str(path))
            _write_flac_ogg(audio, tags, write_fields)
        elif suffix == ".mp3":
            audio = MP3(str(path))
            _write_mp3(audio, tags, write_fields)
        elif suffix == ".m4a":
            audio = MP4(str(path))
            _write_mp4(audio, tags, write_fields)
        elif suffix == ".ogg":
            audio = OggVorbis(str(path))
            _write_flac_ogg(audio, tags, write_fields)
        elif suffix == ".wav":
            audio = WAVE(str(path))
            _write_wave(audio, tags, write_fields)
        elif suffix in (".ape", ".wv"):
            audio = APEv2File(str(path))
            _write_ape(audio, tags, write_fields)
        else:
            raise TagWriteError(f"Unsupported format for tag writing: {suffix}")
    except MutagenError as exc:
        raise TagWriteError(f"Mutagen failed to write {path}: {exc}") from exc

    return original


def resolved_metadata_to_tags(
    mb_title: str | None,
    mb_artist: str | None,
    mb_album: str | None,
    mb_album_artist: str | None,
    mb_year: int | None,
    mb_genre: str | None,
) -> TrackTags:
    """Build a TrackTags dataclass from MusicBrainz-resolved DB columns."""
    return TrackTags(
        title=mb_title,
        artist=mb_artist,
        album=mb_album,
        album_artist=mb_album_artist,
        year=mb_year,
        genre=mb_genre,
    )
