# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] - Unreleased

### Added
- **Ignore duplicate groups from the TUI.** Press `i` on a duplicate group (Duplicates or AcoustID Dups tab) to mark it ignored — both files are kept and the group is hidden from reports. Ignored status survives re-analysis because the analyzer preserves the flag for unchanged hashes/fingerprints.
- **Delete duplicate groups from the TUI.** Press `d` on a duplicate group to delete files marked `DELETE` in that group, with a confirmation modal showing exactly which files will be removed.
- **Delete all marked duplicates from the TUI.** Press `D` (shift+d) on the Duplicates or AcoustID Dups tab to bulk-delete every file marked `DELETE` across all non-ignored groups, with a preview modal showing total count and space freed.
- **CLI command `clean-duplicates`.** Run `soundaudit clean-duplicates` for a dry-run preview, or add `--apply` to actually delete all `DELETE`-marked files across content-hash and AcoustID groups.
- Navidrome folder organizer (`organize` CLI command + TUI screen). Reads tags and restructures files into `Album Artist/Album [Year]/disc-track. Title.ext` with collision-safe naming, move or copy mode, and automatic DB path sync when using `--from-db`.
- Expanded default audio format support: `.wv` (WavPack), `.aiff`, `.aac` — wired in scanner, extractor, and organizer.
- `report` command now accepts `--delete-corrupt` to bulk-remove unreadable files from disk and database (mirrors TUI delete-corrupt feature).
- Global `--version` / `-V` flag and `version` subcommand.
- `follow_symlinks` scan option (default `false`) for libraries using symlinked folders.
- `organize` config section (`output_path`, `template`, `move`, `extensions`) with full example in `config.example.yaml`.
- Track/disc number parsing improved for FLAC (`TRACKNUMBER`), APE (`Track`), and M4A (`trkn`/`disk` tuples) fallback in `organizer._get_tags()`.
- Dashboard stats now show counts of ignored duplicate and AcoustID groups.

### Changed
- Removed Header top bar from all TUI screens to reclaim vertical space.
- Removed fingerprint toggle from TUI scan screen (use `--fingerprint` CLI flag instead).
- TUI scan progress bar resets and uses a green arrow when complete; labels properly escape Rich markup.

### Fixed
- TUI scan progress bar no longer gets stuck at 100% during long-running analysis phases.
  - MusicBrainz resolve now drives the progress bar per-track via `on_total_known` / `per_item_callback` callbacks, resetting the bar to 0/N when resolution starts.
  - Spectral transcode analysis (ffmpeg) now drives the progress bar per-file with the same callback pattern, and the label switches to "Transcodes N" during the phase.
  - Labels update dynamically: "Scanning" → "Resolving 5912" / "Transcodes 3400" → "Scanning".

### Changed
- `MusicBrainzResolver.resolve_library()` and `analyze_library_transcodes()` now accept `on_total_known` and `per_item_callback` hooks for granular progress reporting.

## [0.1.1] - 2026-04-30

### Fixed
- CI type-check step (`mypy`) cleaned up, all source files pass cleanly.
- TUI keyboard navigation edge cases on FixTagsRunScreen.

### Changed
- Switched CI and local development to `uv` for faster installs and builds.

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
