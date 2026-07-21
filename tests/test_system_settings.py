from __future__ import annotations

from app.system_settings import (
    DEFAULT_SYSTEM_SETTINGS,
    SettingState,
    UI_COLUMN_KEYS,
    coerce_system_settings,
    format_energy_status_rows,
    merge_system_settings,
    merge_ui_columns,
    read_battery_charge_limit_capability,
)


def test_merge_system_settings_defaults() -> None:
    m = merge_system_settings(None)
    assert m["display_timeout_seconds"] == DEFAULT_SYSTEM_SETTINGS["display_timeout_seconds"]
    assert m["dark_mode_enabled"] == DEFAULT_SYSTEM_SETTINGS["dark_mode_enabled"]
    assert m["ac_power_button_action"] == "sleep"
    m2 = merge_system_settings({"display_timeout_seconds": 120, "unknown": 1})
    assert m2["display_timeout_seconds"] == 120
    assert "unknown" not in m2


def test_coerce_system_settings_full_config() -> None:
    cfg = {"company_name": "X", "system_settings": {"power_profile": "high_performance"}}
    c = coerce_system_settings(cfg)
    assert c["power_profile"] == "high_performance"


def test_merge_ui_columns_clamps_and_fills() -> None:
    raw = {"program": 40, "status": 5000, "progress": 10}
    m = merge_ui_columns(raw)
    assert m["program"] == 120
    assert m["status"] == 2000
    assert m["progress"] == 80
    for k in UI_COLUMN_KEYS:
        assert k in m


def test_format_energy_status_rows_maps_pill_kinds() -> None:
    rows = [
        ("dark_mode", SettingState("AN")),
        ("screensaver_disabled", SettingState("NEIN")),
        ("adaptive_brightness", SettingState("Nicht verfügbar", available=False)),
    ]
    out = format_energy_status_rows(rows)
    assert out[0][2] == "on"
    assert out[1][2] == "off"
    assert out[2][2] == "na"


def test_battery_charge_limit_guidance_when_unsupported() -> None:
    cap = read_battery_charge_limit_capability({"battery_health": {"desired_charge_limit_percent": 80, "allow_vendor_tools": False}})
    if cap.available:
        # Environment-dependent on Windows dev machines with vendor tool installed.
        assert cap.mode == "vendor_tool_found"
    else:
        assert cap.mode == "not_available"
        assert "Nicht verfügbar" in cap.guidance
