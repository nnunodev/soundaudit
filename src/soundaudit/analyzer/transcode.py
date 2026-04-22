"""Detect fake-lossless files (MP3 re-encoded as FLAC)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from soundaudit.models import FileInfo


def analyze_transcode(file_info: FileInfo) -> tuple[bool, float, str | None]:
    """
    Run ffprobe to detect spectral cutoffs indicating MP3->FLAC transcodes.

    Returns:
        is_transcode, confidence (0.0-1.0), reason_message
    """
    if not file_info.lossless or file_info.format.value != "flac":
        return False, 0.0, None

    ffprobe_cmd = [
        "ffprobe",
        "-v", "quiet",
        "-f", "lavfi",
        "-i", f"amovie={file_info.path},asidedata=all",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
    ]

    try:
        result = subprocess.run(
            ffprobe_cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return False, 0.0, "ffprobe not found"
    except subprocess.TimeoutExpired:
        return False, 0.0, "ffprobe timeout"

    if result.returncode != 0:
        return False, 0.0, f"ffprobe error: {result.stderr[:200]}"

    # Stub analysis — parse result for cutoff detection
    output = result.stdout
    if "Stream #" in output:
        pass  # TODO: implement spectral analysis

    return False, 0.0, None
