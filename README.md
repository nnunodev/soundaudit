# SoundAudit

A Python-based music library health scanner and metadata repair tool for FLAC/MP3 collections.

## What It Does

SoundAudit scans your local or network-mounted music library, extracts metadata with Mutagen, stores results in a local SQLite database, and reports what needs attention.

### Current Capabilities
- Parallel directory scanning with incremental re-scan (skips unchanged files by mtime)
- Configurable content hashing: `head-only`, `head-tail`, `full`, or `none`
- Tag extraction for FLAC, MP3, M4A, OGG, WAVE, APE
- SQLite WAL persistence with incremental upserts
- Rich CLI reports: summary, missing tags, corrupt files
- Speed-optimized for slow storage (network file systems, USB drives)

### Planned Features
See [DEV_PLAN.md](DEV_PLAN.md) for the phased development plan.
- Duplicate detection via AcoustID / content hash
- Transcode detection via spectral analysis
- MusicBrainz metadata resolver
- Tag writeback and file rename actuator
- HTML / JSON / CSV export

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Scan your library (head-only hash = fastest for large/network libraries)
soundaudit scan ~/Music --db ~/.local/share/soundaudit/scan.db --workers 4

# Re-scan only changed files (near-instant if nothing changed)
soundaudit scan ~/Music --db ~/.local/share/soundaudit/scan.db

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
| `head-only` (default) | Fastest | Slow storage, daily scans, change detection |
| `head-tail` | Fast | Better collision resistance than head-only |
| `full` | Slow | Bit-perfect deduplication |
| `none` | Instant | Tag-only scanning |

```bash
# Daily use — fast, good enough for finding most duplicates
soundaudit scan ~/Music --hash-strategy head-only

# When you need certainty (e.g. before deleting suspected duplicates)
soundaudit scan ~/Music --hash-strategy full
```

## Configuration

Create `config.yaml`:

```yaml
scan:
  paths:
    - ~/Music
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

Mounts your Music folder and persists the database locally.

## Architecture

| Module | Purpose |
|--------|---------|
| `scanner` | Walk directories, read tags, compute hashes |
| `analyzer` | Detect duplicates, corruption, transcodes |
| `resolver` | MusicBrainz API lookups |
| `reporter` | Console tables and HTML reports |
| `actuator` | Apply fixes (tags, rename, dedupe) |

## Performance Notes

- **Slow storage (network-attached, USB):** Use `--hash-strategy head-only`. Full-file hashing over high-latency storage is dramatically slower.
- **Incremental scans:** Unchanged files are skipped by mtime — subsequent scans are near-instant.
- **Parallel extraction:** Uses `ThreadPoolExecutor`; overhead is hidden by I/O latency on slow storage.

## Real-World Results

Scanned a ~6,000-file library (96.7% FLAC, 2.6% MP3, 0.7% M4A):

| Metric | Count |
|--------|-------|
| Total files | ~6,000 |
| FLAC | ~5,700 |
| MP3 | ~150 |
| M4A | ~40 |
| Corrupt | 0 |
| Missing title | 6 |

Scan time: **~4.5 minutes** with `head-only` hash, 4 workers, over a network-mounted volume.

## Roadmap

See [DEV_PLAN.md](DEV_PLAN.md) for the full phased development plan.

## License

MIT
