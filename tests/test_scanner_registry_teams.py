"""Registry display matching: Microsoft Teams vs TeamSpeak (no substring false positives)."""

from app.config import SOFTWARE_BY_KEY
from app.scanner import SoftwareScanner


def test_microsoft_teams_does_not_match_teamspeak_display_name() -> None:
    teams = SOFTWARE_BY_KEY["microsoft_teams"]
    assert SoftwareScanner._registry_display_matches(teams, "TeamSpeak 3 Client") is False
    assert SoftwareScanner._registry_display_matches(teams, "TeamSpeak Client 64-bit") is False


def test_microsoft_teams_matches_expected_arp_names() -> None:
    teams = SOFTWARE_BY_KEY["microsoft_teams"]
    assert SoftwareScanner._registry_display_matches(teams, "Microsoft Teams") is True
    assert SoftwareScanner._registry_display_matches(teams, "Microsoft Teams (work or school)") is True
    assert SoftwareScanner._registry_display_matches(teams, "Microsoft Teams machine-wide Installer") is True
    assert SoftwareScanner._registry_display_matches(teams, "MS Teams") is True


def test_teamspeak_matches_teamspeak_only() -> None:
    ts = SOFTWARE_BY_KEY["teamspeak"]
    assert SoftwareScanner._registry_display_matches(ts, "TeamSpeak 3 Client") is True
    assert SoftwareScanner._registry_display_matches(ts, "TeamSpeak Client") is True
    assert SoftwareScanner._registry_display_matches(ts, "Microsoft Teams") is False


def test_other_products_use_word_boundary_for_single_token() -> None:
    tv = SOFTWARE_BY_KEY["teamviewer"]
    assert SoftwareScanner._registry_display_matches(tv, "TeamViewer 15 Host") is True
    assert SoftwareScanner._registry_display_matches(tv, "TeamSpeak 3 Client") is False
