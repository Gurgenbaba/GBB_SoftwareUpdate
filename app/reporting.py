from __future__ import annotations

import csv
import getpass
import importlib
import re
import socket
from datetime import datetime
from pathlib import Path

from .config import REPORT_DIR, SOFTWARE_BY_KEY
from .models import ReportEntry, SoftwareState


def pdf_export_available() -> bool:
    return importlib.util.find_spec("fpdf") is not None


def _pdf_ascii(text: str) -> str:
    """Map common German characters for core PDF fonts (Helvetica)."""
    t = text.replace("\u00df", "ss")
    t = t.replace("\u00e4", "ae").replace("\u00c4", "Ae")
    t = t.replace("\u00f6", "oe").replace("\u00d6", "Oe")
    t = t.replace("\u00fc", "ue").replace("\u00dc", "Ue")
    t = re.sub(r"[^\x09\x0a\x0d\x20-\x7e]", "?", t)
    return t


def write_summary_pdf(entries: list[ReportEntry], report_type: str, title_line: str) -> Path | None:
    """Write a compact PDF summary next to CSV naming convention. Returns None if fpdf2 is missing."""
    try:
        fpdf_mod = importlib.import_module("fpdf")
        FPDF = getattr(fpdf_mod, "FPDF")
    except ImportError:
        return None

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"{report_type}_{timestamp}.pdf"

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 12)
    pdf.multi_cell(0, 7, _pdf_ascii(title_line))
    pdf.set_font("Helvetica", size=9)
    meta = f"Host: {socket.gethostname()}  User: {getpass.getuser()}  {datetime.now().isoformat(timespec='seconds')}"
    pdf.multi_cell(0, 5, _pdf_ascii(meta))
    pdf.ln(2)
    pdf.set_font("Helvetica", size=8)
    for item in entries:
        software = SOFTWARE_BY_KEY.get(item.software_key)
        name = software.display_name if software else item.software_key
        line = (
            f"{name}  |  {item.action}  |  {item.result}\n"
            f"  vorher: {item.status_before}  ->  nachher: {item.status_after}"
        )
        pdf.multi_cell(0, 5, _pdf_ascii(line))
        if item.error_message:
            pdf.set_font("Helvetica", "I", 7)
            pdf.multi_cell(0, 4, _pdf_ascii(f"  ({item.error_message})"))
            pdf.set_font("Helvetica", size=8)
        pdf.ln(1)

    pdf.output(str(report_path))
    return report_path


class ReportWriter:
    def __init__(self, logger) -> None:
        self.logger = logger

    def write_report(self, entries: list[ReportEntry], report_type: str) -> Path:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = REPORT_DIR / f"{report_type}_{timestamp}.csv"

        with report_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(
                [
                    "Datum",
                    "Hostname",
                    "Benutzer",
                    "Software",
                    "Paketname",
                    "Status vorher",
                    "Aktion",
                    "Status nachher",
                    "Ergebnis",
                    "Fehlermeldung",
                    "Neustart erforderlich",
                    "Provider",
                    "Installer Pfad",
                    "Installer SHA256",
                ]
            )
            for item in entries:
                software = SOFTWARE_BY_KEY.get(item.software_key)
                writer.writerow(
                    [
                        datetime.now().isoformat(timespec="seconds"),
                        socket.gethostname(),
                        getpass.getuser(),
                        software.display_name if software else item.software_key,
                        item.package_name,
                        item.status_before,
                        item.action,
                        item.status_after,
                        item.result,
                        item.error_message,
                        item.reboot_required,
                        item.provider,
                        item.installer_path,
                        item.installer_sha256,
                    ]
                )
        self.logger.info("CSV-Report gespeichert: %s", report_path)
        return report_path

    @staticmethod
    def from_scan(states: dict[str, SoftwareState], selected_keys: list[str]) -> list[ReportEntry]:
        entries: list[ReportEntry] = []
        for key in selected_keys:
            state = states.get(key, SoftwareState(status="Unbekannt"))
            entries.append(
                ReportEntry(
                    software_key=key,
                    package_name=state.package_name or "",
                    status_before=state.status,
                    action="Scan",
                    status_after=state.status,
                    result="OK" if state.status != "Fehler" else "Fehler",
                    error_message=state.detail,
                    provider=state.provider or "",
                )
            )
        return entries
