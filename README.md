# SoundAudit

A Python-based music library health scanner and metadata repair tool for FLAC/MP3/M4A/OGG/WAV/APE/WV/AIFF/AAC collections.

> ⚠️ **Beta — work in progress.** Expect rough edges and breaking changes. Back up your library before using the tag writeback features (`fix --apply`, `resolve --auto-write`).

## What It Does

SoundAudit scans your music library, extracts metadata, fingerprints audio, resolves canonical tags via MusicBrainz, detects transcodes and duplicates, then writes corrections back to your files all via terminal UI or scriptable CLI.

**Supported formats:** FLAC, MP3, M4A, OGG, WAVE, APE, WV (WavPack), AIFF, AAC

| Feature | |
|---------|---|
| Parallel incremental directory scanning | ✅ |
| Content-hash duplicate detection | ✅ |
| AcoustID fingerprinting + fuzzy duplicates | ✅ |
| Spectral transcode detection (fake-FLAC) | ✅ |
| MusicBrainz metadata resolver | ✅ |
| Tag writeback with original backup | ✅ |
| Navidrome folder organizer | ✅ |
| Interactive TUI + JSON/CSV/Markdown export | ✅ |

## Quick Start

```bash
# Install from PyPI
pip install soundaudit

# Or install from source with uv
uv pip install -e ".[dev]"

# Launch the interactive TUI
soundaudit tui

# Scan your library
soundaudit scan ~/Music --workers 4

# Re-scan only changed files (near-instant)
soundaudit scan ~/Music

# Resolve MusicBrainz metadata + preview fixes
soundaudit resolve
soundaudit fix --fields artist,album,title,year

# Write corrected tags to files
soundaudit fix --apply

# Reports
soundaudit report                              # summary
soundaudit report --missing-tags               # incomplete metadata
soundaudit report --duplicates                 # duplicate groups with keeper recommendations
soundaudit report --transcodes                 # suspected fake-FLAC
soundaudit report --corrupt                    # unreadable files
soundaudit report --corrupt --delete-corrupt   # delete corrupt files
soundaudit report --duplicates -o dups.json    # export to JSON

# Navidrome Organizer (dry-run by default)
soundaudit organize ~/Downloads --output ~/Music/Navidrome
soundaudit organize --from-db --output ~/Music/Navidrome --apply --move
soundaudit organize ~/Downloads --output ~/Music/Navidrome --apply --copy

# Duplicate Cleanup
soundaudit clean-duplicates                    # preview deletions
soundaudit clean-duplicates --apply             # delete all DELETE-marked files

# Analysis passes (on already-scanned data)
soundaudit analyze --duplicates                # content-hash dups
soundaudit analyze --acoustid                  # fingerprint dups
soundaudit analyze --transcodes --workers 4    # spectral fake-FLAC detection
```

## Requirements

- **Python 3.10+**
- **ffmpeg** — required for spectral transcode detection (`analyze --transcodes`)
- **fpcalc** (chromaprint) — optional, for AcoustID fingerprinting (`analyze --acoustid`)

## Hash Strategies

| Strategy | Speed | Use Case |
|----------|-------|----------|
| `head-only` (default) | Fastest | Daily scans, slow/network storage |
| `head-tail` | Fast | Better collision resistance |
| `full` | Slow | Bit-perfect deduplication |
| `none` | Instant | Tag-only scanning |

## Configuration

Create `config.yaml` in your platform config directory (e.g. `~/.config/soundaudit/config.yaml` on Linux, `~/Library/Application Support/soundaudit/config.yaml` on macOS, `%APPDATA%\soundaudit\config.yaml` on Windows) or pass `--config`:

```yaml
scan:
  paths:
    - ~/Music
  extensions: [".flac", ".mp3", ".m4a", ".ogg", ".wav", ".ape", ".wv", ".aiff", ".aac"]
  workers: 4
  follow_symlinks: false
  hash_strategy: head-only

fingerprinting:
  enabled: false
  fpcalc_path: /usr/bin/fpcalc
  api_key: "your-acoustid-key"

resolvers:
  rate_limit: 1.0
  retry_count: 3

organize:
  output_path: ~/Music/Navidrome
  template: "{album_artist}/{album} [{year}]/{disc_track}. {title}.{format}"
  move: true
  extensions: [".flac", ".mp3", ".m4a", ".ogg", ".wav", ".ape", ".wv", ".aiff", ".aac"]
```

```bash
soundaudit scan --config config.yaml
soundaudit tui --config config.yaml
```

Default database and logs are stored in platform-standard directories (e.g. `~/.local/share/soundaudit/` on Linux).

## Performance

- **Slow storage (network-attached, USB):** Use `--hash-strategy head-only`. Full-file hashing over high-latency storage is dramatically slower.
- **Incremental scans:** Unchanged files are skipped by mtime — subsequent scans are near-instant.
- **Parallel extraction:** Uses `ThreadPoolExecutor`; overhead is hidden by I/O latency on slow storage.

## License

MIT
