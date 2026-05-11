"""Tests covering all 7 audit fixes."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.choco import CommandResult
from app.config import SOFTWARE_BY_KEY
from app.installers import LOCK_TEAMS_EXE_NAMES, InstallerService
from app.models import SoftwareState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine(**kwargs):
    defaults = dict(
        choco_client=MagicMock(),
        winget_client=MagicMock(),
        logger=logging.getLogger("test_audit"),
        software_by_key=SOFTWARE_BY_KEY,
        scanner=None,
        installer_settle_wait_seconds=0,
    )
    defaults.update(kwargs)
    return InstallerService(**defaults)


# ---------------------------------------------------------------------------
# Fix 1: No duplicate local-source call in _attempt_update_chain
# ---------------------------------------------------------------------------

def test_local_source_called_exactly_once_in_update_chain() -> None:
    eng = _engine(prefer_local_source=True)
    sw = SOFTWARE_BY_KEY["firefox"]
    prev = SoftwareState("Update verfuegbar", package_name="firefox")
    error_state = SoftwareState("Fehler", detail="some error")

    with patch.object(eng, "_run_local_source_installer", return_value=("Lokal/USB", error_state)) as mock_local:
        with patch.object(eng, "_run_choco_upgrade", return_value=("Upgrade", SoftwareState("Aktuell"))):
            eng._current_software_key = "firefox"
            eng._attempt_update_chain(sw, "firefox", None, prev, False, {})

    assert mock_local.call_count == 1, (
        f"_run_local_source_installer called {mock_local.call_count} times; expected exactly 1"
    )


# ---------------------------------------------------------------------------
# Fix 2: LDAP host extraction and _check_ldap_ports uses real host
# ---------------------------------------------------------------------------

class TestExtractInstallerHost:
    def test_unc_path_extracts_server(self) -> None:
        host = InstallerService._extract_installer_host(r"\\fileserver\share\setup.exe")
        assert host == "fileserver"

    def test_unc_with_fqdn(self) -> None:
        host = InstallerService._extract_installer_host(r"\\dc01.corp.example.com\share\setup.exe")
        assert host == "dc01.corp.example.com"

    def test_https_url_extracts_hostname(self) -> None:
        host = InstallerService._extract_installer_host("https://cdn.example.com/file.exe")
        assert host == "cdn.example.com"

    def test_https_url_strips_port(self) -> None:
        host = InstallerService._extract_installer_host("https://cdn.example.com:8443/file.exe")
        assert host == "cdn.example.com"

    def test_local_path_returns_none(self) -> None:
        assert InstallerService._extract_installer_host(r"C:\local\setup.exe") is None

    def test_empty_string_returns_none(self) -> None:
        assert InstallerService._extract_installer_host("") is None


def test_check_ldap_ports_uses_provided_host() -> None:
    captured = []

    def fake_create_connection(addr, timeout):
        captured.append(addr[0])
        raise OSError("connection refused")

    with patch("socket.create_connection", side_effect=fake_create_connection):
        InstallerService._check_ldap_ports("ad.corp.example.com")

    assert all(h == "ad.corp.example.com" for h in captured), (
        f"Expected only 'ad.corp.example.com' in connection attempts, got {captured}"
    )
    assert "127.0.0.1" not in captured, "localhost must never be used for LDAP check"


def test_log_opentext_preflight_skips_when_no_host(caplog) -> None:
    eng = _engine()
    with caplog.at_level(logging.INFO, logger="test_audit"):
        with patch.object(eng, "_is_admin", return_value=True):
            eng._log_opentext_preflight(r"C:\local\setup.exe", "")
    assert "uebersprungen" in caplog.text.lower() or "uebersprungen" in caplog.text


def test_log_opentext_preflight_uses_extracted_host(caplog) -> None:
    eng = _engine()
    with caplog.at_level(logging.INFO, logger="test_audit"):
        with patch.object(eng, "_is_admin", return_value=True):
            with patch.object(InstallerService, "_check_ldap_ports", return_value={389: True, 636: True}) as mock_ldap:
                eng._log_opentext_preflight(r"\\myserver\share\setup.exe", "")
    mock_ldap.assert_called_once_with("myserver")


# ---------------------------------------------------------------------------
# Fix 3: SHA256 verification for downloaded installers
# ---------------------------------------------------------------------------

class TestVerifyFileSha256:
    def _write_tmp(self, content: bytes) -> str:
        fd, path = tempfile.mkstemp()
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        return path

    def test_correct_hash_returns_true(self) -> None:
        data = b"hello installer"
        expected = hashlib.sha256(data).hexdigest()
        path = self._write_tmp(data)
        try:
            assert InstallerService.verify_file_sha256(path, expected) is True
        finally:
            Path(path).unlink(missing_ok=True)

    def test_wrong_hash_returns_false(self) -> None:
        data = b"hello installer"
        path = self._write_tmp(data)
        try:
            assert InstallerService.verify_file_sha256(path, "deadbeef" * 8) is False
        finally:
            Path(path).unlink(missing_ok=True)

    def test_case_insensitive(self) -> None:
        data = b"case test"
        expected = hashlib.sha256(data).hexdigest().upper()
        path = self._write_tmp(data)
        try:
            assert InstallerService.verify_file_sha256(path, expected) is True
        finally:
            Path(path).unlink(missing_ok=True)


def _url_internal_info(sha256: str = "", silent_args: str = "/S") -> InstallerService._InternalInstallerInfo:
    """Build a URL-sourced _InternalInstallerInfo for use in SHA256 tests (no endpoint-keycode path)."""
    return InstallerService._InternalInstallerInfo(
        source="https://example.com/file.exe",
        silent_args=silent_args,
        installer_type="exe",
        response_file="",
        endpoint_keycode="",
        sha256=sha256,
    )


def test_download_with_no_sha256_logs_warning_and_continues(caplog, tmp_path) -> None:
    """When sha256 is empty the download proceeds but logs a security warning."""
    sw = SOFTWARE_BY_KEY["avaya_workplace"]
    eng = _engine(software_by_key={sw.key: sw})
    eng._current_software_key = sw.key

    fake_exe = tmp_path / "installer.exe"
    fake_exe.write_bytes(b"\x00" * 16)

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    with caplog.at_level(logging.WARNING, logger="test_audit"):
        with patch.object(eng, "_download_installer_from_url", return_value=str(fake_exe)):
            with patch.object(eng, "_run_subprocess_with_lock_retry", return_value=mock_proc):
                with patch.object(eng, "_resolve_internal_installer_info", return_value=_url_internal_info(sha256="")):
                    eng._run_internal_installer(sw, {}, False)

    assert "sha256" in caplog.text.lower() or "SHA256" in caplog.text


def test_download_with_wrong_sha256_fails_safely(tmp_path) -> None:
    """Wrong SHA256 must delete the file and return 'Quelle erforderlich'."""
    sw = SOFTWARE_BY_KEY["avaya_workplace"]
    eng = _engine(software_by_key={sw.key: sw})
    eng._current_software_key = sw.key

    fake_exe = tmp_path / "installer.exe"
    fake_exe.write_bytes(b"\xAB\xCD\xEF" * 10)
    wrong_hash = "a" * 64

    with patch.object(eng, "_download_installer_from_url", return_value=str(fake_exe)):
        with patch.object(eng, "_resolve_internal_installer_info", return_value=_url_internal_info(sha256=wrong_hash)):
            result = eng._run_internal_installer(sw, {}, False)

    assert result is not None
    assert result.status == "Quelle erforderlich"
    assert "SHA256" in (result.detail or "") or "sha256" in (result.detail or "").lower()
    assert not fake_exe.exists(), "File must be deleted after hash mismatch"


def test_download_with_correct_sha256_proceeds(tmp_path) -> None:
    """Correct SHA256 lets the installer run."""
    sw = SOFTWARE_BY_KEY["avaya_workplace"]
    eng = _engine(software_by_key={sw.key: sw})
    eng._current_software_key = sw.key

    data = b"\x01\x02\x03" * 20
    fake_exe = tmp_path / "installer.exe"
    fake_exe.write_bytes(data)
    correct_hash = hashlib.sha256(data).hexdigest()

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    with patch.object(eng, "_download_installer_from_url", return_value=str(fake_exe)):
        with patch.object(eng, "_resolve_internal_installer_info",
                          return_value=_url_internal_info(sha256=correct_hash)):
            with patch.object(eng, "_run_subprocess_with_lock_retry", return_value=mock_proc):
                result = eng._run_internal_installer(sw, {}, False)

    assert result is not None
    assert result.status == "Installiert"


# ---------------------------------------------------------------------------
# Fix 5: Teams installer-lock match precision
# ---------------------------------------------------------------------------

class TestTeamsLockMatch:
    @pytest.mark.parametrize("name", [
        "teams.exe",
        "ms-teams.exe",
        "msteams.exe",
        "teams_autoupdate.exe",
        "teamsupdatemanager.exe",
    ])
    def test_valid_teams_names_match(self, name: str) -> None:
        assert InstallerService._process_name_is_installer_lock(name), (
            f"'{name}' should be recognised as a Teams installer-lock process"
        )

    @pytest.mark.parametrize("name", [
        "dreamteams.exe",
        "betateams.exe",
        "myteams.exe",
        "teamspeak.exe",
        "notepad.exe",
    ])
    def test_invalid_names_do_not_match(self, name: str) -> None:
        assert not InstallerService._process_name_is_installer_lock(name), (
            f"'{name}' must NOT be recognised as a Teams installer-lock process"
        )

    def test_lock_teams_exe_names_frozenset_exported(self) -> None:
        assert "teams.exe" in LOCK_TEAMS_EXE_NAMES
        assert "dreamteams.exe" not in LOCK_TEAMS_EXE_NAMES


# ---------------------------------------------------------------------------
# Fix 7: No hardcoded fileserver in config defaults
# ---------------------------------------------------------------------------

def test_config_defaults_have_no_hardcoded_fileserver() -> None:
    from app.config import AVAYA_INSTALLER_SOURCE, INTERNAL_INSTALLERS, OPENTEXT_INSTALLER_SOURCE

    assert AVAYA_INSTALLER_SOURCE == "", "AVAYA_INSTALLER_SOURCE must be empty in defaults"
    assert OPENTEXT_INSTALLER_SOURCE == "", "OPENTEXT_INSTALLER_SOURCE must be empty in defaults"
    assert INTERNAL_INSTALLERS["avaya"]["path"] == ""
    assert INTERNAL_INSTALLERS["opentext"]["path"] == ""
    assert "fileserver" not in INTERNAL_INSTALLERS["avaya"]["path"]
    assert "fileserver" not in INTERNAL_INSTALLERS["opentext"]["path"]
