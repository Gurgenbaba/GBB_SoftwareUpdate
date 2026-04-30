"""Normalize raw ReportEntry rows for UI summaries and dialogs (CSV stays raw)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .config import SOFTWARE_BY_KEY
from .models import ReportEntry

Severity = Literal["OK", "WARNING", "ERROR"]

# WinGet / Chocolatey / common: not a hard failure for the user.
_ALREADY_INSTALLED_PHRASES = (
    "already installed",
    "package is already installed",
    "a newer version is already installed",
    "ein upgrade ist nicht erforderlich",
    "bereits installiert",
    "bereits vorhanden",
    "no applicable upgrade found",
    "no newer package versions are available",
    "no upgrades available",
    "nothing to upgrade",
    "nothing to install",
    "es wurde kein paket gefunden, das alle angegebenen kriterien erfüllt",
)

_ALREADY_CURRENT_PHRASES = (
    "no applicable upgrade",
    "no upgrades available",
    "nothing to upgrade",
    "already up to date",
    "bereits auf dem neuesten stand",
    "is the latest version",
    "die neueste version ist installiert",
    "0 packages installed",  # choco: nothing to do
    "0 packages upgraded",
    "no packages updated",
)


@dataclass(frozen=True)
class NormalizedReportRow:
    """UI-facing interpretation of one report row (does not alter CSV)."""

    severity: Severity
    substatus: str
    """Short German label: e.g. Bereits aktuell, Quelle erforderlich."""
    user_detail: str
    """One line for completion dialog lists."""


def _merged_lower(entry: ReportEntry) -> str:
    parts = [
        entry.error_message or "",
        entry.manual_reason or "",
        entry.status_after or "",
        entry.action or "",
        entry.result or "",
    ]
    return "\n".join(parts).lower()


def _mentions(text_lower: str, phrases: tuple[str, ...]) -> bool:
    return any(p in text_lower for p in phrases)


def normalize_report_entry(entry: ReportEntry) -> NormalizedReportRow:
    """Map raw provider / engine fields to OK | WARNING | ERROR + German substatus."""
    raw_res = (entry.result or "").strip()
    if raw_res == "OK" and "cleanup_choco_ghost=true" in (getattr(entry, "extended_metadata", "") or ""):
        return NormalizedReportRow("OK", "", "Erfolgreich entfernt – veralteter Eintrag ignoriert")
    st_after = (entry.status_after or "").strip()
    action = (entry.action or "").strip()
    merged = _merged_lower(entry)

    if st_after == "Quelle erforderlich":
        return NormalizedReportRow(
            "WARNING",
            "Quelle erforderlich",
            entry.error_message or "Keine gültige Installationsquelle gefunden.",
        )

    em_meta = getattr(entry, "extended_metadata", "") or ""
    if raw_res == "Manuell" or "manuelle pruefung" in merged or "manuelle prüfung" in merged or "manuelle pruefung noetig" in merged:
        detail = (entry.manual_reason or entry.error_message or "Manuelle Prüfung erforderlich.").strip()[:400]
        if "office_removal_guidance=true" in em_meta:
            return NormalizedReportRow(
                "WARNING",
                "Hinweis — Manuelle Prüfung nötig",
                detail,
            )
        return NormalizedReportRow(
            "WARNING",
            "Manuelle Prüfung nötig",
            detail,
        )

    if raw_res == "OK" and action == "Keine Aktion erforderlich":
        return NormalizedReportRow("OK", "Bereits aktuell", entry.error_message or "")

    if raw_res == "OK":
        return NormalizedReportRow("OK", "", (entry.error_message or "").strip()[:400] or "OK")

    if raw_res == "DRY-RUN":
        return NormalizedReportRow("OK", "", entry.error_message or "Dry-Run")

    # Raw Fehler — may be benign (already there / noop).
    if raw_res == "Fehler":
        if "office_removal_guidance=true" in em_meta:
            return NormalizedReportRow(
                "WARNING",
                "Hinweis — Manuelle Prüfung nötig",
                (entry.manual_reason or entry.error_message or "Manuelle Prüfung erforderlich.").strip()[:400],
            )
        if _mentions(merged, _ALREADY_INSTALLED_PHRASES) and ("install" in action.lower() or "interner" in action.lower()):
            return NormalizedReportRow(
                "OK",
                "Bereits installiert",
                entry.error_message or "Paket ist bereits installiert.",
            )
        if _mentions(merged, _ALREADY_CURRENT_PHRASES) or _mentions(merged, _ALREADY_INSTALLED_PHRASES):
            if "upgrade" in action.lower() or "update" in action.lower():
                return NormalizedReportRow(
                    "OK",
                    "Bereits aktuell",
                    entry.error_message or "Kein Update erforderlich.",
                )
            if "install" in action.lower():
                return NormalizedReportRow(
                    "OK",
                    "Bereits installiert",
                    entry.error_message or "Paket ist bereits installiert.",
                )
        return NormalizedReportRow(
            "ERROR",
            "",
            (entry.error_message or "Unbekannter Fehler.").strip()[:500],
        )

    return NormalizedReportRow("OK", "", entry.error_message or raw_res)


def any_reboot_required(entries: list[ReportEntry]) -> bool:
    return any((e.reboot_required or "no").strip().lower() == "yes" for e in entries)


def format_install_summary_lines(
    rows: list[ReportEntry],
    *,
    dry_run: bool,
    mandatory: bool,
    operation: str,
) -> list[str]:
    """User-facing summary lines (German)."""
    if dry_run:
        return _format_dry_run_summary(rows, operation)
    if operation == "remove":
        return _format_remove_summary(rows)
    return _format_install_update_summary(rows, mandatory=mandatory)


def _format_install_update_summary(rows: list[ReportEntry], *, mandatory: bool) -> list[str]:
    norms = [normalize_report_entry(r) for r in rows]
    updated = 0
    already_ok = 0
    warn_n = 0
    err_n = 0
    for r, nv in zip(rows, norms):
        if nv.severity == "ERROR":
            err_n += 1
        elif nv.severity == "WARNING":
            warn_n += 1
        elif nv.severity == "OK":
            if nv.substatus in ("Bereits aktuell", "Bereits installiert"):
                already_ok += 1
            elif r.result == "OK" and r.action == "Keine Aktion erforderlich":
                already_ok += 1
            elif r.result == "OK" and r.action in ("Upgrade", "Install", "Interner Installer"):
                updated += 1
            elif r.result == "Fehler":
                already_ok += 1
            else:
                already_ok += 1

    reboot = any_reboot_required(rows)
    head = "Pflichtsoftware-Lauf abgeschlossen" if mandatory else "Zusammenfassung"
    lines = [
        head,
        f"- Aktualisiert / installiert: {updated}",
        f"- Bereits aktuell bzw. installiert: {already_ok}",
        f"- Hinweise: {warn_n}",
        f"- Fehler: {err_n}",
    ]
    lines.append("- Neustart empfohlen" if reboot else "- Neustart: nicht angezeigt")
    return lines


def _format_remove_summary(rows: list[ReportEntry]) -> list[str]:
    norms = [normalize_report_entry(r) for r in rows]
    removed_ok = 0
    absent = 0
    warn_n = 0
    err_n = 0
    for r, nv in zip(rows, norms):
        if nv.severity == "ERROR":
            err_n += 1
        elif nv.severity == "WARNING":
            warn_n += 1
        elif r.result == "OK":
            if r.action == "Keine Aktion erforderlich":
                absent += 1
            else:
                removed_ok += 1
        else:
            err_n += 1
    reboot = any_reboot_required(rows)
    return [
        "Zusammenfassung (Entfernen)",
        f"- Entfernt: {removed_ok}",
        f"- War bereits nicht installiert: {absent}",
        f"- Hinweise (manuell / Prüfung): {warn_n}",
        f"- Fehler: {err_n}",
        "- Neustart empfohlen" if reboot else "- Neustart: nicht angezeigt",
    ]


def _format_dry_run_summary(rows: list[ReportEntry], operation: str) -> list[str]:
    norms = [normalize_report_entry(r) for r in rows]
    sim = noop = src = warn = err = 0
    removed = 0
    for r, nv in zip(rows, norms):
        sa = r.status_after or ""
        act_l = (r.action or "").lower()
        if sa == "Quelle erforderlich" or nv.substatus == "Quelle erforderlich":
            src += 1
        elif r.result == "DRY-RUN":
            sim += 1
        elif nv.severity == "ERROR":
            err += 1
        elif nv.severity == "WARNING":
            warn += 1
        elif "entfernen" in act_l:
            removed += 1
        elif r.action == "Keine Aktion erforderlich":
            noop += 1
        else:
            noop += 1
    reboot = any_reboot_required(rows)
    label = "Installation" if operation == "install" else "Entfernen"
    return [
        f"Zusammenfassung (Dry-Run — {label})",
        f"- Simulierte Aktionen: {sim}",
        f"- Simulierte Entfernungen: {removed}",
        f"- Ohne Änderung: {noop}",
        f"- Quelle erforderlich: {src}",
        f"- Hinweise: {warn}",
        f"- Fehler: {err}",
        "- Neustart empfohlen" if reboot else "- Neustart: nicht angezeigt",
    ]


def format_scan_summary_lines(rows: list[ReportEntry]) -> list[str]:
    n = len(rows)
    norms = [normalize_report_entry(r) for r in rows]
    upd = sum(1 for r in rows if "Update" in (r.status_after or ""))
    miss = sum(1 for r in rows if (r.status_after or "") == "Nicht installiert")
    cur = sum(
        1
        for r in rows
        if (r.status_after or "") in ("Aktuell", "Installiert")
        and "Update" not in (r.status_after or "")
    )
    err_n = sum(1 for nv in norms if nv.severity == "ERROR")
    warn_n = sum(1 for nv in norms if nv.severity == "WARNING")
    manual = sum(1 for nv in norms if "Manuelle" in nv.substatus)
    lines = [
        f"Geprüft: {n} Programme",
        f"- Update verfügbar: {upd}",
        f"- Nicht installiert: {miss}",
        f"- Aktuell / installiert: {cur}",
        f"- Hinweise: {warn_n}",
        f"- Fehler: {err_n}",
    ]
    if manual:
        lines.append(f"- Manuelle Prüfung nötig: {manual}")
    return lines


def dialog_detail_lines(entries: list[ReportEntry], *, limit: int = 120) -> list[str]:
    """Lines for completion dialog (normalized, user-friendly)."""
    lines: list[str] = []
    for r in entries[:limit]:
        nv = normalize_report_entry(r)
        sev = "OK" if nv.severity == "OK" else ("Hinweis" if nv.severity == "WARNING" else "Fehler")
        sub = f" — {nv.substatus}" if nv.substatus else ""
        sw = SOFTWARE_BY_KEY.get(r.software_key)
        nm = sw.display_name if sw else r.software_key
        lines.append(f"- {nm}: {r.action} → {sev}{sub}")
        if nv.user_detail:
            lines.append(f"    {nv.user_detail}")
    return lines
