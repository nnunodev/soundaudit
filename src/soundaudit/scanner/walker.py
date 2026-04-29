"""Walk directories and discover audio files."""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from rich.progress import Progress, SpinnerColumn, TextColumn

from soundaudit.models import FileInfo, HashStrategy
from soundaudit.scanner.extractor import extract_file_info


DEFAULT_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".ape", ".wv"}


def discover_files(root: Path, extensions: set[str]) -> Iterator[Path]:
    """Yield audio file paths recursively."""
    for entry in os.scandir(root):
        try:
            if entry.is_dir(follow_symlinks=False):
                yield from discover_files(Path(entry.path), extensions)
            elif entry.is_file(follow_symlinks=False):
                ext = Path(entry.name).suffix.lower()
                if ext in extensions:
                    yield Path(entry.path)
        except OSError:
            continue


def scan_directory(
    root: Path,
    *,
    extensions: set[str] | None = None,
    workers: int = 4,
    existing: dict[str, str] | None = None,
    progress: Progress | None = None,
    hash_strategy: HashStrategy = HashStrategy.HEAD_ONLY,
    fingerprint: bool = False,
    fpcalc_path: str = "/usr/bin/fpcalc",
) -> Iterator[FileInfo]:
    """Yield FileInfo for each discovered audio file, using parallel extraction."""
    extensions = extensions or DEFAULT_EXTENSIONS
    existing = existing or {}

    # First pass: list all files
    all_files = list(discover_files(root, extensions))

    # Filter: skip unchanged files (incremental scan)
    files_to_scan: list[Path] = []
    skipped = 0
    for p in all_files:
        mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat()
        if str(p) in existing and existing[str(p)] == mtime:
            skipped += 1
            continue
        files_to_scan.append(p)

    if progress and skipped:
        progress.console.print(f"[dim]Skipped {skipped} unchanged files[/dim]")

    if not files_to_scan:
        return

    task_id = None
    if progress:
        task_id = progress.add_task("Scanning...", total=len(files_to_scan))

    def process(p: Path) -> FileInfo | None:
        try:
            info = extract_file_info(
                p,
                hash_strategy=hash_strategy,
                fingerprint=fingerprint,
                fpcalc_path=fpcalc_path,
            )
            if progress and task_id is not None:
                progress.advance(task_id)
            return info
        except Exception:
            return None

    # Parallel extraction -- network-latency on CIFS hides thread overhead
    with ThreadPoolExecutor(max_workers=min(workers, 16)) as pool:
        for info in pool.map(process, files_to_scan):
            if info is not None:
                yield info

    if progress and task_id is not None:
        progress.remove_task(task_id)
