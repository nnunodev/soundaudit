# SoundAudit

A Python-based music library health scanner and metadata repair tool for FLAC/MP3/OGG/M4A collections.

## What It Does

SoundAudit scans your music library, extracts metadata, fingerprints audio, resolves canonical tags via MusicBrainz, detects transcodes and duplicates, then writes corrections back to your files — all with a fast terminal UI or scriptable CLI.

### Capabilities

| Feature | Status |
|---------|--------|
| Parallel directory scanning with incremental re-scan | ✅ |
| Content hashing (head-only / head-tail / full / none) | ✅ |
| Tag extraction (FLAC, MP3, M4A, OGG, WAVE, APE) | ✅ |
| **Content-hash duplicate detection** | ✅ |
| **AcoustID fingerprinting + fuzzy duplicates** | ✅ |
| **Spectral transcode detection (fake-FLAC)** | ✅ |
| **MusicBrainz metadata resolver** | ✅ |
| **Tag writeback with backup** | ✅ |
| **TUI with live progress + interactive reports** | ✅ |
| JSON / CSV / Markdown export | ✅ |

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Launch the interactive TUI (recommended)
soundaudit tui

# --- CLI ---

# Scan your library (head-only hash = fastest for large/network libraries)
soundaudit scan ~/Music --db ~/.local/share/soundaudit/scan.db --workers 4

# Re-scan only changed files (near-instant if nothing changed)
soundaudit scan ~/Music --db ~/.local/share/soundaudit/scan.db

# Resolve MusicBrainz metadata for missing tags
soundaudit resolve --db ~/.local/share/soundaudit/scan.db

# Preview tag fixes (dry-run by default)
soundaudit fix --db ~/.local/share/soundaudit/scan.db --fields artist,album,title,year

# Actually write corrected tags to files
soundaudit fix --apply --db ~/.local/share/soundaudit/scan.db

# Resolve + write in one shot
soundaudit resolve --auto-write --db ~/.local/share/soundaudit/scan.db

# Reports
soundaudit report                              # summary
soundaudit report --missing-tags               # files missing title/artist/album
soundaudit report --duplicates                 # duplicate groups with keeper recommendations
soundaudit report --transcodes                 # suspected transcode files
soundaudit report --corrupt                    # unreadable files
soundaudit report --duplicates -o dups.json  # export to JSON

# Analysis passes (on already-scanned data)
soundaudit analyze --duplicates                # content-hash dups
soundaudit analyze --acoustid                  # fingerprint dups
soundaudit analyze --transcodes --workers 4    # spectral fake-FLAC detection
```

## TUI Navigation

| Screen | Key | What you can do |
|--------|-----|---------------|
| **Dashboard** | `s` | Scan — choose paths, pick scan preset (Quick/Standard/Deep), start |
| **Dashboard** | `r` | Reports — 6 tabs: Summary, Missing Tags, Tag Status, Duplicates, AcoustID, Transcodes, Corrupt |
| **Dashboard** | `f` | Fix Tags — select paths, pick preset (Core/Full/All), toggle dry-run, run |
| **Dashboard** | `R` | Reset database |
| **Reports** | `←→` or `hj` | Switch tabs |
| **Reports** | `s` | Export current tab to JSON/CSV/Markdown |
| **Any screen** | `q` | Quit |
| **Any screen** | `esc` | Back |

### Scan Presets

| Preset | What runs after scan |
|--------|----------------------|
| **Quick** | Content-hash duplicate analysis only |
| **Standard** | + MusicBrainz metadata resolution |
| **Deep** | + AcoustID fingerprinting + transcode detection |

### Fix Tags Presets

| Preset | Fields written |
|--------|---------------|
| **Core** | title, artist, album, year |
| **Full** | + album artist, track number/total, disc number/total |
| **All** | + genre, ISRC |

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
  api_key: "your-acoustid-key"

resolvers:
  rate_limit: 1.0
  retry_count: 3
```

Load it:
```bash
soundaudit scan --config config.yaml
soundaudit tui --config config.yaml
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
| `analyzer` | Detect duplicates (content hash + AcoustID), transcodes, corruption |
| `resolver` | MusicBrainz API lookups by ISRC / AcoustID / artist+title |
| `actuator` | Write tags back to files (mutagen), backup originals to DB |
| `reporter` | Console tables, JSON/CSV/Markdown export |
| `tui` | Interactive terminal UI with live scan progress, reports, tag fixer |

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

See [DEV_PLAN.md](DEV_PLAN.md) for the full phased development plan. Phase 4 (tag writeback) is complete. Phase 5 is reporting enhancements and HTML export.

## License

MIT
