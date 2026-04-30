"""Phase 1: strict identity (Teams vs TeamSpeak, TeamViewer, Chocolatey ids)."""

from app.config import SOFTWARE_BY_KEY
from app.identity import (
    apply_identity_defaults_to_providers,
    detect_choco_with_identity,
    registry_match_with_identity,
    term_matches_display,
)
from app.uninstall_engine import merge_uninstall_profile
from app.residue_cleanup import get_cleanup_config
from app.scanner import SoftwareScanner


def test_term_matches_display_word_boundary_teams_in_teamspeak() -> None:
    assert term_matches_display("teamspeak 3 client", "teams") is False
    assert term_matches_display("microsoft-teams", "teams") is True


def test_registry_identity_teams_negative_teamspeak() -> None:
    teams = SOFTWARE_BY_KEY["microsoft_teams"]
    prov = {
        "identity": {
            "registry_names": ["microsoft teams"],
            "negative_names": ["teamspeak"],
        }
    }
    assert registry_match_with_identity(teams, "teamspeak 3 client".lower(), prov) is False
    assert registry_match_with_identity(teams, "microsoft teams".lower(), prov) is True


def test_choco_identity_rejects_teamspeak_for_teams() -> None:
    teams = SOFTWARE_BY_KEY["microsoft_teams"]
    prov = {"identity": {"detect_names": ["microsoft-teams"], "negative_names": ["teamspeak"]}}
    local = {"teamspeak": "1.0", "microsoft-teams": "2.0"}
    assert detect_choco_with_identity(teams, local, prov) == "microsoft-teams"


def test_choco_identity_exact_choco_id() -> None:
    teams = SOFTWARE_BY_KEY["microsoft_teams"]
    prov = {"identity": {"choco_id": "MicrosoftTeams"}}
    local = {"microsoftteams": "1.2"}
    assert detect_choco_with_identity(teams, local, prov) == "microsoftteams"


def test_scanner_registry_with_providers_identity() -> None:
    teams = SOFTWARE_BY_KEY["microsoft_teams"]
    prov = {
        "microsoft_teams": {
            "identity": {
                "registry_names": ["microsoft teams"],
                "negative_names": ["teamspeak"],
            }
        }
    }
    assert SoftwareScanner._registry_display_matches(teams, "TeamSpeak Client", prov) is False
    assert SoftwareScanner._registry_display_matches(teams, "Microsoft Teams", prov) is True


def test_apply_identity_defaults_microsoft_teams() -> None:
    prov = {"microsoft_teams": {"enabled": True}}
    apply_identity_defaults_to_providers(prov)
    ident = prov["microsoft_teams"]["identity"]
    assert "teamspeak" in ident["negative_names"]


def test_merge_uninstall_profile_identity_registry_override() -> None:
    base = merge_uninstall_profile(
        "microsoft_teams",
        {
            "uninstall": {"registry_names": ["teams"]},
            "identity": {"registry_names": ["microsoft teams"], "safe_processes": ["extra.exe"]},
        },
    )
    assert "microsoft teams" in base["registry_names"]
    assert "extra.exe" in base["processes"]


def test_get_cleanup_config_merges_identity_paths() -> None:
    prov = {
        "x": {
            "cleanup": {"paths": [r"C:\_gbb_cleanup_a\**"], "registry": [], "shortcuts": []},
            "identity": {"cleanup_paths": [r"C:\_gbb_cleanup_b\**"]},
        }
    }
    cfg = get_cleanup_config(prov, "x")
    assert any("b" in p for p in cfg["paths"])
    assert any("a" in p for p in cfg["paths"])
