from __future__ import annotations

import json
import logging
import shutil
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .identity import apply_identity_defaults_to_providers, merge_identity_into_catalog_fields
from .config import (
    APP_DIR,
    BUNDLE_DIR,
    INSTALLER_SETTLE_WAIT_SECONDS,
    INSTALLER_VERIFY_AFTER_TIMEOUT,
    INSTALLER_VERIFY_POLL_INTERVAL_SECONDS,
    INSTALLER_VERIFY_POLL_MAX_SECONDS,
    INTERNAL_INSTALLERS,
    SOFTWARE_CATALOG,
    SoftwarePackage,
)

CONFIG_JSON_PATH = APP_DIR / "config.json"
CONFIG_EXAMPLE_PATH = APP_DIR / "config.example.json"

DEFAULT_EXAMPLE_CONFIG: dict[str, Any] = {
    "company_name": "GBB Beispiel GmbH",
    "enabled_standard_software": [
        "citrix_workspace",
        "adobe_reader",
        "teamviewer",
        "microsoft_teams",
        "office365business",
        "opentext",
        "opentext_core_endpoint",
        "avaya_workplace",
        "filezilla",
        "firefox",
    ],
    "internal_installers": {
        "avaya": {
            "path": r"\\fileserver\software\Avaya\AvayaWorkplaceSetup.exe",
            "silent_args": "/S",
            "type": "auto",
            "response_file": "",
            "display_name": "Avaya Workplace",
        },
        "opentext": {
            "path": r"\\fileserver\software\OpenText\OpenTextSetup.exe",
            "silent_args": "/quiet /norestart",
            "type": "auto",
            "response_file": "",
            "display_name": "OpenText",
        },
        "opentext_endpoint": {
            "path": "https://anywhere.webrootcloudav.com/zerol/wsasme.exe",
            "silent_args": "",
            "type": "exe",
            "response_file": "",
            "display_name": "OpenText Core Endpoint Protection",
            "endpoint_keycode": "",
        },
    },
    "software_providers": {},
    "local_source": {
        "last_path": "",
        "prefer_local": True,
    },
    "chocolatey_source": {
        "name": "chocolatey",
        "url": "https://community.chocolatey.org/api/v2/",
    },
    "enable_backup": True,
    "dry_run_default": False,
    "office_tools": {
        "get_help_cmd_path": "",
        "get_help_args": ["OfficeScrubScenario"],
        "odt_setup_path": "",
        "odt_remove_config_path": "",
        "sara_path": "",
        "sara_args": "",
        "prefer": ["get_help", "odt", "sara", "generic"],
        "kill_click_to_run_service": False,
    },
    "installer_behavior": {
        "settle_wait_seconds": 60,
        "verify_after_timeout": True,
        "verify_poll_interval_seconds": 10,
        "verify_poll_max_seconds": 180,
    },
    "system_settings": {
        "display_timeout_seconds": 30,
        "sleep_timeout_minutes": 3,
        "power_profile": "balanced",
    },
    "ui_columns": {
        "checkbox": 44,
        "program": 280,
        "status": 120,
        "provider": 140,
        "installed": 120,
        "available": 120,
        "progress": 160,
    },
    "battery_health": {
        "desired_charge_limit_percent": 80,
        "allow_vendor_tools": False,
    },
}


@dataclass
class ChocolateySourceConfig:
    name: str = ""
    url: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url.strip())


@dataclass
class RuntimeSettings:
    company_name: str
    internal_installers: dict[str, dict[str, Any]]
    enabled_software_keys: frozenset[str] | None
    chocolatey_source: ChocolateySourceConfig | None
    visible_catalog: tuple[Any, ...]
    software_providers: dict[str, Any]
    office_tools: dict[str, Any]
    local_source_last_path: str
    local_source_prefer_local: bool
    enable_backup: bool = True
    dry_run_default: bool = False
    installer_settle_wait_seconds: int = INSTALLER_SETTLE_WAIT_SECONDS
    installer_verify_after_timeout: bool = INSTALLER_VERIFY_AFTER_TIMEOUT
    installer_verify_poll_interval_seconds: int = INSTALLER_VERIFY_POLL_INTERVAL_SECONDS
    installer_verify_poll_max_seconds: int = INSTALLER_VERIFY_POLL_MAX_SECONDS


def default_software_providers() -> dict[str, Any]:
    providers: dict[str, Any] = {}
    for s in SOFTWARE_CATALOG:
        providers[s.key] = {
            "enabled": True,
            "display_name": s.display_name,
            "choco_package": s.primary_package or "",
            "winget_id": s.winget_id or "",
            "internal_installer": {
                "path": s.installer_source or "",
                "silent_args": "",
                "type": "auto",
                "response_file": "",
                **({"endpoint_keycode": ""} if s.key == "opentext_core_endpoint" else {}),
            },
            "search_terms": list(s.search_terms),
            "local_patterns": [],
        }
    return providers


def get_config_dict(logger: logging.Logger | None = None) -> dict[str, Any]:
    ensure_config_json_exists(logger)
    try:
        data = json.loads(CONFIG_JSON_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError) as exc:
        if logger:
            logger.warning("config.json konnte nicht als dict geladen werden (%s).", exc)
    return dict(DEFAULT_EXAMPLE_CONFIG)


def save_config_dict(data: dict[str, Any], logger: logging.Logger | None = None) -> None:
    CONFIG_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_JSON_PATH.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = CONFIG_JSON_PATH.with_suffix(f".json.bak_{stamp}")
        shutil.copy2(CONFIG_JSON_PATH, backup)
        if logger:
            logger.info("config.json Backup erstellt: %s", backup)
    CONFIG_JSON_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if logger:
        logger.info("config.json gespeichert: %s", CONFIG_JSON_PATH)


def ensure_config_json_exists(logger: logging.Logger | None = None) -> None:
    if CONFIG_JSON_PATH.exists():
        return
    bundled_candidates = (
        BUNDLE_DIR / "config.json",
        BUNDLE_DIR / "config.example.json",
        BUNDLE_DIR / "_internal" / "config.json",
        BUNDLE_DIR / "_internal" / "config.example.json",
    )
    for bundled_path in bundled_candidates:
        if bundled_path.exists():
            CONFIG_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundled_path, CONFIG_JSON_PATH)
            if logger:
                logger.info("config.json aus Bundle uebernommen: %s", bundled_path)
            return
    CONFIG_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_EXAMPLE_PATH.exists():
        CONFIG_EXAMPLE_PATH.write_text(
            json.dumps({**DEFAULT_EXAMPLE_CONFIG, "software_providers": default_software_providers()}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    CONFIG_JSON_PATH.write_text(
        json.dumps({**DEFAULT_EXAMPLE_CONFIG, "software_providers": default_software_providers()}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    msg = "config.json fehlte – Beispielkonfiguration wurde angelegt: %s"
    if logger:
        logger.info(msg, CONFIG_JSON_PATH)
    else:
        logging.getLogger("gbb_updater").info(msg, str(CONFIG_JSON_PATH))


def _parse_chocolatey_source(raw: Any) -> ChocolateySourceConfig | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        url = raw.strip()
        if not url:
            return None
        return ChocolateySourceConfig(name="", url=url)
    if isinstance(raw, dict):
        name = str(raw.get("name", "") or "").strip()
        url = str(raw.get("url", "") or "").strip()
        if not url:
            return None
        return ChocolateySourceConfig(name=name, url=url)
    return None


def _merge_internal_installers(overrides: Any) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {k: deepcopy(v) for k, v in INTERNAL_INSTALLERS.items()}
    if not isinstance(overrides, dict):
        return merged
    for key, data in overrides.items():
        if not isinstance(data, dict):
            continue
        if key not in merged:
            merged[key] = deepcopy(data)
            continue
        for field in ("path", "silent_args", "type", "response_file", "display_name", "endpoint_keycode"):
            if field in data and data[field] is not None:
                merged[key][field] = data[field]
    return merged


def _parse_enabled_keys(raw: Any) -> frozenset[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    if len(raw) == 0:
        return None
    valid = {s.key for s in SOFTWARE_CATALOG}
    keys = [str(x).strip() for x in raw if str(x).strip()]
    selected = frozenset(k for k in keys if k in valid)
    if not selected:
        return None
    return selected


def _parse_office_tools(raw: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "get_help_cmd_path": "",
        "get_help_args": ["OfficeScrubScenario"],
        "odt_setup_path": "",
        "odt_remove_config_path": "",
        "sara_path": "",
        "sara_args": "",
        "prefer": ["get_help", "odt", "sara", "generic"],
        "kill_click_to_run_service": False,
    }
    if not isinstance(raw, dict):
        return defaults.copy()
    pref = raw.get("prefer")
    prefer_l = list(defaults["prefer"])
    if isinstance(pref, list) and pref:
        prefer_l = [str(x).strip().lower() for x in pref if str(x).strip()]
    gh_args = raw.get("get_help_args", defaults["get_help_args"])
    if isinstance(gh_args, list):
        gh_list = [str(x).strip() for x in gh_args if str(x).strip()]
    else:
        gh_list = [str(gh_args).strip()] if str(gh_args or "").strip() else list(defaults["get_help_args"])
    if not gh_list:
        gh_list = list(defaults["get_help_args"])
    sara_raw = raw.get("sara_args", "")
    if isinstance(sara_raw, list):
        sara_norm = " ".join(str(x).strip() for x in sara_raw if str(x).strip())
    else:
        sara_norm = str(sara_raw or "").strip()
    return {
        "get_help_cmd_path": str(raw.get("get_help_cmd_path", "") or "").strip(),
        "get_help_args": gh_list,
        "odt_setup_path": str(raw.get("odt_setup_path", "") or "").strip(),
        "odt_remove_config_path": str(raw.get("odt_remove_config_path", "") or "").strip(),
        "sara_path": str(raw.get("sara_path", "") or "").strip(),
        "sara_args": sara_norm,
        "prefer": prefer_l,
        "kill_click_to_run_service": bool(raw.get("kill_click_to_run_service", False)),
    }


def load_runtime_settings(logger: logging.Logger | None = None) -> RuntimeSettings:
    data = get_config_dict(logger)
    company_name = str(data.get("company_name", "") or "").strip()
    internal_overrides: Any = data.get("internal_installers")
    enabled_raw: Any = data.get("enabled_standard_software")
    choco_src_raw: Any = data.get("chocolatey_source")
    software_providers = data.get("software_providers")
    if not isinstance(software_providers, dict) or not software_providers:
        software_providers = default_software_providers()
    apply_identity_defaults_to_providers(software_providers)
    local_source = data.get("local_source", {})
    if not isinstance(local_source, dict):
        local_source = {}
    local_source_last_path = str(local_source.get("last_path", "") or "").strip()
    local_source_prefer_local = bool(local_source.get("prefer_local", True))
    enable_backup = bool(data.get("enable_backup", True))
    dry_run_default = bool(data.get("dry_run_default", False))
    office_tools = _parse_office_tools(data.get("office_tools"))

    settle_wait = INSTALLER_SETTLE_WAIT_SECONDS
    verify_after_timeout = INSTALLER_VERIFY_AFTER_TIMEOUT
    poll_interval = INSTALLER_VERIFY_POLL_INTERVAL_SECONDS
    poll_max = INSTALLER_VERIFY_POLL_MAX_SECONDS
    ib_raw: Any = data.get("installer_behavior")
    if isinstance(ib_raw, dict):
        try:
            settle_wait = int(ib_raw.get("settle_wait_seconds", settle_wait))
        except (TypeError, ValueError):
            settle_wait = INSTALLER_SETTLE_WAIT_SECONDS
        verify_after_timeout = bool(ib_raw.get("verify_after_timeout", verify_after_timeout))
        try:
            poll_interval = int(ib_raw.get("verify_poll_interval_seconds", poll_interval))
        except (TypeError, ValueError):
            poll_interval = INSTALLER_VERIFY_POLL_INTERVAL_SECONDS
        try:
            poll_max = int(ib_raw.get("verify_poll_max_seconds", poll_max))
        except (TypeError, ValueError):
            poll_max = INSTALLER_VERIFY_POLL_MAX_SECONDS
    settle_wait = max(0, min(settle_wait, 600))
    poll_interval = max(1, min(poll_interval, 120))
    poll_max = max(0, min(poll_max, 3600))

    merged_installers = _merge_internal_installers(internal_overrides)
    enabled = _parse_enabled_keys(enabled_raw)
    choco_source = _parse_chocolatey_source(choco_src_raw)

    customized: list[SoftwarePackage] = []
    for software in SOFTWARE_CATALOG:
        raw = software_providers.get(software.key, {})
        if not isinstance(raw, dict):
            raw = {}
        if "local_patterns" not in raw:
            raw["local_patterns"] = []
            software_providers[software.key] = raw
        internal = raw.get("internal_installer", {})
        if not isinstance(internal, dict):
            internal = {}
        terms = raw.get("search_terms", list(software.search_terms))
        term_tuple = tuple(str(x).strip() for x in terms if str(x).strip()) if isinstance(terms, list) else software.search_terms
        pp_merged, wg_merged = merge_identity_into_catalog_fields(raw, software)
        customized.append(
            replace(
                software,
                display_name=str(raw.get("display_name", software.display_name) or software.display_name),
                primary_package=pp_merged,
                winget_id=wg_merged,
                installer_source=(str(internal.get("path", software.installer_source or "")).strip() or None),
                search_terms=term_tuple or software.search_terms,
            )
        )
    custom_catalog = tuple(customized)

    if enabled is None:
        visible = custom_catalog
    else:
        visible = tuple(s for s in custom_catalog if s.key in enabled)

    if len(visible) == 0:
        if logger:
            logger.warning("enabled_standard_software ergibt leeren Katalog, verwende Standardkatalog (Fallback).")
        visible = tuple(custom_catalog)
        enabled = None

    return RuntimeSettings(
        company_name=company_name,
        internal_installers=merged_installers,
        enabled_software_keys=enabled,
        chocolatey_source=choco_source,
        visible_catalog=visible,
        software_providers=software_providers,
        office_tools=office_tools,
        local_source_last_path=local_source_last_path,
        local_source_prefer_local=local_source_prefer_local,
        enable_backup=enable_backup,
        dry_run_default=dry_run_default,
        installer_settle_wait_seconds=settle_wait,
        installer_verify_after_timeout=verify_after_timeout,
        installer_verify_poll_interval_seconds=poll_interval,
        installer_verify_poll_max_seconds=poll_max,
    )


def update_provider_resolved_source(
    software_key: str,
    *,
    choco_package: str | None = None,
    winget_id: str | None = None,
    logger: logging.Logger | None = None,
) -> None:
    if not choco_package and not winget_id:
        return
    cfg = get_config_dict(logger)
    providers = cfg.get("software_providers")
    if not isinstance(providers, dict):
        providers = default_software_providers()
    raw = providers.get(software_key)
    if not isinstance(raw, dict):
        raw = {}
    resolved = raw.get("resolved")
    if not isinstance(resolved, dict):
        resolved = {}
    if choco_package:
        resolved["choco_package"] = choco_package
    if winget_id:
        resolved["winget_id"] = winget_id
    resolved["last_verified"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    raw["resolved"] = resolved
    providers[software_key] = raw
    cfg["software_providers"] = providers
    save_config_dict(cfg, logger)
