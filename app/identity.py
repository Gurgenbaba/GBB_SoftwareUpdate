"""Strict per-app identity: word-boundary / phrase matching; optional negatives (Teams vs TeamSpeak)."""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import SoftwarePackage

# Applied when config has no non-empty ``identity`` for that key (backward compatible).
DEFAULT_IDENTITY_BY_KEY: dict[str, dict[str, Any]] = {
    "microsoft_teams": {
        "detect_names": ["microsoft-teams", "ms-teams", "msteams"],
        "registry_names": [
            "microsoft teams",
            "msteams",
            "ms teams",
            "teams machine-wide",
            "teams work or school",
        ],
        "negative_names": ["teamspeak", "teamviewer"],
        "winget_id": "",
        "choco_id": "",
        "cleanup_paths": [],
        "safe_processes": [],
    },
    "teamviewer": {
        "detect_names": ["teamviewer"],
        "registry_names": ["teamviewer"],
        "negative_names": ["teamspeak"],
        "winget_id": "",
        "choco_id": "",
        "cleanup_paths": [],
        "safe_processes": [],
    },
}


def _has_nonempty_identity(entry: dict[str, Any]) -> bool:
    raw = entry.get("identity")
    if not isinstance(raw, dict):
        return False
    for _k, v in raw.items():
        if isinstance(v, list) and any(str(x).strip() for x in v):
            return True
        if isinstance(v, str) and v.strip():
            return True
    return False


def apply_identity_defaults_to_providers(providers: dict[str, Any]) -> None:
    """Mutate ``software_providers`` with built-in strict identity where the user omitted ``identity``."""
    for key, ident in DEFAULT_IDENTITY_BY_KEY.items():
        entry = providers.get(key)
        if not isinstance(entry, dict):
            continue
        if _has_nonempty_identity(entry):
            continue
        entry["identity"] = copy.deepcopy(ident)


def _list_str_nonempty(val: Any) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(x).strip() for x in val if str(x).strip()]


def normalize_identity_block(prov: dict[str, Any]) -> dict[str, Any]:
    """Parse ``identity`` from a software_providers entry (may be empty)."""
    raw = prov.get("identity") if isinstance(prov, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    return {
        "detect_names": tuple(x.lower() for x in _list_str_nonempty(raw.get("detect_names"))),
        "registry_names": tuple(x.lower() for x in _list_str_nonempty(raw.get("registry_names"))),
        "negative_names": tuple(x.lower() for x in _list_str_nonempty(raw.get("negative_names"))),
        "winget_id": str(raw.get("winget_id", "") or "").strip(),
        "choco_id": str(raw.get("choco_id", "") or "").strip(),
        "cleanup_paths": _list_str_nonempty(raw.get("cleanup_paths")),
        "safe_processes": _list_str_nonempty(raw.get("safe_processes")),
    }


def term_matches_display(display_lower: str, term: str) -> bool:
    """Multi-word: phrase substring. Single token: word-boundary (hyphen counts as boundary)."""
    t = term.strip().lower()
    if not t:
        return False
    if " " in t:
        return t in display_lower
    return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", display_lower, flags=re.IGNORECASE) is not None


def negative_hit(text_lower: str, negatives: tuple[str, ...]) -> bool:
    for n in negatives:
        if term_matches_display(text_lower, n):
            return True
    return False


def token_in_choco_id(pkg_lower: str, token: str) -> bool:
    """Match token inside Chocolatey package id (e.g. ``teams`` in ``microsoft-teams``, not in ``teamspeak``)."""
    t = token.strip().lower()
    if not t:
        return False
    if " " in t:
        compact = t.replace(" ", "")
        return t in pkg_lower or compact in pkg_lower.replace("-", "") or t.replace(" ", "-") in pkg_lower
    return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", pkg_lower, flags=re.IGNORECASE) is not None


def detect_choco_with_identity(software: SoftwarePackage, local_packages: dict[str, str], prov: dict[str, Any]) -> str | None:
    """
    Return matched package id (lower) or None.
    ``None`` means caller should use legacy substring detection.
    """
    ident = normalize_identity_block(prov)
    strict = bool(ident["choco_id"] or ident["detect_names"])
    neg = ident["negative_names"]
    if strict:
        for pkg in local_packages:
            pl = pkg.lower()
            if negative_hit(pl, neg):
                continue
            if ident["choco_id"] and pl == ident["choco_id"].lower():
                return pl
            for d in ident["detect_names"]:
                if token_in_choco_id(pl, d):
                    return pl
        return None
    cand = _legacy_detect_choco_substring(software, local_packages)
    if cand and neg and negative_hit(cand, neg):
        return None
    return cand


def _legacy_detect_choco_substring(software: SoftwarePackage, local_packages: dict[str, str]) -> str | None:
    from .config import SOFTWARE_ALIASES

    if software.primary_package and software.primary_package.lower() in local_packages:
        return software.primary_package.lower()
    for term in software.search_terms:
        term_lower = term.lower()
        for pkg in local_packages:
            if term_lower in pkg:
                return pkg
    for alias in SOFTWARE_ALIASES.get(software.key, ()):
        alias_lower = alias.lower()
        for pkg in local_packages:
            if alias_lower in pkg:
                return pkg
    return None


def registry_match_with_identity(
    software: SoftwarePackage,
    display_lower: str,
    prov: dict[str, Any],
) -> bool | None:
    """
    ``True``/``False`` if ``identity`` controls registry matching; ``None`` = defer to legacy scanner logic.
    """
    ident = normalize_identity_block(prov)
    if ident["negative_names"] and negative_hit(display_lower, ident["negative_names"]):
        return False
    if ident["registry_names"]:
        return any(term_matches_display(display_lower, r) for r in ident["registry_names"])
    return None


def merge_identity_into_catalog_fields(raw: dict[str, Any], software: SoftwarePackage) -> tuple[str | None, str | None]:
    """Returns ``(primary_package, winget_id)`` after applying optional ``identity`` overrides."""
    if not isinstance(raw, dict):
        return software.primary_package, software.winget_id
    ident = normalize_identity_block(raw)
    choco = str(raw.get("choco_package", software.primary_package or "") or "").strip()
    if ident["choco_id"]:
        choco = ident["choco_id"].strip()
    winget = str(raw.get("winget_id", software.winget_id or "") or "").strip()
    if ident["winget_id"]:
        winget = ident["winget_id"].strip()
    return (choco or None, winget or None)
