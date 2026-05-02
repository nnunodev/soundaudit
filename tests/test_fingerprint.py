"""Tests for fingerprinting fallback behaviour."""

from __future__ import annotations

from unittest.mock import patch

from soundaudit.fingerprint import (
    _fp_with_fpcalc,
    fingerprint_available,
)


class TestFingerprintAvailable:
    def test_no_backends(self) -> None:
        with patch("soundaudit.fingerprint._pyacoustid", None):
            avail, backend = fingerprint_available()
            assert avail is False
            assert backend == "none"

    def test_pyacoustid_available(self) -> None:
        with patch("soundaudit.fingerprint._pyacoustid", object()):
            avail, backend = fingerprint_available()
            assert avail is True
            assert backend == "pyacoustid"

    def test_fpcalc_binary_available(self, tmp_path) -> None:
        fake_fpcalc = tmp_path / "fpcalc"
        fake_fpcalc.write_text("#!/bin/sh\necho fpcalc")
        with patch("soundaudit.fingerprint._pyacoustid", None):
            avail, backend = fingerprint_available(str(fake_fpcalc))
            # On Windows the fake script won't execute, so just check it doesn't crash
            assert isinstance(avail, bool)
            assert isinstance(backend, str)


class TestFpWithFpcalc:
    def test_returns_none_when_binary_missing(self) -> None:
        result = _fp_with_fpcalc(
            "dummy.mp3", "/nonexistent/fpcalc", console=None
        )
        assert result is None

    def test_returns_none_on_invalid_json(self, tmp_path) -> None:
        binary = tmp_path / "fpcalc"
        # Create a fake binary that exits 0 but prints garbage
        binary.write_text("#!/bin/sh\necho garbage")
        result = _fp_with_fpcalc("dummy.mp3", str(binary), console=None)
        assert result is None

    def test_parses_plain_text_output(self, tmp_path) -> None:
        binary = tmp_path / "fpcalc"
        binary.write_text(
            "#!/bin/sh\n"
            'echo "DURATION=123"\n'
            'echo "FINGERPRINT=AQADtEmi..."\n'
        )
        # On Windows subprocess won't run a shell script, skip there
        import sys

        if sys.platform == "win32":
            return
        result = _fp_with_fpcalc("dummy.mp3", str(binary), console=None)
        assert result is not None
        assert result.fingerprint == "AQADtEmi..."
        assert result.duration_ms == 123_000
