# SoundAudit

A Python-based music library health scanner and metadata repair tool for FLAC/MP3 collections.

## What It Does

- **Scan** your music library for metadata quality, corruption, duplicates, and transcodes
- **Report** a clean overview of what needs attention
- **Repair** tags, rename files, and deduplicate — all with dry-run safety

## Quick Start

```bash
# Install locally
pip install -e ".[dev]"

# Scan your library
soundaudit scan /mnt/nas2/Music --db ~/.local/share/soundaudit/scan.db

# View report
soundaudit report --missing-tags
soundaudit report --duplicates
soundaudit report --corrupt

# Fix tags (dry-run first)
soundaudit fix-tags --dry-run

# Apply fixes
soundaudit fix-tags --apply
```

## Docker

```bash
docker compose up --build
```

## Architecture

| Module | Purpose |
|--------|---------|
| `scanner` | Walk directories, read tags, compute hashes |
| `analyzer` | Detect duplicates, corruption, transcodes |
| `resolver` | MusicBrainz API lookups |
| `reporter` | Console tables and HTML reports |
| `actuator` | Apply fixes (tags, rename, dedupe) |

## License

MIT
