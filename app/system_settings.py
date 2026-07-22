from __future__ import annotations

import ctypes
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
    "display_timeout_seconds": 0,
    "sleep_timeout_minutes": 0,
    "power_profile": "balanced",
    "dark_mode_enabled": True,
    "screensaver_disabled": True,
    "show_battery_percent": True,
    "ac_lid_action": "none",
    "dc_lid_action": "none",
    "ac_power_button_action": "sleep",
    "dc_power_button_action": "sleep",
    "ac_standby_disabled": True,
    "dc_standby_disabled": True,
    "adaptive_brightness_enabled": True,
    "usb_power_saving_enabled": True,
    "ac_power_mode": "best_performance",
    "dc_power_mode": "balanced",
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


def _console_encoding() -> str:
    """Konsolen-Codepage (OEM), nicht die ANSI/CP1252-Systemcodepage: powercfg & Co. schreiben
    auf die OEM-Codepage. Auf deutschsprachigem Windows ist das meist CP850, nicht CP1252 —
    bestimmte Umlaute/Sonderzeichen fuehren sonst zu UnicodeDecodeError im subprocess-Reader-
    Thread und die Ausgabe geht verloren (Einstellung erscheint faelschlich als nicht lesbar)."""
    if platform.system() != "Windows":
        return "utf-8"
    try:
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "cp850"


def _run(cmd: list[str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding=_console_encoding(),
        errors="replace",
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


def _broadcast_setting_change(param: str) -> None:
    """Sendet eine WM_SETTINGCHANGE-Broadcast-Nachricht an alle Top-Level-Fenster.

    Wichtig: SendMessageTimeout (synchron, wartet auf alle Empfaenger), nicht PostMessage
    (asynchron). PostMessage mit einem String-lParam ist fuer einen Prozess-uebergreifenden
    Broadcast unsicher — der String zeigt auf Speicher dieses Prozesses, der nach Rueckkehr aus
    PostMessage bereits ungueltig sein kann, bevor andere Prozesse ihn lesen. Das fuehrte dazu,
    dass z. B. der Explorer die neue Theme-Einstellung aufnahm, andere Shell-Komponenten aber
    nicht — sichtbar als "halb Hell/halb Dunkel"-Zustand nach dem Umschalten.
    """
    if winreg is None or platform.system() != "Windows":
        return
    try:
        hwnd_broadcast = 0xFFFF
        wm_settingchange = 0x1A
        smto_abortifhung = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            ctypes.c_void_p(hwnd_broadcast),
            wm_settingchange,
            ctypes.c_void_p(0),
            param,
            smto_abortifhung,
            2000,
            ctypes.byref(result),
        )
    except Exception:  # pylint: disable=broad-except
        pass


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
    # Alle Fenster/Shell-Komponenten ueber den Theme-Wechsel informieren, damit nicht nur der
    # Explorer, sondern auch bereits laufende Apps (Taskleiste, Start, Settings, ...) sofort
    # konsistent umschalten statt in einem gemischten Hell/Dunkel-Zustand zu verharren.
    _broadcast_setting_change("ImmersiveColorSet")
    return True, "OK"


_GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _parse_powercfg_minutes(block: str, prefer: str = "AC") -> int | None:
    """Extract timeout minutes from powercfg /query output (locale tolerant)."""
    # GUID-Zeilen (Schema-/Setting-Header) enthalten oft Ziffernfragmente vor einem Hex-Buchstaben
    # (z. B. "381b4222-..."), die sonst faelschlich als Minutenwert erkannt wuerden.
    lines = [ln.strip() for ln in block.splitlines() if ln.strip() and not _GUID_RE.search(ln)]
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
        m = re.search(r"(\d{1,5})\s*(?:Min|min|Minute|MINUTE|minutes|Minutes)", ln)
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


def _parse_dc_setting_index_hex(block: str) -> int | None:
    """Zweite 'Index der aktuellen ...einstellung: 0x....'-Zeile (DC/Akku) anhand der
    Reihenfolge, nicht anhand von Schluesselwoertern wie 'dc'/'battery' (die in lokalisierter
    powercfg-Ausgabe, z. B. Deutsch 'Gleichstromeinstellung', nicht vorkommen)."""
    values: list[int] = []
    for ln in block.splitlines():
        if "index" not in ln.lower():
            continue
        m = re.search(r"0x([0-9a-fA-F]{1,8})\b", ln, re.I)
        if not m:
            continue
        try:
            values.append(int(m.group(1), 16))
        except ValueError:
            continue
        if len(values) >= 2:
            break
    return values[1] if len(values) >= 2 else None


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


# Aliase -> feste GUIDs. IT-Richtlinien verstecken Einstellungen haeufig vor `powercfg /query`
# (z. B. Deckel-/Netzschalter-Aktion auf verwalteten Laptop-Images) ueber das "Hidden"-Attribut —
# der Wert bleibt dabei uebers Registry direkt lesbar; nur die powercfg-Textausgabe blendet ihn aus.
_POWER_ALIAS_GUIDS: dict[str, str] = {
    "SUB_BUTTONS": "4f971e89-eebd-4455-a8de-9e59040e7347",
    "PBUTTONACTION": "7648efa3-dd9c-4e3e-b566-50f929386280",
    "LIDACTION": "5ca83367-6e45-459f-a27b-476b1d01c936",
    "SUB_VIDEO": "7516b95f-f776-4464-8c53-06167f40cc99",
    "VIDEOIDLE": "3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e",
    "ADAPTBRIGHT": "fbd9aa66-9553-4097-ba44-ed6e9d65eab8",
    "SUB_SLEEP": "238c9fa8-0aad-41ed-83f4-97be242c8f20",
    "STANDBYIDLE": "29f6c1db-86da-48c5-9fdb-f2b67b1f44da",
    "SUB_PROCESSOR": "54533251-82be-4824-96c1-47b60b740d00",
}


def _read_power_setting_index_registry(scheme: str, subgroup: str, setting: str, *, ac: bool) -> int | None:
    """Liest ACSettingIndex/DCSettingIndex direkt aus der Registry — findet auch Werte, die per
    Richtlinie vor `powercfg /query` versteckt sind (z. B. Deckel-/Netzschalter-Aktion auf manchen
    Firmen-Notebook-Images)."""
    if winreg is None or platform.system() != "Windows":
        return None
    subgroup_guid = _POWER_ALIAS_GUIDS.get(subgroup.upper(), subgroup)
    setting_guid = _POWER_ALIAS_GUIDS.get(setting.upper(), setting)
    path = (
        rf"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes\{scheme}\{subgroup_guid}\{setting_guid}"
    )
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value, _ = winreg.QueryValueEx(key, "ACSettingIndex" if ac else "DCSettingIndex")
        return int(value)
    except OSError:
        return None


def _read_power_setting_index(subgroup: str, setting: str, *, ac: bool) -> int | None:
    scheme = active_power_scheme_guid()
    if not scheme:
        return None
    reg_idx = _read_power_setting_index_registry(scheme, subgroup, setting, ac=ac)
    if reg_idx is not None:
        return reg_idx
    block = _query_power_line(subgroup, setting, scheme)
    if not block:
        return None
    # Existiert die Einstellung auf diesem System/Schema nicht (z. B. PBUTTONACTION/LIDACTION
    # auf manchen Desktops/verwalteten Images), liefert powercfg nur den Schema-Header ohne
    # eine "Index der aktuellen ...einstellung"-Zeile zurueck. Dann sauber "nicht verfuegbar"
    # melden statt Ziffernfragmente aus dem GUID-Header als Wert misszudeuten.
    if not re.search(r"index", block, re.I):
        return None
    if ac:
        idx = _parse_ac_setting_index_hex(block)
        if idx is not None:
            return idx
        return _parse_powercfg_minutes(block, "AC")
    # Nicht per "dc"/"battery"-Substring filtern: die deutsche powercfg-Ausgabe verwendet
    # "Gleichstromeinstellung", das weder "dc" noch "battery" enthaelt. powercfg listet pro
    # Einstellung stattdessen immer zuerst die AC- (Wechselstrom-), dann die DC-Zeile
    # (Gleichstrom/Akku) — das ist locale-unabhaengig und robuster als Schluesselwoerter.
    idx_dc = _parse_dc_setting_index_hex(block)
    if idx_dc is not None:
        return idx_dc
    return _parse_powercfg_minutes(block, "DC")


def _set_power_setting_index(subgroup: str, setting: str, *, ac: bool, value: int) -> tuple[bool, str]:
    scheme = active_power_scheme_guid()
    if not scheme:
        return False, "Aktives Energieschema nicht lesbar."
    cmd = ["powercfg", "/setacvalueindex" if ac else "/setdcvalueindex", scheme, subgroup, setting, str(value)]
    res = _run(cmd, timeout=60)
    if res.returncode != 0:
        return False, ((res.stderr or "") + (res.stdout or "")).strip()[:300] or f"Code {res.returncode}"
    _run(["powercfg", "/setactive", scheme], timeout=40)
    return True, "OK"


# Windows-11-"Energiemodus"-Schieberegler (separat pro Eingesteckt/Akku, sichtbar unter
# Einstellungen > System > Strom und Akku > Energiestatus). Anders als das klassische
# Energieschema (Balanced/High Performance GUID, siehe oben) ist das ein Wert-Paar INNERHALB
# des aktiven Schemas unter SUB_PROCESSOR — undokumentiert, aber auf allen getesteten
# Windows-11-Builds stabil und per Registry (versteckt vor `powercfg /query`) sowie
# `powercfg /setacvalueindex` lesbar/schreibbar. Beide GUIDs muessen synchron gesetzt werden,
# sonst laufen UI-Anzeige und tatsaechliches Verhalten auseinander.
_POWER_MODE_GUID_PRIMARY = "36687f9e-e3a5-4dbf-b1dc-15eb381c6863"
_POWER_MODE_GUID_SECONDARY = "36687f9e-e3a5-4dbf-b1dc-15eb381c6864"

_POWER_MODE_TARGETS: dict[str, int] = {
    "best_performance": 0,
    "balanced": 50,
    "best_efficiency": 100,
}


def _power_mode_label(idx: int) -> str:
    # Grenzen empirisch ermittelt (nicht offiziell dokumentiert): Index 33 zeigt sich in der
    # Windows-Einstellungen-UI als "Beste Leistung", Index 50 als "Ausbalanciert" — die Mitte
    # zwischen den drei von uns geschriebenen Zielwerten (0/50/100) liegt also bei 40 bzw. 75.
    if idx < 40:
        return "Beste Leistung"
    if idx < 75:
        return "Ausbalanciert"
    return "Beste Energieeffizienz"


def read_power_mode_ac_dc() -> tuple[SettingState, SettingState]:
    if platform.system() != "Windows":
        na = _state("Nicht verfügbar", False, "Nur unter Windows.")
        return na, na
    active = active_power_scheme_guid()
    if not active:
        na = _state("Nicht verfügbar", False, "Aktives Energieschema nicht lesbar.")
        return na, na
    # Der Energiemodus-Regler ist undokumentiert und existiert je nach Windows-Version/-Build
    # unterschiedlich (neu ab Windows 11, teils zurueckportiert nach Windows 10). Lesefehler auf
    # abweichenden Systemen sollen nie hochgereicht werden, sondern immer auf den robusten
    # Einzelschema-Fallback zurueckfallen.
    try:
        idx_ac = _read_power_setting_index("SUB_PROCESSOR", _POWER_MODE_GUID_PRIMARY, ac=True)
        idx_dc = _read_power_setting_index("SUB_PROCESSOR", _POWER_MODE_GUID_PRIMARY, ac=False)
    except Exception:  # pylint: disable=broad-except
        idx_ac = idx_dc = None
    if idx_ac is None or idx_dc is None:
        # Energiemodus-Regler auf diesem System nicht verfuegbar (z. B. aeltere Windows-10-Builds) —
        # Fallback auf das aktive Energieschema, das dann fuer AC/DC identisch angezeigt wird.
        if _guid_matches(active, GUID_HIGH):
            mode = "Beste Leistung"
        elif _guid_matches(active, GUID_BALANCED):
            mode = "Ausbalanciert"
        else:
            mode = "Energiesparmodus/Benutzerdefiniert"
        st = _state(mode, True, f"Aktives Schema: {active}")
        return st, st
    return (
        _state(_power_mode_label(idx_ac), True, f"Index={idx_ac}"),
        _state(_power_mode_label(idx_dc), True, f"Index={idx_dc}"),
    )


def apply_power_mode(ac: bool, level: str) -> tuple[bool, str]:
    if platform.system() != "Windows":
        return False, "Nur unter Windows."
    idx = _POWER_MODE_TARGETS.get((level or "").strip().lower())
    if idx is None:
        return False, f"Unbekannte Energiemodus-Stufe: {level}"
    try:
        ok1, msg1 = _set_power_setting_index("SUB_PROCESSOR", _POWER_MODE_GUID_PRIMARY, ac=ac, value=idx)
        ok2, msg2 = _set_power_setting_index("SUB_PROCESSOR", _POWER_MODE_GUID_SECONDARY, ac=ac, value=idx)
    except Exception as exc:  # pylint: disable=broad-except
        # Auf Systemen ohne diesen Regler (z. B. aeltere Windows-10-Builds) soll ein unerwarteter
        # powercfg-Fehler nie den Aufrufer crashen, sondern sauber als "nicht verfuegbar" zurueckkommen.
        return False, f"Energiemodus auf diesem System nicht verfuegbar: {exc}"
    if not ok1 and not ok2:
        return False, msg1 or msg2
    return True, "OK"


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def read_battery_percent() -> SettingState:
    if platform.system() != "Windows":
        return _state("Nicht verfügbar", False, "Nur unter Windows.")
    # GetSystemPowerStatus statt WMI Win32_Battery: Win32_Battery.EstimatedChargeRemaining wird vom
    # ACPI-Subsystem oft nur alle paar Minuten aktualisiert und kann daher spuerbar hinter dem
    # Taskleisten-Symbol zurueckliegen. GetSystemPowerStatus ist dieselbe Win32-API, die auch die
    # Windows-Taskleiste fuer ihr Akkusymbol nutzt, und liefert deshalb einen konsistenten Wert.
    try:
        status = _SystemPowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            raise OSError("GetSystemPowerStatus fehlgeschlagen")
        if status.BatteryFlag == 128 or status.BatteryLifePercent == 255:
            return _state("Nicht verfügbar", False, "Kein Akku erkannt.")
        val = max(0, min(int(status.BatteryLifePercent), 100))
    except Exception as exc:  # pylint: disable=broad-except
        return _state("Nicht verfügbar", False, f"Akkustand nicht lesbar: {exc}")
    return _state(f"{val}%")


def read_show_battery_percent_state() -> ToggleReadResult:
    if winreg is None or platform.system() != "Windows":
        return ToggleReadResult(False, False, "Nur unter Windows verfügbar.")
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
        ) as key:
            # Windows 11 (ab ca. 23H2) hat einen nativen Taskleisten-Schalter "Akkuprozentsatz
            # anzeigen" bekommen, der ueber IsBatteryPercentageEnabled gesteuert wird. Das aeltere
            # ShowBatteryPercentageOnTaskbar (frueherer Registry-Workaround vor dem nativen Support)
            # existiert auf aktuellen Builds oft noch als Karteileiche mit veraltetem Wert und wird
            # vom System nicht mehr ausgewertet — deshalb zuerst den neuen Wert pruefen.
            try:
                value, _ = winreg.QueryValueEx(key, "IsBatteryPercentageEnabled")
                return ToggleReadResult(True, int(value) == 1, detail=f"IsBatteryPercentageEnabled={int(value)}")
            except FileNotFoundError:
                pass
            value, _ = winreg.QueryValueEx(key, "ShowBatteryPercentageOnTaskbar")
        return ToggleReadResult(True, int(value) == 1, detail=f"ShowBatteryPercentageOnTaskbar={int(value)}")
    except (OSError, ValueError, TypeError):
        # ValueError/TypeError zusaetzlich zu OSError: auf manchen Windows-Versionen/-Builds kann
        # der Wertetyp der Registry-Eintraege abweichen (z. B. REG_SZ statt REG_DWORD).
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
            val = 1 if enable else 0
            # Beide Werte setzen: IsBatteryPercentageEnabled fuer aktuelle Windows-11-Builds,
            # ShowBatteryPercentageOnTaskbar als Fallback fuer aeltere Windows-10-Systeme.
            winreg.SetValueEx(key, "IsBatteryPercentageEnabled", 0, winreg.REG_DWORD, val)
            winreg.SetValueEx(key, "ShowBatteryPercentageOnTaskbar", 0, winreg.REG_DWORD, val)
        return True, "OK"
    except OSError as exc:
        return False, str(exc)


def read_show_file_extensions() -> ToggleReadResult:
    if winreg is None or platform.system() != "Windows":
        return ToggleReadResult(False, False, "Nur unter Windows verfügbar.")
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "HideFileExt")
        # HideFileExt=0 means extensions ARE shown (toggle ON), 1 means hidden
        return ToggleReadResult(True, int(value) == 0, detail=f"HideFileExt={int(value)}")
    except OSError:
        return ToggleReadResult(True, True, detail="HideFileExt not set (default: shown)")


def apply_show_file_extensions(show: bool) -> tuple[bool, str]:
    if winreg is None or platform.system() != "Windows":
        return False, "Nur unter Windows verfügbar."
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "HideFileExt", 0, winreg.REG_DWORD, 0 if show else 1)
        # Notify Explorer to refresh without restarting it
        _run(
            [
                "powershell", "-NoProfile", "-Command",
                "Add-Type @'\nusing System;using System.Runtime.InteropServices;\n"
                "public class Shell32 { [DllImport(\"Shell32.dll\")] public static extern int "
                "SHChangeNotify(int e, int f, IntPtr a, IntPtr b); }\n'@; "
                "[Shell32]::SHChangeNotify(0x8000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)",
            ],
            timeout=15,
        )
        return True, "OK"
    except OSError as exc:
        return False, str(exc)


def read_power_button_action(ac: bool) -> SettingState:
    idx = _read_power_setting_index("SUB_BUTTONS", "PBUTTONACTION", ac=ac)
    if idx is None:
        return _state("Nicht verfügbar", False, "PBUTTONACTION nicht lesbar.")
    mapping = {0: "Nichts tun", 1: "Standbymodus", 2: "Ruhezustand", 3: "Herunterfahren"}
    return _state(mapping.get(idx, f"Unbekannt ({idx})"))


def apply_power_button_action(ac: bool, action: str) -> tuple[bool, str]:
    mapping = {
        "none": 0,
        "sleep": 1,
        "hibernate": 2,
        "shutdown": 3,
        "nichts tun": 0,
        "standbymodus": 1,
        "herunterfahren": 3,
    }
    key = (action or "").strip().lower()
    if key not in mapping:
        return False, f"Unbekannte Aktion: {action}"
    return _set_power_setting_index("SUB_BUTTONS", "PBUTTONACTION", ac=ac, value=mapping[key])


def read_lid_close_action(ac: bool) -> SettingState:
    idx = _read_power_setting_index("SUB_BUTTONS", "LIDACTION", ac=ac)
    if idx is None:
        return _state("Nicht verfügbar", False, "LIDACTION nicht lesbar.")
    mapping = {0: "Keine Aktion", 1: "Standbymodus", 2: "Ruhezustand", 3: "Herunterfahren"}
    return _state(mapping.get(idx, f"Unbekannt ({idx})"))


def apply_lid_close_action(ac: bool, action: str) -> tuple[bool, str]:
    mapping = {
        "none": 0,
        "sleep": 1,
        "hibernate": 2,
        "shutdown": 3,
        "keine aktion": 0,
        "standbymodus": 1,
        "herunterfahren": 3,
    }
    key = (action or "").strip().lower()
    if key not in mapping:
        return False, f"Unbekannte Aktion: {action}"
    return _set_power_setting_index("SUB_BUTTONS", "LIDACTION", ac=ac, value=mapping[key])


def read_display_timeout(ac: bool) -> SettingState:
    idx = _read_power_setting_index("SUB_VIDEO", "VIDEOIDLE", ac=ac)
    if idx is None:
        return _state("Nicht verfügbar", False, "VIDEOIDLE nicht lesbar.")
    minutes = 0 if idx == 0 else max(1, (idx + 59) // 60) if idx >= 60 else idx
    return _state("Nie" if minutes == 0 else f"{minutes} Min")


def apply_display_timeout(ac: bool, seconds: int) -> tuple[bool, str]:
    minutes = 0 if seconds <= 0 else max(1, (int(seconds) + 59) // 60)
    cmd = ["powercfg", "/change", "monitor-timeout-ac" if ac else "monitor-timeout-dc", str(minutes)]
    res = _run(cmd, timeout=60)
    if res.returncode != 0:
        return False, ((res.stderr or "") + (res.stdout or "")).strip()[:300] or f"Code {res.returncode}"
    return True, "OK"


def read_sleep_timeout(ac: bool) -> SettingState:
    idx = _read_power_setting_index("SUB_SLEEP", "STANDBYIDLE", ac=ac)
    if idx is None:
        return _state("Nicht verfügbar", False, "STANDBYIDLE nicht lesbar.")
    minutes = 0 if idx == 0 else max(1, (idx + 59) // 60) if idx >= 60 else idx
    return _state("Nie" if minutes == 0 else f"{minutes} Min")


def apply_sleep_timeout(ac: bool, minutes: int) -> tuple[bool, str]:
    val = max(0, int(minutes))
    cmd = ["powercfg", "/change", "standby-timeout-ac" if ac else "standby-timeout-dc", str(val)]
    res = _run(cmd, timeout=60)
    if res.returncode != 0:
        return False, ((res.stderr or "") + (res.stdout or "")).strip()[:300] or f"Code {res.returncode}"
    return True, "OK"


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


def apply_adaptive_brightness_state(enable: bool) -> tuple[bool, str]:
    want = 1 if enable else 0
    ok_ac, msg_ac = _set_power_setting_index("SUB_VIDEO", "ADAPTBRIGHT", ac=True, value=want)
    ok_dc, msg_dc = _set_power_setting_index("SUB_VIDEO", "ADAPTBRIGHT", ac=False, value=want)
    if not ok_ac and not ok_dc:
        return False, msg_ac or msg_dc
    return True, "OK"



# SUB_USB / "USBSELECTIVE SUSPEND" sind auf vielen Systemen keine registrierten powercfg-Aliase
# (fehlen z. B. in `powercfg /aliases`) — die GUIDs sind stabil ueber Windows-Version/Sprache
# hinweg und funktionieren immer als Fallback.
_SUB_USB_GUID = "2a737441-1930-4402-8d77-b2bebba308a3"
_USB_SELECTIVE_SUSPEND_GUID = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"


def read_usb_power_saving_state() -> SettingState:
    # USB selective suspend as closest safe proxy.
    idx = _read_power_setting_index("SUB_USB", "USBSELECTIVE SUSPEND", ac=True)
    if idx is None:
        idx = _read_power_setting_index("SUB_USB", "USBSELECT", ac=True)
    if idx is None:
        idx = _read_power_setting_index(_SUB_USB_GUID, _USB_SELECTIVE_SUSPEND_GUID, ac=True)
    if idx is None:
        return _state("Nicht verfügbar", False, "USB-Energiesparen nicht lesbar.")
    return _state("AN" if idx == 1 else "AUS")


def apply_usb_power_saving_state(enable: bool) -> tuple[bool, str]:
    # Use common values for selective suspend: 1=enabled, 0=disabled.
    want = 1 if enable else 0
    scheme = active_power_scheme_guid()
    if not scheme:
        return False, "Aktives Schema nicht lesbar."
    attempted = []
    for subgroup, setting in (
        ("SUB_USB", "USBSELECTIVE SUSPEND"),
        ("SUB_USB", "USBSELECT"),
        (_SUB_USB_GUID, _USB_SELECTIVE_SUSPEND_GUID),
    ):
        ok_ac, _ = _set_power_setting_index(subgroup, setting, ac=True, value=want)
        ok_dc, _ = _set_power_setting_index(subgroup, setting, ac=False, value=want)
        attempted.append(ok_ac or ok_dc)
        if ok_ac or ok_dc:
            return True, "OK"
    if any(attempted):
        return True, "OK"
    return False, "USB-Energiesparen nicht unterstützt."


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
