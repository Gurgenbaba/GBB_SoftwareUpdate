"""Tests for local_source_strict mode and find_installer pattern matching."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import SOFTWARE_BY_KEY
from app.installers import InstallerService
from app.local_source import LocalSourceService
from app.models import SoftwareState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine(prefer_local: bool = True, strict: bool = False, local_svc=None, **kwargs):
    defaults = dict(
        choco_client=MagicMock(),
        winget_client=MagicMock(),
        logger=logging.getLogger("test_local_strict"),
        software_by_key=SOFTWARE_BY_KEY,
        scanner=None,
        installer_settle_wait_seconds=0,
        prefer_local_source=prefer_local,
        local_source_strict=strict,
        local_source_service=local_svc,
    )
    defaults.update(kwargs)
    return InstallerService(**defaults)


def _local_svc_with_file(tmp_path: Path, filename: str, software_providers: dict | None = None) -> LocalSourceService:
    """Create a LocalSourceService that has already scanned a directory containing `filename`."""
    f = tmp_path / filename
    f.write_bytes(b"\x00" * 16)
    svc = LocalSourceService(logging.getLogger("test_local_svc"), software_providers or {})
    svc.root_path = tmp_path
    svc._files = [f]
    return svc


# ---------------------------------------------------------------------------
# find_installer: configured local_patterns are exclusive
# ---------------------------------------------------------------------------

class TestFindInstallerPatterns:
    def test_configured_pattern_matches_correct_file(self, tmp_path):
        svc = _local_svc_with_file(
            tmp_path, "firefox-setup-123.exe",
            {"firefox": {"local_patterns": ["firefox-setup*.exe"]}}
        )
        result = svc.find_installer("firefox")
        assert result is not None
        assert result.name == "firefox-setup-123.exe"

    def test_configured_pattern_rejects_wrong_filename(self, tmp_path):
        """When local_patterns are set, a file that doesn't match them must be rejected."""
        svc = _local_svc_with_file(
            tmp_path, "adobe_reader_setup.exe",
            {"firefox": {"local_patterns": ["firefox-setup*.exe"]}}
        )
        result = svc.find_installer("firefox")
        assert result is None, "File not matching configured pattern must not be returned"

    def test_no_configured_patterns_falls_back_to_broad_search(self, tmp_path):
        svc = _local_svc_with_file(
            tmp_path, "Firefox Setup 120.exe",
            {"firefox": {"local_patterns": []}}
        )
        result = svc.find_installer("firefox")
        assert result is not None

    def test_adobe_installer_does_not_match_firefox_patterns(self, tmp_path):
        svc = _local_svc_with_file(
            tmp_path, "AcroRdrDC2300_en_US.exe",
            {"firefox": {"local_patterns": ["firefox*.exe"]}}
        )
        result = svc.find_installer("firefox")
        assert result is None


# ---------------------------------------------------------------------------
# Strict mode: no Choco/WinGet fallback after local failure
# ---------------------------------------------------------------------------

class TestLocalSourceStrictMode:
    def test_strict_blocks_choco_fallback_after_local_failure(self, tmp_path):
        local_svc = _local_svc_with_file(tmp_path, "firefox-setup.exe")
        eng = _engine(prefer_local=True, strict=True, local_svc=local_svc)
        sw = SOFTWARE_BY_KEY["firefox"]
        prev = SoftwareState("Update verfuegbar", package_name="firefox")

        error_state = SoftwareState("Fehler", detail="exit code 1")
        with patch.object(eng, "_run_local_source_installer", return_value=("Lokal/USB", error_state)):
            with patch.object(eng, "_run_choco_upgrade") as mock_choco:
                with patch.object(eng, "_run_internal_installer", return_value=None):
                    eng._current_software_key = "firefox"
                    action, state = eng._attempt_update_chain(sw, "firefox", None, prev, False, {})

        mock_choco.assert_not_called()
        assert state.status == "Fehler"
        assert "Lokaler Installer fehlgeschlagen" in (state.detail or "")

    def test_strict_blocks_winget_fallback_after_local_failure(self, tmp_path):
        local_svc = _local_svc_with_file(tmp_path, "firefox-setup.exe")
        eng = _engine(prefer_local=True, strict=True, local_svc=local_svc)
        sw = SOFTWARE_BY_KEY["firefox"]
        prev = SoftwareState("Update verfuegbar", package_name="firefox")

        error_state = SoftwareState("Fehler", detail="exit code 1")
        with patch.object(eng, "_run_local_source_installer", return_value=("Lokal/USB", error_state)):
            with patch.object(eng, "_run_winget_upgrade") as mock_winget:
                with patch.object(eng, "_run_internal_installer", return_value=None):
                    eng._current_software_key = "firefox"
                    action, state = eng._attempt_update_chain(sw, None, "Mozilla.Firefox", prev, False, {})

        mock_winget.assert_not_called()
        assert state.status == "Fehler"

    def test_non_strict_allows_choco_fallback_after_local_failure(self, tmp_path):
        local_svc = _local_svc_with_file(tmp_path, "firefox-setup.exe")
        eng = _engine(prefer_local=True, strict=False, local_svc=local_svc)
        sw = SOFTWARE_BY_KEY["firefox"]
        prev = SoftwareState("Update verfuegbar", package_name="firefox")

        error_state = SoftwareState("Fehler", detail="exit code 1")
        ok_state = SoftwareState("Aktuell")
        with patch.object(eng, "_run_local_source_installer", return_value=("Lokal/USB", error_state)):
            with patch.object(eng, "_run_choco_upgrade", return_value=("Upgrade", ok_state)) as mock_choco:
                with patch.object(eng, "_run_internal_installer", return_value=None):
                    eng._current_software_key = "firefox"
                    action, state = eng._attempt_update_chain(sw, "firefox", None, prev, False, {})

        mock_choco.assert_called_once()
        assert state.status == "Aktuell"

    def test_strict_no_installer_found_still_tries_choco(self, tmp_path):
        """When no local installer is found at all, strict mode should NOT block other providers."""
        local_svc = LocalSourceService(logging.getLogger("test"), {})
        local_svc.root_path = tmp_path
        local_svc._files = []  # empty — no match possible
        eng = _engine(prefer_local=True, strict=True, local_svc=local_svc)
        sw = SOFTWARE_BY_KEY["firefox"]
        prev = SoftwareState("Update verfuegbar", package_name="firefox")

        ok_state = SoftwareState("Aktuell")
        with patch.object(eng, "_run_choco_upgrade", return_value=("Upgrade", ok_state)) as mock_choco:
            with patch.object(eng, "_run_internal_installer", return_value=None):
                eng._current_software_key = "firefox"
                action, state = eng._attempt_update_chain(sw, "firefox", None, prev, False, {})

        mock_choco.assert_called_once()
        assert state.status == "Aktuell"

    def test_strict_install_chain_also_blocks_fallback(self, tmp_path):
        local_svc = _local_svc_with_file(tmp_path, "firefox-setup.exe")
        eng = _engine(prefer_local=True, strict=True, local_svc=local_svc)
        sw = SOFTWARE_BY_KEY["firefox"]
        prev = SoftwareState("Nicht installiert")

        error_state = SoftwareState("Fehler", detail="bad installer")
        with patch.object(eng, "_run_local_source_installer", return_value=("Lokal/USB", error_state)):
            with patch.object(eng, "_run_choco_upgrade") as mock_choco:
                with patch.object(eng, "_run_internal_installer", return_value=None):
                    eng._current_software_key = "firefox"
                    action, state = eng._attempt_install_chain(sw, "firefox", None, prev, False, {})

        mock_choco.assert_not_called()
        assert state.status == "Fehler"
        assert "Lokaler Installer fehlgeschlagen" in (state.detail or "")


# ---------------------------------------------------------------------------
# [LOCAL] log line is emitted
# ---------------------------------------------------------------------------

def test_local_installer_emits_local_log(tmp_path, caplog):
    fake_exe = tmp_path / "firefox-setup.exe"
    fake_exe.write_bytes(b"\x00" * 16)

    local_svc = LocalSourceService(logging.getLogger("test"), {})
    local_svc.root_path = tmp_path
    local_svc._files = [fake_exe]

    eng = _engine(prefer_local=True, strict=False, local_svc=local_svc)
    sw = SOFTWARE_BY_KEY["firefox"]
    prev = SoftwareState("Update verfuegbar")

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    with caplog.at_level(logging.INFO, logger="test_local_strict"):
        with patch.object(eng, "_run_subprocess_with_lock_retry", return_value=mock_proc):
            result = eng._run_local_source_installer(sw, prev, False)

    assert result is not None
    assert "[LOCAL]" in caplog.text
    assert "firefox-setup.exe" in caplog.text
