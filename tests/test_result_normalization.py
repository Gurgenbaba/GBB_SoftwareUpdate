"""Result normalization for UI summaries (CSV remains raw)."""

from app.models import ReportEntry
from app.result_normalization import (
    any_reboot_required,
    format_install_summary_lines,
    normalize_report_entry,
)


def test_winget_no_upgrade_classifies_as_ok_already_current() -> None:
    r = ReportEntry(
        software_key="firefox",
        package_name="Mozilla.Firefox",
        status_before="Update verfuegbar",
        action="Upgrade",
        status_after="Fehler",
        result="Fehler",
        error_message="No applicable upgrade found.",
        reboot_required="no",
        provider="WinGet",
    )
    nv = normalize_report_entry(r)
    assert nv.severity == "OK"
    assert nv.substatus == "Bereits aktuell"


def test_cleanup_choco_ghost_metadata_friendlier_detail() -> None:
    r = ReportEntry(
        software_key="firefox",
        package_name="firefox",
        status_before="Installiert",
        action="Entfernen",
        status_after="Nicht installiert",
        result="OK",
        error_message="Chocolatey failed RC=1",
        reboot_required="no",
        provider="Choco",
        extended_metadata="cleanup_choco_ghost=true",
    )
    nv = normalize_report_entry(r)
    assert nv.severity == "OK"
    assert "veralteter" in nv.user_detail.lower()


def test_office_removal_guidance_manuell_is_hinweis_not_error() -> None:
    r = ReportEntry(
        software_key="office365business",
        package_name="",
        status_before="Installiert",
        action="Manuelle Deinstallation",
        status_after="Fehler: Manuelle Deinstallation nötig",
        result="Manuell",
        error_message="",
        manual_reason="Kein erweitertes Office-Removal-Tool gefunden.",
        extended_metadata="office_removal_guidance=true",
    )
    nv = normalize_report_entry(r)
    assert nv.severity == "WARNING"
    assert "Hinweis" in nv.substatus
    lines = format_install_summary_lines([r], dry_run=False, mandatory=False, operation="remove")
    assert any("Hinweise" in ln for ln in lines)
    assert any("Fehler: 0" in ln for ln in lines)


def test_office_removal_guidance_fehler_row_downgraded_to_warning() -> None:
    r = ReportEntry(
        software_key="office365business",
        package_name="",
        status_before="Installiert",
        action="Entfernen",
        status_after="Fehler: Deinstallation fehlgeschlagen",
        result="Fehler",
        error_message="WinGet failed",
        manual_reason="",
        extended_metadata="office_removal_guidance=true",
    )
    nv = normalize_report_entry(r)
    assert nv.severity == "WARNING"
    assert "Hinweis" in nv.substatus


def test_quelle_erforderlich_is_warning() -> None:
    r = ReportEntry(
        software_key="x",
        package_name="x",
        status_before="Nicht installiert",
        action="Install",
        status_after="Quelle erforderlich",
        result="Fehler",
        error_message="Keine gueltige Quelle",
        reboot_required="no",
        provider="Quelle erforderlich",
    )
    nv = normalize_report_entry(r)
    assert nv.severity == "WARNING"
    assert nv.substatus == "Quelle erforderlich"


def test_reboot_any_not_counted_in_summary_text() -> None:
    rows = [
        ReportEntry("a", "p", "Aktuell", "Upgrade", "Aktuell", "OK", "", "yes", "Choco"),
        ReportEntry("b", "p", "Aktuell", "Upgrade", "Aktuell", "OK", "", "yes", "Choco"),
    ]
    assert any_reboot_required(rows) is True
    lines = format_install_summary_lines(rows, dry_run=False, mandatory=False, operation="install")
    assert any("Neustart empfohlen" in ln for ln in lines)
    assert not any(ln.startswith("- ") and "2" in ln and "Neustart" in ln for ln in lines)


def test_install_summary_counts_benign_fehler_as_already_ok() -> None:
    rows = [
        ReportEntry(
            "firefox",
            "Mozilla.Firefox",
            "Update verfuegbar",
            "Upgrade",
            "Fehler",
            "Fehler",
            "No applicable upgrade found.",
            "no",
            "WinGet",
        ),
        ReportEntry(
            "sevenzip",
            "7zip",
            "Nicht installiert",
            "Install",
            "Installiert",
            "OK",
            "",
            "no",
            "Choco",
        ),
    ]
    text = "\n".join(format_install_summary_lines(rows, dry_run=False, mandatory=False, operation="install"))
    assert "Bereits aktuell" in text or "1" in text
    assert "Fehler: 0" in text
