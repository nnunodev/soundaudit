"""Fix missing track/disc numbers and uppercase keys for Navidrome compatibility."""
from __future__ import annotations

import json
from pathlib import Path

from mutagen.flac import FLAC


def standardize_flac(path: Path, *, dry_run: bool = True, position: int = 1, total_tracks: int = 1, total_discs: int = 1) -> bool:
    """Uppercase keys, add missing TRACKNUMBER/DISCNUMBER, dedup totals.

    Returns True if changes were made.
    """
    audio = FLAC(str(path))

    # Read current tags
    tags: dict[str, list[str]] = {}
    for k, vals in audio.items():
        uk = k.upper()
        if uk in tags:
            tags[uk] = list(dict.fromkeys(tags[uk] + [str(v) for v in vals]))
        else:
            tags[uk] = [str(v) for v in vals]

    changed = False

    # --- TRACKNUMBER ---
    has_tracknum = any(
        k.startswith("TRACKNUMBER") or k in ("TRACK", "TRCK")
        for k in tags
    )
    if not has_tracknum and total_tracks > 0:
        tags["TRACKNUMBER"] = [f"{position}/{total_tracks}"]
        changed = True

    # --- DISCNUMBER ---
    has_discnum = any(
        k.startswith("DISCNUMBER") or k in ("DISC", "TPOS")
        for k in tags
    )
    if not has_discnum and total_discs > 0:
        tags["DISCNUMBER"] = [f"1/{total_discs}"]
        changed = True

    # --- Deduplicate redundant totals ---
    # Keep only TRACKTOTAL (remove TOTALTRACKS if both exist)
    if "TRACKTOTAL" in tags and "TOTALTRACKS" in tags:
        del tags["TOTALTRACKS"]
        changed = True
    if "DISCTOTAL" in tags and "TOTALDISCS" in tags:
        del tags["TOTALDISCS"]
        changed = True
    # Normalize: if only TOTALTRACKS exists, rename to TRACKTOTAL
    if "TOTALTRACKS" in tags and "TRACKTOTAL" not in tags:
        tags["TRACKTOTAL"] = tags.pop("TOTALTRACKS")
        changed = True
    if "TOTALDISCS" in tags and "DISCTOTAL" not in tags:
        tags["DISCTOTAL"] = tags.pop("TOTALDISCS")
        changed = True

    # Check if any lowercase keys remain (shouldn't happen after uppercase logic above, but safety net)
    for k in list(tags):
        if k != k.upper():
            uk = k.upper()
            if uk not in tags:
                tags[uk] = tags.pop(k)
            else:
                tags[uk] = list(dict.fromkeys(tags[uk] + tags.pop(k)))
            changed = True

    if not changed:
        return False

    if not dry_run:
        # Backup
        backup = path.with_suffix(path.suffix + ".tags_backup.json")
        if not backup.exists():
            original = {k: [str(v) for v in vals] for k, vals in FLAC(str(path)).items()}
            backup.write_text(json.dumps(original, indent=2, ensure_ascii=False), encoding="utf-8")

        audio.clear()
        for k, vals in tags.items():
            audio[k] = vals
        audio.save()

    return True


def process_folder(folder: Path, *, dry_run: bool = True) -> tuple[int, int]:
    """Process all FLACs in a single album folder. Returns (changed, skipped)."""
    flacs = sorted(folder.glob("*.flac"))
    if not flacs:
        return 0, 0

    total_tracks = len(flacs)
    total_discs = 1  # assume single disc; override if DISCTOTAL says otherwise

    # Peek at first file to see if DISCTOTAL > 1
    peek = FLAC(str(flacs[0]))
    for k, vals in peek.items():
        if k.upper() in ("DISCTOTAL", "TOTALDISCS"):
            try:
                total_discs = max(1, int(str(vals[0])))
            except (ValueError, IndexError):
                pass

    changed = 0
    skipped = 0
    for idx, f in enumerate(flacs, 1):
        if standardize_flac(f, dry_run=dry_run, position=idx, total_tracks=total_tracks, total_discs=total_discs):
            changed += 1
        else:
            skipped += 1
    return changed, skipped


def process_tree(root: Path, *, dry_run: bool = True) -> tuple[int, int, int]:
    """Process every subfolder under root that contains FLACs."""
    total_changed = 0
    total_skipped = 0
    errors = 0
    for folder in root.rglob("*"):
        if not folder.is_dir():
            continue
        flacs = list(folder.glob("*.flac"))
        if not flacs:
            continue
        try:
            c, s = process_folder(folder, dry_run=dry_run)
            total_changed += c
            total_skipped += s
        except Exception as exc:
            print(f"[ERROR] {folder.name}: {exc}")
            errors += 1
    return total_changed, total_skipped, errors


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "dryrun"
    target = sys.argv[2] if len(sys.argv) > 2 else "//HyperionNAS/Vol2/Music/Rammstein"
    root = Path(target)
    if not root.exists():
        print(f"Path not found: {root}")
        sys.exit(1)

    dry = cmd != "apply"
    c, s, e = process_tree(root, dry_run=dry)
    mode = "DRY-RUN" if dry else "APPLIED"
    print(f"\n[{mode}] {c} changed, {s} skipped, {e} errors")
