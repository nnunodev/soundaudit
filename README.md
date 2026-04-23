# SoundAudit

A Python-based music library health scanner and metadata repair tool for FLAC/MP3 collections.

## Features

| Feature | Status |
|---------|--------|
| Parallel directory scanning with incremental re-scan | ✅ |
| Configurable content hashing (head-only / head-tail / full / none) | ✅ |
| Mutagen-based tag extraction (FLAC, MP3, M4A, OGG, WAVE, APE) | ✅ |
| SQLite WAL persistence with incremental upserts | ✅ |
| Rich CLI reports (summary, missing tags, corrupt files) | ✅ |
| Duplicate detection | 🔄 Phase 1 |
| Transcode detection (spectral analysis) | 📋 Phase 2 |
| MusicBrainz metadata resolver | 📋 Phase 3 |
| Tag writeback / rename actuator | 📋 Phase 4 |
| HTML/JSON/CSV export | 📋 Phase 5 |

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Scan your library (head-only hash = fast over CIFS/NAS)
soundaudit scan /mnt/nas2/Music --db ~/.local/share/soundaudit/scan.db --workers 4

# Re-scan only changed files (instant if nothing changed)
soundaudit scan /mnt/nas2/Music --db ~/.local/share/soundaudit/scan.db

# View summary
soundaudit report

# View missing tags
soundaudit report --missing-tags

# View corrupt/unreadable files
soundaudit report --corrupt
```

## Hash Strategies

| Strategy | Speed | Use Case |
|----------|-------|----------|
| `head-only` (default) | Fastest | CIFS/NAS scanning, change detection |
| `head-tail` | Fast | Better collision resistance |
| `full` | Slow | Bit-perfect deduplication |
| `none` | Instant | Tag-only scanning |

```bash
# Fast scan for daily use
soundaudit scan /mnt/nas2/Music --hash-strategy head-only

# Full hash when you suspect duplicates
soundaudit scan /mnt/nas2/Music --hash-strategy full
```

## Configuration

Create `config.yaml`:

```yaml
scan:
  paths:
    - /mnt/nas2/Music
  extensions: [".flac", ".mp3", ".m4a"]
  workers: 4
  hash_strategy: head-only

database:
  path: ~/.local/share/soundaudit/scan.db

fingerprinting:
  enabled: false
  fpcalc_path: /usr/bin/fpcalc
```

Load it:
```bash
soundaudit scan --config config.yaml
```

## Docker

```bash
docker compose up --build
```

Mounts your NAS Music folder and persists the database locally.

## Architecture

| Module | Purpose |
|--------|---------|
| `scanner` | Walk directories, read tags, compute hashes |
| `analyzer` | Detect duplicates, corruption, transcodes |
| `resolver` | MusicBrainz API lookups |
| `reporter` | Console tables and HTML reports |
| `actuator` | Apply fixes (tags, rename, dedupe) |

## Performance Notes

- **Network file systems (CIFS/NFS):** Use `--hash-strategy head-only`. Full-file hashing over CIFS is ~30x slower due to per-read latency.
- **Incremental scans:** Unchanged files are skipped by mtime comparison — subsequent scans are near-instant.
- **Parallel extraction:** Uses `ThreadPoolExecutor`; overhead is negligible on CIFS since network latency dominates.

## Real-World Results

Scanned a 5,886-file library (96.7% FLAC, 2.6% MP3, 0.7% M4A):

| Metric | Count |
|--------|-------|
| Total files | 5,886 |
| FLAC | 5,694 |
| MP3 | 151 |
| M4A | 41 |
| Corrupt | 0 |
| Missing title | 6 |

Scan time: **~4.5 minutes** with `head-only` hash, 4 workers, over CIFS.

## Roadmap

See [DEV_PLAN.md](DEV_PLAN.md) for the full phased plan.

## License

MIT
