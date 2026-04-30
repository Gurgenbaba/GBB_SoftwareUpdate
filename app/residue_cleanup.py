from __future__ import annotations

import glob
import os
import queue
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None

from .models import ReportEntry

_HIVE_MAP = {
    "HKLM": lambda: winreg.HKEY_LOCAL_MACHINE if winreg else None,
    "HKEY_LOCAL_MACHINE": lambda: winreg.HKEY_LOCAL_MACHINE if winreg else None,
    "HKCU": lambda: winreg.HKEY_CURRENT_USER if winreg else None,
    "HKEY_CURRENT_USER": lambda: winreg.HKEY_CURRENT_USER if winreg else None,
}


def _expand_cleanup_placeholders(text: str) -> str:
    t = os.path.expandvars(text or "")
    repl = {
        "{PROGRAMFILES}": os.environ.get("ProgramFiles", r"C:\Program Files"),
        "{PROGRAMFILES(X86)}": os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        "{PROGRAMDATA}": os.environ.get("ProgramData", r"C:\ProgramData"),
        "{LOCALAPPDATA}": os.environ.get("LOCALAPPDATA", ""),
        "{APPDATA}": os.environ.get("APPDATA", ""),
        "{PUBLIC}": os.environ.get("PUBLIC", r"C:\Users\Public"),
    }
    for k, v in repl.items():
        t = t.replace(k, v)
    return t


def _safe_roots() -> list[Path]:
    roots: list[Path] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA", "APPDATA", "PUBLIC"):
        v = os.environ.get(env, "")
        if v:
            roots.append(Path(v))
    return [p for p in roots if p.parts]


def _is_under_safe_root(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in _safe_roots():
        try:
            resolved.relative_to(root.resolve())
            if resolved == root.resolve():
                return False
            return True
        except ValueError:
            continue
    return False


def _registry_key_exists(hive_name: str, subkey: str) -> bool:
    if winreg is None:
        return False
    fn = _HIVE_MAP.get(hive_name.strip().upper())
    if not fn or not subkey.strip():
        return False
    hive = fn()
    if hive is None:
        return False
    try:
        winreg.OpenKey(hive, subkey)
        return True
    except OSError:
        return False


def _iter_shortcut_search_roots() -> list[Path]:
    roots: list[Path] = []
    pd = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    roots.append(pd / "Microsoft" / "Windows" / "Start Menu")
    ap = os.environ.get("APPDATA", "")
    if ap:
        roots.append(Path(ap) / "Microsoft" / "Windows" / "Start Menu")
    pub = Path(os.environ.get("PUBLIC", r"C:\Users\Public"))
    roots.append(pub / "Desktop")
    hd = Path.home() / "Desktop"
    roots.append(hd)
    return [r for r in roots if r.exists()]


@dataclass
class CleanupItem:
    id: str
    kind: str  # path | registry | shortcut
    label: str
    target: str
    removable: bool
    skip_reason: str = ""


@dataclass
class CleanupResult:
    paths_found: list[str] = field(default_factory=list)
    registry_keys_found: list[str] = field(default_factory=list)
    shortcuts_found: list[str] = field(default_factory=list)
    removable_items: list[CleanupItem] = field(default_factory=list)
    skipped_items: list[CleanupItem] = field(default_factory=list)


def get_cleanup_config(software_providers: dict[str, Any], software_key: str) -> dict[str, Any]:
    raw = software_providers.get(software_key, {})
    if not isinstance(raw, dict):
        return {}
    c = raw.get("cleanup")
    c = c if isinstance(c, dict) else {}
    paths: list[str] = []
    for p in c.get("paths") or []:
        if isinstance(p, str) and p.strip():
            paths.append(p.strip())
    ident = raw.get("identity") if isinstance(raw.get("identity"), dict) else {}
    extra = ident.get("cleanup_paths") if isinstance(ident.get("cleanup_paths"), list) else []
    for p in extra:
        if isinstance(p, str) and p.strip() and p.strip() not in paths:
            paths.append(p.strip())
    return {
        "paths": paths,
        "registry": c.get("registry") if isinstance(c.get("registry"), list) else [],
        "shortcuts": c.get("shortcuts") if isinstance(c.get("shortcuts"), list) else [],
    }


def _cleanup_config_nonempty(cfg: dict[str, Any]) -> bool:
    for k in ("paths", "registry", "shortcuts"):
        v = cfg.get(k)
        if isinstance(v, list) and len(v) > 0:
            return True
    return False


def scan_residues(
    software_key: str,
    cleanup_cfg: dict[str, Any],
    logger,
) -> CleanupResult:
    result = CleanupResult()
    if not _cleanup_config_nonempty(cleanup_cfg):
        return result

    seen_paths: set[str] = set()
    for raw_pattern in cleanup_cfg.get("paths") or []:
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            continue
        pattern = _expand_cleanup_placeholders(raw_pattern.strip())
        try:
            glob_matches = [Path(p) for p in glob.glob(pattern, recursive=True)]
        except (OSError, ValueError) as exc:
            logger.debug("[CLEANUP] path pattern skipped %s: %s", pattern[:160], exc)
            continue
        for p in glob_matches:
            ps = str(p)
            if p.is_file() or p.is_dir():
                if ps in seen_paths:
                    continue
                seen_paths.add(ps)
                if ps not in result.paths_found:
                    result.paths_found.append(ps)
                safe = _is_under_safe_root(p)
                item = CleanupItem(
                    id=str(uuid.uuid4())[:12],
                    kind="path",
                    label=ps[:120],
                    target=ps,
                    removable=safe,
                    skip_reason="" if safe else "Ausserhalb erlaubter Installationspfade",
                )
                (result.removable_items if safe else result.skipped_items).append(item)

    for reg_entry in cleanup_cfg.get("registry") or []:
        if not isinstance(reg_entry, dict):
            continue
        hive = str(reg_entry.get("hive", "HKLM")).strip()
        sub = str(reg_entry.get("key", "")).strip()
        if not sub:
            continue
        if not _registry_key_exists(hive, sub):
            continue
        full = f"{hive}\\{sub}"
        if full not in result.registry_keys_found:
            result.registry_keys_found.append(full)
        item = CleanupItem(
            id=str(uuid.uuid4())[:12],
            kind="registry",
            label=full[:160],
            target=full,
            removable=True,
            skip_reason="",
        )
        result.removable_items.append(item)

    subs: list[str] = []
    for s in cleanup_cfg.get("shortcuts") or []:
        if isinstance(s, str) and s.strip():
            subs.append(s.lower())
    seen_shortcuts: set[str] = set()
    if subs:
        for root in _iter_shortcut_search_roots():
            try:
                for lnk in root.rglob("*.lnk"):
                    name_l = lnk.name.lower()
                    if any(t in name_l for t in subs):
                        ps = str(lnk)
                        if ps in seen_shortcuts:
                            continue
                        seen_shortcuts.add(ps)
                        if ps not in result.shortcuts_found:
                            result.shortcuts_found.append(ps)
                        safe = _is_under_safe_root(lnk)
                        item = CleanupItem(
                            id=str(uuid.uuid4())[:12],
                            kind="shortcut",
                            label=lnk.name[:120],
                            target=ps,
                            removable=safe,
                            skip_reason="" if safe else "Verknuepfung ausserhalb erlaubter Bereiche",
                        )
                        (result.removable_items if safe else result.skipped_items).append(item)
            except OSError:
                continue

    n = len(result.paths_found) + len(result.registry_keys_found) + len(result.shortcuts_found)
    logger.info("[CLEANUP] Found %s residue item(s) for %s", n, software_key)
    return result


def _delete_path_target(path_str: str, logger) -> bool:
    p = Path(path_str)
    if not p.exists():
        return True
    if not _is_under_safe_root(p):
        logger.warning("[CLEANUP] Skipped (not safe): %s", path_str[:200])
        return False
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=False)
        else:
            p.unlink(missing_ok=True)
        logger.info("[CLEANUP] Deleted path: %s", path_str[:240])
        return True
    except OSError as exc:
        logger.warning("[CLEANUP] Skipped (error): %s — %s", path_str[:200], exc)
        return False


def _delete_registry_target(full: str, logger) -> bool:
    try:
        cp = subprocess.run(
            ["reg", "delete", full, "/f"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            shell=False,
        )
        if cp.returncode == 0:
            logger.info("[CLEANUP] Deleted registry: %s", full[:240])
            return True
        logger.warning("[CLEANUP] Skipped registry RC=%s: %s", cp.returncode, full[:200])
        return False
    except OSError as exc:
        logger.warning("[CLEANUP] Skipped registry: %s — %s", full[:200], exc)
        return False


def _delete_shortcut_target(path_str: str, logger) -> bool:
    p = Path(path_str)
    if not p.exists() or p.suffix.lower() != ".lnk":
        return True
    if not _is_under_safe_root(p.parent):
        logger.warning("[CLEANUP] Skipped (not safe): %s", path_str[:200])
        return False
    try:
        p.unlink(missing_ok=True)
        logger.info("[CLEANUP] Deleted shortcut: %s", path_str[:240])
        return True
    except OSError as exc:
        logger.warning("[CLEANUP] Skipped shortcut: %s — %s", path_str[:200], exc)
        return False


def apply_selected_cleanup(items: list[CleanupItem], logger) -> tuple[int, int]:
    """Returns (removed_ok, failed)."""
    ok = 0
    failed = 0
    for it in items:
        if it.kind == "path":
            if _delete_path_target(it.target, logger):
                ok += 1
            else:
                failed += 1
        elif it.kind == "registry":
            if _delete_registry_target(it.target, logger):
                ok += 1
            else:
                failed += 1
        elif it.kind == "shortcut":
            if _delete_shortcut_target(it.target, logger):
                ok += 1
            else:
                failed += 1
    return ok, failed


def row_eligible_for_residue_phase(row: ReportEntry) -> bool:
    if (row.result or "") != "OK":
        return False
    if "entfernen" not in (row.action or "").lower():
        return False
    vs = (row.verification_status or "").strip().lower()
    if vs == "already_absent":
        return False
    if vs == "absent":
        return True
    if vs == "" and "Nicht installiert" in (row.status_after or ""):
        return True
    return False


def _classify_after_dialog(
    total_found: int,
    cancelled: bool,
    selected_count: int,
    removed_ok: int,
    failed: int,
    info_only: bool = False,
) -> str:
    if info_only and total_found > 0:
        return "manual_cleanup_needed"
    if failed > 0:
        return "manual_cleanup_needed"
    if total_found == 0:
        return "success_clean"
    if cancelled:
        return "success_with_residue"
    if removed_ok >= selected_count and selected_count > 0:
        return "success_clean"
    if selected_count == 0 and total_found > 0:
        return "success_with_residue"
    return "success_with_residue"


def run_residue_cleanup_phase(
    rows: list[ReportEntry],
    software_providers: dict[str, Any],
    software_display_name: Callable[[str], str],
    logger,
    ui_queue: queue.Queue,
) -> list[ReportEntry]:
    """
    After successful uninstall rows, optionally scan and interactively clean residues.
    Uses ui_queue: posts ('residue_cleanup_dialog', payload) and blocks on response_q.
    """
    out: list[ReportEntry] = []
    for row in rows:
        if not row_eligible_for_residue_phase(row):
            out.append(row)
            continue
        cfg = get_cleanup_config(software_providers, row.software_key)
        if not _cleanup_config_nonempty(cfg):
            out.append(row)
            continue
        cr = scan_residues(row.software_key, cfg, logger)
        total_found = len(cr.paths_found) + len(cr.registry_keys_found) + len(cr.shortcuts_found)
        if total_found == 0:
            out.append(
                replace(
                    row,
                    cleanup_items_found="0",
                    cleanup_items_removed="0",
                    cleanup_classification="success_clean",
                )
            )
            continue
        response_q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        ui_queue.put(
            (
                "residue_cleanup_dialog",
                {
                    "software_key": row.software_key,
                    "display_name": software_display_name(row.software_key),
                    "cleanup": cr,
                    "response_q": response_q,
                },
            )
        )
        try:
            resp = response_q.get(timeout=7200)
        except queue.Empty:
            resp = {"cancelled": True, "removed_ok": 0, "failed": 0, "selected_count": 0}
        cancelled = bool(resp.get("cancelled"))
        removed_ok = int(resp.get("removed_ok", 0) or 0)
        failed = int(resp.get("failed", 0) or 0)
        selected_count = int(resp.get("selected_count", 0) or 0)
        info_only = bool(resp.get("info_only"))
        clf = _classify_after_dialog(total_found, cancelled, selected_count, removed_ok, failed, info_only=info_only)
        out.append(
            replace(
                row,
                cleanup_items_found=str(total_found),
                cleanup_items_removed=str(removed_ok),
                cleanup_classification=clf,
            )
        )
    return out
