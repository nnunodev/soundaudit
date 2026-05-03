"""AcoustID / chromaprint duplicate detection.

Groups files by identical chromaprint fingerprints. Within a group,
files that also share a content_hash are flagged as bit-for-bit duplicates;
others are flagged as transcode duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from rich.console import Console
from sqlalchemy import func

from soundaudit.analyzer.duplicates import (
    DuplicateGroupResult,
    FileVerdict,
    GroupVerdict,
    _human_size,
)
from soundaudit.analyzer.duplicates import (
    analyze_keepers as _analyze_keepers,
)
from soundaudit.db.store import AcoustidGroup, DBFile


class DupType(str, Enum):
    BIT_FOR_BIT = "bit-for-bit"
    TRANSCODE = "transcode"


@dataclass
class AcoustidGroupVerdict(GroupVerdict):
    """Verdict for an AcoustID-based duplicate group."""

    fingerprint: str = ""

    @property
    def bit_for_bit_files(self) -> list[FileVerdict]:
        return [v for v in self.file_verdicts if v.dup_type == DupType.BIT_FOR_BIT.value]

    @property
    def transcode_files(self) -> list[FileVerdict]:
        return [v for v in self.file_verdicts if v.dup_type == DupType.TRANSCODE.value]


def find_acoustid_groups(database) -> list[DuplicateGroupResult]:
    """Find files grouped by identical acoustid_fingerprint.

    Ignores NULL fingerprints and groups with only a single file.
    """
    with database.session() as s:
        rows = (
            s.query(
                DBFile.acoustid_fingerprint,
                func.count(DBFile.id).label("count"),
                func.sum(DBFile.size_bytes).label("total_size"),
            )
            .filter(DBFile.acoustid_fingerprint.is_not(None))
            .group_by(DBFile.acoustid_fingerprint)
            .having(func.count(DBFile.id) > 1)
            .all()
        )

        results = []
        for fingerprint, count, total_size in rows:
            files = (
                s.query(DBFile)
                .filter_by(acoustid_fingerprint=fingerprint)
                .order_by(DBFile.path)
                .all()
            )
            results.append(
                DuplicateGroupResult(
                    content_hash=fingerprint,
                    file_count=count,
                    total_size_bytes=total_size or 0,
                    files=files,
                )
            )

    results.sort(key=lambda r: r.wasted_bytes, reverse=True)
    return results


def write_acoustid_groups(database, results: list[DuplicateGroupResult]) -> int:
    """Persist AcoustID groups and update files.acoustid_group_id.

    Clears old AcoustidGroup memberships first.
    Preserves ignored status for fingerprints that were previously ignored.
    Returns number of groups written.
    """
    with database.session() as s:
        # Collect ignored fingerprints before clearing
        ignored_fps = {
            g.fingerprint for g in s.query(AcoustidGroup).filter_by(ignored=1).all()
        }

        # Clear existing
        s.query(DBFile).update(
            {"acoustid_group_id": None},
            synchronize_session=False,
        )
        s.query(AcoustidGroup).delete(synchronize_session=False)

        for result in results:
            group = AcoustidGroup(
                fingerprint=result.content_hash,
                ignored=int(result.content_hash in ignored_fps),
            )
            s.add(group)
            s.flush()

            s.query(DBFile).filter_by(acoustid_fingerprint=result.content_hash).update(
                {"acoustid_group_id": group.id},
                synchronize_session=False,
            )
            result.group_id = group.id

        s.commit()
    return len(results)


def _classify_dup_type(files: list[DBFile]) -> dict[int, DupType]:
    """Map each DBFile.id to BIT_FOR_BIT or TRANSCODE.

    A file is BIT_FOR_BIT if at least one other file in the same group
    shares its exact content_hash.
    """
    content_hash_counts: dict[str | None, int] = {}
    for f in files:
        content_hash_counts[f.content_hash] = content_hash_counts.get(f.content_hash, 0) + 1

    return {
        f.id: (
            DupType.BIT_FOR_BIT
            if f.content_hash and content_hash_counts.get(f.content_hash, 0) > 1
            else DupType.TRANSCODE
        )
        for f in files
    }


def analyze_acoustid_keepers(group: DuplicateGroupResult) -> AcoustidGroupVerdict:
    """Run keeper analysis on an AcoustID group and flag dup types."""
    base = _analyze_keepers(group)
    dup_types = _classify_dup_type(group.files)

    file_verdicts: list[FileVerdict] = []
    for fv in base.file_verdicts:
        dt = dup_types.get(fv.db_file.id, DupType.TRANSCODE)
        reasons = list(fv.reasons)
        if dt == DupType.BIT_FOR_BIT:
            reasons.append("bit-for-bit")
        else:
            reasons.append("transcode")
        file_verdicts.append(
            FileVerdict(
                db_file=fv.db_file,
                score=fv.score,
                verdict=fv.verdict,
                reasons=reasons,
                dup_type=dt.value,
            )
        )

    return AcoustidGroupVerdict(
        group=base.group,
        file_verdicts=file_verdicts,
        fingerprint=group.content_hash,
    )


class AcoustidDuplicateAnalyzer:
    """High-level AcoustID duplicate analyzer with progress UI."""

    def __init__(self, database, console: Console | None = None) -> None:
        self.database = database
        self.console = console or Console()

    def run(self) -> list[DuplicateGroupResult]:
        self.console.print("[cyan]Analyzing duplicates by AcoustID fingerprint...[/cyan]")

        results = find_acoustid_groups(self.database)
        if results:
            write_acoustid_groups(self.database, results)
            total_wasted = sum(r.wasted_bytes for r in results)
            self.console.print(
                f"[green]Found {len(results)} AcoustID groups, "
                f"{sum(r.file_count for r in results)} files total. "
                f"Potential space saved: {_human_size(total_wasted)}[/green]"
            )
        else:
            self.console.print("[green]No AcoustID duplicates found.[/green]")

        return results


def delete_acoustid_group_files(
    database,
    group_id: int,
    *,
    dry_run: bool = False,
) -> tuple[list[str], list[str], int]:
    """Delete files marked DELETE in a single AcoustID duplicate group.

    Returns (deleted_paths, errors, bytes_freed).
    If dry_run, returns what WOULD be deleted without touching disk.
    """
    from soundaudit.db.store import AcoustidGroup, DBFile

    with database.session() as s:
        db_group = s.query(AcoustidGroup).get(group_id)
        if not db_group:
            return [], ["Group not found"], 0
        files = (
            s.query(DBFile)
            .filter_by(acoustid_group_id=group_id)
            .order_by(DBFile.path)
            .all()
        )

    if not files or len(files) < 2:
        return [], ["Group has fewer than 2 files"], 0

    from soundaudit.analyzer.duplicates import (
        DuplicateGroupResult,
    )

    result = DuplicateGroupResult(
        content_hash=db_group.fingerprint,
        file_count=len(files),
        total_size_bytes=sum(f.size_bytes for f in files),
        files=files,
        group_id=db_group.id,
    )
    verdict = analyze_acoustid_keepers(result)

    deleted: list[str] = []
    errors: list[str] = []
    bytes_freed = 0

    for fv in verdict.file_verdicts:
        if fv.verdict.value != "DELETE":
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
