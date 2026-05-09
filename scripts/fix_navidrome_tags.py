"""Preview and fix lowercase Vorbis comment keys for Navidrome compatibility."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from mutagen.flac import FLAC


def load_tags(audio: FLAC) -> dict[str, list[str]]:
    return {k: [str(v) for v in vals] for k, vals in audio.items()}


def needs_fix(tags: dict[str, list[str]]) -> list[str]:
    return [k for k in tags if k != k.upper()]


def preview(root: Path) -> None:
    albums = sorted(d for d in root.iterdir() if d.is_dir())
    for album in albums:
        flacs = sorted(album.glob("*.flac"))
        if not flacs:
            continue
        audio = FLAC(str(flacs[0]))
        tags = load_tags(audio)
        bad = needs_fix(tags)
        print(f"\n{album.name}")
        print(f"  {flacs[0].name}")
        if bad:
            for k in bad:
                print(f"    {k} -> {k.upper()}")
        else:
            print("    (all keys uppercase ✓)")


def backup_and_fix(root: Path, dry_run: bool = True) -> None:
    flacs = list(root.rglob("*.flac"))
    fixed = 0
    skipped = 0
    errors = 0
    for f in flacs:
        try:
            audio = FLAC(str(f))
            tags = load_tags(audio)
            bad = needs_fix(tags)
            if not bad:
                skipped += 1
                continue

            if not dry_run:
                # Save JSON backup
                backup_path = f.with_suffix(f.suffix + ".tags_backup.json")
                backup_path.write_text(
                    json.dumps(tags, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                # Rewrite with uppercase keys
                new_tags: dict[str, list[str]] = {}
                for k, vals in tags.items():
                    uk = k.upper()
                    if uk in new_tags:
                        # merge duplicates (unlikely)
                        new_tags[uk] = list(dict.fromkeys(new_tags[uk] + vals))
                    else:
                        new_tags[uk] = vals
                audio.clear()
                for k, vals in new_tags.items():
                    audio[k] = vals
                audio.save()
            fixed += 1
        except Exception as exc:
            print(f"[ERROR] {f.name}: {exc}")
            errors += 1

    mode = "DRY-RUN" if dry_run else "APPLIED"
    print(f"\n[{mode}] {fixed} files need fixing, {skipped} already ok, {errors} errors")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "preview"
    target = sys.argv[2] if len(sys.argv) > 2 else "//HyperionNAS/Vol2/navidrome/Mork"
    root = Path(target)
    if not root.exists():
        print(f"Path not found: {root}")
        sys.exit(1)
    if cmd == "preview":
        preview(root)
    elif cmd == "dryrun":
        backup_and_fix(root, dry_run=True)
    elif cmd == "apply":
        backup_and_fix(root, dry_run=False)
    else:
        print("Usage: python fix_navidrome_tags.py [preview|dryrun|apply] [PATH]")
