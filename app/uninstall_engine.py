from __future__ import annotations

import copy
import shlex
import subprocess
import os
import shutil
import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import winreg
except ImportError:  # pragma: no cover - Windows only
    winreg = None

from .config import INSTALL_TIMEOUT_SECONDS, SOFTWARE_ALIASES, SOFTWARE_BY_KEY, SoftwarePackage
from .enterprise import parse_install_mode
from .identity import normalize_identity_block, term_matches_display
from .models import SoftwareState
from .office_uninstall import (
    office_removal_tools_available,
    office_tools_configured,
    run_office_removal_strategies,
    stop_safe_office_processes,
)
from .scanner import PostUninstallVerification, SoftwareScanner

EXCERPT_LEN = 600

_DEFAULT_UNINSTALL: dict[str, dict[str, Any]] = {
    "citrix_workspace": {
        "processes": ["*citrix*.exe", "*selfservice*.exe", "*wfcrun32.exe"],
        "registry_names": ["citrix", "workspace", "receiver"],
        "appx_names": [],
        "uninstall_silent_args": [],
    },
    "microsoft_teams": {
        "processes": ["*teams*.exe", "*ms-teams*.exe", "*teamsbootstrapper*.exe", "msteams.exe", "ms-teams.exe"],
        "registry_names": ["teams", "microsoft teams"],
        "appx_names": ["MSTeams", "MicrosoftTeams", "Teams"],
        "uninstall_silent_args": [],
    },
    "office365business": {
        "processes": [
            "winword.exe",
            "excel.exe",
            "powerpnt.exe",
            "outlook.exe",
            "onenote.exe",
            "msaccess.exe",
            "mspub.exe",
            "visio.exe",
            "winproj.exe",
            "teams.exe",
            "ms-teams.exe",
            "*officeclicktorun*.exe",
        ],
        "registry_names": ["microsoft 365", "office", "m365", "click-to-run", "office 16"],
        "appx_names": [],
        "uninstall_silent_args": [],
    },
    "adobe_reader": {"processes": ["*acrord32.exe", "*acrobat*.exe"], "registry_names": ["adobe", "acrobat", "reader"], "appx_names": [], "uninstall_silent_args": []},
    "teamviewer": {"processes": ["*teamviewer*.exe"], "registry_names": ["teamviewer"], "appx_names": [], "uninstall_silent_args": []},
    "filezilla": {"processes": ["*filezilla*.exe"], "registry_names": ["filezilla"], "appx_names": [], "uninstall_silent_args": []},
    "firefox": {"processes": ["*firefox*.exe"], "registry_names": ["firefox", "mozilla"], "appx_names": [], "uninstall_silent_args": []},
    "opentext": {"processes": ["*opentext*.exe"], "registry_names": ["opentext"], "appx_names": [], "uninstall_silent_args": []},
    "opentext_core_endpoint": {"processes": ["*wr*.exe", "*wsa*.exe"], "registry_names": ["webroot"], "appx_names": [], "uninstall_silent_args": []},
    "avaya_workplace": {"processes": ["*avaya*.exe"], "registry_names": ["avaya"], "appx_names": [], "uninstall_silent_args": []},
}


def merge_uninstall_profile(software_key: str, provider_cfg: dict[str, Any]) -> dict[str, Any]:
    base = copy.deepcopy(_DEFAULT_UNINSTALL.get(software_key, {"processes": [], "registry_names": [], "appx_names": [], "uninstall_silent_args": []}))
    raw = provider_cfg.get("uninstall") if isinstance(provider_cfg, dict) else None
    if not isinstance(raw, dict):
        return base
    for key, val in raw.items():
        if key in ("processes", "registry_names", "appx_names", "uninstall_silent_args") and isinstance(val, list) and isinstance(base.get(key), list):
            merged = [*base[key], *[str(x) for x in val if str(x).strip()]]
            base[key] = list(dict.fromkeys(merged))
        else:
            base[key] = val
    ident = normalize_identity_block(provider_cfg if isinstance(provider_cfg, dict) else {})
    if ident["registry_names"]:
        base["registry_names"] = list(ident["registry_names"])
    if ident["safe_processes"]:
        proc = base.get("processes") if isinstance(base.get("processes"), list) else []
        base["processes"] = list(dict.fromkeys([*proc, *ident["safe_processes"]]))
    return base


@dataclass
class UninstallAttempt:
    method: str
    outcome: str
    return_code: int | None = None
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    detail: str = ""


@dataclass
class UninstallEngineResult:
    software_key: str
    display_name: str
    action: str
    status: str
    method_used: str
    attempts: list[UninstallAttempt] = field(default_factory=list)
    reboot_required: bool = False
    manual_reason: str = ""
    error_summary: str = ""
    final_state: SoftwareState | None = None
    ui_action: str = "Entfernen"
    verification_status: str = ""
    verification_evidence: str = ""
    stale_evidence_ignored: str = ""
    office_removal_strategy_used: str = ""
    office_removal_exit_code: str = ""
    office_removal_config_path: str = ""
    office_removal_verification: str = ""
    office_removal_guidance: bool = False


def _office_removal_result_fields(software_key: str, office_meta: dict[str, str]) -> dict[str, str]:
    if software_key != "office365business":
        return {
            "office_removal_strategy_used": "",
            "office_removal_exit_code": "",
            "office_removal_config_path": "",
            "office_removal_verification": "",
        }
    return {
        "office_removal_strategy_used": office_meta.get("office_removal_strategy_used", ""),
        "office_removal_exit_code": office_meta.get("office_removal_exit_code", ""),
        "office_removal_config_path": office_meta.get("office_removal_config_path", ""),
        "office_removal_verification": office_meta.get("office_removal_verification", ""),
    }


def enumerate_uninstall_entries() -> list[dict[str, str]]:
    """Alle Uninstall-Einträge aus HKLM/HKCU (inkl. WOW6432Node)."""
    if winreg is None:
        return []
    entries: list[dict[str, str]] = []
    hives = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]
    paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for hive in hives:
        for base_path in paths:
            try:
                with winreg.OpenKey(hive, base_path) as root:
                    i = 0
                    while True:
                        try:
                            sub_name = winreg.EnumKey(root, i)
                        except OSError:
                            break
                        i += 1
                        key_path = f"{base_path}\\{sub_name}"
                        try:
                            with winreg.OpenKey(hive, key_path) as sk:

                                def _get(name: str, _sk=sk) -> str:
                                    try:
                                        value, _ = winreg.QueryValueEx(_sk, name)
                                        return str(value or "").strip()
                                    except OSError:
                                        return ""

                                entries.append(
                                    {
                                        "hive": "HKLM" if hive == winreg.HKEY_LOCAL_MACHINE else "HKCU",
                                        "key_path": key_path,
                                        "display_name": _get("DisplayName"),
                                        "uninstall_string": _get("UninstallString"),
                                        "quiet_uninstall_string": _get("QuietUninstallString"),
                                        "install_location": _get("InstallLocation"),
                                        "display_icon": _get("DisplayIcon"),
                                    }
                                )
                        except OSError:
                            continue
            except OSError:
                continue
    return entries


def detect_msi_guid(text: str) -> str | None:
    m = re.search(r"\{[0-9A-Fa-f-]{36}\}", text or "", flags=re.I)
    return m.group(0).upper() if m else None


def append_silent_args_if_safe(cmd: list[str], extra: list[str]) -> list[str]:
    if not cmd or not extra:
        return cmd
    out = list(cmd)
    low = [a.lower() for a in out]
    for a in extra:
        al = a.lower()
        if al not in low:
            out.append(a)
            low.append(al)
    return out


def run_uninstall_command(
    cmd: list[str],
    *,
    cwd: str | None,
    timeout: int,
    logger,
    label: str,
) -> tuple[int, str, str]:
    logger.debug("run_uninstall_command %s: %s", label, cmd)
    try:
        cp = subprocess.run(
            cmd,
            check=False,
            timeout=timeout,
            capture_output=True,
            text=True,
            shell=False,
            cwd=cwd,
        )
        so = (cp.stdout or "")[:EXCERPT_LEN]
        se = (cp.stderr or "")[:EXCERPT_LEN]
        return cp.returncode, so, se
    except Exception as exc:  # pylint: disable=broad-except
        return -1, "", str(exc)[:EXCERPT_LEN]


class MultiStageUninstallEngine:
    @staticmethod
    def _contains_term(name: str, term: str) -> bool:
        clean_name = (name or "").lower()
        clean_term = (term or "").strip().lower()
        if not clean_name or not clean_term:
            return False
        if re.search(rf"(?<![a-z0-9]){re.escape(clean_term)}(?![a-z0-9])", clean_name):
            return True
        return False

    def __init__(
        self,
        choco_client,
        winget_client,
        logger,
        software_by_key: dict[str, SoftwarePackage] | None = None,
        scanner=None,
        software_providers: dict[str, Any] | None = None,
        office_tools: dict[str, Any] | None = None,
    ) -> None:
        self.choco = choco_client
        self.winget = winget_client
        self.logger = logger
        self.software_by_key = software_by_key or SOFTWARE_BY_KEY
        self.scanner = scanner
        self.software_providers = software_providers or {}
        self.office_tools = office_tools if isinstance(office_tools, dict) else {}

    def _ulog(self, software: SoftwarePackage, msg: str, *args: Any) -> None:
        """Log an uninstall line; supports ``msg`` only or ``msg % args`` like ``logger.info``."""
        if args:
            try:
                rendered = msg % args
            except (TypeError, ValueError):
                rendered = f"{msg} | unformatted_args={args!r}"
        else:
            rendered = msg
        self.logger.info("[UNINSTALL] %s: %s", software.display_name, rendered)

    def execute(
        self,
        software: SoftwarePackage,
        prev: SoftwareState,
        dry_run: bool,
        method_progress_callback: Callable[[str, str], None] | None = None,
        office_removal_generic_ack: bool = False,
    ) -> UninstallEngineResult:
        attempts: list[UninstallAttempt] = []
        office_meta: dict[str, str] = {
            "office_removal_strategy_used": "",
            "office_removal_exit_code": "",
            "office_removal_config_path": "",
            "office_removal_verification": "",
        }
        profile = merge_uninstall_profile(software.key, self.software_providers.get(software.key, {}))

        def progress(label: str) -> None:
            if method_progress_callback:
                method_progress_callback(software.key, label)
            self._ulog(software, f"Trying {label}...")

        def add_att(method: str, outcome: str, rc: int | None = None, so: str = "", se: str = "", det: str = "") -> None:
            attempts.append(
                UninstallAttempt(
                    method=method,
                    outcome=outcome,
                    return_code=rc,
                    stdout_excerpt=(so or "")[:EXCERPT_LEN],
                    stderr_excerpt=(se or "")[:EXCERPT_LEN],
                    detail=det[:EXCERPT_LEN],
                )
            )
            if outcome == "failed":
                self._ulog(software, f"{method} failed RC={rc} {det[:120]}")
            elif outcome == "success":
                self._ulog(software, f"SUCCESS via {method}")
            elif outcome == "skipped":
                self._ulog(software, f"{method} skipped: {det[:120]}")

        if prev.status == "Nicht installiert":
            add_att("precheck", "skipped", det="already absent")
            st = SoftwareState("Nicht installiert", package_name=prev.package_name, provider=prev.provider or "")
            return UninstallEngineResult(
                software.key,
                software.display_name,
                "uninstall",
                "already_absent",
                "",
                attempts,
                False,
                "",
                "",
                st,
                "Keine Aktion erforderlich",
                verification_status="already_absent",
                verification_evidence="Vor der Deinstallation als nicht installiert erkannt.",
                stale_evidence_ignored="",
                **_office_removal_result_fields(software.key, office_meta),
            )

        if (
            software.key == "office365business"
            and not dry_run
            and not office_removal_tools_available(self.office_tools, self.logger)
            and not office_removal_generic_ack
        ):
            manual_reason = (
                "Kein erweitertes Office-Removal-Tool gefunden. "
                "Für sauberes Entfernen bitte ODT oder Microsoft Get Help bereitstellen."
            )
            add_att("precheck", "skipped", det="no_office_removal_tools_without_generic_ack")
            st = SoftwareState(
                "Hinweis: Office-Removal-Tool fehlt",
                detail=manual_reason[:500],
                provider="Intern",
                package_name=prev.package_name,
            )
            return UninstallEngineResult(
                software.key,
                software.display_name,
                "uninstall",
                "manual_required",
                "",
                attempts,
                False,
                manual_reason,
                "",
                st,
                "Manuelle Deinstallation",
                verification_status="",
                verification_evidence="",
                stale_evidence_ignored="",
                **_office_removal_result_fields(software.key, office_meta),
                office_removal_guidance=True,
            )

        cleanup_messages: list[str] = []
        hard_failures: list[str] = []
        citrix_native_ok = False
        method_used = ""
        reboot_required = False

        if dry_run:
            progress("Dry-Run (Plan)")
            add_att(
                "dry_run",
                "success",
                det="Would run: Office safe process stop, Get Help/ODT/SaRA (if configured), process-stop, Chocolatey, WinGet, Registry, MSI, AppX, vendor",
            )
            if software.key == "office365business":
                kill_svc = bool(self.office_tools.get("kill_click_to_run_service", False))
                stop_safe_office_processes(
                    logger=self.logger,
                    dry_run=True,
                    kill_click_to_run_service=kill_svc,
                )
                im = parse_install_mode(self.software_providers.get(software.key, {}))
                oc = run_office_removal_strategies(
                    office_tools=self.office_tools,
                    logger=self.logger,
                    dry_run=True,
                    add_att=add_att,
                    scanner=self.scanner,
                    software=software,
                    install_mode=im,
                )
                office_meta["office_removal_strategy_used"] = oc.strategy_used
                office_meta["office_removal_exit_code"] = oc.exit_code
                office_meta["office_removal_config_path"] = oc.config_path
                office_meta["office_removal_verification"] = oc.verification
            st = SoftwareState(
                "Dry-Run (unveraendert)",
                detail="DRY-RUN: Multi-Stage Uninstall wuerde ausgefuehrt",
                package_name=prev.package_name,
                provider=prev.provider or "Intern",
            )
            return UninstallEngineResult(
                software.key,
                software.display_name,
                "uninstall",
                "skipped",
                "dry_run",
                attempts,
                False,
                "",
                "",
                st,
                "DRY-RUN: wuerde entfernen",
                **_office_removal_result_fields(software.key, office_meta),
            )

        if software.key == "office365business":
            progress("Office sicher beenden")
            kill_svc = bool(self.office_tools.get("kill_click_to_run_service", False))
            stop_safe_office_processes(
                logger=self.logger,
                dry_run=False,
                kill_click_to_run_service=kill_svc,
            )
            progress("Office removal (Get Help / ODT / SaRA)")
            im = parse_install_mode(self.software_providers.get(software.key, {}))
            oc = run_office_removal_strategies(
                office_tools=self.office_tools,
                logger=self.logger,
                dry_run=False,
                add_att=add_att,
                scanner=self.scanner,
                software=software,
                install_mode=im,
            )
            office_meta["office_removal_strategy_used"] = oc.strategy_used
            office_meta["office_removal_exit_code"] = oc.exit_code
            office_meta["office_removal_config_path"] = oc.config_path
            office_meta["office_removal_verification"] = oc.verification
            if oc.summary_notes:
                cleanup_messages.append(oc.summary_notes[:400])
            if oc.any_tool_exit_ok:
                method_used = method_used or (oc.strategy_used or "Office removal tools")
            try:
                rc_i = int(oc.exit_code) if str(oc.exit_code).strip() else None
            except ValueError:
                rc_i = None
            if rc_i in (3010, 1641):
                reboot_required = True

        progress("Process stop (related)")
        killed, kill_detail = self._terminate_related_processes(software, dry_run=False, profile=profile)
        if killed > 0:
            cleanup_messages.append(f"Lock-Cleanup: {killed} Prozess(e) beendet")
            add_att("process_stop", "success", det=f"{killed} processes")
        else:
            add_att("process_stop", "skipped", det="none matched" if not kill_detail else kill_detail[:200])
        if kill_detail:
            cleanup_messages.append(kill_detail)

        if prev.provider == "Chocolatey" and prev.package_name:
            progress("Chocolatey")
            result = self.choco.run(["uninstall", prev.package_name, "-y"], timeout=INSTALL_TIMEOUT_SECONDS)
            choco_msg = (result.stderr or result.stdout or "").strip()
            if choco_msg:
                cleanup_messages.append(choco_msg)
            choco_failed = result.returncode != 0
            add_att(
                "Chocolatey",
                "failed" if choco_failed else "success",
                result.returncode,
                (result.stdout or "")[:EXCERPT_LEN],
                (result.stderr or "")[:EXCERPT_LEN],
            )
            if choco_failed:
                hard_failures.append(f"Chocolatey RC={result.returncode}")
                self._ulog(software, f"Chocolatey failed RC={result.returncode}")
            else:
                method_used = method_used or "Chocolatey"
            if choco_failed and software.key == "citrix_workspace":
                native_ok, native_msg = self._try_citrix_workspace_native_uninstall(False)
                if native_msg:
                    cleanup_messages.append(native_msg)
                if native_ok:
                    citrix_native_ok = True
                    method_used = "Citrix native"
                    hard_failures = [h for h in hard_failures if not h.startswith("Chocolatey RC=")]

        if software.winget_id and self.winget.is_available():
            progress("WinGet")
            result = self.winget.uninstall(software.winget_id)
            winget_msg = (result.stderr or result.stdout or "").strip()
            if winget_msg:
                cleanup_messages.append(winget_msg)
            wg_ok = result.returncode == 0
            wg_nf = False
            if not wg_ok:
                winget_msg_l = winget_msg.lower()
                wg_nf = (
                    "no installed package found" in winget_msg_l
                    or "kein installiertes paket gefunden" in winget_msg_l
                    or "es wurde kein installiertes paket gefunden" in winget_msg_l
                )
                if not wg_nf:
                    hard_failures.append(f"WinGet RC={result.returncode}")
            add_att("WinGet", "success" if wg_ok else ("skipped" if wg_nf else "failed"), result.returncode, (result.stdout or "")[:EXCERPT_LEN], (result.stderr or "")[:EXCERPT_LEN])
            if wg_ok:
                method_used = method_used or "WinGet"
                self._ulog(software, "SUCCESS via WinGet")
            elif wg_nf:
                self._ulog(software, "WinGet skipped (not in WinGet inventory)")
            else:
                self._ulog(software, f"WinGet failed RC={result.returncode}")

        if software.key == "citrix_workspace" and not citrix_native_ok:
            progress("Citrix vendor native")
            native_ok, native_msg = self._try_citrix_workspace_native_uninstall(False)
            if native_msg:
                cleanup_messages.append(native_msg)
            add_att("Citrix native", "success" if native_ok else "failed", det=native_msg[:EXCERPT_LEN])
            if native_ok:
                citrix_native_ok = True
                method_used = method_used or "Citrix native"
                hard_failures = [h for h in hard_failures if not (h.startswith("Chocolatey RC=") or h.startswith("WinGet RC="))]

        if software.key == "office365business":
            progress("Office Click-to-Run")
            c2r_ok, c2r_msg = self._try_office_click_to_run_uninstall()
            if c2r_msg:
                cleanup_messages.append(c2r_msg)
            add_att("Office Click-to-Run", "success" if c2r_ok else "failed", det=c2r_msg[:EXCERPT_LEN])
            if c2r_ok:
                method_used = method_used or "Office Click-to-Run"
                hard_failures = [h for h in hard_failures if not (h.startswith("WinGet RC=") or h.startswith("Chocolatey RC="))]

        progress("Registry UninstallString / QuietUninstallString")
        removed_count, cleanup_detail = self._force_registry_cleanup(software, prev, profile)
        if removed_count > 0:
            method_used = method_used or "Registry"
            add_att("Registry", "success", det=f"removed_ok={removed_count}")
        else:
            add_att("Registry", "failed" if cleanup_detail else "skipped", det=cleanup_detail[:EXCERPT_LEN] if cleanup_detail else "no match")
        if cleanup_detail:
            cleanup_messages.append(cleanup_detail)

        progress("MSI product codes (ARP)")
        msi_rc = self._msi_guid_uninstall_pass(software, prev, profile, add_att)
        if msi_rc:
            method_used = method_used or "MSI"

        if profile.get("appx_names"):
            progress("AppX / MSIX")
            appx_ok, appx_msg = self._try_appx_removal(profile["appx_names"])
            add_att("AppX", "success" if appx_ok else "failed", det=appx_msg[:EXCERPT_LEN])
            if appx_ok:
                method_used = method_used or "AppX"
            if appx_msg:
                cleanup_messages.append(appx_msg)

        vu = profile.get("vendor_uninstall")
        if isinstance(vu, dict) and str(vu.get("type", "")).lower() == "command":
            progress("Vendor command (config)")
            exe = str(vu.get("path", "") or "").strip()
            args = vu.get("args") if isinstance(vu.get("args"), list) else []
            if exe and Path(exe).is_file():
                cmd = [exe, *[str(a) for a in args]]
                rc, so, se = run_uninstall_command(cmd, cwd=str(Path(exe).parent), timeout=INSTALL_TIMEOUT_SECONDS, logger=self.logger, label="vendor")
                add_att("vendor_uninstall", "success" if rc in (0, 3010, 1641) else "failed", rc, so, se)
                if rc in (0, 3010, 1641):
                    method_used = method_used or "vendor_uninstall"
                    if rc in (3010, 1641):
                        reboot_required = True
            else:
                add_att("vendor_uninstall", "skipped", det="path missing")

        post_state: SoftwareState | None = None
        raw_scan_absent = False
        verification = PostUninstallVerification(
            still_present=True,
            verification_status="unknown",
            evidence="",
            stale_evidence_ignored="",
        )
        if self.scanner:
            progress("Post-uninstall detection (scan)")
            fresh = self.scanner.scan()
            post_state = fresh.get(software.key)
            raw_scan_absent = post_state is not None and post_state.status == "Nicht installiert"
            progress("Post-uninstall verification (strict)")
            verification = self.scanner.post_uninstall_verify(software)
            self._ulog(
                software,
                "Post-verify: status=%s still_present=%s raw_scan_absent=%s | evidence=%s | stale_ignored=%s",
                verification.verification_status,
                verification.still_present,
                raw_scan_absent,
                (verification.evidence or "")[:320],
                (verification.stale_evidence_ignored or "")[:320],
            )
            if raw_scan_absent != (not verification.still_present):
                self._ulog(
                    software,
                    "Scan vs strict verify differ: scan_absent=%s strict_absent=%s (using strict for result)",
                    raw_scan_absent,
                    not verification.still_present,
                )
            scan_hint = (post_state.status if post_state else "?")[:80]
            add_att(
                "post_scan",
                "success" if not verification.still_present else "failed",
                det=f"strict={verification.verification_status}; scan={scan_hint}",
            )
        else:
            reg_still = self._registry_has_actionable_match(software, prev, profile)
            verification = PostUninstallVerification(
                still_present=reg_still,
                verification_status="registry_heuristic",
                evidence="Kein Scanner; nur Registry-Heuristik.",
                stale_evidence_ignored="",
            )
            add_att("post_scan", "success" if not reg_still else "failed", det="registry heuristic (no scanner)")

        absent = not verification.still_present

        deep_detail = ""
        if absent:
            deep_removed, deep_detail = self._deep_cleanup_leftovers(software, dry_run=False)
            if deep_removed > 0:
                cleanup_messages.append(f"Deep-Cleanup entfernt: {deep_removed} Rest(e)")
            if deep_detail:
                cleanup_messages.append(deep_detail)
            add_att("post_cleanup", "success", det=deep_detail[:200] if deep_detail else "ok")
        else:
            add_att("post_cleanup", "skipped", det="app still detected - no destructive cleanup")

        self._append_access_denied_hint(cleanup_messages)
        detail_blob = " | ".join(m for m in cleanup_messages if m).strip()[:500]

        office_guidance_context = (
            software.key == "office365business"
            and office_removal_generic_ack
            and not office_removal_tools_available(self.office_tools, self.logger)
        )

        if absent:
            st = SoftwareState("Nicht installiert", detail=detail_blob, provider="Intern", package_name=prev.package_name)
            return UninstallEngineResult(
                software.key,
                software.display_name,
                "uninstall",
                "success",
                method_used or "detection",
                attempts,
                reboot_required,
                "",
                "",
                st,
                "Entfernen",
                verification_status=verification.verification_status,
                verification_evidence=verification.evidence,
                stale_evidence_ignored=verification.stale_evidence_ignored,
                **_office_removal_result_fields(software.key, office_meta),
            )

        manual_reason = ""
        if verification.verification_status == "access_denied":
            manual_reason = (
                "Office/Click-to-Run konnte nicht verifiziert werden (Registry: Zugriff verweigert). "
                "Als Administrator ausfuehren, Office-Anwendungen schliessen oder ODT/SaRA nutzen."
            )
            if software.key == "office365business" and not office_tools_configured(
                self.office_tools, logger=self.logger
            ):
                manual_reason += (
                    " Erweitertes Removal: office_tools.get_help_cmd_path oder odt_setup_path (+ remove.xml) setzen."
                )
            st = SoftwareState(
                "Fehler: Manuelle Deinstallation nötig",
                detail=" | ".join(x for x in (detail_blob, manual_reason, verification.evidence) if x).strip()[:500],
                provider="Intern",
            )
            add_att("final", "failed", det=manual_reason[:EXCERPT_LEN])
            return UninstallEngineResult(
                software.key,
                software.display_name,
                "uninstall",
                "manual_required",
                method_used,
                attempts,
                reboot_required,
                manual_reason,
                detail_blob,
                st,
                "Manuelle Deinstallation",
                verification_status=verification.verification_status,
                verification_evidence=verification.evidence,
                stale_evidence_ignored=verification.stale_evidence_ignored,
                **_office_removal_result_fields(software.key, office_meta),
                office_removal_guidance=office_guidance_context,
            )

        if self._registry_has_actionable_match(software, prev, profile):
            blob_lower = detail_blob.lower()
            if "zugriff verweigert" in blob_lower or "winerror 5" in blob_lower or "access is denied" in blob_lower:
                manual_reason = (
                    "Close Office apps or run elevated / device policy may block silent uninstall. "
                    "Siehe auch Microsoft SaRA."
                )
            else:
                manual_reason = "ARP/Registry still lists actionable uninstall entries after all automated methods."
            st = SoftwareState(
                "Fehler: Manuelle Deinstallation nötig",
                detail=" | ".join(x for x in (detail_blob, manual_reason, verification.evidence) if x).strip()[:500],
                provider="Intern",
            )
            add_att("final", "failed", det=manual_reason[:EXCERPT_LEN])
            return UninstallEngineResult(
                software.key,
                software.display_name,
                "uninstall",
                "manual_required",
                method_used,
                attempts,
                reboot_required,
                manual_reason,
                detail_blob,
                st,
                "Manuelle Deinstallation",
                verification_status=verification.verification_status,
                verification_evidence=verification.evidence,
                stale_evidence_ignored=verification.stale_evidence_ignored,
                **_office_removal_result_fields(software.key, office_meta),
                office_removal_guidance=office_guidance_context,
            )

        if hard_failures:
            if office_guidance_context:
                manual_reason = (
                    "Kein erweitertes Office-Removal-Tool gefunden; generische Deinstallation fehlgeschlagen: "
                    + "; ".join(hard_failures)
                )[:900]
                st = SoftwareState(
                    "Fehler: Manuelle Deinstallation nötig",
                    detail=" | ".join(x for x in (detail_blob, manual_reason) if x).strip()[:500],
                    provider="Intern",
                )
                add_att("final", "failed", det=manual_reason[:EXCERPT_LEN])
                return UninstallEngineResult(
                    software.key,
                    software.display_name,
                    "uninstall",
                    "manual_required",
                    method_used,
                    attempts,
                    reboot_required,
                    manual_reason,
                    detail_blob,
                    st,
                    "Manuelle Deinstallation",
                    verification_status=verification.verification_status,
                    verification_evidence=verification.evidence,
                    stale_evidence_ignored=verification.stale_evidence_ignored,
                    **_office_removal_result_fields(software.key, office_meta),
                    office_removal_guidance=True,
                )
            st = SoftwareState("Fehler: Deinstallation fehlgeschlagen", detail=" | ".join([*hard_failures, detail_blob])[:500], provider="Intern")
            add_att("final", "failed", det=";".join(hard_failures)[:200])
            return UninstallEngineResult(
                software.key,
                software.display_name,
                "uninstall",
                "failed",
                method_used,
                attempts,
                reboot_required,
                "",
                "; ".join(hard_failures),
                st,
                "Entfernen",
                verification_status=verification.verification_status,
                verification_evidence=verification.evidence,
                stale_evidence_ignored=verification.stale_evidence_ignored,
                **_office_removal_result_fields(software.key, office_meta),
            )

        manual_reason = (
            verification.evidence
            or "Post-verify meldet die Software weiterhin als installiert; keine stille ARP-Route mehr."
        )
        if software.key == "office365business":
            if office_guidance_context:
                manual_reason = (
                    "Kein erweitertes Office-Removal-Tool gefunden; generische Deinstallation hat Office nicht zuverlässig entfernt. "
                    + manual_reason
                )[:900]
            elif not office_tools_configured(self.office_tools, logger=self.logger):
                manual_reason = (
                    "Office benötigt erweitertes Removal: Get Help CLI oder ODT konfigurieren (config.json → office_tools). "
                    + manual_reason
                )[:900]
            else:
                manual_reason = (
                    "Office weiterhin erkannt trotz konfigurierter Removal-Tools; Neustart oder manuelle Schritte prüfen. "
                    + manual_reason
                )[:900]
        st = SoftwareState(
            "Fehler: Manuelle Deinstallation nötig",
            detail=" | ".join(x for x in (detail_blob, manual_reason) if x).strip()[:500],
            provider="Intern",
        )
        add_att("final", "failed", det=manual_reason[:EXCERPT_LEN])
        return UninstallEngineResult(
            software.key,
            software.display_name,
            "uninstall",
            "manual_required",
            method_used,
            attempts,
            reboot_required,
            manual_reason,
            detail_blob,
            st,
            "Manuelle Deinstallation",
            verification_status=verification.verification_status,
            verification_evidence=verification.evidence,
            stale_evidence_ignored=verification.stale_evidence_ignored,
            **_office_removal_result_fields(software.key, office_meta),
            office_removal_guidance=office_guidance_context,
        )

    @staticmethod
    def _parse_cmdline(text: str) -> list[str]:
        raw = (text or "").strip()
        if not raw:
            return []
        try:
            return [part for part in shlex.split(raw, posix=False) if part]
        except ValueError:
            return [part for part in raw.split() if part]

    @staticmethod
    def _exe_from_cmd(cmd: list[str]) -> str:
        if not cmd:
            return ""
        return Path(cmd[0]).name.lower()

    @staticmethod
    def _to_silent_uninstall(cmd: list[str]) -> list[str]:
        if not cmd:
            return []
        exe = MultiStageUninstallEngine._exe_from_cmd(cmd)
        lower = [arg.lower() for arg in cmd]
        joined_l = " ".join(cmd).lower()
        # Microsoft Teams (per-user): Update.exe nutzt --uninstall -s; blindes /S erzeugt WinError 87.
        teams_update = exe == "update.exe" and ("teams" in joined_l or r"\microsoft\teams" in joined_l)
        if teams_update:
            out = list(cmd)
            ls = [a.lower() for a in out]
            if "--uninstall" not in joined_l:
                out.append("--uninstall")
            if "-s" not in ls and "/s" not in ls:
                out.append("-s")
            return out
        if exe == "msiexec.exe" or exe == "msiexec":
            out = [cmd[0]]
            for arg in cmd[1:]:
                arg_l = arg.lower()
                if arg_l.startswith("/i{"):
                    out.extend(["/x", arg[2:]])
                    continue
                if arg_l.startswith("/x{"):
                    out.extend(["/x", arg[2:]])
                    continue
                out.append(arg)

            out_lower = [arg.lower() for arg in out]
            if "/i" in out_lower:
                idx = out_lower.index("/i")
                out[idx] = "/x"
                out_lower[idx] = "/x"
            if "/x" not in out_lower and "/uninstall" not in out_lower:
                out.insert(1, "/x")
                out_lower = [arg.lower() for arg in out]

            has_target = any(
                (not arg.startswith("/")) or (arg.startswith("{") and arg.endswith("}"))
                for arg in out[1:]
            )
            if not has_target:
                return []
            if not re.search(r"\{[0-9A-Fa-f-]{36}\}", " ".join(out)):
                return []

            if "/qn" not in out_lower:
                out.append("/qn")
            if "/norestart" not in out_lower:
                out.append("/norestart")
            return out
        if "/quiet" not in lower and "/qn" not in lower and "/s" not in lower and "/silent" not in lower:
            return [*cmd, "/S"]
        return cmd

    def _iter_uninstall_entries(self) -> list[dict[str, str]]:
        return enumerate_uninstall_entries()

    def _msi_guid_uninstall_pass(
        self,
        software: SoftwarePackage,
        prev: SoftwareState,
        profile: dict[str, Any],
        add_att: Callable[..., None],
    ) -> bool:
        seen: set[str] = set()
        any_ok = False
        for entry in self._iter_uninstall_entries():
            if not self._matches_prev(software, prev, entry.get("display_name", ""), profile):
                continue
            for raw in (entry.get("uninstall_string") or "", entry.get("quiet_uninstall_string") or ""):
                g = detect_msi_guid(raw)
                if not g or g in seen:
                    continue
                seen.add(g)
                msi_exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "msiexec.exe"
                cmd = [str(msi_exe), "/x", g, "/qn", "/norestart"]
                rc, so, se = run_uninstall_command(cmd, cwd=None, timeout=INSTALL_TIMEOUT_SECONDS, logger=self.logger, label=f"msi:{g}")
                ok = rc in (0, 1605, 1614, 3010, 1641)
                add_att(f"MSI {g}", "success" if ok else "failed", rc, so, se)
                if ok:
                    any_ok = True
        return any_ok

    def _try_appx_removal(self, names: list[str]) -> tuple[bool, str]:
        if not names:
            return False, ""
        lit = ",".join(repr(str(n)) for n in names[:10])
        ps = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"$names=@({lit});"
            "foreach($n in $names){"
            " Get-AppxPackage -AllUsers | Where-Object { $_.Name -like ('*'+$n+'*') -or $_.PackageFullName -like ('*'+$n+'*') } "
            " | ForEach-Object { Remove-AppxPackage -Package $_.PackageFullName -AllUsers -ErrorAction SilentlyContinue } };"
            "Get-AppxProvisionedPackage -Online | Where-Object { $_.DisplayName -like '*Teams*' -or $_.DisplayName -like '*MSTeams*' } "
            " | ForEach-Object { Remove-AppxProvisionedPackage -Online -PackageName $_.PackageName -ErrorAction SilentlyContinue }"
        )
        try:
            cp = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps],
                capture_output=True,
                text=True,
                check=False,
                timeout=600,
                shell=False,
            )
            tail = ((cp.stdout or "") + (cp.stderr or "")).strip()[:400]
            ok = cp.returncode == 0 or "Remove-Appx" in (cp.stdout or "") or not tail
            return ok, f"AppX PS RC={cp.returncode} {tail}"
        except Exception as exc:  # pylint: disable=broad-except
            return False, str(exc)[:400]

    def _try_citrix_workspace_native_uninstall(self, dry_run: bool) -> tuple[bool, str]:
        """Wenn Chocolatey/WinGet scheitern: TrolleyExpress/CitrixWorkspaceApp direkt starten."""
        if dry_run:
            return False, "[DRY-RUN] Citrix nativer Deinstaller (TrolleyExpress/CitrixWorkspaceApp)"
        roots = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Citrix",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Citrix",
            Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Citrix",
        ]
        candidates: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            for sub in sorted(root.glob("Citrix Workspace*"), key=lambda p: str(p), reverse=True):
                for rel in ("TrolleyExpress.exe", "CitrixWorkspaceApp.exe", "bootstrapperhelper.exe"):
                    p = sub / rel
                    if p.is_file():
                        candidates.append(p)
                try:
                    for name in ("TrolleyExpress.exe", "CitrixWorkspaceApp.exe", "bootstrapperhelper.exe"):
                        for p in sub.rglob(name):
                            if p.is_file() and p not in candidates:
                                candidates.append(p)
                except OSError:
                    pass
            try:
                for name in ("TrolleyExpress.exe", "CitrixWorkspaceApp.exe", "bootstrapperhelper.exe"):
                    for p in root.rglob(name):
                        if p.is_file() and p not in candidates:
                            candidates.append(p)
            except OSError:
                pass
        seen: set[str] = set()
        uniq: list[Path] = []
        for p in candidates:
            try:
                key = str(p.resolve())
            except OSError:
                key = str(p)
            if key not in seen:
                seen.add(key)
                uniq.append(p)
        for exe_path in uniq[:8]:
            self.logger.info("Citrix nativ: versuche %s", exe_path)
            try:
                cp = subprocess.run(
                    [str(exe_path), "/silent", "/uninstall"],
                    check=False,
                    timeout=900,
                    capture_output=True,
                    text=True,
                    shell=False,
                    cwd=str(exe_path.parent),
                )
                tail = ((cp.stdout or "") + (cp.stderr or "")).strip()[:200]
                msg = f"Citrix nativ ({exe_path.name}): RC={cp.returncode}"
                if tail:
                    msg += f" — {tail}"
                if cp.returncode in (0, 3010, 1641):
                    return True, msg
                return False, msg
            except Exception as exc:  # pylint: disable=broad-except
                return False, f"Citrix nativ {exe_path.name}: {exc}"
        return (
            False,
            "Citrix nativ: Kein TrolleyExpress/CitrixWorkspaceApp unter Citrix- oder ProgramData-Pfaden gefunden.",
        )

    @staticmethod
    def _find_office_click_to_run_exe() -> Path | None:
        for stem in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ):
            p = Path(stem) / "Common Files" / "Microsoft Shared" / "ClickToRun" / "OfficeClickToRun.exe"
            if p.is_file():
                return p
        return None

    def _read_c2r_product_release_ids(self) -> str:
        """Kommagetrennte ProductReleaseIds aus der Click-to-Run-Konfiguration (z. B. O365BusinessRetail.16_de-de_x-none)."""
        if winreg is None:
            return ""
        for sub in (
            r"SOFTWARE\Microsoft\Office\ClickToRun\Configuration",
            r"SOFTWARE\WOW6432Node\Microsoft\Office\ClickToRun\Configuration",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                    val, _ = winreg.QueryValueEx(k, "ProductReleaseIds")
                    s = str(val or "").strip()
                    if s:
                        return s
            except OSError:
                continue
        return ""

    def _try_office_click_to_run_uninstall(self) -> tuple[bool, str]:
        """Office/Microsoft 365 per Click-to-Run entfernen (ARP/WinGet scheitern oft mit WinError 5/87)."""
        exe = self._find_office_click_to_run_exe()
        if not exe:
            return False, "Office Click-to-Run: OfficeClickToRun.exe nicht gefunden."
        products = self._read_c2r_product_release_ids()
        if not products:
            products = "O365BusinessRetail"
        self.logger.info("Office C2R Deinstall: %s productstoremove=%s", exe, products[:160])
        try:
            cp = subprocess.run(
                [
                    str(exe),
                    "scenario=install",
                    "scenariosubtype=ARP",
                    "sourcetype=None",
                    f"productstoremove={products}",
                ],
                check=False,
                timeout=INSTALL_TIMEOUT_SECONDS,
                capture_output=True,
                text=True,
                shell=False,
                cwd=str(exe.parent),
            )
        except Exception as exc:  # pylint: disable=broad-except
            return False, f"Office Click-to-Run: {exc}"
        tail = ((cp.stdout or "") + (cp.stderr or "")).strip()[:400]
        msg = f"Office Click-to-Run: RC={cp.returncode}"
        if tail:
            msg += f" — {tail}"
        if cp.returncode in (0, 3010, 1641):
            return True, msg
        return False, msg

    @staticmethod
    def _append_access_denied_hint(messages: list[str]) -> None:
        blob = " ".join(messages).lower()
        if "zugriff verweigert" in blob or "winerror 5" in blob or "access is denied" in blob:
            messages.append(
                "Hinweis: Office/365-Deinstall oft nur als Administrator oder mit Microsoft "
                "Support and Recovery Assistant (SaRA) moeglich."
            )

    @staticmethod
    def _matches_prev(
        software: SoftwarePackage,
        prev: SoftwareState,
        display_name: str,
        profile: dict[str, Any] | None = None,
    ) -> bool:
        name = (display_name or "").lower()
        if not name:
            return False
        if software.key == "microsoft_teams":
            if "teamspeak" in name or "teamviewer" in name:
                return False
            if "machine-wide" in name and "teams" in name:
                return True
            if "teams" in name and "work or school" in name:
                return True
            if "microsoft teams" in name:
                return True
            nl = name.strip().lower()
            if nl == "teams" or nl.startswith("teams "):
                return True
        pkg = (prev.package_name or "").lower()
        if pkg and MultiStageUninstallEngine._contains_term(name, pkg):
            return True
        if MultiStageUninstallEngine._contains_term(name, software.display_name):
            return True
        software_key_compact = software.key.lower().replace("_", "")
        if software_key_compact and software_key_compact in name.replace(" ", ""):
            return True
        aliases = SOFTWARE_ALIASES.get(software.key, ())
        if any(MultiStageUninstallEngine._contains_term(name, alias) for alias in aliases):
            return True
        prof = profile or {}
        for rn in prof.get("registry_names", []) or []:
            s = str(rn).strip().lower()
            if s and term_matches_display(name, s):
                return True
        return False

    def _resolve_teams_update_exe_cmd(self, cmd: list[str]) -> list[str]:
        """Registry-Uninstall kann 'Update.exe' ohne Pfad liefern."""
        if not cmd or Path(cmd[0]).name.lower() != "update.exe":
            return cmd
        p0 = Path(cmd[0])
        if p0.is_file():
            return cmd
        for base in (
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Teams",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Teams" / "current",
        ):
            cand = base / "Update.exe"
            if cand.is_file():
                return [str(cand), *cmd[1:]]
        return cmd

    def _force_registry_cleanup(
        self,
        software: SoftwarePackage,
        prev: SoftwareState,
        profile: dict[str, Any],
    ) -> tuple[int, str]:
        removed = 0
        messages: list[str] = []
        for entry in self._iter_uninstall_entries():
            if not self._matches_prev(software, prev, entry.get("display_name", ""), profile):
                continue
            if software.key == "microsoft_teams":
                raw_cmd = entry.get("uninstall_string") or entry.get("quiet_uninstall_string")
            else:
                raw_cmd = entry.get("quiet_uninstall_string") or entry.get("uninstall_string")
            parts = [p for p in self._parse_cmdline(raw_cmd) if p]
            cmd = self._to_silent_uninstall(parts)
            if not cmd:
                continue
            if software.key == "microsoft_teams":
                cmd = self._resolve_teams_update_exe_cmd(cmd)
            exe0 = Path(cmd[0])
            exe_name = exe0.name.lower()
            if exe_name == "update.exe" and not exe0.is_file():
                messages.append(f"{entry.get('display_name')}: Update.exe nicht gefunden (Teams)")
                continue
            if exe_name.endswith(".exe") and exe_name not in ("msiexec.exe", "rundll32.exe"):
                if exe0.is_absolute() and not exe0.is_file():
                    hive = entry.get("hive", "")
                    kp = entry.get("key_path", "")
                    messages.append(f"{entry.get('display_name')}: EXE fehlt ({cmd[0]}) [{hive}\\{kp}]")
                    continue
            try:
                completed = subprocess.run(
                    cmd,
                    check=False,
                    timeout=INSTALL_TIMEOUT_SECONDS,
                    capture_output=True,
                    text=True,
                    shell=False,
                    cwd=str(exe0.parent) if exe0.is_file() else None,
                )
                if completed.returncode == 0:
                    removed += 1
                elif completed.returncode in (1605, 1614):
                    # MSI: product already uninstalled / removed.
                    removed += 1
                else:
                    messages.append(f"{entry.get('display_name')}: RC={completed.returncode}")
            except Exception as exc:  # pylint: disable=broad-except
                hive = entry.get("hive", "")
                kp = entry.get("key_path", "")
                messages.append(f"{hive}\\{kp}: {exc}")
        return removed, " | ".join(messages)[:350]

    def _registry_has_actionable_match(
        self,
        software: SoftwarePackage,
        prev: SoftwareState,
        profile: dict[str, Any],
    ) -> bool:
        for entry in self._iter_uninstall_entries():
            if not self._matches_prev(software, prev, entry.get("display_name", ""), profile):
                continue
            if SoftwareScanner.registry_entry_signals_real_install(entry):
                return True
        return False

    @staticmethod
    def _keywords_for_software(software: SoftwarePackage) -> list[str]:
        base = [software.display_name, software.key.replace("_", " ")]
        aliases = list(SOFTWARE_ALIASES.get(software.key, ()))
        words: list[str] = []
        for raw in [*base, *aliases]:
            t = str(raw or "").strip().lower()
            if len(t) >= 4:
                words.append(t)
        dedup = []
        for item in words:
            if item not in dedup:
                dedup.append(item)
        return dedup

    @staticmethod
    def _path_exists(path: str) -> bool:
        try:
            return Path(path).exists()
        except OSError:
            return False

    def _delete_path(self, path: str, dry_run: bool) -> bool:
        if not self._path_exists(path):
            return False
        if dry_run:
            self.logger.info("[DRY-RUN] wuerde Rest loeschen: %s", path)
            return True
        p = Path(path)
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=False)
            else:
                p.unlink(missing_ok=True)
            self.logger.info("Rest entfernt: %s", path)
            return True
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.warning("Konnte Rest nicht entfernen (%s): %s", path, exc)
            return False

    def _candidate_leftover_paths(self, software: SoftwarePackage) -> list[str]:
        env = {
            "PROGRAMFILES": os.environ.get("ProgramFiles", r"C:\Program Files"),
            "PROGRAMFILESX86": os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            "PROGRAMDATA": os.environ.get("ProgramData", r"C:\ProgramData"),
            "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
            "APPDATA": os.environ.get("APPDATA", ""),
            "PUBLIC": os.environ.get("PUBLIC", r"C:\Users\Public"),
        }
        by_software: dict[str, list[str]] = {
            "citrix_workspace": [
                r"{PROGRAMFILES}\Citrix",
                r"{PROGRAMFILESX86}\Citrix",
                r"{PROGRAMDATA}\Citrix",
                r"{LOCALAPPDATA}\Citrix",
            ],
            "adobe_reader": [
                r"{PROGRAMFILES}\Adobe",
                r"{PROGRAMFILESX86}\Adobe",
                r"{PROGRAMDATA}\Adobe",
                r"{APPDATA}\Adobe",
                r"{LOCALAPPDATA}\Adobe",
            ],
            "teamviewer": [
                r"{PROGRAMFILES}\TeamViewer",
                r"{PROGRAMFILESX86}\TeamViewer",
                r"{PROGRAMDATA}\TeamViewer",
                r"{APPDATA}\TeamViewer",
                r"{LOCALAPPDATA}\TeamViewer",
            ],
            "microsoft_teams": [
                r"{LOCALAPPDATA}\Microsoft\Teams",
                r"{APPDATA}\Microsoft\Teams",
                r"{PROGRAMDATA}\Microsoft\Teams",
                r"{PROGRAMFILES}\WindowsApps\Microsoft.Teams*",
            ],
            "office365business": [
                # Kein shutil unter Program Files\Microsoft Office: schuetzt installierte Office-Instanz / WinError 5.
                r"{PROGRAMDATA}\Microsoft\Office",
                r"{LOCALAPPDATA}\Microsoft\Office",
                r"{APPDATA}\Microsoft\Office",
            ],
            "opentext": [
                r"{PROGRAMFILES}\OpenText",
                r"{PROGRAMFILESX86}\OpenText",
                r"{PROGRAMDATA}\OpenText",
                r"{APPDATA}\OpenText",
                r"{LOCALAPPDATA}\OpenText",
            ],
            "opentext_core_endpoint": [
                r"{PROGRAMFILES}\Webroot",
                r"{PROGRAMFILESX86}\Webroot",
                r"{PROGRAMDATA}\WRData",
                r"{APPDATA}\Webroot",
                r"{LOCALAPPDATA}\Webroot",
            ],
            "avaya_workplace": [
                r"{PROGRAMFILES}\Avaya",
                r"{PROGRAMFILESX86}\Avaya",
                r"{PROGRAMDATA}\Avaya",
                r"{APPDATA}\Avaya",
                r"{LOCALAPPDATA}\Avaya",
            ],
            "filezilla": [
                r"{PROGRAMFILES}\FileZilla FTP Client",
                r"{PROGRAMFILESX86}\FileZilla FTP Client",
                r"{APPDATA}\FileZilla",
                r"{LOCALAPPDATA}\FileZilla",
            ],
            "firefox": [
                r"{PROGRAMFILES}\Mozilla Firefox",
                r"{PROGRAMFILESX86}\Mozilla Firefox",
                r"{APPDATA}\Mozilla\Firefox",
                r"{LOCALAPPDATA}\Mozilla\Firefox",
            ],
        }
        templates = by_software.get(software.key, [])
        expanded: list[str] = []
        for template in templates:
            path = template.format(**env)
            if "*" in path:
                expanded.extend(str(p) for p in Path(path.split("*")[0]).parent.glob(Path(path).name))
            else:
                expanded.append(path)
        return expanded

    def _cleanup_shortcuts(self, software: SoftwarePackage, dry_run: bool) -> int:
        keywords = self._keywords_for_software(software)
        roots = [
            Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
            Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
            Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
            Path.home() / "Desktop",
        ]
        removed = 0
        for root in roots:
            if not root.exists():
                continue
            for link in root.rglob("*.lnk"):
                name = link.name.lower()
                if any(k in name for k in keywords):
                    if self._delete_path(str(link), dry_run=dry_run):
                        removed += 1
        return removed

    def _cleanup_registry_orphans(self, software: SoftwarePackage, dry_run: bool) -> tuple[int, str]:
        if winreg is None:
            return 0, ""
        removed = 0
        issues: list[str] = []
        keywords = self._keywords_for_software(software)
        for entry in self._iter_uninstall_entries():
            display_name = entry.get("display_name", "").lower()
            if not display_name or not any(k in display_name for k in keywords):
                continue
            key_path = entry.get("key_path", "")
            hive_name = entry.get("hive", "HKLM")
            hive = winreg.HKEY_CURRENT_USER if hive_name == "HKCU" else winreg.HKEY_LOCAL_MACHINE
            if dry_run:
                self.logger.info("[DRY-RUN] wuerde Registry-Orphan entfernen: %s\\%s", hive_name, key_path)
                removed += 1
                continue
            try:
                winreg.DeleteKeyEx(hive, key_path, 0, winreg.KEY_WOW64_64KEY)
                removed += 1
                continue
            except Exception:
                pass
            try:
                winreg.DeleteKeyEx(hive, key_path, 0, winreg.KEY_WOW64_32KEY)
                removed += 1
            except Exception as exc:  # pylint: disable=broad-except
                issues.append(f"{hive_name}\\{key_path}: {exc}")
        return removed, " | ".join(issues)[:300]

    def _deep_cleanup_leftovers(self, software: SoftwarePackage, dry_run: bool) -> tuple[int, str]:
        removed = 0
        issues: list[str] = []
        for path in self._candidate_leftover_paths(software):
            if self._delete_path(path, dry_run=dry_run):
                removed += 1
        short_removed = self._cleanup_shortcuts(software, dry_run=dry_run)
        removed += short_removed
        reg_removed, reg_issues = self._cleanup_registry_orphans(software, dry_run=dry_run)
        removed += reg_removed
        if reg_issues:
            issues.append(reg_issues)
        return removed, " | ".join(issues)[:320]

    @staticmethod
    def _process_patterns(software: SoftwarePackage) -> list[str]:
        by_software: dict[str, list[str]] = {
            "citrix_workspace": ["*citrix*.exe", "*selfservice*.exe", "*wfcrun32.exe"],
            "adobe_reader": ["*acrord32.exe", "*acrobat*.exe", "*adobe*.exe"],
            "teamviewer": ["*teamviewer*.exe"],
            "microsoft_teams": ["*teams*.exe", "*ms-teams*.exe", "*teamsbootstrapper*.exe"],
            "office365business": ["*officeclicktorun*.exe", "*winword.exe", "*excel.exe", "*powerpnt.exe"],
            "opentext": ["*opentext*.exe", "*edir*.exe", "*ndstrace*.exe"],
            "opentext_core_endpoint": ["*wr*.exe", "*webroot*.exe", "*wsa*.exe"],
            "avaya_workplace": ["*avaya*.exe", "*workplace*.exe"],
            "filezilla": ["*filezilla*.exe"],
            "firefox": ["*firefox*.exe", "*updater.exe"],
        }
        return by_software.get(software.key, [f"*{software.key.replace('_', '')}*.exe"])

    def _terminate_related_processes(
        self,
        software: SoftwarePackage,
        dry_run: bool,
        profile: dict[str, Any] | None = None,
    ) -> tuple[int, str]:
        patterns = list(self._process_patterns(software))
        if profile:
            for p in profile.get("processes", []) or []:
                pl = str(p).strip().lower()
                if not pl:
                    continue
                if "*" not in pl:
                    pl = f"*{pl}"
                if pl not in patterns:
                    patterns.append(pl)
        try:
            listed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
                shell=False,
            )
        except Exception as exc:  # pylint: disable=broad-except
            return 0, str(exc)
        if listed.returncode != 0:
            return 0, (listed.stderr or listed.stdout or "").strip()[:220]
        killed = 0
        issues: list[str] = []
        for line in (listed.stdout or "").splitlines():
            clean = line.strip().strip('"')
            if not clean:
                continue
            proc_name = clean.split('","')[0].lower()
            if not any(fnmatch.fnmatch(proc_name, pat.lower()) for pat in patterns):
                continue
            if dry_run:
                self.logger.info("[DRY-RUN] wuerde Prozess beenden: %s", proc_name)
                killed += 1
                continue
            cmd = ["taskkill", "/F", "/IM", proc_name, "/T"]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=20, shell=False)
            if res.returncode == 0:
                killed += 1
            else:
                issues.append(f"{proc_name}: RC={res.returncode}")
        return killed, " | ".join(issues)[:220]


def normalize_uninstall_command(raw: str, software_key: str) -> list[str]:
    _ = software_key
    parts = [p for p in MultiStageUninstallEngine._parse_cmdline(raw) if p]
    return MultiStageUninstallEngine._to_silent_uninstall(parts)


def find_uninstall_entries_for_app(
    entries: list[dict[str, str]],
    software: SoftwarePackage,
    prev: SoftwareState,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    return [e for e in entries if MultiStageUninstallEngine._matches_prev(software, prev, e.get("display_name", ""), profile)]
