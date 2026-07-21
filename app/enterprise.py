"""Enterprise extensions: backup, provider order, install mode, UI-safe copy."""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .config import SOFTWARE_BY_KEY

if TYPE_CHECKING:
    from .models import ReportEntry

InstallMode = Literal["silent", "visible", "auto"]
ProviderId = Literal["local", "choco", "winget", "internal"]

_DEFAULT_PRIORITY: tuple[ProviderId, ...] = ("choco", "winget", "internal", "local")
_AVAYA_PRIORITY: tuple[ProviderId, ...] = ("winget", "internal", "choco", "local")


def _console_encoding() -> str:
    """OEM-Konsolen-Codepage statt ANSI/CP1252 (z. B. Deutsch meist CP850) — sonst stirbt der
    subprocess-Reader-Thread mit UnicodeDecodeError und die Ausgabe geht verloren."""
    if platform.system() != "Windows":
        return "utf-8"
    try:
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "cp850"


def parse_provider_priority(raw_cfg: dict[str, Any], *, software_key: str, prefer_local: bool) -> tuple[ProviderId, ...]:
    """Return ordered provider ids. Custom `provider_priority` list overrides defaults."""
    raw = raw_cfg.get("provider_priority")
    if isinstance(raw, list) and raw:
        out: list[ProviderId] = []
        for x in raw:
            s = str(x).strip().lower()
            if s in ("winget", "choco", "internal", "local"):
                pid: ProviderId = s  # type: ignore[assignment]
                if pid not in out:
                    out.append(pid)
        if out:
            return tuple(out)  # type: ignore[return-value]
    if software_key == "avaya_workplace":
        return _AVAYA_PRIORITY if not prefer_local else ("local", "winget", "internal", "choco")
    if prefer_local:
        return ("local", "choco", "winget", "internal")
    return _DEFAULT_PRIORITY


def parse_install_mode(raw_cfg: dict[str, Any]) -> InstallMode:
    v = str(raw_cfg.get("install_mode", "auto") or "auto").strip().lower()
    if v in ("silent", "visible", "auto"):
        return v  # type: ignore[return-value]
    return "auto"


def choco_use_native_installer_ui(
    *,
    install_mode: InstallMode,
    software_key: str,
    native_ui_keys: frozenset[str],
) -> bool:
    if install_mode == "visible":
        return True
    if install_mode == "silent":
        return False
    return software_key in native_ui_keys


def winget_interactive(
    *,
    install_mode: InstallMode,
    software_key: str,
    native_ui_keys: frozenset[str],
) -> bool:
    if install_mode == "visible":
        return True
    if install_mode == "silent":
        return False
    return software_key in native_ui_keys


def sanitize_public_detail(
    raw_detail: str,
    *,
    result_ok: bool,
    verification_absent: bool,
    cleanup_choco_ghost: bool,
) -> str:
    """User-facing row/dialog text; logs and CSV keep raw_detail."""
    d = (raw_detail or "").strip()
    if not result_ok:
        return d
    if cleanup_choco_ghost and verification_absent:
        return "Erfolgreich entfernt – veralteter Eintrag ignoriert"
    low = d.lower()
    if verification_absent and ("chocolatey" in low or "choco " in low) and ("rc=" in low or "exit code" in low or "fehlgeschlagen" in low):
        return "Erfolgreich entfernt – veralteter Eintrag ignoriert"
    return d


def try_restore_point_or_registry_export(logger, *, reports_dir: Path) -> tuple[bool, str]:
    """
    Option A: system restore point (requires admin + System Restore enabled).
    Option B: export uninstall registry hive to .reg (best-effort).
    """
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        ps = (
            "Checkpoint-Computer -Description 'Compexx-InstallTool pre-cleanup' -ErrorAction Stop;"
            "Write-Output 'RP_OK'"
        )
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
            encoding=_console_encoding(),
            errors="replace",
            timeout=120,
            check=False,
            shell=False,
        )
        if cp.returncode == 0 and "RP_OK" in (cp.stdout or ""):
            logger.info("[BACKUP] Restore point created")
            return True, "restore_point"
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("[BACKUP] Restore point failed: %s", exc)

    out_reg = reports_dir / f"backup_uninstall_hives_{stamp}.reg"
    try:
        if os.name == "nt":
            subprocess.run(
                ["reg", "export", "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall", str(out_reg), "/y"],
                capture_output=True,
                text=True,
                encoding=_console_encoding(),
                errors="replace",
                timeout=60,
                check=False,
                shell=False,
            )
            if out_reg.is_file() and out_reg.stat().st_size > 0:
                logger.info("[BACKUP] Registry export created: %s", out_reg)
                return True, str(out_reg)
    except OSError as exc:
        logger.warning("[BACKUP] Registry export failed: %s", exc)
    logger.warning("[BACKUP] No restore point or registry export created")
    return False, ""


def apply_choco_ghost_cleanup_after_uninstall(rows: list[Any], choco, logger) -> list[Any]:
    """If verification says absent but Chocolatey still lists the package, strip stale metadata."""
    out: list[Any] = []
    for row in rows:
        if not hasattr(row, "software_key"):
            out.append(row)
            continue
        vs = (getattr(row, "verification_status", "") or "").strip().lower()
        if (getattr(row, "result", "") or "") != "OK":
            out.append(row)
            continue
        if "entfernen" not in (getattr(row, "action", "") or "").lower():
            out.append(row)
            continue
        if vs != "absent":
            out.append(row)
            continue
        pkg = (getattr(row, "package_name", "") or "").strip().lower()
        if not pkg:
            sw = SOFTWARE_BY_KEY.get(row.software_key)
            pkg = (sw.primary_package or "").strip().lower() if sw else ""
        if not pkg or not choco.is_choco_installed():
            out.append(row)
            continue
        local = choco.list_local()
        if pkg not in local:
            out.append(row)
            continue
        res = choco.uninstall_remove_metadata_only(pkg)
        flag = "cleanup_choco_ghost=true" if res.ok else "cleanup_choco_ghost=failed"
        logger.info("[CHOCO-GHOST] %s for package %s (ok=%s)", flag, pkg, res.ok)
        prev_meta = getattr(row, "extended_metadata", "") or ""
        sep = ";" if prev_meta else ""
        out.append(replace(row, extended_metadata=f"{prev_meta}{sep}{flag}"))
    return out
