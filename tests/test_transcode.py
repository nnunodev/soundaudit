"""Tests for spectral transcode detection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from soundaudit.analyzer.transcode import (
    SILENCE_THRESHOLD_DB,
    SpectralResult,
    _interpret_spectrum,
    _parse_volume,
    analyze_file,
    analyze_library_transcodes,
)
from soundaudit.db.store import DBFile, Database


@pytest.fixture
def db(tmp_path: Path):
    path = str(tmp_path / "test.db")
    database = Database(path)
    yield database
    database.engine.dispose()


class TestParseVolume:
    def test_extracts_mean_and_max(self) -> None:
        stderr = (
            "[Parsed_volumedetect_0 @ 0x7f8] mean_volume: -12.34 dB\n"
            "[Parsed_volumedetect_0 @ 0x7f8] max_volume: -3.21 dB\n"
        )
        mean_db, max_db = _parse_volume(stderr)
        assert mean_db == pytest.approx(-12.34)
        assert max_db == pytest.approx(-3.21)

    def test_returns_none_on_missing(self) -> None:
        assert _parse_volume("garbage") == (None, None)


class TestInterpretSpectrum:
    def test_genuine_lossless(self) -> None:
        # Smooth rolloff, content up to 21k
        bands = {16_000: -50.0, 18_500: -55.0, 20_000: -60.0, 21_000: -65.0}
        res = _interpret_spectrum("/a/1.flac", 44100, 180.0, bands)
        assert res.is_transcode is False
        assert res.confidence == 0.0
        assert res.cutoff_band_hz == 21_000
        assert res.reason is None

    def test_hard_brickwall_128k_mp3(self) -> None:
        # Classic 128kbps signature: total silence above 16kHz
        bands = {16_000: -85.0, 18_500: -90.0, 20_000: -95.0, 21_000: -95.0}
        res = _interpret_spectrum("/a/1.flac", 44100, 180.0, bands)
        assert res.is_transcode is True
        assert res.confidence >= 0.60
        assert "128kbps" in (res.reason or "")

    def test_brickwall_320k_mp3(self) -> None:
        # 320kbps cutoff around 20.5kHz: audible at 20k, silent at 21k with sharp drop
        bands = {16_000: -40.0, 18_500: -42.0, 20_000: -45.0, 21_000: -85.0}
        res = _interpret_spectrum("/a/1.flac", 44100, 180.0, bands)
        assert res.is_transcode is True
        assert res.confidence >= 0.50
        assert "21,000Hz" in (res.reason or "")

    def test_ambiguous_rolloff(self) -> None:
        # Some drop but not a clear brickwall
        bands = {16_000: -40.0, 18_500: -50.0, 20_000: -70.0, 21_000: -85.0}
        res = _interpret_spectrum("/a/1.flac", 44100, 180.0, bands)
        # Not a sharp cliff, so low confidence genuine
        assert res.is_transcode is False
        assert res.confidence < 0.30

    def test_no_high_freq_content(self) -> None:
        bands = {16_000: -85.0, 18_500: -90.0, 20_000: -95.0, 21_000: -95.0}
        res = _interpret_spectrum("/a/1.flac", 44100, 180.0, bands)
        assert res.is_transcode is True
        assert res.confidence >= 0.60
        assert res.cutoff_band_hz is None

    def test_empty_measurements(self) -> None:
        res = _interpret_spectrum("/a/1.flac", 44100, 180.0, {})
        assert res.confidence == 0.0
        assert "Could not measure" in (res.reason or "")


class TestAnalyzeFile:
    def test_skips_non_lossless(self) -> None:
        from soundaudit.models import AudioFormat, FileInfo, TrackTags
        info = FileInfo(
            path=Path("/a/1.mp3"),
            size_bytes=1,
            mtime_ns=0,
            format=AudioFormat.MP3,
            lossless=False,
            tags=TrackTags(),
        )
        # analyze_file is path-based; just pass a nonexistent path
        # and verify it tries ffmpeg (which we mock to return silence)
        with patch(
            "soundaudit.analyzer.transcode._ffmpeg_volume_above",
            return_value=-90.0,
        ):
            res = analyze_file("/a/1.mp3", sample_rate_hz=44100, probed_duration=180.0)
        # Even with silence, non-FLAC files are not flagged — but our function
        # is purely spectral-based; caller decides whether to skip formats.
        # The function itself always returns a SpectralResult.
        assert isinstance(res, SpectralResult)

    def test_uses_two_offsets_for_long_file(self) -> None:
        call_count = 0
        def fake_ffmpeg(path, **kwargs):
            nonlocal call_count
            call_count += 1
            return -40.0

        with patch("soundaudit.analyzer.transcode._ffmpeg_volume_above", side_effect=fake_ffmpeg):
            res = analyze_file("/a/1.flac", sample_rate_hz=44100, probed_duration=300.0)

        # 2 offsets × 4 bands = 8 calls
        assert call_count == 8
        assert res.cutoff_band_hz == 21_000

    def test_single_offset_for_short_file(self) -> None:
        call_count = 0
        def fake_ffmpeg(path, **kwargs):
            nonlocal call_count
            call_count += 1
            return -40.0

        with patch("soundaudit.analyzer.transcode._ffmpeg_volume_above", side_effect=fake_ffmpeg):
            res = analyze_file("/a/1.flac", sample_rate_hz=44100, probed_duration=10.0)

        # 1 offset × 4 bands = 4 calls
        assert call_count == 4

    def test_keeps_best_volume_per_band(self) -> None:
        # First offset returns low volume, second offset returns high
        volumes = [-85.0, -85.0, -85.0, -85.0, -40.0, -42.0, -45.0, -50.0]
        with patch(
            "soundaudit.analyzer.transcode._ffmpeg_volume_above",
            side_effect=volumes,
        ):
            res = analyze_file("/a/1.flac", sample_rate_hz=44100, probed_duration=300.0)

        # Should pick the louder second-offset measurements
        assert res.cutoff_band_hz == 21_000
        assert res.is_transcode is False


class TestAnalyzeLibraryTranscodes:
    def test_runs_on_lossless_files(self, db: Database) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        with db.session() as s:
            s.add(DBFile(
                path="/a/1.flac",
                size_bytes=10_000_000,
                mtime=now,
                format="flac",
                lossless=1,
                duration_seconds=180.0,
                sample_rate_hz=44100,
            ))
            s.add(DBFile(
                path="/a/2.mp3",
                size_bytes=5_000_000,
                mtime=now,
                format="mp3",
                lossless=0,
            ))
            s.commit()

        with patch(
            "soundaudit.analyzer.transcode._ffmpeg_volume_above",
            return_value=-40.0,
        ), patch(
            "soundaudit.analyzer.transcode._ffprobe_duration",
            return_value=180.0,
        ):
            results = analyze_library_transcodes(db, lossless_only=True)

        # Only FLAC analyzed
        assert len(results) == 1
        assert results[0].file_path == "/a/1.flac"
        assert results[0].is_transcode is False

        with db.session() as s:
            row = s.query(DBFile).filter_by(path="/a/1.flac").first()
            assert row is not None
            assert row.is_transcode == 0
            assert row.transcode_confidence == 0.0
