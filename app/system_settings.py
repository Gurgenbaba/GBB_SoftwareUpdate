from __future__ import annotations

import logging
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None


GUID_BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
GUID_HIGH = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"

DEFAULT_SYSTEM_SETTINGS: dict[str, Any] = {
    "display_timeout_seconds": 30,
    "sleep_timeout_minutes": 3,
    "power_profile": "balanced",
}

DEFAULT_UI_COLUMNS: dict[str, int] = {
    "checkbox": 44,
    "program": 280,
    "status": 120,
    "provider": 140,
    "installed": 120,
    "available": 120,
    "progress": 160,
}

UI_COLUMN_KEYS: tuple[str, ...] = ("checkbox", "program", "status", "provider", "installed", "available", "progress")

MIN_UI_COLUMNS: dict[str, int] = {
    "checkbox": 36,
    "program": 120,
    "status": 72,
    "provider": 72,
    "installed": 72,
    "available": 72,
    "progress": 80,
}


@dataclass
class ToggleReadResult:
    """Result of reading a single Windows-facing toggle."""

    available: bool
    on: bool
    hint: str = ""
    detail: str = ""


@dataclass
class SettingState:
    value: str
    available: bool = True
    hint: str = ""


@dataclass
class BatteryChargeCapability:
    available: bool
    vendor: str = ""
    mode: str = "not_available"
    guidance: str = ""
    can_apply: bool = False


def merge_system_settings(raw: Any) -> dict[str, Any]:
    out = dict(DEFAULT_SYSTEM_SETTINGS)
    if isinstance(raw, dict):
        for k, v in DEFAULT_SYSTEM_SETTINGS.items():
            if k in raw:
                out[k] = raw[k]
    return out


def coerce_system_settings(cfg: Any) -> dict[str, Any]:
    """Accept either the `system_settings` dict or a full config root dict."""
    if isinstance(cfg, dict) and "system_settings" in cfg and isinstance(cfg["system_settings"], dict):
        return merge_system_settings(cfg["system_settings"])
    return merge_system_settings(cfg)


def merge_ui_columns(raw: Any) -> dict[str, int]:
    out = {k: int(DEFAULT_UI_COLUMNS[k]) for k in UI_COLUMN_KEYS}
    if not isinstance(raw, dict):
        return out
    for key in UI_COLUMN_KEYS:
        if key not in raw:
            continue
        try:
            w = int(raw[key])
        except (TypeError, ValueError):
            continue
        lo = MIN_UI_COLUMNS.get(key, 40)
        out[key] = max(lo, min(w, 2000))
    return out


def _hidden_kwargs() -> dict:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    startupinfo = None
    if creationflags and hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    return {"creationflags": creationflags, "startupinfo": startupinfo}


def _run(cmd: list[str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        **_hidden_kwargs(),
    )


def active_power_scheme_guid(logger: logging.Logger | None = None) -> str | None:
    if platform.system() != "Windows":
        return None
    res = _run(["powercfg", "/getactivescheme"], timeout=30)
    text = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
    m = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        text,
        re.I,
    )
    if not m and logger:
        logger.debug("powercfg /getactivescheme: %s", text[:400])
    return m.group(1).lower() if m else None


def _norm_guid(g: str) -> str:
    return (g or "").strip().lower()


def _guid_matches(a: str, b: str) -> bool:
    return _norm_guid(a) == _norm_guid(b)


def read_dark_mode() -> ToggleReadResult:
    if winreg is None or platform.system() != "Windows":
        return ToggleReadResult(False, False, "Nur unter Windows mit Registry-Zugriff verfügbar.")
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            apps, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            sysu, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        apps_v = int(apps) if apps is not None else 1
        sys_v = int(sysu) if sysu is not None else 1
        # 0 = dark theme active, 1 = light
        dark_on = apps_v == 0 and sys_v == 0
        return ToggleReadResult(True, dark_on, detail=f"AppsUseLightTheme={apps_v}, SystemUsesLightTheme={sys_v}")
    except OSError as exc:
        return ToggleReadResult(False, False, f"Registry nicht lesbar: {exc}")


def apply_dark_mode(want_dark: bool, logger: logging.Logger | None = None) -> tuple[bool, str]:
    if winreg is None or platform.system() != "Windows":
        return False, "Nur unter Windows verfügbar."
    val = 0 if want_dark else 1
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, val)
            winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, val)
    except OSError as exc:
        if logger:
            logger.warning("Dark mode registry: %s", exc)
        return False, str(exc)
    # Best-effort refresh (ignore failures)
    _run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Add-Type @'\nusing System;\nusing System.Runtime.InteropServices;\n"
            "public class U { [DllImport(\"user32.dll\", SetLastError=true)] public static extern bool "
            "PostMessage(IntPtr h, uint m, IntPtr w, string l); }\n'@; "
            "[void][U]::PostMessage([IntPtr]0xffff, 0x001A, [IntPtr]::Zero, 'ImmersiveColorSet')",
        ],
        timeout=20,
    )
    return True, "OK"


def _parse_powercfg_minutes(block: str, prefer: str = "AC") -> int | None:
    """Extract timeout minutes from powercfg /query output (locale tolerant)."""
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    pick: list[str] = []
    for ln in lines:
        u = ln.upper()
        if prefer.upper() == "AC" and "DC" in u and "AC" not in u:
            continue
        if "AC" in u or "AC-" in u or "AC " in u.upper():
            pick.append(ln)
    if not pick:
        pick = lines
    for ln in pick:
        hx = re.search(r"0x([0-9a-fA-F]{1,8})\b", ln, re.I)
        if hx:
            try:
                v = int(hx.group(1), 16)
                return v
            except ValueError:
                continue
        m = re.search(r"(\d{1,5})\s*(?:Min|min|Minute|MINUTE|minutes|Minutes)?", ln)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def _parse_ac_setting_index_hex(block: str) -> int | None:
    """Parse 'Current AC Power Setting Index: 0x....' (used for VIDEOIDLE seconds, LIDACTION enum)."""
    for ln in block.splitlines():
        if "index" not in ln.lower():
            continue
        if "dc" in ln.lower() and "ac" not in ln.lower():
            continue
        m = re.search(r"0x([0-9a-fA-F]{1,8})\b", ln, re.I)
        if not m:
            continue
        try:
            return int(m.group(1), 16)
        except ValueError:
            continue
    return None


def _video_idle_ac_minutes(block: str) -> int | None:
    raw = _parse_ac_setting_index_hex(block)
    if raw is None:
        return _parse_powercfg_minutes(block, "AC")
    if raw == 0:
        return 0
    # VIDEOIDLE is commonly stored as seconds (e.g. 0x258 = 600s = 10 min)
    if raw >= 60 or raw > 45:
        return max(0, (raw + 59) // 60)
    return raw


def _query_power_line(subgroup: str, setting: str, scheme: str | None = None) -> str:
    if platform.system() != "Windows":
        return ""
    args = ["powercfg", "/query"]
    if scheme:
        args.append(scheme)
    args.extend([subgroup, setting])
    res = _run(args, timeout=45)
    return ((res.stdout or "") + "\n" + (res.stderr or "")).strip()


def read_power_profile_toggle(cfg: dict[str, Any]) -> ToggleReadResult:
    """ON means active scheme matches configured `power_profile` target (balanced or high)."""
    if platform.system() != "Windows":
        return ToggleReadResult(False, False, "Nur unter Windows.")
    merged = coerce_system_settings(cfg)
    target = str(merged.get("power_profile", "balanced") or "balanced").strip().lower()
    if target in ("high", "high_performance", "turbo", "performance"):
        want_guid = GUID_HIGH
        label = "High Performance"
    else:
        want_guid = GUID_BALANCED
        label = "Balanced"
    active = active_power_scheme_guid()
    if not active:
        return ToggleReadResult(False, False, "Aktives Energieschema konnte nicht gelesen werden.")
    on = _guid_matches(active, want_guid)
    return ToggleReadResult(True, on, detail=f"Aktiv={active}, Ziel ({label})={want_guid}")


def apply_power_profile_toggle(cfg: dict[str, Any], want_on: bool, logger: logging.Logger | None = None) -> tuple[bool, str]:
    if platform.system() != "Windows":
        return False, "Nur unter Windows."
    merged = coerce_system_settings(cfg)
    target = str(merged.get("power_profile", "balanced") or "balanced").strip().lower()
    prefer_high = target in ("high", "high_performance", "turbo", "performance")
    # When want_on True -> activate configured profile; False -> opposite
    if want_on:
        guid = GUID_HIGH if prefer_high else GUID_BALANCED
    else:
        guid = GUID_BALANCED if prefer_high else GUID_HIGH
    res = _run(["powercfg", "/setactive", guid], timeout=60)
    if res.returncode != 0:
        tail = ((res.stderr or "") + (res.stdout or "")).strip()[:400]
        if logger:
            logger.warning("powercfg /setactive: %s", tail)
        return False, tail or f"powercfg exit {res.returncode}"
    return True, guid


def _display_timeout_custom_seconds(cfg: dict[str, Any]) -> int:
    merged = coerce_system_settings(cfg)
    try:
        s = int(merged.get("display_timeout_seconds", 30))
    except (TypeError, ValueError):
        s = 30
    return max(0, min(s, 86400))


def _monitor_timeout_minutes_from_seconds(sec: int) -> int:
    if sec <= 0:
        return 0
    return max(1, (sec + 59) // 60)


def read_display_timeout_toggle(cfg: dict[str, Any]) -> ToggleReadResult:
    if platform.system() != "Windows":
        return ToggleReadResult(False, False, "Nur unter Windows.")
    scheme = active_power_scheme_guid()
    if not scheme:
        return ToggleReadResult(False, False, "Kein Energieschema.")
    block = _query_power_line("SUB_VIDEO", "VIDEOIDLE", scheme)
    if not block:
        return ToggleReadResult(False, False, "VIDEOIDLE nicht lesbar.")
    ac_min = _video_idle_ac_minutes(block)
    if ac_min is None:
        return ToggleReadResult(True, False, "AC-Timeout nicht erkannt.", detail=block[:500])
    custom = _display_timeout_custom_seconds(cfg)
    target_min = _monitor_timeout_minutes_from_seconds(custom)
    if custom == 0:
        on = ac_min == 0
    else:
        on = ac_min == target_min
    return ToggleReadResult(True, on, detail=f"AC monitor={ac_min} min, Ziel-ON={target_min} min")


def apply_display_timeout_toggle(cfg: dict[str, Any], want_on: bool, logger: logging.Logger | None = None) -> tuple[bool, str]:
    if platform.system() != "Windows":
        return False, "Nur unter Windows."
    if want_on:
        sec = _display_timeout_custom_seconds(cfg)
        ac_min = _monitor_timeout_minutes_from_seconds(sec)
        dc_min = ac_min
    else:
        ac_min = dc_min = 10  # sensible default when "OFF"
    for args in (
        ["powercfg", "/change", "monitor-timeout-ac", str(ac_min)],
        ["powercfg", "/change", "monitor-timeout-dc", str(dc_min)],
    ):
        res = _run(args, timeout=60)
        if res.returncode != 0:
            tail = ((res.stderr or "") + (res.stdout or "")).strip()[:400]
            if logger:
                logger.warning("powercfg monitor: %s %s", args, tail)
            return False, tail or str(args)
    return True, f"monitor-timeout ac/dc={ac_min}"


def read_lid_sleep_toggle() -> ToggleReadResult:
    if platform.system() != "Windows":
        return ToggleReadResult(False, False, "Nur unter Windows.")
    scheme = active_power_scheme_guid()
    if not scheme:
        return ToggleReadResult(False, False, "Kein Energieschema.")
    block_ac = _query_power_line("SUB_BUTTONS", "LIDACTION", scheme)
    if not block_ac:
        return ToggleReadResult(False, False, "LIDACTION nicht lesbar.")
    ac_idx = _parse_ac_setting_index_hex(block_ac)
    if ac_idx is None:
        ac_idx = _parse_powercfg_minutes(block_ac, "AC")
    if ac_idx is None:
        return ToggleReadResult(True, False, "Deckel-Aktion nicht erkannt.", detail=block_ac[:400])
    # 1 = sleep, 0 = nothing (typical)
    on = ac_idx in (1, 0x01)
    return ToggleReadResult(True, on, detail=f"LIDACTION AC index={ac_idx}")


def apply_lid_sleep_toggle(want_sleep_on_lid: bool, logger: logging.Logger | None = None) -> tuple[bool, str]:
    if platform.system() != "Windows":
        return False, "Nur unter Windows."
    scheme = active_power_scheme_guid()
    if not scheme:
        return False, "Kein aktives Energieschema."
    idx = "1" if want_sleep_on_lid else "0"
    for cmd in (
        ["powercfg", "/setacvalueindex", scheme, "SUB_BUTTONS", "LIDACTION", idx],
        ["powercfg", "/setdcvalueindex", scheme, "SUB_BUTTONS", "LIDACTION", idx],
        ["powercfg", "/setactive", scheme],
    ):
        res = _run(cmd, timeout=60)
        if res.returncode != 0:
            tail = ((res.stderr or "") + (res.stdout or "")).strip()[:400]
            if logger:
                logger.warning("powercfg lid: %s %s", cmd, tail)
            return False, tail or str(cmd)
    return True, f"LIDACTION={idx}"


def read_ac_sleep_disabled_toggle(cfg: dict[str, Any]) -> ToggleReadResult:
    """ON = standby on AC disabled (0 minutes = never)."""
    if platform.system() != "Windows":
        return ToggleReadResult(False, False, "Nur unter Windows.")
    scheme = active_power_scheme_guid()
    if not scheme:
        return ToggleReadResult(False, False, "Kein Energieschema.")
    block = _query_power_line("SUB_SLEEP", "STANDBYIDLE", scheme)
    if not block:
        return ToggleReadResult(False, False, "STANDBYIDLE nicht lesbar (evtl. nicht unterstützt).")
    raw = _parse_ac_setting_index_hex(block)
    if raw is None:
        ac_min = _parse_powercfg_minutes(block, "AC")
    else:
        ac_min = 0 if raw == 0 else max(0, (raw + 59) // 60) if raw >= 60 else raw
    if ac_min is None:
        return ToggleReadResult(True, False, "Standby AC nicht erkannt.", detail=block[:400])
    on = ac_min == 0
    return ToggleReadResult(True, on, detail=f"standby AC={ac_min} min")


def apply_ac_sleep_disabled_toggle(cfg: dict[str, Any], want_never_on_ac: bool, logger: logging.Logger | None = None) -> tuple[bool, str]:
    if platform.system() != "Windows":
        return False, "Nur unter Windows."
    merged = coerce_system_settings(cfg)
    try:
        restore = int(merged.get("sleep_timeout_minutes", 3))
    except (TypeError, ValueError):
        restore = 3
    restore = max(0, min(restore, 720))
    minutes = 0 if want_never_on_ac else max(1, restore)
    res = _run(["powercfg", "/change", "standby-timeout-ac", str(minutes)], timeout=60)
    if res.returncode != 0:
        tail = ((res.stderr or "") + (res.stdout or "")).strip()[:400]
        if logger:
            logger.warning("powercfg standby ac: %s", tail)
        return False, tail or "standby-timeout-ac failed"
    return True, f"standby-timeout-ac={minutes}"


def verify_after(
    kind: str,
    cfg: dict[str, Any],
    expect_on: bool,
    logger: logging.Logger | None = None,
) -> ToggleReadResult:
    syscfg = coerce_system_settings(cfg)
    try:
        if kind == "dark":
            r = read_dark_mode()
        elif kind == "power":
            r = read_power_profile_toggle(syscfg)
        elif kind == "display":
            r = read_display_timeout_toggle(syscfg)
        elif kind == "lid":
            r = read_lid_sleep_toggle()
        elif kind == "ac_sleep":
            r = read_ac_sleep_disabled_toggle(syscfg)
        else:
            return ToggleReadResult(False, False, f"Unbekannte Art: {kind}")
    except Exception as exc:  # pylint: disable=broad-except
        if logger:
            logger.exception("verify_after %s", kind)
        return ToggleReadResult(False, False, f"Hinweis: Verifikation fehlgeschlagen: {exc}")
    if not r.available:
        return r
    if r.on != expect_on:
        return ToggleReadResult(
            True,
            r.on,
            hint="Hinweis: Zustand weicht nach Anwendung ab (Rechte, Gruppenrichtlinien oder manuelle Änderung).",
            detail=r.detail,
        )
    return r


def _state(value: str, available: bool = True, hint: str = "") -> SettingState:
    return SettingState(value=value, available=available, hint=hint)


def _run_powershell(script: str, timeout: int = 40) -> str:
    res = _run(["powershell", "-NoProfile", "-Command", script], timeout=timeout)
    return ((res.stdout or "") + "\n" + (res.stderr or "")).strip()


def _read_power_setting_index(subgroup: str, setting: str, *, ac: bool) -> int | None:
    scheme = active_power_scheme_guid()
    if not scheme:
        return None
    block = _query_power_line(subgroup, setting, scheme)
    if not block:
        return None
    if ac:
        idx = _parse_ac_setting_index_hex(block)
        if idx is not None:
            return idx
        return _parse_powercfg_minutes(block, "AC")
    dc_block = "\n".join(ln for ln in block.splitlines() if "dc" in ln.lower() or "battery" in ln.lower())
    idx_dc = _parse_ac_setting_index_hex(dc_block)
    if idx_dc is not None:
        return idx_dc
    for ln in block.splitlines():
        if "dc" not in ln.lower() and "battery" not in ln.lower():
            continue
        m = re.search(r"0x([0-9a-fA-F]{1,8})\b", ln, re.I)
        if m:
            try:
                return int(m.group(1), 16)
            except ValueError:
                pass
        m2 = re.search(r"(\d{1,8})", ln)
        if m2:
            try:
                return int(m2.group(1))
            except ValueError:
                pass
    return None


def read_power_mode_ac_dc() -> tuple[SettingState, SettingState]:
    if platform.system() != "Windows":
        na = _state("Nicht verfügbar", False, "Nur unter Windows.")
        return na, na
    active = active_power_scheme_guid()
    if not active:
        na = _state("Nicht verfügbar", False, "Aktives Energieschema nicht lesbar.")
        return na, na
    if _guid_matches(active, GUID_HIGH):
        mode = "Beste Leistung"
    elif _guid_matches(active, GUID_BALANCED):
        mode = "Ausbalanciert"
    else:
        mode = "Energiesparmodus/Benutzerdefiniert"
    st = _state(mode, True, f"Aktives Schema: {active}")
    return st, st


def read_battery_percent() -> SettingState:
    if platform.system() != "Windows":
        return _state("Nicht verfügbar", False, "Nur unter Windows.")
    text = _run_powershell(
        "(Get-CimInstance Win32_Battery | Select-Object -First 1 -ExpandProperty EstimatedChargeRemaining) | Out-String"
    )
    m = re.search(r"(\d{1,3})", text)
    if not m:
        return _state("Nicht verfügbar", False, "Kein Akku oder keine Sensordaten.")
    try:
        val = max(0, min(int(m.group(1)), 100))
    except ValueError:
        return _state("Nicht verfügbar", False, "Akkustand nicht auswertbar.")
    return _state(f"{val}%")


def read_show_battery_percent_state() -> ToggleReadResult:
    if winreg is None or platform.system() != "Windows":
        return ToggleReadResult(False, False, "Nur unter Windows verfügbar.")
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "ShowBatteryPercentageOnTaskbar")
        return ToggleReadResult(True, int(value) == 1, detail=f"ShowBatteryPercentageOnTaskbar={int(value)}")
    except OSError:
        return ToggleReadResult(False, False, "Nicht verfügbar (abhängig von Windows-Version).")


def apply_show_battery_percent_state(enable: bool) -> tuple[bool, str]:
    if winreg is None or platform.system() != "Windows":
        return False, "Nur unter Windows verfügbar."
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "ShowBatteryPercentageOnTaskbar", 0, winreg.REG_DWORD, 1 if enable else 0)
        return True, "OK"
    except OSError as exc:
        return False, str(exc)


def read_power_button_action(ac: bool) -> SettingState:
    idx = _read_power_setting_index("SUB_BUTTONS", "PBUTTONACTION", ac=ac)
    if idx is None:
        return _state("Nicht verfügbar", False, "PBUTTONACTION nicht lesbar.")
    mapping = {0: "Nichts tun", 1: "Standbymodus", 2: "Ruhezustand", 3: "Herunterfahren"}
    return _state(mapping.get(idx, f"Unbekannt ({idx})"))


def read_lid_close_action(ac: bool) -> SettingState:
    idx = _read_power_setting_index("SUB_BUTTONS", "LIDACTION", ac=ac)
    if idx is None:
        return _state("Nicht verfügbar", False, "LIDACTION nicht lesbar.")
    mapping = {0: "Keine Aktion", 1: "Standbymodus", 2: "Ruhezustand", 3: "Herunterfahren"}
    return _state(mapping.get(idx, f"Unbekannt ({idx})"))


def read_display_timeout(ac: bool) -> SettingState:
    idx = _read_power_setting_index("SUB_VIDEO", "VIDEOIDLE", ac=ac)
    if idx is None:
        return _state("Nicht verfügbar", False, "VIDEOIDLE nicht lesbar.")
    minutes = 0 if idx == 0 else max(1, (idx + 59) // 60) if idx >= 60 else idx
    return _state("Nie" if minutes == 0 else f"{minutes} Min")


def read_sleep_timeout(ac: bool) -> SettingState:
    idx = _read_power_setting_index("SUB_SLEEP", "STANDBYIDLE", ac=ac)
    if idx is None:
        return _state("Nicht verfügbar", False, "STANDBYIDLE nicht lesbar.")
    minutes = 0 if idx == 0 else max(1, (idx + 59) // 60) if idx >= 60 else idx
    return _state("Nie" if minutes == 0 else f"{minutes} Min")


def read_screensaver_state() -> ToggleReadResult:
    if winreg is None or platform.system() != "Windows":
        return ToggleReadResult(False, False, "Nur unter Windows.")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
            active, _ = winreg.QueryValueEx(key, "ScreenSaveActive")
        val = str(active).strip()
        return ToggleReadResult(True, val == "1", detail=f"ScreenSaveActive={val}")
    except OSError:
        return ToggleReadResult(False, False, "Nicht verfügbar.")


def apply_screensaver_state(enable: bool) -> tuple[bool, str]:
    if winreg is None or platform.system() != "Windows":
        return False, "Nur unter Windows."
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ScreenSaveActive", 0, winreg.REG_SZ, "1" if enable else "0")
        return True, "OK"
    except OSError as exc:
        return False, str(exc)


def read_adaptive_brightness_state() -> SettingState:
    # Device dependent: many systems expose this via power setting ADAPTBRIGHT.
    idx = _read_power_setting_index("SUB_VIDEO", "ADAPTBRIGHT", ac=True)
    if idx is None:
        return _state("Nicht verfügbar", False, "Adaptive Helligkeit wird nicht bereitgestellt.")
    return _state("AN" if idx == 1 else "AUS")


def read_usb_power_saving_state() -> SettingState:
    # USB selective suspend as closest safe proxy.
    idx = _read_power_setting_index("SUB_USB", "USBSELECTIVE SUSPEND", ac=True)
    if idx is None:
        idx = _read_power_setting_index("SUB_USB", "USBSELECT", ac=True)
    if idx is None:
        return _state("Nicht verfügbar", False, "USB-Energiesparen nicht lesbar.")
    return _state("AN" if idx == 1 else "AUS")


def read_battery_saver_state() -> SettingState:
    text = _run_powershell("(Get-CimInstance -Namespace root\\cimv2\\power -ClassName Win32_PowerPlan -ErrorAction SilentlyContinue) | Out-String")
    if not text:
        return _state("Nicht verfügbar", False, "Nicht unterstützt.")
    return _state("Aktiv" if "power saver" in text.lower() else "Inaktiv")


def read_battery_charge_limit_capability(cfg: dict[str, Any] | None = None) -> BatteryChargeCapability:
    cfg = cfg or {}
    bh = cfg.get("battery_health") if isinstance(cfg.get("battery_health"), dict) else {}
    allow_vendor = bool((bh or {}).get("allow_vendor_tools", False))
    desired = int((bh or {}).get("desired_charge_limit_percent", 80) or 80)
    guidance = (
        "Windows bietet keinen einheitlichen Standard-Schalter, um den Akku-Ladestand bei 80% zu begrenzen. "
        "Viele Hersteller lösen das über eigene Tools oder BIOS/UEFI."
    )
    if platform.system() != "Windows":
        return BatteryChargeCapability(False, guidance=guidance, mode="not_available")
    checks: tuple[tuple[str, str], ...] = (
        ("Lenovo Vantage", r"C:\Program Files\Lenovo\VantageService\VantageService.exe"),
        ("Dell Power Manager", r"C:\Program Files\Dell\Dell Power Manager\DPM.exe"),
        ("HP Battery Health Manager", r"C:\Program Files\HP\HP Power Manager\HPPowerManager.exe"),
        ("ASUS Battery Health Charging", r"C:\Program Files (x86)\ASUS\ASUS Battery Health Charging\BatteryHealthCharging.exe"),
        ("Acer Care Center", r"C:\Program Files\Acer\Care Center\ACCStd.exe"),
    )
    for vendor, path in checks:
        if re.search(r"[a-z]:\\", path, re.I):
            try:
                import os

                if os.path.exists(path):
                    return BatteryChargeCapability(
                        True,
                        vendor=vendor,
                        mode="vendor_tool_found",
                        guidance=guidance + (" Vendor-Tool erkannt, Änderungen nur manuell im Hersteller-Tool." if not allow_vendor else ""),
                        can_apply=False,
                    )
            except OSError:
                continue
    return BatteryChargeCapability(
        False,
        vendor="",
        mode="not_available",
        guidance=guidance + f" Gewünschtes Ziel: {desired}%. Nicht verfügbar – Hersteller-Tool erforderlich.",
        can_apply=False,
    )


def format_energy_status_rows(rows: list[tuple[str, SettingState]]) -> list[tuple[str, str, str]]:
    """Return UI-friendly rows: (label, value, pill_kind)."""
    out: list[tuple[str, str, str]] = []
    for label, state in rows:
        value = state.value if state.available else "Nicht verfügbar"
        norm = value.strip().lower()
        if not state.available or "nicht verfügbar" in norm:
            kind = "na"
        elif norm in ("an", "ein", "ja", "aktiv", "beste leistung"):
            kind = "on"
        elif norm in ("aus", "nein", "inaktiv"):
            kind = "off"
        else:
            kind = "neutral"
        out.append((label, value, kind))
    return out
