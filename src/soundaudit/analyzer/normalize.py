"""Tag-normalization analyzer — detects and proposes fixes for inconsistent
metadata within album folders (same parent directory).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from soundaudit.models import TrackTags
from soundaudit.organizer import _get_tags, _strip_featured_artists

# Fields that album tracks are expected to share
_NORMALIZE_FIELDS = ("album", "album_artist", "year", "artist")


def _tag_value(tags: TrackTags, field: str) -> str | int | None:
    """Safely extract a scalar value from TrackTags."""
    val = getattr(tags, field, None)
    if val is None:
        return None
    if field == "year":
        return val if isinstance(val, int) else None
    return str(val).strip() if str(val).strip() else None


def _normalize_value(field: str, val: str | int | None) -> str | int | None:
    """Clean a tag value for comparison."""
    if val is None:
        return None
    if field == "year":
        return val if isinstance(val, int) else None
    v = str(val).strip()
    # Collapse whitespace
    v = re.sub(r"\s+", " ", v)
    return v


def _majority_value(
    values: Sequence[str | int | None],
    field: str = "",
    *,
    min_agree: int = 2,
    threshold: float = 0.4,
) -> str | int | None:
    """Return the majority value if it meets *threshold* share and *min_agree*.

    Returns None if no clear majority (e.g. every track differs).
    """
    cleaned = [_normalize_value(field, v) for v in values]
    filtered = [v for v in cleaned if v is not None]
    if not filtered:
        return None
    most_common = Counter(filtered).most_common(1)[0]
    count = most_common[1]
    if count < min_agree:
        return None
    if count / len(values) < threshold:
        return None
    return most_common[0]


@dataclass(slots=True)
class TagInconsistency:
    path: Path
    field: str
    current: str | int | None
    proposed: str | int | None


@dataclass(slots=True)
class FolderNormalization:
    folder: Path
    file_count: int
    fixes: list[TagInconsistency] = field(default_factory=list)
    # majority values per field
    majorities: dict[str, str | int | None] = field(default_factory=dict)


def analyze_folder(
    folder: Path,
    *,
    fields: tuple[str, ...] = _NORMALIZE_FIELDS,
    min_files: int = 2,
    extensions: set[str] | None = None,
    tag_reader: Callable[[Path], TrackTags] = _get_tags,
) -> FolderNormalization | None:
    """Scan a single folder for tag inconsistencies.

    Returns None if the folder has fewer than *min_files* audio files or
    no clear majority values.
    """
    _exts = extensions or {
        ".flac", ".mp3", ".m4a", ".ogg", ".wav", ".ape", ".wv", ".aiff", ".aac"
    }
    files: list[Path] = []
    for child in folder.iterdir():
        if child.is_file() and child.suffix.lower() in _exts:
            files.append(child)

    if len(files) < min_files:
        return None

    # Read tags
    tags_list: list[TrackTags] = []
    for f in files:
        try:
            tags_list.append(tag_reader(f))
        except Exception:
            tags_list.append(TrackTags())

    result = FolderNormalization(folder=folder, file_count=len(files))

    for fld in fields:
        vals: list[str | int | None] = []
        for tags in tags_list:
            raw = _tag_value(tags, fld)
            if fld == "artist":
                raw = _strip_featured_artists(raw) if isinstance(raw, str) else raw
            vals.append(raw)

        majority = _majority_value(vals, field=fld)
        if majority is not None:
            result.majorities[fld] = majority
            for idx, f in enumerate(files):
                current = vals[idx]
                norm_current = _normalize_value(fld, current)
                norm_majority = _normalize_value(fld, majority)
                if norm_current is not None and norm_current != norm_majority:
                    result.fixes.append(
                        TagInconsistency(
                            path=f,
                            field=fld,
                            current=current,
                            proposed=majority,
                        )
                    )

    return result if result.fixes else None


def scan_folders(
    paths: list[Path],
    *,
    fields: tuple[str, ...] = _NORMALIZE_FIELDS,
    min_files: int = 2,
    extensions: set[str] | None = None,
    tag_reader: Callable[[Path], TrackTags] = _get_tags,
) -> list[FolderNormalization]:
    """Recursively scan *paths* and return folders with detected inconsistencies."""
    _exts = extensions or {
        ".flac", ".mp3", ".m4a", ".ogg", ".wav", ".ape", ".wv", ".aiff", ".aac"
    }
    seen: set[Path] = set()
    results: list[FolderNormalization] = []

    for p in paths:
        root = Path(p)
        if not root.exists():
            continue
        if root.is_file():
            root = root.parent
        # Include root itself plus all descendant directories
        folders = [root]
        folders.extend(root.rglob("*"))
        for folder in folders:
            if not folder.is_dir():
                continue
            if folder in seen:
                continue
            seen.add(folder)
            # Quick check: does this folder contain >= min_files audio files?
            audio_count = sum(
                1 for c in folder.iterdir() if c.is_file() and c.suffix.lower() in _exts
            )
            if audio_count < min_files:
                continue
            analysis = analyze_folder(
                folder,
                fields=fields,
                min_files=min_files,
                extensions=_exts,
                tag_reader=tag_reader,
            )
            if analysis:
                results.append(analysis)

    return results
