"""Microsoft 365 removal: Get Help, ODT (generated XML), SaRA opt-in, dry-run, engine wiring."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import SOFTWARE_BY_KEY
from app.office_uninstall import (
    build_odt_remove_xml,
    office_exit_ok,
    office_removal_tools_available,
    office_tools_configured,
    odt_display_level_from_install_mode,
    run_office_removal_strategies,
    stop_safe_office_processes,
    try_get_help_office_scrub,
    try_odt_remove,
    try_sara_remove,
)
from app.uninstall_engine import MultiStageUninstallEngine


@pytest.fixture
def odt_files(tmp_path: Path) -> tuple[str, str]:
    setup = tmp_path / "setup.exe"
    xml = tmp_path / "remove.xml"
    setup.write_bytes(b"0")
    xml.write_text("<Configuration/>", encoding="utf-8")
    return str(setup), str(xml)


def test_office_exit_ok_reboot_codes() -> None:
    assert office_exit_ok(0)[0] is True
    assert office_exit_ok(3010)[1] is True
    assert office_exit_ok(999)[0] is False


def test_odt_display_level_from_install_mode() -> None:
    assert odt_display_level_from_install_mode("silent") == "None"
    assert odt_display_level_from_install_mode("visible") == "Full"


def test_build_odt_remove_xml_contains_remove_all() -> None:
    xml = build_odt_remove_xml(display_level="Full")
    assert "Remove" in xml and "All" in xml and "FORCEAPPSHUTDOWN" in xml


def test_try_odt_remove_dry_run_no_subprocess(odt_files: tuple[str, str]) -> None:
    log = MagicMock()
    ok, det, rc, cfg = try_odt_remove(
        odt_setup_path=odt_files[0],
        odt_remove_config_path=odt_files[1],
        logger=log,
        dry_run=True,
    )
    assert ok is True
    assert rc is None
    assert "DRY-RUN" in det
    assert cfg


def test_try_odt_remove_generates_temp_xml(tmp_path: Path) -> None:
    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"x")
    log = MagicMock()
    ran: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        ran.append(list(cmd))
        m = MagicMock()
        m.returncode = 0
        m.stdout = m.stderr = ""
        return m

    ok, _det, rc, cfg = try_odt_remove(
        odt_setup_path=str(setup),
        odt_remove_config_path="",
        logger=log,
        dry_run=False,
        display_level="None",
        run_cmd=fake_run,
    )
    assert ok and rc == 0
    assert ran and "configure" in ran[0][1].lower() or "/configure" in " ".join(ran[0]).lower()
    assert Path(cfg).suffix == ".xml"


def test_try_get_help_dry_run_no_subprocess(tmp_path: Path) -> None:
    exe = tmp_path / "GetHelpCmd.exe"
    exe.write_bytes(b"1")
    log = MagicMock()
    ok, det, rc, _cfg = try_get_help_office_scrub(
        get_help_exe=exe,
        get_help_args=["OfficeScrubScenario"],
        logger=log,
        dry_run=True,
    )
    assert ok and rc is None and "DRY-RUN" in det


def test_get_help_preferred_in_chain(tmp_path: Path) -> None:
    gh = tmp_path / "GetHelpCmd.exe"
    gh.write_bytes(b"1")
    log = MagicMock()
    ran: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        ran.append(list(cmd))
        m = MagicMock()
        m.returncode = 0
        m.stdout = m.stderr = ""
        return m

    scan = MagicMock()
    v = MagicMock()
    v.still_present = True
    v.verification_status = "present"
    scan.post_uninstall_verify.return_value = v

    def add_att(*_a, **_k) -> None:
        pass

    run_office_removal_strategies(
        office_tools={
            "prefer": ["get_help", "odt"],
            "get_help_cmd_path": str(gh),
            "get_help_args": ["OfficeScrubScenario"],
            "odt_setup_path": "",
            "odt_remove_config_path": "",
            "sara_path": "",
            "sara_args": "",
        },
        logger=log,
        dry_run=False,
        add_att=add_att,
        scanner=scan,
        software=SOFTWARE_BY_KEY["office365business"],
        install_mode="visible",
        run_cmd=fake_run,
    )
    assert ran and Path(ran[0][0]).name.lower() == "gethelpcmd.exe"
    assert "OfficeScrubScenario" in ran[0]


def test_sara_only_when_path_configured_logs_deprecation(tmp_path: Path) -> None:
    sara = tmp_path / "SaraCmd.exe"
    sara.write_bytes(b"1")
    log = MagicMock()

    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 0
        m.stdout = m.stderr = ""
        return m

    scan = MagicMock()
    v = MagicMock()
    v.still_present = True
    v.verification_status = "present"
    scan.post_uninstall_verify.return_value = v

    def add_att(*_a, **_k) -> None:
        pass

    run_office_removal_strategies(
        office_tools={
            "prefer": ["sara"],
            "get_help_cmd_path": "",
            "odt_setup_path": "",
            "sara_path": str(sara),
            "sara_args": ["-silent"],
        },
        logger=log,
        dry_run=False,
        add_att=add_att,
        scanner=scan,
        software=SOFTWARE_BY_KEY["office365business"],
        install_mode="auto",
        run_cmd=fake_run,
    )
    log.warning.assert_called()


def test_office_removal_tools_available_false_without_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr("app.office_uninstall.resolve_get_help_exe", lambda _ot, _lg: None)
    assert office_removal_tools_available({}, log) is False


def test_office_removal_tools_available_true_with_odt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log = MagicMock()
    monkeypatch.setattr("app.office_uninstall.resolve_get_help_exe", lambda _ot, _lg: None)
    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"x")
    assert office_removal_tools_available({"odt_setup_path": str(setup)}, log) is True


@patch("app.uninstall_engine.office_removal_tools_available", return_value=False)
def test_office365_no_tools_without_ack_returns_manual(_mock_avail: MagicMock) -> None:
    log = MagicMock()
    choco = MagicMock()
    winget = MagicMock()
    winget.is_available.return_value = False
    scan = MagicMock()
    office = SOFTWARE_BY_KEY["office365business"]
    prev = MagicMock()
    prev.status = "Installiert"
    prev.package_name = "Office365Business"
    prev.provider = "Choco"

    eng = MultiStageUninstallEngine(
        choco,
        winget,
        log,
        SOFTWARE_BY_KEY,
        scanner=scan,
        office_tools={},
    )
    r = eng.execute(office, prev, dry_run=False, office_removal_generic_ack=False)
    assert r.status == "manual_required"
    assert r.office_removal_guidance is True
    assert "Kein erweitertes Office-Removal-Tool" in r.manual_reason


def test_office_tools_configured_requires_explicit_get_help(tmp_path: Path) -> None:
    log = MagicMock()
    assert office_tools_configured({}, logger=log) is False
    gh = tmp_path / "GetHelpCmd.exe"
    gh.write_bytes(b"1")
    assert office_tools_configured({"get_help_cmd_path": str(gh)}, logger=log) is True


def test_stop_safe_office_dry_run_no_taskkill() -> None:
    log = MagicMock()
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(str(cmd))
        return MagicMock(returncode=0)

    n = stop_safe_office_processes(logger=log, dry_run=True, kill_click_to_run_service=False, run_cmd=fake_run)
    assert n == 0
    assert not calls


def test_office365_dry_run_records_office_metadata() -> None:
    log = MagicMock()
    choco = MagicMock()
    winget = MagicMock()
    winget.is_available.return_value = False
    scan = MagicMock()
    office = SOFTWARE_BY_KEY["office365business"]
    prev = MagicMock()
    prev.status = "Installiert"
    prev.package_name = "x"
    prev.provider = "Choco"

    eng = MultiStageUninstallEngine(
        choco,
        winget,
        log,
        SOFTWARE_BY_KEY,
        scanner=scan,
        software_providers={"office365business": {"install_mode": "silent"}},
        office_tools={"prefer": ["odt"], "odt_setup_path": "", "odt_remove_config_path": ""},
    )
    r = eng.execute(office, prev, dry_run=True)
    assert r.status == "skipped"
    methods = [a.method for a in r.attempts]
    assert any("Office" in m or m == "dry_run" for m in methods)


def test_try_sara_remove_accepts_arg_list(tmp_path: Path) -> None:
    sara = tmp_path / "SaraCmd.exe"
    sara.write_bytes(b"1")
    log = MagicMock()
    ok, det, rc, _cfg = try_sara_remove(sara_path=str(sara), sara_args=["-a", "b"], logger=log, dry_run=True)
    assert ok and "DRY-RUN" in det
