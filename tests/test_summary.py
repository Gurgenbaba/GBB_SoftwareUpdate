from app.models import ReportEntry
from app.ui import UpdaterApp


def test_install_summary_contains_categories() -> None:
    rows = [
        ReportEntry("firefox", "", "Nicht installiert", "Install", "Installiert", "OK", ""),
        ReportEntry("filezilla", "", "Aktuell", "Keine Aktion erforderlich", "Aktuell", "OK", ""),
    ]
    text = UpdaterApp._install_summary_message(rows, dry_run=False)
    assert "installiert" in text.lower()
