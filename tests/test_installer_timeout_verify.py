"""Post-timeout install verification (polling scanner, single retry)."""

import logging
from unittest.mock import MagicMock, patch

from app.choco import CommandResult
from app.config import SOFTWARE_BY_KEY
from app.installers import InstallerService
from app.models import SoftwareState


def _engine(*, scanner=None, poll_interval=5, poll_max=30, verify=True):
    return InstallerService(
        MagicMock(),
        MagicMock(),
        logging.getLogger("test_itv"),
        software_by_key={"firefox": SOFTWARE_BY_KEY["firefox"]},
        scanner=scanner,
        installer_settle_wait_seconds=0,
        installer_verify_after_timeout=verify,
        installer_verify_poll_interval_seconds=poll_interval,
        installer_verify_poll_max_seconds=poll_max,
    )


def test_poll_success_when_scan_shows_installed() -> None:
    scanner = MagicMock()
    scanner.scan.side_effect = [
        {"firefox": SoftwareState("Fehler", package_name="firefox", provider="Chocolatey")},
        {"firefox": SoftwareState("Installiert", package_name="firefox", provider="Chocolatey")},
    ]
    eng = _engine(scanner=scanner, poll_interval=5, poll_max=60)
    eng._current_software_key = "firefox"
    initial = CommandResult("choco upgrade firefox -y", 1, "", "timed out", timed_out=True)

    m = [0.0]

    def monotonic() -> float:
        return m[0]

    def sleep(s: float) -> None:
        m[0] += float(s)

    with patch("time.monotonic", monotonic), patch("time.sleep", sleep):
        with patch.object(eng, "_installer_lock_active", return_value=True):
            out = eng._poll_install_completion("chocolatey", lambda: CommandResult("x", 1, "", ""), initial)

    assert out is not None
    assert out.ok
    assert out.verified_after_timeout
    assert "Installation erfolgreich verifiziert." in (out.stdout or "")
    assert scanner.scan.call_count >= 2


def test_poll_recovery_exhausted_when_lock_remains_until_deadline() -> None:
    scanner = MagicMock()
    bad = SoftwareState("Fehler", package_name="firefox", provider="Chocolatey")
    scanner.scan.return_value = {"firefox": bad}
    eng = _engine(scanner=scanner, poll_interval=5, poll_max=15)
    eng._current_software_key = "firefox"
    initial = CommandResult("choco upgrade firefox -y", 1, "", "timed out", timed_out=True)

    m = [0.0]

    def monotonic() -> float:
        return m[0]

    def sleep(s: float) -> None:
        m[0] += float(s)

    with patch("time.monotonic", monotonic), patch("time.sleep", sleep):
        with patch.object(eng, "_installer_lock_active", return_value=True):
            out = eng._poll_install_completion("chocolatey", lambda: CommandResult("x", 1, "", ""), initial)

    assert not out.ok
    assert getattr(out, "recovery_exhausted", False)
    assert out.timed_out


def test_single_retry_when_lock_cleared_then_retry_fails() -> None:
    scanner = MagicMock()
    bad = SoftwareState("Fehler", package_name="firefox", provider="Chocolatey")
    scanner.scan.return_value = {"firefox": bad}
    eng = _engine(scanner=scanner, poll_interval=5, poll_max=60)
    eng._current_software_key = "firefox"
    initial = CommandResult("choco upgrade firefox -y", 1, "", "timed out", timed_out=True)
    retry = CommandResult("choco upgrade firefox -y", 1, "", "choco failed")

    m = [0.0]

    def monotonic() -> float:
        return m[0]

    def sleep(s: float) -> None:
        m[0] += float(s)

    with patch("time.monotonic", monotonic), patch("time.sleep", sleep):
        with patch.object(eng, "_installer_lock_active", return_value=False):
            out = eng._poll_install_completion("chocolatey", lambda: retry, initial)

    assert not out.ok
    assert getattr(out, "recovery_exhausted", False)


def test_verify_disabled_falls_back_to_legacy_lock_loop() -> None:
    choco = MagicMock()
    choco.upgrade.return_value = CommandResult("c", 1, "", "not a lock", timed_out=False)
    eng = InstallerService(
        choco,
        MagicMock(),
        logging.getLogger("test_itv_legacy"),
        software_by_key={"firefox": SOFTWARE_BY_KEY["firefox"]},
        scanner=MagicMock(),
        installer_verify_after_timeout=False,
    )
    eng._current_software_key = "firefox"
    # _is_installer_lock_result falls back to tasklist; avoid real process scan / 30s sleeps.
    with patch.object(eng, "_has_installer_lock_processes", return_value=False):
        out = eng._run_choco_with_lock_retry("upgrade", "firefox", native_ui=False)
    assert out.returncode == 1
    choco.upgrade.assert_called_once()


def test_is_installer_lock_ignores_timed_out() -> None:
    eng = InstallerService(MagicMock(), MagicMock(), logging.getLogger("test_itv3"), software_by_key=SOFTWARE_BY_KEY)
    assert eng._is_installer_lock_result(1, "", "", timed_out=True) is False


def test_combine_error_prefers_stdout_on_verified() -> None:
    r = CommandResult("c", 0, "Installation erfolgreich verifiziert.", "", verified_after_timeout=True)
    assert "verifiziert" in InstallerService._combine_error(r)


def test_combine_error_recovery_exhausted() -> None:
    r = CommandResult("c", 1, "", "Installer abgeschlossen, aber Programm nicht erkannt.", recovery_exhausted=True)
    assert "nicht erkannt" in InstallerService._combine_error(r)
