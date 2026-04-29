import queue

from app.models import ReportEntry
from app.residue_cleanup import (
    _classify_after_dialog,
    row_eligible_for_residue_phase,
    run_residue_cleanup_phase,
)


def test_row_eligible_requires_ok_absent() -> None:
    row = ReportEntry(
        "firefox",
        "",
        "Installiert",
        "Entfernen",
        "Nicht installiert",
        "OK",
        "",
        verification_status="absent",
    )
    assert row_eligible_for_residue_phase(row) is True


def test_row_not_eligible_already_absent() -> None:
    row = ReportEntry(
        "firefox",
        "",
        "Nicht installiert",
        "Keine Aktion erforderlich",
        "Nicht installiert",
        "OK",
        "",
        verification_status="already_absent",
    )
    assert row_eligible_for_residue_phase(row) is False


def test_classify_success_clean_no_residue() -> None:
    assert _classify_after_dialog(0, False, 0, 0, 0) == "success_clean"


def test_classify_manual_on_fail() -> None:
    assert _classify_after_dialog(3, False, 2, 1, 1) == "manual_cleanup_needed"


def test_run_residue_phase_no_config_passthrough() -> None:
    rows = [
        ReportEntry(
            "firefox",
            "",
            "Installiert",
            "Entfernen",
            "Nicht installiert",
            "OK",
            "",
            verification_status="absent",
        )
    ]
    out = run_residue_cleanup_phase(rows, {}, lambda k: k, __import__("logging").getLogger(__name__), queue.Queue())
    assert out[0] is rows[0]


def test_run_residue_phase_empty_scan_sets_success_clean() -> None:
    prov = {
        "firefox": {
            "cleanup": {
                "paths": ["{PROGRAMFILES}\\ZZZ_NoSuchResidueFolder987654321\\**"],
                "registry": [],
                "shortcuts": [],
            }
        }
    }
    row = ReportEntry(
        "firefox",
        "",
        "Installiert",
        "Entfernen",
        "Nicht installiert",
        "OK",
        "",
        verification_status="absent",
    )
    out = run_residue_cleanup_phase([row], prov, lambda k: "Firefox", __import__("logging").getLogger(__name__), queue.Queue())
    assert len(out) == 1
    assert out[0].cleanup_classification == "success_clean"
    assert out[0].cleanup_items_found == "0"
