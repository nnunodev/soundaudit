# SoundAudit — Development Plan

Working document. **Not versioned.** Updated as we go.

---

## Status

**Phase:** Scaffold → Core implementation  
**Remote:** https://forgejo.voidnode.dev/nuno/soundaudit.git (Forgejo primary, mirrors to GitHub manually)  
**Language:** Python 3.12+, Hatch build, Typer CLI, Rich output, SQLAlchemy 2.1  

---

## Done

### Foundation
- [x] Repo structure: `src/soundaudit/*`, tests/, hatchling build
- [x] `pyproject.toml` — deps, scripts, dev deps (pytest, black, ruff, mypy)
- [x] Pydantic config loader with validation (`config.yaml` support)
- [x] Data models: `FileInfo`, `TrackTags`, `AudioSignature`, completeness scoring
- [x] SQLite store: WAL mode, incremental `upsert_file()`, unique by `(path, mtime)`
- [x] Parallel directory walker (`ThreadPoolExecutor`), filters audio extensions
- [x] Mutagen tag extractor — FLAC, MP3 (ID3), M4A, OGG Vorbis, WAVE, APE
- [x] Typer CLI: `scan`, `report`, `version` commands
- [x] Rich console output: scan progress + report tables (missing tags, corrupt files)
- [x] Dockerfile + docker-compose.yml (mounts NAS Music, outputs to ./data)
- [x] README.md with install, config, usage
- [x] Token-based auth for Forgejo push (no SSH available on container)
- [x] Git remote cleaned: Forgejo only, token embedded in HTTPS URL

### Known working
```bash
pip install -e ".[dev]"
soundaudit scan /mnt/nas2/Music --db ~/.local/share/soundaudit/scan.db
soundaudit report
soundaudit report --missing-tags
soundaudit report --corrupt
```

---

## Next

### Phase 1 — Content-Hash Duplicates ✅ (Done)
- [x] Group files by content_hash in DB, flag `is_duplicate` + `duplicate_group_id`
- [x] CLI: `soundaudit analyze --duplicates`
- [x] CLI: `soundaudit scan --analyze-duplicates` (default on, `--skip-analyze` to disable)
- [x] Report: `soundaudit report --duplicates` with wasted space calculation
- [x] Smart keeper recommendations — scores by: lossless > album > bit depth > sample rate > size > tags
- [x] Per-file verdict: KEEP / DELETE / REVIEW with reasons
- [x] TUI: Duplicates tab with color-coded rows (green=keep, red=delete, yellow=review)
- [x] TUI: Dashboard shows dup group count when present
- [x] TUI: Auto-analyze after scan
- [x] **Export reports to JSON / CSV / Markdown via `--output` / `-o`**
  - JSON: nested groups with full file metadata and verdicts
  - CSV: flat rows, one per file, group_id repeated
  - Markdown: human-readable tables per group with summary
- [x] **TUI Export dialog (press `s`)** — filename input + JSON/CSV/Markdown shortcuts
  - Generates `soundaudit_{tab}_{YYYYMMDD_HHMMSS}.{ext}` default name
  - Exports the currently active tab's data

### Phase 1b — AcoustID Fingerprinting + Fuzzy Duplicates ✅ (Done)
- [x] AcoustID fingerprint via `fpcalc` / `chromaprint` FFI (pyacoustid preferred, fpcalc binary fallback)
- [x] Store `acoustid_fingerprint` in `AudioSignature` table
- [x] `acoustid_groups` DB table + `files.acoustid_group_id`
- [x] Duplicate analyzer: group by acoustid, flag bit-for-bit vs transcode dups
- [x] CLI: `soundaudit duplicates [--delete-prompt|--auto-select-best]`
- [x] CLI: `soundaudit analyze --acoustid`
- [x] CLI: `soundaudit scan --fingerprint` (wired through to extractor)

### Phase 2 — Transcode Detection (High value, high effort)
- [ ] `ffprobe` spectral frequency analysis (cutoff detection)
- [ ] FLAC fake detection: sudden brickwall above 16kHz = likely MP3 source
- [ ] Report: "This FLAC has a 16kHz wall → likely transcoded from 320kbps MP3"
- [ ] Score confidence level (low/medium/high) per file

### Phase 3 — MusicBrainz Resolver (Medium value, high effort)
- [ ] Search by existing ISRC (if present) or acoustid
- [ ] Pull canonical tags: title, artist, album, year, genre, MBID
- [ ] CLI: `soundaudit resolve --auto-write|--dry-run`
- [ ] Rate limiting + retry logic for MB API

### Phase 4 — Tag Updater / Writeback (Medium value, low effort)
- [ ] Write corrected tags back to files via mutagen
- [ ] Preserve existing tags we don't touch (comment, lyrics, etc.)
- [ ] Backup original tags before write (store in DB as JSON)
- [ ] CLI: `soundaudit fix --fields artist,album,year`

### Phase 5 — Reporting + Export (Now part of Phase 1)
- [x] JSON / CSV / Markdown export via `--output` / `-o`
- [ ] HTML report with charts
- [ ] Dedicated `export` command for full-library dumps

---

## Wishlist / Icebox

- [ ] Navidrome API integration — cross-reference library with scan results
- [ ] Tidal/Qobuz/deezer quality check (compare against store metadata)
- [ ] Spectrogram generation for manual transcode inspection
- [ ] Web UI (Starlette/HTMX or SvelteKit + API)
- [ ] Background daemon mode: watch Music folder, auto-scan on file events
- [ ] Plugin system for custom analyzers

---

## Decisions

| Decision | Rationale |
|----------|-----------|
| Forgejo only | GitHub auth blocked in container; Forgejo token works; mirror manually when needed |
| SQLite WAL | Avoids CIFS locking issues; keep DB local on SSD |
| Parallel walker | 4,617 FLAC files → needs speed |
| Mutagen not ffmpeg for tags | Mutagen is fast and non-destructive for reads |
| Hatch not Poetry | Simpler, PEP 621 native, no lock file wars |

---

## Notes

- NAS path: `/mnt/nas2/Music` (CIFS, 4,617 FLAC files, some metadata gaps)
- Download staging: `/mnt/nas2/Albums` (kept outside Music to avoid Navidrome dup scanning)
- Docker image based on `python:3.12-alpine` with ffmpeg

Last updated: 2026-04-22
