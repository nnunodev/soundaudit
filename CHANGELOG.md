# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-04-30

### Added
- Parallel directory scanning with incremental re-scan (mtime-based skip)
- Content hashing: `head-only` (default), `head-tail`, `full`, `none`
- Tag extraction for FLAC, MP3, M4A, OGG, WAVE, APE via mutagen
- Content-hash duplicate detection with smart keeper recommendations
- AcoustID fingerprinting + fuzzy duplicate detection (pyacoustid)
- Spectral transcode detection (fake-FLAC) via ffmpeg
- MusicBrainz metadata resolver with rate limiting and retry logic
- Tag writeback with original tag backup stored in database
- Interactive TUI (Textual) with live progress, reports, and tag fixer
- JSON / CSV / Markdown report export
- Cross-platform config and data directories (Windows, macOS, Linux)
- Global `--verbose` / `--quiet` flags with structured logging
- Graceful shutdown on SIGINT (finishes current file, saves partial results)
- CI pipeline (GitHub Actions) for lint, type-check, and test across platforms

### Removed
- Docker support (not appropriate for a CLI/TUI tool)
- Unused dependencies: `jinja2`, `pillow`, `pydantic-yaml`
