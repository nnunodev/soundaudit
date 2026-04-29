"""Detect fake-lossless files (MP3 re-encoded as FLAC / WAV).

Uses ffmpeg to measure energy in high-frequency bands. A genuine lossless file
should have a gradual rolloff toward the Nyquist frequency (~22kHz for CD).
A transcoded MP3 source shows a "brickwall" — a sharp cliff at the encoder's
cutoff frequency:

    128 kbps  → ~16 kHz
    192 kbps  → ~18 kHz
    V0 / 256  → ~19.5 kHz
    320 kbps  → ~20.5 kHz
    CD / FLAC → content above 20 kHz (gradual rolloff)

"""

from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from rich.console import Console

# ---------------------------------------------------------------------------
# Regex for parsing ffmpeg volumedetect stderr
# ---------------------------------------------------------------------------

_VOLUMEDETECT_MEAN_RE = re.compile(
    r"\[Parsed_volumedetect_\d+ @ [^\]]+\] mean_volume: ([-+]?[0-9]*\.?[0-9]+) dB"
)
_VOLUMEDETECT_MAX_RE = re.compile(
    r"\[Parsed_volumedetect_\d+ @ [^\]]+\] max_volume: ([-+]?[0-9]*\.?[0-9]+) dB"
)

# Threshold below which we treat a band as "silent"
SILENCE_THRESHOLD_DB = -80.0

# Frequency bands we probe.  The gaps between them let us measure the *slope*
# of the rolloff — a genuine file rolls off gradually, a transcode drops
# like a stone at the encoder cutoff.
_PROBE_BANDS: list[int] = [16_000, 18_500, 20_000, 21_000]


@dataclass(slots=True)
class SpectralResult:
    """Outcome of spectral analysis for a single file."""

    file_path: str
    sample_rate_hz: int
    duration_seconds: float
    # Per-band mean_volume (dB) — highest value across all sampled segments.
    #  None if that band could not be measured.
    band_volumes: dict[int, float | None]
    # Highest frequency band with content (not silent)
    cutoff_band_hz: int | None
    # True if the rolloff looks like a sharp brickwall
    is_transcode: bool
    # 0.0 (definitely lossless) → 1.0 (definitely transcode)
    confidence: float
    # Human-readable explanation
    reason: str | None


def _parse_volume(stderr: str) -> tuple[float | None, float | None]:
    """Extract (mean_volume_db, max_volume_db) from ffmpeg volumedetect output."""
    mean_match = _VOLUMEDETECT_MEAN_RE.search(stderr)
    max_match = _VOLUMEDETECT_MAX_RE.search(stderr)
    mean_db = float(mean_match.group(1)) if mean_match else None
    max_db = float(max_match.group(1)) if max_match else None
    return mean_db, max_db


def _ffmpeg_volume_above(
    path: str,
    *,
    highpass_freq_hz: int,
    offset_sec: float = 0.0,
    duration_sec: float = 1.0,
    timeout: float = 15.0,
) -> float | None:
    """Run ffmpeg highpass + volumedetect for a single band.

    Returns mean_volume in dB, or None on failure / timeout.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", str(offset_sec),
        "-t", str(duration_sec),
        "-i", path,
        "-af", f"highpass=f={highpass_freq_hz}:poles=4,volumedetect",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    mean_db, _ = _parse_volume(result.stderr)
    return mean_db


def _ffprobe_duration(path: str, timeout: float = 10.0) -> float | None:
    """Return file duration in seconds via ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (ValueError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _sample_offsets(duration: float) -> list[float]:
    """Pick 2 offsets to sample from a track (avoid intro / outro silence)."""
    if duration < 5.0:
        return [0.0]
    if duration < 30.0:
        return [duration * 0.25]
    return [duration * 0.25, duration * 0.60]


def analyze_file(
    path: str | Path,
    *,
    sample_rate_hz: int | None = None,
    probed_duration: float | None = None,
    bands: list[int] | None = None,
    console: Console | None = None,
) -> SpectralResult:
    """Run spectral analysis on a single audio file.

    The function is safe to call on any file type — non-FLAC / non-lossless
    files simply return ``is_transcode=False`` with zero confidence.
    """
    p = str(path)
    bands = bands or _PROBE_BANDS

    duration = probed_duration or _ffprobe_duration(p) or 0.0
    offsets = _sample_offsets(duration)

    # Gather volumes: for each band, take max across all offsets.
    band_volumes: dict[int, float | None] = dict.fromkeys(bands, None)

    for offset in offsets:
        for freq in bands:
            vol = _ffmpeg_volume_above(
                p,
                highpass_freq_hz=freq,
                offset_sec=offset,
                duration_sec=1.0,
            )
            if vol is None:
                continue
            # Keep the *loudest* (least negative) measurement per band.
            existing = band_volumes[freq]
            if existing is None or vol > existing:
                band_volumes[freq] = vol

    return _interpret_spectrum(p, sample_rate_hz or 44100, duration, band_volumes)


def _interpret_spectrum(
    file_path: str,
    sample_rate_hz: int,
    duration: float,
    band_volumes: dict[int, float | None],
) -> SpectralResult:
    """Derive transcode verdict from measured per-band volumes."""

    # If we got zero usable measurements, punt.
    valid = {f: v for f, v in band_volumes.items() if v is not None}
    if not valid:
        return SpectralResult(
            file_path=file_path,
            sample_rate_hz=sample_rate_hz,
            duration_seconds=duration,
            band_volumes=band_volumes,
            cutoff_band_hz=None,
            is_transcode=False,
            confidence=0.0,
            reason="Could not measure spectrum (ffmpeg missing or file unreadable)",
        )

    # --- 1. Find the highest band with audible content ---
    audible_bands = [f for f, v in valid.items() if v > SILENCE_THRESHOLD_DB]
    highest_audible = max(audible_bands) if audible_bands else None

    # --- 2. Slope analysis : look for sharp cliffs ---
    # A genuine lossless file rolls off smoothly.  A transcode drops like a
    # stone at the encoder cutoff.  We compute the largest dB drop between
    # *adjacent probed bands*.
    sorted_bands = sorted(valid.keys())
    drops: list[tuple[int, float]] = []
    for i in range(len(sorted_bands) - 1):
        lo, hi = sorted_bands[i], sorted_bands[i + 1]
        v_lo, v_hi = valid[lo], valid[hi]
        if v_lo is not None and v_hi is not None:
            drops.append((hi, v_lo - v_hi))

    max_drop_freq, max_drop_db = max(drops, key=lambda x: x[1]) if drops else (None, 0.0)

    # --- 3. Build verdict ---
    # Confidence rules (heuristics tuned on common encoder cutoffs).

    if highest_audible is None:
        # Nothing audible above 16kHz — dead silence or very low quality source.
        return SpectralResult(
            file_path=file_path,
            sample_rate_hz=sample_rate_hz,
            duration_seconds=duration,
            band_volumes=band_volumes,
            cutoff_band_hz=None,
            is_transcode=True,
            confidence=0.70,
            reason="No audible content above 16kHz — MP3 ≤128kbps or silence",
        )

    # Genuine lossless characteristics:
    #   - content up to 20kHz or higher
    #   - gradual rolloff (small dB drop between adjacent bands)
    if highest_audible >= 20_000 and max_drop_db < 25.0:
        return SpectralResult(
            file_path=file_path,
            sample_rate_hz=sample_rate_hz,
            duration_seconds=duration,
            band_volumes=band_volumes,
            cutoff_band_hz=highest_audible,
            is_transcode=False,
            confidence=0.0,
            reason=None,
        )

    # Sharp brickwall >25 dB between adjacent bands = strong transcode signal.
    if max_drop_db >= 40.0:
        return SpectralResult(
            file_path=file_path,
            sample_rate_hz=sample_rate_hz,
            duration_seconds=duration,
            band_volumes=band_volumes,
            cutoff_band_hz=max_drop_freq,
            is_transcode=True,
            confidence=min(0.95, 0.60 + (max_drop_db / 100.0)),
            reason=f"Sharp brickwall at ~{max_drop_freq:,}Hz ({max_drop_db:.1f}dB cliff) — likely MP3 source",
        )

    if max_drop_db >= 25.0:
        return SpectralResult(
            file_path=file_path,
            sample_rate_hz=sample_rate_hz,
            duration_seconds=duration,
            band_volumes=band_volumes,
            cutoff_band_hz=max_drop_freq,
            is_transcode=True,
            confidence=min(0.85, 0.50 + (max_drop_db / 80.0)),
            reason=f"Suspect brickwall at ~{max_drop_freq:,}Hz ({max_drop_db:.1f}dB cliff) — possible transcode",
        )

    # Soft or ambiguous rolloff — flag as low-confidence review.
    return SpectralResult(
        file_path=file_path,
        sample_rate_hz=sample_rate_hz,
        duration_seconds=duration,
        band_volumes=band_volumes,
        cutoff_band_hz=highest_audible,
        is_transcode=False,
        confidence=0.15,
        reason=f"Gradual rolloff above {highest_audible:,}Hz — likely genuine but confirm manually",
    )


def analyze_library_transcodes(
    database,
    *,
    lossless_only: bool = True,
    workers: int = 4,
    console: Console | None = None,
) -> list[SpectralResult]:
    """Batch spectral analysis over every file in the database.

    Only FLAC / lossless files are scanned by default. Results are written
    back to the DB (``is_transcode``, ``transcode_confidence``)."""
    from soundaudit.db.store import DBFile

    c = console or Console()

    with database.session() as s:
        q = s.query(DBFile)
        if lossless_only:
            q = q.filter(DBFile.lossless == 1)
        # Only analyze files we haven't checked yet, plus those whose
        # content changed (signature updated). In practice we just do all.
        rows = q.all()

    if not rows:
        c.print("[green]No lossless files to analyze.[/green]")
        return []

    c.print(f"[cyan]Analysing {len(rows)} lossless file(s) for spectral transcodes...[/cyan]")

    results: list[SpectralResult] = []

    def _worker(db_file: DBFile) -> SpectralResult:
        res = analyze_file(
            db_file.path,
            sample_rate_hz=db_file.sample_rate_hz or 44100,
            probed_duration=db_file.duration_seconds or None,
        )
        # Persist immediately so a crash doesn't lose progress.
        with database.session() as s:
            row = s.query(DBFile).filter_by(path=db_file.path).first()
            if row:
                row.is_transcode = int(res.is_transcode)
                row.transcode_confidence = res.confidence
                row.transcode_reason = res.reason
                row.spectral_cutoff_hz = res.cutoff_band_hz
            s.commit()
        return res

    processed = 0
    total = len(rows)
    with ThreadPoolExecutor(max_workers=min(workers, 8)) as pool:
        for res in pool.map(_worker, rows):
            processed += 1
            results.append(res)
            if processed % 50 == 0 or processed == total:
                c.print(
                    f"  [dim]{processed}/{total}  "
                    f"transcodes={sum(1 for r in results if r.is_transcode)}"
                    f"[/dim]",
                    end="\r",
                )

    transcode_count = sum(1 for r in results if r.is_transcode)
    c.print(
        f"\n[green]Done. {transcode_count}/{len(results)} flagged as possible transcodes.[/green]"
    )
    return results
