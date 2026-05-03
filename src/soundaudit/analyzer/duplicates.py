"""Duplicate detection by content hash with smart keeper recommendations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from rich.console import Console
from sqlalchemy import func

from soundaudit.db.store import DBFile, DuplicateGroup


class KeeperVerdict(str, Enum):
    KEEP = "KEEP"
    DELETE = "DELETE"
    REVIEW = "REVIEW"


@dataclass
class FileVerdict:
    """Per-file recommendation within a duplicate group."""

    db_file: DBFile
    score: float
    verdict: KeeperVerdict
    reasons: list[str]
    dup_type: str = ""

    @property
    def tech_summary(self) -> str:
        """Short technical summary e.g. 'FLAC 24bit/96kHz'."""
        parts = []
        if self.db_file.format:
            parts.append(self.db_file.format.upper())
        if self.db_file.bit_depth:
            parts.append(f"{self.db_file.bit_depth}bit")
        if self.db_file.sample_rate_hz:
            khz = self.db_file.sample_rate_hz / 1000
            parts.append(f"{khz:g}kHz")
        if self.db_file.bitrate_kbps and not self.db_file.lossless:
            parts.append(f"{int(self.db_file.bitrate_kbps)}kbps")
        return " ".join(parts) if parts else "unknown"

    @property
    def album_context(self) -> str:
        """Album name, or '(single)' if it looks like a single release."""
        album = self.db_file.album or ""
        title = self.db_file.title or ""
        path = self.db_file.path.lower()

        # Explicit single indicators
        if "single" in path.split(os.sep):
            return f"[dim]{album or 'Single'}[/dim]"
        if album and title and album.strip().lower() == title.strip().lower():
            return "[dim]Single[/dim]"
        return album or "[dim]—[/dim]"


@dataclass
class GroupVerdict:
    """Full analysis of a duplicate group with per-file recommendations."""

    group: DuplicateGroupResult
    file_verdicts: list[FileVerdict]

    @property
    def keepers(self) -> list[FileVerdict]:
        return [v for v in self.file_verdicts if v.verdict == KeeperVerdict.KEEP]

    @property
    def deletions(self) -> list[FileVerdict]:
        return [v for v in self.file_verdicts if v.verdict == KeeperVerdict.DELETE]

    @property
    def reviews(self) -> list[FileVerdict]:
        return [v for v in self.file_verdicts if v.verdict == KeeperVerdict.REVIEW]

    @property
    def wasted_bytes(self) -> int:
        return sum(v.db_file.size_bytes for v in self.deletions)




def find_duplicate_groups(database) -> list[DuplicateGroupResult]:
    """
    Find files grouped by identical content_hash.
    Ignores NULL hashes (unreadable/corrupt files) and single-instance hashes.

    Returns list of DuplicateGroupResult sorted by total wasted bytes desc.
    """
    with database.session() as s:
        rows = (
            s.query(
                DBFile.content_hash,
                func.count(DBFile.id).label("count"),
                func.sum(DBFile.size_bytes).label("total_size"),
            )
            .filter(DBFile.content_hash.is_not(None))
            .group_by(DBFile.content_hash)
            .having(func.count(DBFile.id) > 1)
            .all()
        )

        results = []
        for content_hash, count, total_size in rows:
            files = (
                s.query(DBFile)
                .filter_by(content_hash=content_hash)
                .order_by(DBFile.path)
                .all()
            )
            results.append(
                DuplicateGroupResult(
                    content_hash=content_hash,
                    file_count=count,
                    total_size_bytes=total_size or 0,
                    files=files,
                )
            )

    results.sort(key=lambda r: r.wasted_bytes, reverse=True)
    return results


def write_duplicate_groups(database, results: list[DuplicateGroupResult]) -> int:
    """
    Persist duplicate groups to DB and update files.duplicate_group_id.
    Clears old groups first. Preserves ignored status for hashes that were
    previously marked ignored by the user.
    Returns number of groups written.
    """
    with database.session() as s:
        # Collect ignored hashes before clearing so we can restore them
        ignored_hashes = {
            g.acoustid for g in s.query(DuplicateGroup).filter_by(ignored=1).all()
            if g.acoustid
        }

        # Clear existing
        s.query(DBFile).update(
            {"duplicate_group_id": None},
            synchronize_session=False,
        )
        s.query(DuplicateGroup).delete(synchronize_session=False)

        for result in results:
            group = DuplicateGroup(
                acoustid=result.content_hash,
                group_type="content_hash",
                ignored=int(result.content_hash in ignored_hashes),
            )
            s.add(group)
            s.flush()  # get group.id

            # Update files in THIS session (result.files are detached)
            s.query(DBFile).filter_by(content_hash=result.content_hash).update(
                {"duplicate_group_id": group.id},
                synchronize_session=False,
            )

            result.group_id = group.id

        s.commit()
    return len(results)


def analyze_keepers(group: DuplicateGroupResult) -> GroupVerdict:
    """
    Score every file in a duplicate group and decide which to keep/delete.

    Scoring (highest wins):
      1. Lossless (FLAC/OGG/WAV)  → +1000
      2. Album (not single)        → +100
      3. Bit depth                 → +depth * 10
      4. Sample rate (kHz)         → +rate
      5. File size (normalized)    → 0..50
      6. Tag completeness          → +10 per filled field
    """
    if not group.files:
        return GroupVerdict(group=group, file_verdicts=[])

    max_size = max(f.size_bytes for f in group.files) or 1

    scored: list[tuple[DBFile, float, list[str]]] = []
    for f in group.files:
        score = 0.0
        reasons: list[str] = []

        # 1. Lossless
        if f.lossless:
            score += 1000
            reasons.append("lossless")

        # 2. Album vs Single
        album = (f.album or "").strip().lower()
        title = (f.title or "").strip().lower()
        path_parts = f.path.lower().split(os.sep)
        is_single = "single" in path_parts or (album and album == title)
        if not is_single and f.album:
            score += 100
            reasons.append("album")
        elif is_single:
            reasons.append("single")

        # 3. Bit depth
        if f.bit_depth:
            score += f.bit_depth * 10
            reasons.append(f"{f.bit_depth}bit")

        # 4. Sample rate
        if f.sample_rate_hz:
            score += f.sample_rate_hz / 1000
            reasons.append(f"{f.sample_rate_hz / 1000:g}kHz")

        # 5. File size (normalized 0-50)
        score += (f.size_bytes / max_size) * 50
        reasons.append(_human_size(f.size_bytes))

        # 6. Tag completeness
        tag_fields = [f.title, f.artist, f.album, f.album_artist, f.year, f.genre, f.isrc]
        filled = sum(1 for t in tag_fields if t)
        score += filled * 10
        if filled >= 5:
            reasons.append("complete tags")

        scored.append((f, score, reasons))

    # Sort by score descending
    scored.sort(key=lambda x: x[1], reverse=True)

    # Assign verdicts — the top scorer gets KEEP, others DELETE.
    # If top two are within 5%, mark REVIEW.
    file_verdicts: list[FileVerdict] = []
    best_score = scored[0][1] if scored else 0
    for i, (f, score, reasons) in enumerate(scored):
        if i == 0:
            verdict = KeeperVerdict.KEEP
        else:
            if best_score > 0 and abs(score - best_score) / best_score < 0.05:
                verdict = KeeperVerdict.REVIEW
            else:
                verdict = KeeperVerdict.DELETE
        file_verdicts.append(
            FileVerdict(db_file=f, score=score, verdict=verdict, reasons=reasons)
        )

    return GroupVerdict(group=group, file_verdicts=file_verdicts)


@dataclass
class DuplicateGroupResult:
    """Result of duplicate analysis for one content hash."""

    content_hash: str
    file_count: int
    total_size_bytes: int
    files: list[DBFile]  # list of DBFile objects (may be detached)
    group_id: int | None = None

    @property
    def wasted_bytes(self) -> int:
        """Space that could be freed by keeping one copy."""
        sizes = [f.size_bytes for f in self.files]
        return self.total_size_bytes - max(sizes)

    @property
    def wasted_human(self) -> str:
        return _human_size(self.wasted_bytes)

    @property
    def total_human(self) -> str:
        return _human_size(self.total_size_bytes)


class DuplicateAnalyzer:
    """High-level duplicate analyzer with progress UI."""

    def __init__(self, database, console: Console | None = None) -> None:
        self.database = database
        self.console = console or Console()

    def run(self) -> list[DuplicateGroupResult]:
        self.console.print("[cyan]Analyzing duplicates by content hash...[/cyan]")

        results = find_duplicate_groups(self.database)
        if results:
            write_duplicate_groups(self.database, results)
            total_wasted = sum(r.wasted_bytes for r in results)
            self.console.print(
                f"[green]Found {len(results)} duplicate groups, "
                f"{sum(r.file_count for r in results)} files total. "
                f"Potential space saved: {_human_size(total_wasted)}[/green]"
            )
        else:
            self.console.print("[green]No duplicates found.[/green]")

        return results


def delete_duplicate_group_files(
    database,
    group_id: int,
    *,
    dry_run: bool = False,
) -> tuple[list[str], list[str], int]:
    """Delete files marked DELETE in a single content-hash duplicate group.

    Returns (deleted_paths, errors, bytes_freed).
    If dry_run, returns what WOULD be deleted without touching disk.
    """
    from soundaudit.db.store import DBFile, DuplicateGroup

    with database.session() as s:
        db_group = s.query(DuplicateGroup).get(group_id)
        if not db_group:
            return [], ["Group not found"], 0
        files = (
            s.query(DBFile)
            .filter_by(duplicate_group_id=group_id)
            .order_by(DBFile.path)
            .all()
        )

    if not files or len(files) < 2:
        return [], ["Group has fewer than 2 files"], 0

    result = DuplicateGroupResult(
        content_hash=db_group.acoustid or "",
        file_count=len(files),
        total_size_bytes=sum(f.size_bytes for f in files),
        files=files,
        group_id=db_group.id,
    )
    verdict = analyze_keepers(result)

    deleted: list[str] = []
    errors: list[str] = []
    bytes_freed = 0

    for fv in verdict.file_verdicts:
        if fv.verdict != KeeperVerdict.DELETE:
            continue
        p = Path(fv.db_file.path)
        if dry_run:
            deleted.append(str(p))
            bytes_freed += fv.db_file.size_bytes
            continue
        try:
            if p.exists():
                p.unlink()
            deleted.append(str(p))
            bytes_freed += fv.db_file.size_bytes
        except OSError as exc:
            errors.append(f"Failed to delete {p}: {exc}")

    if deleted and not dry_run:
        database.delete_by_paths(set(deleted))

    return deleted, errors, bytes_freed


def delete_all_marked_group_files(
    database,
    *,
    dry_run: bool = False,
    include_acoustid: bool = True,
) -> tuple[list[str], list[str], int]:
    """Delete ALL files marked DELETE across all non-ignored groups.

    Returns (deleted_paths, errors, bytes_freed).
    """
    from soundaudit.db.store import DuplicateGroup

    deleted: list[str] = []
    errors: list[str] = []
    total_bytes = 0

    with database.session() as s:
        groups = s.query(DuplicateGroup).filter_by(ignored=0).all()

    for g in groups:
        d_paths, e_paths, b = delete_duplicate_group_files(database, g.id, dry_run=dry_run)
        deleted.extend(d_paths)
        errors.extend(e_paths)
        total_bytes += b

    if include_acoustid:
        from soundaudit.analyzer.acoustid import delete_acoustid_group_files
        from soundaudit.db.store import AcoustidGroup

        with database.session() as s:
            groups = s.query(AcoustidGroup).filter_by(ignored=0).all()

        for g in groups:
            d_paths, e_paths, b = delete_acoustid_group_files(database, g.id, dry_run=dry_run)
            deleted.extend(d_paths)
            errors.extend(e_paths)
            total_bytes += b

    return deleted, errors, total_bytes


def _human_size(size_bytes: int | float) -> str:
    """Convert bytes to human readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"
