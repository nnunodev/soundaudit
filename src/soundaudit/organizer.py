"""Navidrome folder-structure organizer.

Rearranges downloaded audio files into the Artist/Album/Track hierarchy
that Navidrome expects, using embedded metadata (tags) to build safe,
collision-free destination paths.
"""

from __future__ import annotations

import contextlib
import re
import shutil
from collections import Counter, defaultdict
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


# Regex to strip featured-artist suffixes from artist strings.
# Handles  "Artist feat. Guest", "Artist (feat. Guest)", "Artist [ft. Guest]", etc.
_FEAT_RE = re.compile(
    r"\s*[\(\[](?:feat\.?|ft\.?|featuring)[^\)\]]*[\)\]]"
    r"|\s+(?:feat\.?|ft\.?|featuring)\b.*$",
    re.IGNORECASE,
)

_KNOWN_COMP_NAMES = {
    "various artists", "va", "v.a.", "various",
    "ost", "soundtrack", "original soundtrack",
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


def _strip_featured_artists(name: str) -> str:
    """Remove 'feat.', 'ft.', 'featuring' suffixes from an artist string."""
    if not name:
        return ""
    clean = _FEAT_RE.sub("", name).strip()
    # Collapse multiple spaces left behind
    clean = re.sub(r"\s+", " ", clean)
    return clean


def _normalize_album_artist(
    album_artist: str | None,
    artist: str | None,
    compilation_names: set[str] | None = None,
    strip_featured: bool = True,
) -> str:
    """Return a canonical album-artist string for folder naming."""
    if album_artist is not None:
        normalized = album_artist.strip()
        if normalized:
            comp_set = compilation_names if compilation_names is not None else _KNOWN_COMP_NAMES
            if normalized.lower() in comp_set:
                return "Various Artists"
            return normalized
    if artist:
        if strip_featured:
            clean = _strip_featured_artists(artist.strip())
            return clean or "Unknown Artist"
        return artist.strip() or "Unknown Artist"
    return "Unknown Artist"


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

    # Compilation flag (iTunes / ID3 / Vorbis)
    comp_val = _get("TCMP", "COMPILATION")
    if comp_val and comp_val.strip() in ("1", "True", "true", "yes", "Yes"):
        tags.compilation = True
    if isinstance(audio, MP4):
        cpil = audio.get("cpil")
        if cpil and isinstance(cpil, list) and cpil and cpil[0]:
            tags.compilation = True

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

    # Fallback: parse track number from filename (e.g. "01 - Title.flac")
    if tags.track_number is None:
        m = re.search(r"^\s*(\d{1,3})(?:\s*[-._\s]\s*|\s+)", path.stem)
        if m:
            with contextlib.suppress(ValueError):
                tags.track_number = int(m.group(1))

    return tags


def _is_compilation_group(
    group: list[OrganizePlan],
    *,
    strip_featured: bool = True,
) -> bool:
    """Heuristic: does this album group look like a compilation?"""
    # Explicit compilation flag on any track
    if any(p.tags.compilation for p in group if p.tags.compilation is not None):
        return True

    artists_counter: Counter[str] = Counter()
    for plan in group:
        a = plan.tags.artist or ""
        if strip_featured:
            a = _strip_featured_artists(a)
        a = a.strip().lower()
        if a:
            artists_counter[a] += 1

    total = len(group)
    distinct = len(artists_counter)
    if distinct == 0:
        return False
    if distinct >= 3:
        return True
    if distinct >= 2 and total >= 3:
        max_share = artists_counter.most_common(1)[0][1] / total
        return max_share < 0.6
    return False


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
    status: str = "pending"  # pending, moved, error, skipped, dry-run, already, copied
    error: str | None = None
    skip_reason: str | None = None


def plan_organization(
    paths: list[Path],
    output_root: Path,
    template: str | None = None,
    compilation_names: set[str] | None = None,
    strip_featured: bool = True,
    min_album_tracks: int = 0,
) -> list[OrganizePlan]:
    """Build a move plan for every *path* without touching disk."""
    tmpl = template or _default_template()
    plans: list[OrganizePlan] = []
    errored: list[OrganizePlan] = []

    # First pass: read tags
    for p in paths:
        try:
            tags = _get_tags(p)
        except MutagenError as exc:
            errored.append(OrganizePlan(source=p, proposed=output_root / p.name, error=str(exc), status="error"))
            continue
        except Exception as exc:
            errored.append(OrganizePlan(source=p, proposed=output_root / p.name, error=str(exc), status="error"))
            continue
        plans.append(OrganizePlan(source=p, proposed=Path(), tags=tags, status="pending"))

    # Normalize album_artist within albums that sit in the same source folder.
    # If some tracks in a folder have album_artist and others don't, propagate
    # the existing value so the album stays together.
    groups: dict[tuple[str, str, int], list[OrganizePlan]] = defaultdict(list)
    for plan in plans:
        album = plan.tags.album or "Unknown Album"
        year = plan.tags.year or 0
        key = (str(plan.source.parent), sanitize_filename(album), year)
        groups[key].append(plan)

    for group in groups.values():
        # Gather explicit album_artists, treating empty/whitespace as missing
        explicit = []
        for p in group:
            aa = p.tags.album_artist
            if aa and str(aa).strip():
                explicit.append(str(aa).strip())

        # Does this group represent a real album (at least one track has a
        # non-empty album tag)?  Loose tracks with no album should not be
        # unified — they fall back to individual artist folders.
        has_real_album = any(
            p.tags.album and str(p.tags.album).strip() and str(p.tags.album).strip().lower() != "unknown album"
            for p in group
        )

        if explicit:
            chosen = Counter(explicit).most_common(1)[0][0]
            if has_real_album and _is_compilation_group(group, strip_featured=strip_featured):
                comp_set = compilation_names if compilation_names is not None else _KNOWN_COMP_NAMES
                for name, _ in Counter(explicit).most_common():
                    if name.lower() in comp_set:
                        chosen = name
                        break
            for plan in group:
                if not plan.tags.album_artist or not str(plan.tags.album_artist).strip():
                    plan.tags.album_artist = chosen
            continue

        # No explicit album_artist at all.
        if has_real_album and _is_compilation_group(group, strip_featured=strip_featured):
            for plan in group:
                if not plan.tags.album_artist or not str(plan.tags.album_artist).strip():
                    plan.tags.album_artist = "Various Artists"
            continue

        # Real album but not a compilation — unify under majority artist so
        # the album doesn't fragment across multiple folders.
        if has_real_album:
            artists_counter: Counter[str] = Counter()
            for plan in group:
                a = plan.tags.artist or ""
                if strip_featured:
                    a = _strip_featured_artists(a)
                a = a.strip()
                if a:
                    artists_counter[a] += 1
            if artists_counter:
                chosen_artist = artists_counter.most_common(1)[0][0]
                for plan in group:
                    if not plan.tags.album_artist or not str(plan.tags.album_artist).strip():
                        plan.tags.album_artist = chosen_artist

    # Second pass: build destination paths using normalized album_artist
    for plan in plans:
        plan.tags.album_artist = _normalize_album_artist(
            plan.tags.album_artist,
            plan.tags.artist,
            compilation_names=compilation_names,
            strip_featured=strip_featured,
        )
        rel = _apply_template(tmpl, plan.tags, plan.source.suffix.lstrip(".").lower())
        plan.proposed = output_root / rel

    # If min_album_tracks is set, skip reorganizing albums with fewer than N
    # tracks in this source batch.  Tracks that belong to "sparse" albums stay
    # where they are (proposed == source).
    if min_album_tracks > 0:
        album_counts: Counter[str] = Counter()
        for plan in plans:
            aa = sanitize_filename(plan.tags.album_artist or "Unknown Artist")
            album = sanitize_filename(plan.tags.album or "Unknown Album")
            key = f"{aa}/{album}"
            album_counts[key] += 1
        for plan in plans:
            aa = sanitize_filename(plan.tags.album_artist or "Unknown Artist")
            album = sanitize_filename(plan.tags.album or "Unknown Album")
            key = f"{aa}/{album}"
            if album_counts[key] < min_album_tracks:
                plan.status = "skipped"
                plan.skip_reason = f"incomplete album ({album_counts[key]} < {min_album_tracks} tracks)"
                # keep proposed pointing to original source so preview/execution
                # both show it staying put
                plan.proposed = plan.source

    return plans + errored


def execute_organization(
    plans: list[OrganizePlan],
    *,
    dry_run: bool = True,
    move: bool = True,
    skip_existing: bool = False,
    on_move: Callable[[OrganizePlan], None] | None = None,
) -> list[OrganizePlan]:
    """Execute (or preview) a list of move plans.

    Returns the updated plans with *status* and *proposed* set to the
    actual path used (collision suffixes applied).
    """
    for plan in plans:
        if plan.status in ("error", "skipped"):
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

        if skip_existing and plan.proposed.exists():
            plan.status = "skipped"
            plan.skip_reason = "destination already exists"
            continue

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
