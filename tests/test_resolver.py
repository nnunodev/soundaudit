"""Tests for MusicBrainz resolver."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from soundaudit.db.store import DBFile, Database
from soundaudit.resolver.musicbrainz import (
    AcoustidLookupClient,
    MusicBrainzClient,
    MusicBrainzResolver,
    ResolvedMetadata,
)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.engine.dispose()


class TestMusicBrainzClient:
    def test_search_by_isrc_returns_metadata(self) -> None:
        client = MusicBrainzClient(rate_limit=0)
        mock_data = {
            "recordings": [
                {
                    "id": "rec-1",
                    "title": "Test Song",
                    "artist-credit": [
                        {"name": "Test Artist", "joinphrase": ""}
                    ],
                    "releases": [
                        {"id": "rel-1", "title": "Test Album", "date": "2020"}
                    ],
                    "score": 100,
                }
            ]
        }
        with patch.object(client, "_get", return_value=mock_data):
            md = client.search_by_isrc("USABC1234567")

        assert md is not None
        assert md.mb_recording_id == "rec-1"
        assert md.title == "Test Song"
        assert md.artist == "Test Artist"
        assert md.album == "Test Album"
        assert md.year == 2020
        assert md.score == 1.0

    def test_search_by_isrc_no_results(self) -> None:
        client = MusicBrainzClient(rate_limit=0)
        with patch.object(
            client, "_get", return_value={"recordings": []}
        ):
            assert client.search_by_isrc("NONE") is None

    def test_search_by_isrc_uses_cache(self) -> None:
        client = MusicBrainzClient(rate_limit=0)
        md = ResolvedMetadata(mb_recording_id="cached")
        client._isrc_cache["US123"] = md
        with patch.object(client, "_get") as mock_get:
            result = client.search_by_isrc("US123")
        assert result is md
        mock_get.assert_not_called()

    def test_search_by_recording_mbid(self) -> None:
        client = MusicBrainzClient(rate_limit=0)
        mock_data = {
            "id": "rec-1",
            "title": "Test Song",
            "artist-credit": [
                {"name": "Artist", "joinphrase": ""}
            ],
            "releases": [
                {
                    "id": "rel-1",
                    "title": "Album",
                    "date": "2019-05-01",
                    "artist-credit": [
                        {"name": "Album Artist", "joinphrase": ""}
                    ],
                    "media": [
                        {"tracks": [{"id": "track-1"}]}
                    ],
                }
            ],
            "tags": [{"name": "rock", "count": 10}],
        }
        with patch.object(client, "_get", return_value=mock_data):
            md = client.search_by_recording_mbid("rec-1")

        assert md is not None
        assert md.mb_track_id == "track-1"
        assert md.genre == "rock"
        assert md.year == 2019
        assert md.album_artist == "Album Artist"

    def test_rate_limit_enforced(self) -> None:
        import time

        client = MusicBrainzClient(rate_limit=0.2)
        with patch.object(
            client._session,
            "get",
            return_value=MagicMock(
                status_code=200,
                json=MagicMock(return_value={"recordings": []}),
                raise_for_status=MagicMock(),
            ),
        ):
            t0 = time.monotonic()
            client.search_by_isrc("A")
            client.search_by_isrc("B")
            elapsed = time.monotonic() - t0
            assert elapsed >= 0.15

    def test_retry_on_503(self) -> None:
        client = MusicBrainzClient(rate_limit=0, retry_count=2)
        responses = [
            MagicMock(
                status_code=503,
                raise_for_status=MagicMock(side_effect=Exception("503")),
            ),
            MagicMock(
                status_code=200,
                json=MagicMock(return_value={"recordings": []}),
                raise_for_status=MagicMock(),
            ),
        ]
        with patch.object(
            client._session, "get", side_effect=responses
        ) as mock_get:
            md = client.search_by_isrc("X")
        assert md is None
        assert mock_get.call_count == 2

    def test_join_artists_empty(self) -> None:
        assert MusicBrainzClient._join_artists([]) == ""

    def test_join_artists_with_joinphrase(self) -> None:
        ac = [
            {"name": "A", "joinphrase": " & "},
            {"name": "B", "joinphrase": ""},
        ]
        assert MusicBrainzClient._join_artists(ac) == "A & B"


class TestAcoustidLookupClient:
    def test_lookup_finds_mbid_via_http(self) -> None:
        client = AcoustidLookupClient(api_key="test-key")
        with patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = {
                "status": "ok",
                "results": [
                    {
                        "score": 0.95,
                        "recordings": [{"id": "rec-abc"}],
                    }
                ],
            }
            mock_get.return_value.raise_for_status = MagicMock()
            mbid, score = client.lookup("fingerprint", 180_000)
        assert mbid == "rec-abc"
        assert score == 0.95

    def test_lookup_uses_cache_no_http(self) -> None:
        client = AcoustidLookupClient(api_key="k")
        client._cache[("fp", 120_000)] = ("rec-x", 0.8)
        with patch("requests.get") as mock_get:
            mbid, score = client.lookup("fp", 120_000)
        assert mbid == "rec-x"
        assert score == 0.8
        mock_get.assert_not_called()

    def test_lookup_no_api_key(self) -> None:
        client = AcoustidLookupClient(api_key="")
        assert client.lookup("fp", 100_000) == (None, 0.0)

    def test_lookup_http_fallback(self) -> None:
        client = AcoustidLookupClient(api_key="test-key")
        with patch.dict("sys.modules", {"acoustid": None}):
            with patch("requests.get") as mock_get:
                mock_get.return_value.json.return_value = {
                    "status": "ok",
                    "results": [
                        {"score": 0.88, "recordings": [{"id": "rec-def"}]}
                    ],
                }
                mock_get.return_value.raise_for_status = MagicMock()
                mbid, score = client.lookup("fp", 120_000)
        assert mbid == "rec-def"
        assert score == 0.88


class TestMusicBrainzResolver:
    def test_resolve_prioritizes_isrc(self, db: Database) -> None:
        with db.session() as s:
            f = DBFile(
                path="/a/1.flac",
                size_bytes=1,
                mtime=datetime.now(),
                isrc="US123",
            )
            s.add(f)
            s.flush()
            s.expunge(f)
            s.commit()

        resolver = MusicBrainzResolver(
            db,
            MagicMock(rate_limit=1, retry_count=3),
            MagicMock(api_key=""),
        )
        with patch.object(
            resolver.mb_client,
            "search_by_isrc",
            return_value=ResolvedMetadata(title="ISRC Match"),
        ) as mock_isrc, patch.object(
            resolver.mb_client, "search_by_recording_mbid"
        ) as mock_mbid:
            md = resolver.resolve_file(f)

        assert md is not None
        assert md.title == "ISRC Match"
        mock_isrc.assert_called_once_with("US123")
        mock_mbid.assert_not_called()

    def test_resolve_fallback_to_artist_title(self, db: Database) -> None:
        with db.session() as s:
            f = DBFile(
                path="/a/2.flac",
                size_bytes=1,
                mtime=datetime.now(),
                artist="Artist",
                title="Title",
            )
            s.add(f)
            s.flush()
            s.expunge(f)
            s.commit()

        resolver = MusicBrainzResolver(
            db,
            MagicMock(rate_limit=1, retry_count=3),
            MagicMock(api_key=""),
        )
        with patch.object(
            resolver.mb_client, "search_by_isrc", return_value=None
        ), patch.object(
            resolver.acoustid_client, "lookup", return_value=(None, 0.0)
        ), patch.object(
            resolver.mb_client,
            "search_by_artist_title",
            return_value=ResolvedMetadata(title="Artist Match"),
        ) as mock_search:
            md = resolver.resolve_file(f)

        assert md is not None
        assert md.title == "Artist Match"
        mock_search.assert_called_once()

    def test_resolve_library_respects_force_flag(self, db: Database) -> None:
        with db.session() as s:
            f1 = DBFile(
                path="/a/1.flac",
                size_bytes=1,
                mtime=datetime.now(),
                mb_recording_id="existing",
                title="Already Has Title",
                artist="Already Has Artist",
            )
            f2 = DBFile(
                path="/a/2.flac",
                size_bytes=1,
                mtime=datetime.now(),
            )
            s.add_all([f1, f2])
            s.commit()

        resolver = MusicBrainzResolver(
            db,
            MagicMock(rate_limit=1, retry_count=3),
            MagicMock(api_key=""),
        )
        with patch.object(
            resolver, "resolve_file", return_value=ResolvedMetadata(title="X")
        ):
            results = resolver.resolve_library(force=False)

        assert len(results) == 1

        with patch.object(
            resolver, "resolve_file", return_value=ResolvedMetadata(title="Y")
        ):
            results = resolver.resolve_library(force=True)

        assert len(results) == 2

    def test_resolve_library_dry_run_does_not_save(self, db: Database) -> None:
        with db.session() as s:
            f = DBFile(
                path="/a/1.flac",
                size_bytes=1,
                mtime=datetime.now(),
                artist="A",
                title="T",
            )
            s.add(f)
            s.commit()

        resolver = MusicBrainzResolver(
            db,
            MagicMock(rate_limit=1, retry_count=3),
            MagicMock(api_key=""),
        )
        with patch.object(
            resolver,
            "resolve_file",
            return_value=ResolvedMetadata(
                mb_recording_id="rec-1", title="Resolved"
            ),
        ):
            resolver.resolve_library(dry_run=True)

        with db.session() as s:
            row = s.query(DBFile).filter_by(path="/a/1.flac").first()
            assert row is not None
            assert row.mb_recording_id is None

    def test_save_resolution_persists(self, db: Database) -> None:
        with db.session() as s:
            f = DBFile(
                path="/a/1.flac",
                size_bytes=1,
                mtime=datetime.now(),
            )
            s.add(f)
            s.commit()
            file_id = f.id

        resolver = MusicBrainzResolver(
            db,
            MagicMock(rate_limit=1, retry_count=3),
            MagicMock(api_key=""),
        )
        md = ResolvedMetadata(
            mb_recording_id="rec-1",
            mb_release_id="rel-1",
            mb_track_id="track-1",
            score=0.95,
            title="T",
            artist="A",
            album="Al",
            album_artist="AA",
            year=2020,
            genre="rock",
        )
        resolver._save_resolution(file_id, md)

        with db.session() as s:
            row = s.query(DBFile).filter_by(id=file_id).first()
            assert row.mb_recording_id == "rec-1"
            assert row.mb_release_id == "rel-1"
            assert row.mb_track_id == "track-1"
            assert row.mb_score == 0.95
            assert row.mb_title == "T"
            assert row.mb_artist == "A"
            assert row.mb_album == "Al"
            assert row.mb_album_artist == "AA"
            assert row.mb_year == 2020
            assert row.mb_genre == "rock"
            assert row.mb_match_date is not None
