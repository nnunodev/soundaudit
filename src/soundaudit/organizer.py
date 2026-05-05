"""Navidrome folder-structure organizer.

Rearranges downloaded audio files into the Artist/Album/Track hierarchy
that Navidrome expects, using embedded metadata (tags) to build safe,
collision-free destination paths.
"""

from __future__ import annotations

import contextlib
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from mutagen import MutagenError
from mutagen.aac import AAC
from mutagen.aiff import AIFF
from mutagen.apev2 import APEv2File
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from soundaudit.models import TrackTags

# Characters unsafe on any common filesystem
_UNSAFE_CHARS = re.compile(r'[<>|:"?*\\/\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def sanitize_filename(name: str) -> str:
    """Make a string safe to use as a single path component."""
    if not name:
        return "Unknown"
    # Replace unsafe characters with underscore
    safe = _UNSAFE_CHARS.sub("_", name)
    # Trim trailing spaces/dots (problematic on Windows)
    safe = safe.rstrip(" .")
    # Collapse multiple spaces
    safe = re.sub(r"\s+", " ", safe)
    # Limit length
    if len(safe) > 120:
        safe = safe[:120].rstrip(" .")
    # Guard against reserved Windows names
    base = safe.split(".")[0].upper()
    if base in _RESERVED_NAMES:
        safe = f"_{safe}"
    if not safe:
        safe = "Unknown"
    return safe


def _get_tags(path: Path) -> TrackTags:
    """Read tags directly from an audio file."""
    suffix = path.suffix.lower()
    mutagen_map = {
        ".flac": FLAC,
        ".mp3": MP3,
        ".m4a": MP4,
        ".ogg": OggVorbis,
        ".wav": WAVE,
        ".ape": APEv2File,
        ".wv": APEv2File,
        ".aiff": AIFF,
        ".aac": AAC,
    }
    cls = mutagen_map.get(suffix)
    if cls is None:
        raise ValueError(f"Unsupported extension: {suffix}")

    audio = cls(str(path))
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

    tags.title = _get("TITLE", "TIT2", "\xa9nam")
    tags.artist = _get("ARTIST", "TPE1", "\xa9ART")
    tags.album = _get("ALBUM", "TALB", "\xa9alb")
    tags.album_artist = _get("ALBUMARTIST", "TPE2", "aART")
    tags.genre = _get("GENRE", "TCON", "\xa9gen")

    def _parse_int(v: str | None) -> int | None:
        if not v:
            return None
        try:
            return int(v.split("-")[0].split("/")[0])
        except ValueError:
            return None

    tags.year = _parse_int(_get("DATE", "TYER", "TDRC", "\xa9day"))
    tags.track_number = _parse_int(_get("TRACKNUMBER", "TRCK", "TRACK"))
    tags.disc_number = _parse_int(_get("DISCNUMBER", "TPOS", "DISC"))
    # Parse totals if present in slash notation (e.g. "3/12" or "1/2")
    trk = _get("TRACKNUMBER", "TRCK", "TRACK")
    if trk and "/" in str(trk):
        with contextlib.suppress(ValueError, IndexError):
            tags.track_total = int(str(trk).split("/")[1])
    disc = _get("DISCNUMBER", "TPOS")
    if disc and "/" in str(disc):
        with contextlib.suppress(ValueError, IndexError):
            tags.disc_total = int(str(disc).split("/")[1])

    # Format-specific numeric tags (MP4, APEv2)
    if isinstance(audio, MP4):
        trkn = audio.get("trkn")
        if isinstance(trkn, list) and trkn and isinstance(trkn[0], tuple):
            tags.track_number, tags.track_total = trkn[0]
        disk = audio.get("disk")
        if isinstance(disk, list) and disk and isinstance(disk[0], tuple):
            tags.disc_number, tags.disc_total = disk[0]
    elif isinstance(audio, APEv2File):
        trk = audio.get("Track")
        if trk:
            parts = str(trk).split("/")
            with contextlib.suppress(ValueError):
                tags.track_number = int(parts[0])
            if len(parts) > 1:
                with contextlib.suppress(ValueError):
                    tags.track_total = int(parts[1])
        disc_val = audio.get("Disc")
        if disc_val:
            parts = str(disc_val).split("/")
            with contextlib.suppress(ValueError):
                tags.disc_number = int(parts[0])
            if len(parts) > 1:
                with contextlib.suppress(ValueError):
                    tags.disc_total = int(parts[1])

    return tags


def _default_template() -> str:
    return "{album_artist}/{album} [{year}]/{disc_track}. {title}.{format}"


def _apply_template(template: str, tags: TrackTags, fmt: str) -> Path:
    """Substitute template variables and return a relative Path."""
    artist = tags.artist or "Unknown Artist"
    album_artist = tags.album_artist or artist
    album = tags.album or "Unknown Album"
    title = tags.title or "Unknown Title"
    year = tags.year or 0
    disc = tags.disc_number or 1
    track = tags.track_number or 1
    disc_total = tags.disc_total

    # Smart disc-track prefix: omit disc number for single-disc albums
    disc_track = f"{disc:02d}-{track:02d}" if (disc_total and disc_total > 1) else f"{track:02d}"

    # Sanitize each path component
    parts = template.split("/")
    resolved: list[str] = []
    for part in parts:
        name = part.format(
            artist=sanitize_filename(artist),
            album_artist=sanitize_filename(album_artist),
            album=sanitize_filename(album),
            title=sanitize_filename(title),
            year=year,
            disc=disc,
            track=track,
            disc_track=disc_track,
            format=fmt,
        )
        resolved.append(name)

    return Path(*resolved)


def _resolve_collision(dest: Path) -> Path:
    """If *dest* exists, append (1), (2), … before the extension."""
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


@dataclass(slots=True)
class OrganizePlan:
    source: Path
    proposed: Path
    tags: TrackTags = field(default_factory=TrackTags)
    status: str = "pending"  # pending, moved, error
    error: str | None = None


def plan_organization(
    paths: list[Path],
    output_root: Path,
    template: str | None = None,
) -> list[OrganizePlan]:
    """Build a move plan for every *path* without touching disk."""
    tmpl = template or _default_template()
    plans: list[OrganizePlan] = []
    for p in paths:
        try:
            tags = _get_tags(p)
        except MutagenError as exc:
            plans.append(OrganizePlan(source=p, proposed=output_root / p.name, error=str(exc), status="error"))
            continue
        except Exception as exc:
            plans.append(OrganizePlan(source=p, proposed=output_root / p.name, error=str(exc), status="error"))
            continue

        rel = _apply_template(tmpl, tags, p.suffix.lstrip(".").lower())
        proposed = output_root / rel
        plans.append(OrganizePlan(source=p, proposed=proposed, tags=tags, status="pending"))
    return plans


def execute_organization(
    plans: list[OrganizePlan],
    *,
    dry_run: bool = True,
    move: bool = True,
    on_move: Callable[[OrganizePlan], None] | None = None,
) -> list[OrganizePlan]:
    """Execute (or preview) a list of move plans.

    Returns the updated plans with *status* and *proposed* set to the
    actual path used (collision suffixes applied).
    """
    for plan in plans:
        if plan.status == "error":
            continue

        if not plan.source.exists():
            plan.status = "error"
            plan.error = f"Source file missing: {plan.source}"
            continue

        # Guard against self-moves (already at target) — skip gracefully
        try:
            if plan.source.samefile(plan.proposed):
                plan.status = "already"
                continue
        except OSError:
            pass  # can't determine samefile, proceed and let shutil decide

        dest = _resolve_collision(plan.proposed)
        plan.proposed = dest

        if dry_run:
            plan.status = "dry-run"
            continue

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(str(plan.source), str(dest))
            else:
                shutil.copy2(str(plan.source), str(dest))
            plan.status = "moved" if move else "copied"
            if on_move:
                on_move(plan)
        except OSError as exc:
            plan.status = "error"
            plan.error = f"{type(exc).__name__}: {exc}"
        except shutil.Error as exc:
            plan.status = "error"
            plan.error = f"shutil.Error: {exc}"

    return plans
