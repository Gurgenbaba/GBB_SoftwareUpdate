from __future__ import annotations

import csv
import getpass
import html
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
                    "Uninstall Methode",
                    "Uninstall Versuche JSON",
                    "Manuell Grund",
                    "verification_status",
                    "verification_evidence",
                    "stale_evidence_ignored",
                    "cleanup_items_found",
                    "cleanup_items_removed",
                    "cleanup_classification",
                    "extended_metadata",
                ]
            )
            report_ts = datetime.now().isoformat(timespec="seconds")
            for item in entries:
                software = SOFTWARE_BY_KEY.get(item.software_key)
                writer.writerow(
                    [
                        report_ts,
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
                        getattr(item, "uninstall_method", "") or "",
                        getattr(item, "uninstall_attempts_json", "") or "",
                        getattr(item, "manual_reason", "") or "",
                        getattr(item, "verification_status", "") or "",
                        getattr(item, "verification_evidence", "") or "",
                        getattr(item, "stale_evidence_ignored", "") or "",
                        getattr(item, "cleanup_items_found", "") or "",
                        getattr(item, "cleanup_items_removed", "") or "",
                        getattr(item, "cleanup_classification", "") or "",
                        getattr(item, "extended_metadata", "") or "",
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
                    uninstall_method="",
                    uninstall_attempts_json="",
                    manual_reason="",
                    extended_metadata="",
                )
            )
        return entries


def _html_badge(text: str, status: str) -> str:
    """Return a colored <span> badge for a status value."""
    s = status.lower()
    if s in ("installiert", "aktuell", "ok"):
        bg, fg = "#14532D", "#BBF7D0"
    elif s in ("fehler", "quelle erforderlich"):
        bg, fg = "#7F1D1D", "#FECACA"
    elif s in ("update verfuegbar", "hinweis", "warnung"):
        bg, fg = "#78350F", "#FDE68A"
    elif s == "prueft":
        bg, fg = "#1E3A8A", "#BFDBFE"
    elif s in ("nicht installiert", "offen", "nicht geprueft"):
        bg, fg = "#334155", "#E2E8F0"
    else:
        bg, fg = "#2F455C", "#CBD5E1"
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
        f'font-size:11px;font-weight:600;background:{bg};color:{fg}">'
        f'{html.escape(text)}</span>'
    )


def write_html_report(entries: list[ReportEntry], report_type: str, title_line: str) -> Path:
    """UTF-8 HTML summary — corporate dark theme."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stamp_readable = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    path = REPORT_DIR / f"{report_type}_{stamp}.html"

    ok_n = sum(1 for e in entries if e.status_after in ("Installiert", "Aktuell"))
    warn_n = sum(1 for e in entries if e.status_after == "Update verfuegbar")
    err_n = sum(1 for e in entries if "Fehler" in e.status_after or e.status_after == "Quelle erforderlich")
    reboot_n = sum(1 for e in entries if str(e.reboot_required).lower() in ("ja", "yes", "true", "1"))

    summary_cards = (
        f'<div class="card"><div class="card-num" style="color:#BBF7D0">{ok_n}</div><div class="card-lbl">OK / Aktuell</div></div>'
        f'<div class="card"><div class="card-num" style="color:#FDE68A">{warn_n}</div><div class="card-lbl">Hinweise</div></div>'
        f'<div class="card"><div class="card-num" style="color:#FECACA">{err_n}</div><div class="card-lbl">Fehler</div></div>'
        f'<div class="card"><div class="card-num" style="color:#BFDBFE">{reboot_n}</div><div class="card-lbl">Neustart</div></div>'
    )

    rows_html: list[str] = []
    for item in entries:
        software = SOFTWARE_BY_KEY.get(item.software_key)
        name = software.display_name if software else item.software_key
        reboot_txt = "Ja" if str(item.reboot_required).lower() in ("ja", "yes", "true", "1") else "Nein"
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{html.escape(item.action)}</td>"
            f"<td>{_html_badge(item.status_after, item.status_after)}</td>"
            f"<td>{html.escape(item.result)}</td>"
            f"<td>{html.escape(item.provider)}</td>"
            f"<td>{html.escape(item.verification_status or '')}</td>"
            f"<td>{html.escape(item.cleanup_classification or '')}</td>"
            f"<td>{'⚠ Ja' if reboot_txt == 'Ja' else 'Nein'}</td>"
            "</tr>"
        )

    body = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<title>{html.escape(title_line)}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',Arial,sans-serif;background:#111827;color:#F8FAFC;padding:24px 32px;font-size:13px}}
  .header{{background:#163847;border:1px solid #2F455C;border-radius:8px;padding:18px 24px;margin-bottom:20px}}
  .header h1{{font-size:20px;font-weight:700;color:#D9A441;margin-bottom:4px}}
  .header p{{color:#CBD5E1;font-size:12px}}
  .cards{{display:flex;gap:12px;margin-bottom:20px}}
  .card{{background:#1B2836;border:1px solid #2F455C;border-radius:8px;padding:14px 20px;min-width:120px;text-align:center}}
  .card-num{{font-size:28px;font-weight:700;line-height:1}}
  .card-lbl{{font-size:11px;color:#CBD5E1;margin-top:4px}}
  table{{width:100%;border-collapse:collapse;background:#1B2836;border-radius:8px;overflow:hidden;border:1px solid #2F455C}}
  thead tr{{background:#223244}}
  th{{padding:10px 12px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#CBD5E1;font-weight:600;border-bottom:1px solid #2F455C}}
  td{{padding:9px 12px;border-bottom:1px solid #2F455C;color:#F8FAFC;vertical-align:middle}}
  tbody tr:nth-child(even){{background:#26384C}}
  tbody tr:hover{{background:#2F455C}}
</style>
</head>
<body>
<div class="header">
  <h1>{html.escape(title_line)}</h1>
  <p>{html.escape(stamp_readable)} &nbsp;·&nbsp; {html.escape(socket.gethostname())} &nbsp;·&nbsp; {html.escape(getpass.getuser())}</p>
</div>
<div class="cards">{summary_cards}</div>
<table>
  <thead><tr>
    <th>Software</th><th>Aktion</th><th>Status</th><th>Ergebnis</th><th>Provider</th><th>Verifikation</th><th>Bereinigung</th><th>Neustart</th>
  </tr></thead>
  <tbody>{"".join(rows_html)}</tbody>
</table>
</body></html>"""
    path.write_text(body, encoding="utf-8")
    return path
