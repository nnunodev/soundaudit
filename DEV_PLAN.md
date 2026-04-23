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

### Phase 1 — Fingerprinting + Duplicates (High value, medium effort)
- [ ] AcoustID fingerprint via `fpcalc` / `chromaprint` FFI
- [ ] Store `acoustid` in `AudioSignature` table
- [ ] Duplicate analyzer: group by acoustid, flag bit-for-bit vs transcode dups
- [ ] CLI: `soundaudit duplicates [--delete-prompt|--auto-select-best]`

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

### Phase 5 — Reporting + Export (Low effort, nice to have)
- [ ] HTML report with charts (missing tags pie chart, duplicates table, etc.)
- [ ] JSON export for CI / external tools
- [ ] CSV export for spreadsheet warriors
- [ ] CLI: `soundaudit export --format html|json|csv`

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
