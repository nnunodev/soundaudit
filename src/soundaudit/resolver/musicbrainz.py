"""MusicBrainz metadata resolver.

Searches MusicBrainz web service by ISRC, AcoustID fingerprint, or
artist+title fallback.  Stores canonical tags back into the database.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from rich.console import Console

from soundaudit._version import __version__


@dataclass
class ResolvedMetadata:
    """Canonical metadata returned by MusicBrainz."""

    mb_recording_id: str | None = None
    mb_release_id: str | None = None
    mb_track_id: str | None = None
    score: float = 0.0
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    year: int | None = None
    genre: str | None = None


class MusicBrainzClient:
    """Low-rate-limited MusicBrainz web-service client."""

    BASE_URL = "https://musicbrainz.org/ws/2"

    def __init__(
        self,
        rate_limit: float = 1.0,
        retry_count: int = 3,
        console: Console | None = None,
    ) -> None:
        self.rate_limit = rate_limit
        self.retry_count = retry_count
        self.console = console or Console()
        self._session = requests.Session()
        self._session.headers["User-Agent"] = (
            f"SoundAudit/{__version__} ( https://github.com/nnunodev/soundaudit )"
        )
        self._session.headers["Accept"] = "application/json"
        self._last_request = 0.0
        self._isrc_cache: dict[str, ResolvedMetadata] = {}
        self._mbid_cache: dict[str, ResolvedMetadata] = {}
        self._query_cache: dict[str, ResolvedMetadata] = {}

    def _get(self, url: str, params: dict | None = None) -> dict | None:
        """GET with rate-limiting and retry/back-off."""
        for attempt in range(self.retry_count + 1):
            now = time.monotonic()
            elapsed = now - self._last_request
            if elapsed < self.rate_limit:
                time.sleep(self.rate_limit - elapsed)
            try:
                resp = self._session.get(url, params=params, timeout=30)
                self._last_request = time.monotonic()
                if resp.status_code == 503 and attempt < self.retry_count:
                    backoff = 2 ** attempt
                    if self.console:
                        self.console.print(
                            f"[dim]MusicBrainz 503, retrying in {backoff}s…[/dim]"
                        )
                    time.sleep(backoff)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                if attempt < self.retry_count:
                    backoff = 2 ** attempt
                    if self.console:
                        self.console.print(
                            f"[dim]MusicBrainz error ({exc}), retrying in {backoff}s…[/dim]"
                        )
                    time.sleep(backoff)
                    continue
                if self.console:
                    self.console.print(f"[red]MusicBrainz request failed: {exc}[/red]")
                return None
        return None

    def search_by_isrc(self, isrc: str) -> ResolvedMetadata | None:
        """Search recordings by ISRC code."""
        if not isrc:
            return None
        cached = self._isrc_cache.get(isrc)
        if cached:
            return cached
        data = self._get(
            f"{self.BASE_URL}/recording",
            params={"query": f"isrc:{isrc}", "fmt": "json", "limit": 5},
        )
        if not data or not data.get("recordings"):
            return None
        recordings = sorted(
            data["recordings"],
            key=lambda r: r.get("score", 0),
            reverse=True,
        )
        recording = recordings[0]
        md = self._parse_recording(recording, score=recording.get("score", 0) / 100.0)
        self._isrc_cache[isrc] = md
        return md

    def search_by_recording_mbid(self, mbid: str) -> ResolvedMetadata | None:
        """Lookup a recording by its MBID (with releases + tags)."""
        if not mbid:
            return None
        cached = self._mbid_cache.get(mbid)
        if cached:
            return cached
        data = self._get(
            f"{self.BASE_URL}/recording/{mbid}",
            params={
                "inc": "releases+artist-credits+tags+release-groups",
                "fmt": "json",
            },
        )
        if not data:
            return None
        md = self._parse_recording(data, score=1.0)
        self._mbid_cache[mbid] = md
        return md

    def search_by_artist_title(
        self,
        artist: str,
        title: str,
        album: str | None = None,
    ) -> ResolvedMetadata | None:
        """Free-text search by artist and recording title."""
        if not artist or not title:
            return None
        artist = artist.replace('"', '\\"')
        title = title.replace('"', '\\"')
        query = f'artist:"{artist}" AND recording:"{title}"'
        if album:
            query += f' AND release:"{album.replace(chr(34), chr(92)+chr(34))}"'
        cache_key = query
        cached = self._query_cache.get(cache_key)
        if cached:
            return cached
        data = self._get(
            f"{self.BASE_URL}/recording",
            params={"query": query, "fmt": "json", "limit": 5},
        )
        if not data or not data.get("recordings"):
            return None
        recordings = sorted(
            data["recordings"],
            key=lambda r: r.get("score", 0),
            reverse=True,
        )
        recording = recordings[0]
        md = self._parse_recording(recording, score=recording.get("score", 0) / 100.0)
        self._query_cache[cache_key] = md
        return md

    def _parse_recording(self, data: dict, score: float) -> ResolvedMetadata:
        md = ResolvedMetadata(
            mb_recording_id=data.get("id"),
            score=score,
            title=data.get("title"),
        )
        artist_credits = data.get("artist-credit", [])
        if artist_credits:
            md.artist = self._join_artists(artist_credits)

        releases = data.get("releases", [])
        if releases:
            release = releases[0]
            md.mb_release_id = release.get("id")
            md.album = release.get("title")
            rel_ac = release.get("artist-credit", [])
            if rel_ac:
                md.album_artist = self._join_artists(rel_ac)
            date_str = release.get("date") or ""
            if date_str and len(date_str) >= 4:
                with contextlib.suppress(ValueError):
                    md.year = int(date_str[:4])
            # Try to grab a track id from the first medium
            track_list = release.get("media", [])
            if track_list:
                tracks = track_list[0].get("tracks", [])
                if tracks:
                    md.mb_track_id = tracks[0].get("id")

        if not md.year:
            rgs = data.get("release-groups", [])
            if rgs:
                rg_date = rgs[0].get("first-release-date") or ""
                if len(rg_date) >= 4:
                    with contextlib.suppress(ValueError):
                        md.year = int(rg_date[:4])

        tags = data.get("tags", [])
        if tags:
            sorted_tags = sorted(
                tags, key=lambda t: t.get("count", 0), reverse=True
            )
            md.genre = sorted_tags[0].get("name")

        return md

    @staticmethod
    def _join_artists(artist_credits: list[dict]) -> str:
        parts: list[str] = []
        for ac in artist_credits:
            name = ac.get("name", "")
            if name:
                parts.append(name)
            join_phrase = ac.get("joinphrase", "")
            # avoid double spacing when joinphrase already contains spaces
            if join_phrase and parts and (
                not parts[-1].endswith(join_phrase.strip()[-1] if join_phrase.strip() else "")
                or join_phrase and not parts[-1].endswith(join_phrase)
            ):
                parts.append(join_phrase)
        joined = "".join(parts).strip()
        return joined


class AcoustidLookupClient:
    """Map AcoustID fingerprints to MusicBrainz recording IDs."""

    def __init__(
        self,
        api_key: str,
        console: Console | None = None,
    ) -> None:
        self.api_key = api_key
        self.console = console or Console()
        self._last_request = 0.0
        self._rate_limit = 0.5  # polite minimum for AcoustID API
        self._cache: dict[tuple[str, int], tuple[str | None, float]] = {}

    def lookup(self, fingerprint: str, duration_ms: int) -> tuple[str | None, float]:
        """Return (recording_mbid, score) or (None, 0.0)."""
        if not self.api_key or not fingerprint or not duration_ms:
            return None, 0.0
        cache_key = (fingerprint, duration_ms)
        if cache_key in self._cache:
            return self._cache[cache_key]

        duration_sec = duration_ms // 1000
        mbid: str | None = None
        score = 0.0

        # Try pyacoustid first (same optional dep used for fingerprinting)
        try:
            import acoustid  # type: ignore[import-untyped]

            now = time.monotonic()
            elapsed = now - self._last_request
            if elapsed < self._rate_limit:
                time.sleep(self._rate_limit - elapsed)
            results = acoustid.lookup(
                self.api_key, fingerprint, duration_sec, meta="recordings"
            )
            self._last_request = time.monotonic()
            mbid, score = self._extract_best_mbid(results)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            if self.console:
                self.console.print(f"[dim]AcoustID lookup failed: {exc}[/dim]")

        if not mbid:
            mbid, score = self._lookup_http(fingerprint, duration_sec)

        self._cache[cache_key] = (mbid, score)
        return mbid, score

    def _lookup_http(self, fingerprint: str, duration_sec: int) -> tuple[str | None, float]:
        now = time.monotonic()
        elapsed = now - self._last_request
        if elapsed < self._rate_limit:
            time.sleep(self._rate_limit - elapsed)
        try:
            resp = requests.get(
                "https://api.acoustid.org/v2/lookup",
                params={
                    "client": self.api_key,
                    "fingerprint": fingerprint,
                    "duration": duration_sec,
                    "meta": "recordings",
                    "format": "json",
                },
                timeout=30,
            )
            self._last_request = time.monotonic()
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "ok":
                return None, 0.0
            return self._extract_best_mbid(data.get("results", []))
        except requests.RequestException as exc:
            if self.console:
                self.console.print(f"[dim]AcoustID HTTP lookup failed: {exc}[/dim]")
            return None, 0.0

    @staticmethod
    def _extract_best_mbid(results: list[dict]) -> tuple[str | None, float]:
        best_mbid: str | None = None
        best_score = 0.0
        for result in results:
            score = result.get("score", 0.0)
            recordings = result.get("recordings", [])
            for rec in recordings:
                mbid = rec.get("id")
                if mbid and score > best_score:
                    best_score = score
                    best_mbid = mbid
        return best_mbid, best_score


class MusicBrainzResolver:
    """High-level resolver that drives the DB → MusicBrainz pipeline."""

    def __init__(
        self,
        database,
        mb_config,
        fingerprint_config,
        console: Console | None = None,
    ) -> None:
        self.database = database
        self.mb_client = MusicBrainzClient(
            rate_limit=mb_config.rate_limit,
            retry_count=mb_config.retry_count,
            console=console,
        )
        self.acoustid_client = AcoustidLookupClient(
            api_key=fingerprint_config.api_key,
            console=console,
        )
        self.console = console or Console()

    def resolve_file(
        self,
        db_file,
        *,
        progress_callback: object | None = None,
    ) -> ResolvedMetadata | None:
        """Resolve a single DBFile using ISRC → AcoustID → artist+title fallback."""
        # 1) ISRC → highest confidence
        if db_file.isrc:
            md = self.mb_client.search_by_isrc(db_file.isrc)
            if md:
                if progress_callback:
                    progress_callback(
                        f"Resolved via ISRC: {Path(db_file.path).name}"
                    )
                elif self.console:
                    self.console.print(
                        f"[green]Resolved via ISRC[/green]  {db_file.path}"
                    )
                return md

        # 2) AcoustID fingerprint → MBID
        if db_file.acoustid_fingerprint and db_file.acoustid_duration_ms:
            mbid, score = self.acoustid_client.lookup(
                db_file.acoustid_fingerprint,
                db_file.acoustid_duration_ms,
            )
            if mbid:
                md = self.mb_client.search_by_recording_mbid(mbid)
                if md:
                    md.score = max(md.score, score)
                    if progress_callback:
                        progress_callback(
                            f"Resolved via AcoustID: {Path(db_file.path).name}"
                        )
                    elif self.console:
                        self.console.print(
                            f"[green]Resolved via AcoustID[/green] {db_file.path}"
                        )
                    return md

        # 3) Fallback to existing tags
        if db_file.artist and db_file.title:
            md = self.mb_client.search_by_artist_title(
                db_file.artist,
                db_file.title,
                album=db_file.album,
            )
            if md:
                if progress_callback:
                    progress_callback(
                        f"Resolved via query: {Path(db_file.path).name}"
                    )
                elif self.console:
                    self.console.print(
                        f"[yellow]Resolved via query[/yellow]  {db_file.path}"
                    )
                return md

        if progress_callback:
            progress_callback(f"Unresolved: {Path(db_file.path).name}")
        elif self.console:
            self.console.print(f"[dim]Unresolved[/dim]          {db_file.path}")
        return None

    def resolve_library(
        self,
        *,
        dry_run: bool = False,
        force: bool = False,
        workers: int = 1,
        progress_callback: object | None = None,
    ) -> list[tuple[int, ResolvedMetadata]]:
        """Batch-resolve all (or unresolved) files in the database."""
        from soundaudit.db.store import DBFile

        with self.database.session() as s:
            query = s.query(DBFile)
            if not force:
                # Only files missing MusicBrainz data OR missing basic tags
                query = query.filter(
                    (DBFile.mb_recording_id.is_(None))
                    | (DBFile.title.is_(None))
                    | (DBFile.artist.is_(None))
                )
            files = query.all()
            # Prevent DetachedInstanceError after session closes
            s.expunge_all()

        if not files:
            if progress_callback:
                progress_callback("No files needing metadata found.")
            elif self.console:
                self.console.print("[green]No files needing metadata found.[/green]")
            return []

        total = len(files)
        if progress_callback:
            progress_callback(f"Resolving {total} file(s) via MusicBrainz…")
        elif self.console:
            self.console.print(
                f"[cyan]Resolving {total} file(s) via MusicBrainz…[/cyan]"
            )

        results: list[tuple[int, ResolvedMetadata]] = []
        resolved_count = 0

        # Sequential – rate limits make parallelism pointless for a single IP.
        for idx, db_file in enumerate(files, 1):
            file_name = Path(db_file.path).name
            # Update the yellow label with current file (no log spam)
            if progress_callback:
                progress_callback(file_name)
            md = self.resolve_file(db_file, progress_callback=None)
            if md:
                results.append((db_file.id, md))
                resolved_count += 1
                if not dry_run:
                    self._save_resolution(db_file.id, md)
            # Only log failures and periodic summaries to keep the log clean
            if progress_callback:
                if md is None:
                    progress_callback(f"  ✗ {file_name}")
                if idx % 10 == 0 or idx == total:
                    progress_callback(
                        f"  {idx}/{total}  ({resolved_count} resolved)"
                    )

        if progress_callback:
            progress_callback(
                f"Done. {resolved_count}/{total} resolved."
            )
        elif self.console:
            self.console.print(
                f"[green]Resolved {resolved_count}/{total} files.[/green]"
            )
        return results

    def _save_resolution(self, file_id: int, md: ResolvedMetadata) -> None:
        from soundaudit.db.store import DBFile

        with self.database.session() as s:
            row = s.query(DBFile).filter_by(id=file_id).first()
            if row:
                row.mb_recording_id = md.mb_recording_id
                row.mb_release_id = md.mb_release_id
                row.mb_track_id = md.mb_track_id
                row.mb_score = md.score
                row.mb_match_date = datetime.now(timezone.utc)
                row.mb_title = md.title
                row.mb_artist = md.artist
                row.mb_album = md.album
                row.mb_album_artist = md.album_artist
                row.mb_year = md.year
                row.mb_genre = md.genre
                s.commit()
