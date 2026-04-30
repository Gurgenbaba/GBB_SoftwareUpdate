"""Enterprise extensions: ghost cleanup, provider order, reboot gate contract, dry-run summary."""

import logging
from unittest.mock import MagicMock

import pytest

from app.enterprise import (
    apply_choco_ghost_cleanup_after_uninstall,
    parse_install_mode,
    parse_provider_priority,
    sanitize_public_detail,
)
from app.models import ReportEntry
from app.result_normalization import format_install_summary_lines


@pytest.mark.parametrize(
    ("dry_run", "reboot_pending", "expect_gate"),
    [
        (True, True, False),
        (False, False, False),
        (False, True, True),
    ],
)
def test_reboot_gate_only_when_prod_and_pending(dry_run: bool, reboot_pending: bool, expect_gate: bool) -> None:
    """Mirrors UI condition before blocking reboot dialog."""
    assert (not dry_run and reboot_pending) is expect_gate


def test_parse_provider_priority_custom_order() -> None:
    cfg = {"provider_priority": ["winget", "choco", "internal"]}
    p = parse_provider_priority(cfg, software_key="firefox", prefer_local=False)
    assert p == ("winget", "choco", "internal")


def test_parse_provider_priority_dedupes() -> None:
    cfg = {"provider_priority": ["choco", "choco", "winget"]}
    p = parse_provider_priority(cfg, software_key="x", prefer_local=False)
    assert p == ("choco", "winget")


def test_parse_install_mode_visible() -> None:
    assert parse_install_mode({"install_mode": "VISIBLE"}) == "visible"


def test_sanitize_public_detail_choco_ghost_heuristic() -> None:
    d = sanitize_public_detail(
        "Chocolatey failed RC=1",
        result_ok=True,
        verification_absent=True,
        cleanup_choco_ghost=False,
    )
    assert "veralteter" in d


def test_apply_choco_ghost_cleanup_sets_metadata() -> None:
    row = ReportEntry(
        software_key="firefox",
        package_name="firefox",
        status_before="Installiert",
        action="Entfernen",
        status_after="Nicht installiert",
        result="OK",
        error_message="",
        verification_status="absent",
    )
    choco = MagicMock()
    choco.is_choco_installed.return_value = True
    choco.list_local.return_value = {"firefox"}
    choco.uninstall_remove_metadata_only.return_value = MagicMock(ok=True)
    out = apply_choco_ghost_cleanup_after_uninstall([row], choco, logging.getLogger(__name__))
    assert len(out) == 1
    assert "cleanup_choco_ghost=true" in (out[0].extended_metadata or "")
    choco.uninstall_remove_metadata_only.assert_called_once_with("firefox")


def test_apply_choco_ghost_skipped_when_not_in_choco_list() -> None:
    row = ReportEntry(
        software_key="firefox",
        package_name="firefox",
        status_before="Installiert",
        action="Entfernen",
        status_after="Nicht installiert",
        result="OK",
        error_message="",
        verification_status="absent",
    )
    choco = MagicMock()
    choco.is_choco_installed.return_value = True
    choco.list_local.return_value = set()
    out = apply_choco_ghost_cleanup_after_uninstall([row], choco, logging.getLogger(__name__))
    assert out[0] is row
    choco.uninstall_remove_metadata_only.assert_not_called()


def test_dry_run_summary_mentions_simulated() -> None:
    rows = [
        ReportEntry(
            "firefox",
            "Mozilla.Firefox",
            "Nicht installiert",
            "Install",
            "Nicht installiert",
            "DRY-RUN",
            "DRY-RUN: wuerde installieren",
            "no",
            "Choco",
        )
    ]
    lines = format_install_summary_lines(rows, dry_run=True, mandatory=False, operation="install")
    assert any("Simulierte" in ln for ln in lines)
