"""Tag-standardization analyzer — fixes structural tag issues for Navidrome.

- Uppercases all Vorbis comment keys (e.g. tracknumber → TRACKNUMBER)
- Fills in missing TRACKNUMBER / DISCNUMBER from file order
- Deduplicates redundant totals (totaltracks → TRACKTOTAL, totaldiscs → DISCTOTAL)
- Creates JSON backups before writing
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from mutagen.flac import FLAC

from soundaudit.models import TrackTags
from soundaudit.organizer import _get_tags


@dataclass(slots=True)
class StandardizeFix:
    path: Path
    field: str
    current: str | None
    proposed: str


@dataclass(slots=True)
class FolderStandardization:
    folder: Path
    file_count: int
    fixes: list[StandardizeFix] = field(default_factory=list)


def _read_raw_tags(path: Path) -> dict[str, list[str]]:
    """Read all tags from a FLAC, returning uppercase keys."""
    audio = FLAC(str(path))
    tags: dict[str, list[str]] = {}
    for k, vals in audio.items():
        uk = k.upper()
        if uk in tags:
            # Merge, deduplicate
            existing = set(tags[uk])
            for v in vals:
                existing.add(str(v))
            tags[uk] = list(existing)
        else:
            tags[uk] = [str(v) for v in vals]
    return tags


def _needs_standardize(path: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Return (uppercase_tags, list of needed fixes).

    Fix types:
    - missing_tracknum
    - missing_discnum
    - lowercase_key  (shouldn't happen after _read_raw_tags, but safety)
    - redundant_totaltracks
    - redundant_totaldiscs
    """
    tags = _read_raw_tags(path)
    needed: list[str] = []

    has_tracknum = "TRACKNUMBER" in tags or "TRACK" in tags or "TRCK" in tags
    if not has_tracknum:
        needed.append("missing_tracknum")

    has_discnum = "DISCNUMBER" in tags or "DISC" in tags or "TPOS" in tags
    if not has_discnum:
        needed.append("missing_discnum")

    # Redundant totals
    if "TOTALTRACKS" in tags:
        needed.append("redundant_totaltracks")
    if "TOTALDISCS" in tags:
        needed.append("redundant_totaldiscs")

    return tags, needed


def standardize_folder(
    folder: Path,
    *,
    extensions: set[str] | None = None,
    tag_reader: Callable[[Path], TrackTags] = _get_tags,
) -> FolderStandardization | None:
    """Analyze a folder and return needed standardizations.

    Returns None if the folder has no FLAC files or all files are already
    standardized.
    """
    _exts = extensions or {".flac"}
    files = sorted(
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in _exts
    )
    if not files:
        return None

    result = FolderStandardization(folder=folder, file_count=len(files))

    # Determine totals from first file (assume uniform across folder)
    total_tracks = len(files)
    total_discs = 1
    first_tags, first_needed = _needs_standardize(files[0])
    for k, vals in first_tags.items():
        if k in ("DISCTOTAL", "TOTALDISCS"):
            with contextlib.suppress(ValueError, IndexError):
                total_discs = max(1, int(vals[0]))

    for idx, f in enumerate(files, 1):
        tags, needed = _needs_standardize(f)
        if not needed:
            continue

        # Missing track number
        if "missing_tracknum" in needed:
            result.fixes.append(
                StandardizeFix(
                    path=f,
                    field="TRACKNUMBER",
                    current=None,
                    proposed=f"{idx}/{total_tracks}",
                )
            )

        # Missing disc number
        if "missing_discnum" in needed:
            result.fixes.append(
                StandardizeFix(
                    path=f,
                    field="DISCNUMBER",
                    current=None,
                    proposed=f"1/{total_discs}",
                )
            )

        # Redundant totaltracks → we silently rename during apply
        if "redundant_totaltracks" in needed:
            result.fixes.append(
                StandardizeFix(
                    path=f,
                    field="TOTALTRACKS→TRACKTOTAL",
                    current=tags.get("TOTALTRACKS", [""])[0],
                    proposed=tags.get("TOTALTRACKS", [""])[0],
                )
            )

        # Redundant totaldiscs → we silently rename during apply
        if "redundant_totaldiscs" in needed:
            result.fixes.append(
                StandardizeFix(
                    path=f,
                    field="TOTALDISCS→DISCTOTAL",
                    current=tags.get("TOTALDISCS", [""])[0],
                    proposed=tags.get("TOTALDISCS", [""])[0],
                )
            )

    return result if result.fixes else None


def scan_folders_for_standardize(
    paths: list[Path],
    *,
    extensions: set[str] | None = None,
    min_files: int = 1,
) -> list[FolderStandardization]:
    """Recursively scan paths and return folders needing standardization."""
    _exts = extensions or {".flac"}
    seen: set[Path] = set()
    results: list[FolderStandardization] = []

    for p in paths:
        root = Path(p)
        if not root.exists():
            continue
        if root.is_file():
            root = root.parent

        folders = [root]
        folders.extend(root.rglob("*"))
        for folder in folders:
            if not folder.is_dir():
                continue
            if folder in seen:
                continue
            seen.add(folder)

            audio_count = sum(
                1 for c in folder.iterdir()
                if c.is_file() and c.suffix.lower() in _exts
            )
            if audio_count < min_files:
                continue

            analysis = standardize_folder(folder, extensions=_exts)
            if analysis:
                results.append(analysis)

    return results


def apply_standardize(path: Path, backup: bool = True) -> bool:
    """Apply standardization to a single file. Returns True if changed."""
    audio = FLAC(str(path))
    tags: dict[str, list[str]] = {}
    for k, vals in audio.items():
        uk = k.upper()
        if uk in tags:
            existing = set(tags[uk])
            for v in vals:
                existing.add(str(v))
            tags[uk] = list(existing)
        else:
            tags[uk] = [str(v) for v in vals]

    changed = False

    # Determine totals
    total_tracks = 1
    total_discs = 1
    for k, vals in tags.items():
        if k in ("TRACKTOTAL", "TOTALTRACKS"):
            with contextlib.suppress(ValueError, IndexError):
                total_tracks = max(1, int(vals[0]))
        if k in ("DISCTOTAL", "TOTALDISCS"):
            with contextlib.suppress(ValueError, IndexError):
                total_discs = max(1, int(vals[0]))

    # Missing track number
    has_tracknum = "TRACKNUMBER" in tags or "TRACK" in tags or "TRCK" in tags
    if not has_tracknum:
        # Derive position from folder sort order
        folder = path.parent
        siblings = sorted(
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() == ".flac"
        )
        try:
            position = siblings.index(path) + 1
        except ValueError:
            position = 1
        tags["TRACKNUMBER"] = [f"{position}/{total_tracks}"]
        changed = True

    # Missing disc number
    has_discnum = "DISCNUMBER" in tags or "DISC" in tags or "TPOS" in tags
    if not has_discnum:
        tags["DISCNUMBER"] = [f"1/{total_discs}"]
        changed = True

    # Deduplicate totals
    if "TOTALTRACKS" in tags:
        if "TRACKTOTAL" not in tags:
            tags["TRACKTOTAL"] = tags.pop("TOTALTRACKS")
        else:
            del tags["TOTALTRACKS"]
        changed = True
    if "TOTALDISCS" in tags:
        if "DISCTOTAL" not in tags:
            tags["DISCTOTAL"] = tags.pop("TOTALDISCS")
        else:
            del tags["TOTALDISCS"]
        changed = True

    if not changed:
        return False

    if backup:
        backup_path = path.with_suffix(path.suffix + ".tags_backup.json")
        if not backup_path.exists():
            original = {k: [str(v) for v in vals] for k, vals in audio.items()}
            backup_path.write_text(
                json.dumps(original, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    audio.clear()
    for k, vals in tags.items():
        audio[k] = vals
    audio.save()
    return True
