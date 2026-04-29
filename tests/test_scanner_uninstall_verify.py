"""Tests for strict registry / post-uninstall verification helpers."""

from pathlib import Path

from app.scanner import SoftwareScanner


def test_registry_entry_rejects_missing_uninstall_exe() -> None:
    entry = {
        "display_name": "Ghost App",
        "uninstall_string": r"C:\ThisPathShouldNotExist987654321\uninstall.exe /S",
        "quiet_uninstall_string": "",
        "install_location": "",
        "display_icon": "",
    }
    assert SoftwareScanner.registry_entry_signals_real_install(entry) is False


def test_registry_entry_accepts_msiexec_without_target_file() -> None:
    entry = {
        "display_name": "MSI Product",
        "uninstall_string": "MsiExec.exe /X{00000000-0000-0000-0000-000000000001}",
        "quiet_uninstall_string": "",
        "install_location": "",
        "display_icon": "",
    }
    assert SoftwareScanner.registry_entry_signals_real_install(entry) is True


def test_registry_entry_accepts_existing_install_location(tmp_path: Path) -> None:
    loc = tmp_path / "app"
    loc.mkdir()
    entry = {
        "display_name": "Local App",
        "uninstall_string": "",
        "quiet_uninstall_string": "",
        "install_location": str(loc),
        "display_icon": "",
    }
    assert SoftwareScanner.registry_entry_signals_real_install(entry) is True
