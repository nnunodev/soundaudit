"""Tests for tag writeback actuator."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from mutagen.id3 import COMM, TDRC, TIT2, TPE1, TPOS, TRCK
from mutagen.mp3 import MP3

from soundaudit.actuator.tags import (
    TagWriteError,
    _snapshot_flac_ogg,
    _write_flac_ogg,
    resolved_metadata_to_tags,
    snapshot_tags,
    validate_fields,
    write_tags,
)
from soundaudit.models import TrackTags

# Minimal silent MPEG-1 Layer III frame (128kbps, 44100Hz, stereo)
# Frame size = 417 bytes; header + zero-fill
_FRAME_HEADER = b"\xff\xfb\x90\x64"
_FRAME_BODY = b"\x00" * 413
_DUMMY_MP3_FRAMES = 2 * (_FRAME_HEADER + _FRAME_BODY)


class FakeVorbisLike:
    """Mock dict-like audio object for FLAC/Ogg tests."""

    def __init__(self) -> None:
        self._data: dict[str, list[str]] = {}

    def __getitem__(self, key: str) -> list[str]:
        return self._data[key]

    def __setitem__(self, key: str, val: list[str] | str) -> None:
        self._data[key] = val if isinstance(val, list) else [val]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def save(self) -> None:
        pass


def _make_mp3(path: Path) -> None:
    """Create an empty file that mutagen.mp3 can load."""
    path.write_bytes(b"")
    MP3().save(str(path))
    with open(path, "ab") as f:
        f.write(_DUMMY_MP3_FRAMES)


class TestValidateFields:
    def test_valid_fields(self) -> None:
        assert validate_fields({"title", "artist", "album"}) == {"title", "artist", "album"}

    def test_invalid_field_raises(self) -> None:
        with pytest.raises(TagWriteError, match="Invalid fields: foo"):
            validate_fields({"title", "foo"})


class TestResolvedMetadataToTags:
    def test_basic(self) -> None:
        tags = resolved_metadata_to_tags(
            mb_title="Song",
            mb_artist="Artist",
            mb_album="Album",
            mb_album_artist="Album Artist",
            mb_year=2024,
            mb_genre="Rock",
        )
        assert tags.title == "Song"
        assert tags.artist == "Artist"
        assert tags.album == "Album"
        assert tags.album_artist == "Album Artist"
        assert tags.year == 2024
        assert tags.genre == "Rock"


class TestSnapshotTags:
    def test_snapshot_empty_mp3(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.mp3"
            _make_mp3(path)
            result = snapshot_tags(path)
            assert result["title"] is None
            assert result["artist"] is None

    def test_snapshot_tagged_mp3(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.mp3"
            _make_mp3(path)
            audio = MP3(str(path))
            audio.add_tags()
            audio.tags["TIT2"] = TIT2(encoding=3, text="My Song")
            audio.tags["TPE1"] = TPE1(encoding=3, text="Band")
            audio.tags["TRCK"] = TRCK(encoding=3, text="3/12")
            audio.tags["TPOS"] = TPOS(encoding=3, text="1/2")
            audio.tags["TDRC"] = TDRC(encoding=3, text="2024")
            audio.save(str(path))
            result = snapshot_tags(path)
            assert result["title"] == "My Song"
            assert result["artist"] == "Band"
            assert result["track_number"] == 3
            assert result["track_total"] == 12
            assert result["disc_number"] == 1
            assert result["disc_total"] == 2
            assert result["year"] == 2024

    def test_unsupported_format(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.xyz"
            path.write_text("nope")
            with pytest.raises(TagWriteError, match="Unsupported format"):
                snapshot_tags(path)


class TestWriteTags:
    def test_write_mp3_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "write.mp3"
            _make_mp3(path)
            tags = TrackTags(
                title="New Title",
                artist="New Artist",
                album="New Album",
                track_number=5,
                track_total=10,
                year=2023,
                genre="Jazz",
                isrc="USABC1234567",
            )
            backup = write_tags(
                path,
                tags,
                fields={"title", "artist", "album", "track_number", "track_total", "year", "genre", "isrc"},
                backup=True,
            )
            assert backup["title"] is None
            audio = MP3(str(path))
            assert audio.tags is not None
            assert str(audio.tags["TIT2"]) == "New Title"
            assert str(audio.tags["TPE1"]) == "New Artist"
            assert str(audio.tags["TRCK"]) == "5/10"
            assert str(audio.tags["TDRC"]) == "2023"
            assert str(audio.tags["TCON"]) == "Jazz"
            assert str(audio.tags["TSRC"]) == "USABC1234567"

    def test_preserve_untouched_tags_mp3(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "preserve.mp3"
            _make_mp3(path)
            audio = MP3(str(path))
            audio.add_tags()
            audio.tags["TIT2"] = TIT2(encoding=3, text="Keep")
            audio.tags["COMM"] = COMM(encoding=3, lang="eng", desc="", text="Preserve me")
            audio.save(str(path))
            tags = TrackTags(title="Replaced", artist="Added")
            write_tags(path, tags, fields={"title", "artist"})
            audio = MP3(str(path))
            assert audio.tags is not None
            assert str(audio.tags["TIT2"]) == "Replaced"
            assert str(audio.tags["TPE1"]) == "Added"
            comm_frames = [v for k, v in audio.tags.items() if k.startswith("COMM")]
            assert any("Preserve me" in str(v) for v in comm_frames)

    def test_invalid_fields_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.mp3"
            _make_mp3(path)
            with pytest.raises(TagWriteError, match="Invalid fields"):
                write_tags(path, TrackTags(), fields={"foo"})

    def test_file_not_found(self) -> None:
        with pytest.raises(TagWriteError, match="File not found"):
            write_tags(Path("/nonexistent/file.mp3"), TrackTags())


class TestFlacOggHelpers:
    def test_snapshot_flac_ogg(self) -> None:
        audio = FakeVorbisLike()
        audio["TITLE"] = ["Song"]
        audio["ARTIST"] = ["Artist"]
        audio["TRACKNUMBER"] = ["2/10"]
        audio["DISCNUMBER"] = ["1/2"]
        audio["DATE"] = ["2021"]
        audio["GENRE"] = ["Pop"]
        audio["ISRC"] = ["ABCDE1234567"]
        result = _snapshot_flac_ogg(audio)  # type: ignore[arg-type]
        assert result["title"] == "Song"
        assert result["artist"] == "Artist"
        assert result["track_number"] == 2
        assert result["track_total"] == 10
        assert result["disc_number"] == 1
        assert result["disc_total"] == 2
        assert result["year"] == "2021"
        assert result["genre"] == "Pop"
        assert result["isrc"] == "ABCDE1234567"

    def test_write_flac_ogg(self) -> None:
        audio = FakeVorbisLike()
        tags = TrackTags(
            title="New",
            artist="Me",
            album="LP",
            album_artist="Us",
            track_number=3,
            track_total=12,
            disc_number=1,
            disc_total=2,
            year=2025,
            genre="Rock",
            isrc="ZZ9998765432",
        )
        _write_flac_ogg(audio, tags, fields=set(validate_fields({  # type: ignore[arg-type]
            "title", "artist", "album", "album_artist",
            "track_number", "track_total", "disc_number", "disc_total",
            "year", "genre", "isrc",
        })))
        assert audio["TITLE"] == ["New"]
        assert audio["ARTIST"] == ["Me"]
        assert audio["ALBUM"] == ["LP"]
        assert audio["ALBUMARTIST"] == ["Us"]
        assert audio["TRACKNUMBER"] == ["3/12"]
        assert audio["DISCNUMBER"] == ["1/2"]
        assert audio["DATE"] == ["2025"]
        assert audio["GENRE"] == ["Rock"]
        assert audio["ISRC"] == ["ZZ9998765432"]

    def test_write_flac_ogg_clears_empty(self) -> None:
        audio = FakeVorbisLike()
        audio["TITLE"] = ["Old"]
        tags = TrackTags(title="New")
        _write_flac_ogg(audio, tags, fields={"title"})  # type: ignore[arg-type]
        assert audio["TITLE"] == ["New"]
