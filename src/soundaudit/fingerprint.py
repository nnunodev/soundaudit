"""Chromaprint / AcoustID fingerprint generation.

Tries pyacoustid first (uses chromaprint C library or fpcalc binary,
whichever is available), then falls back to calling fpcalc directly.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console


@dataclass(frozen=True, slots=True)
class FingerprintResult:
    fingerprint: str
    duration_ms: int


# Optional backends
_pyacoustid: object | None = None
try:
    import acoustid  # type: ignore[import-untyped]

    _pyacoustid = acoustid
except ImportError:
    pass


def fingerprint_file(
    path: Path,
    *,
    fpcalc_path: str = "/usr/bin/fpcalc",
    console: Console | None = None,
) -> FingerprintResult | None:
    """Generate a chromaprint fingerprint for an audio file.

    Returns ``FingerprintResult`` or ``None`` on failure.
    """
    if _pyacoustid is not None:
        result = _fp_with_pyacoustid(path, console=console)
        if result:
            return result
    return _fp_with_fpcalc(path, fpcalc_path, console=console)


def _fp_with_pyacoustid(
    path: Path,
    *,
    console: Console | None = None,
) -> FingerprintResult | None:
    try:
        duration, fp = _pyacoustid.fingerprint_file(str(path))  # type: ignore[union-attr]
        return FingerprintResult(
            fingerprint=fp,
            duration_ms=int(duration * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        if console:
            console.print(f"[dim]pyacoustid failed for {path.name}: {exc}[/dim]")
        return None


def _fp_with_fpcalc(
    path: Path,
    fpcalc_path: str,
    *,
    console: Console | None = None,
) -> FingerprintResult | None:
    """Call the fpcalc binary directly.

    Tries JSON mode first, then plain text fallback.
    """
    candidates = [fpcalc_path]
    if env_path := os.environ.get("FPCALC"):
        candidates.insert(0, env_path)

    for binary in candidates:
        for args in (
            [binary, "-json", str(path)],
            [binary, str(path)],
        ):
            try:
                proc = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    encoding="utf-8",
                    errors="replace",
                )
                if proc.returncode != 0:
                    continue
                output = proc.stdout.strip()
                if args[1] == "-json":
                    data = json.loads(output)
                    return FingerprintResult(
                        fingerprint=data["fingerprint"],
                        duration_ms=int(float(data.get("duration", 0)) * 1000),
                    )
                # plain text: DURATION=123\nFINGERPRINT=AQAD...
                duration = 0
                fingerprint: str | None = None
                for line in output.splitlines():
                    if line.startswith("DURATION="):
                        with contextlib.suppress(ValueError):
                            duration = int(float(line.split("=", 1)[1]))
                    elif line.startswith("FINGERPRINT="):
                        fingerprint = line.split("=", 1)[1]
                if fingerprint:
                    return FingerprintResult(
                        fingerprint=fingerprint,
                        duration_ms=duration * 1000,
                    )
            except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, OSError):
                continue
    if console:
        console.print(f"[dim]fpcalc unavailable for {path.name}[/dim]")
    return None


def fingerprint_available(
    fpcalc_path: str = "/usr/bin/fpcalc",
) -> tuple[bool, str]:
    """Return (available, backend_name) describing which backend we have."""
    if _pyacoustid is not None:
        return True, "pyacoustid"
    candidates = [fpcalc_path]
    if env_path := os.environ.get("FPCALC"):
        candidates.insert(0, env_path)
    for binary in candidates:
        try:
            proc = subprocess.run(
                [binary, "--help"],
                capture_output=True,
                timeout=5,
            )
            if proc.returncode in (0, 1):  # fpcalc --help exits 1 but prints help
                return True, f"fpcalc ({binary})"
        except (FileNotFoundError, OSError):
            pass
    return False, "none"
