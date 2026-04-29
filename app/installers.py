from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import socket
import ctypes
import winreg
from pathlib import Path
from typing import Any, Callable

from .config import INSTALL_TIMEOUT_SECONDS, INTERNAL_INSTALLERS, MAX_CHOCO_RETRIES, SOFTWARE_BY_KEY, SoftwarePackage
from .local_source import LocalSourceService
from .models import ReportEntry, SoftwareState

DownloadProgressCallback = Callable[[int, int | None], None]
ActivityCallback = Callable[[str, str], None]

INSTALLER_LOCK_WAIT_SECONDS = 30
INSTALLER_LOCK_MAX_ATTEMPTS = 5
LOCK_PROCESS_NAMES = (
    "msiexec.exe",
    "setup.exe",
    "installer.exe",
    "officeclicktorun.exe",
)

NATIVE_VENDOR_INSTALL_UI_KEYS = frozenset({"adobe_reader"})


class InstallerService:
    def __init__(
        self,
        choco_client,
        winget_client,
        logger,
        software_by_key: dict[str, SoftwarePackage] | None = None,
        provider_configs: dict[str, Any] | None = None,
        local_source_service: LocalSourceService | None = None,
        prefer_local_source: bool = False,
    ) -> None:
        self.choco = choco_client
        self.winget = winget_client
        self.logger = logger
        self.software_by_key = software_by_key or SOFTWARE_BY_KEY
        self.provider_configs = provider_configs or {}
        self.local_source = local_source_service
        self.prefer_local_source = prefer_local_source
        self._download_progress: DownloadProgressCallback | None = None
        self._activity_callback: ActivityCallback | None = None
        self._prefetched_internal_path: str | None = None
        self._native_vendor_install_ui: bool = False
        self._current_software_key: str = ""

    def _emit_activity(self, software: SoftwarePackage, message: str) -> None:
        if self._activity_callback:
            self._activity_callback(software.display_name, message)

    @staticmethod
    def _hide_for_internal_subprocess(is_msi: bool, native_ui: bool) -> bool:
        _ = (is_msi, native_ui)
        return True

    @staticmethod
    def _subprocess_hidden_kwargs() -> dict[str, Any]:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = None
        if creationflags and hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        return {"creationflags": creationflags, "startupinfo": startupinfo}

    def precheck_sources(
        self,
        selected_keys: list[str],
        internal_installers: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        installers = internal_installers if internal_installers is not None else INTERNAL_INSTALLERS
        missing: dict[str, str] = {}
        for key in selected_keys:
            software = self.software_by_key.get(key)
            if software is None:
                continue
            package_name = self._resolve_choco_package_name(software)
            winget_id = self._resolve_winget_id(software)
            dynamic_choco = None if package_name else self._find_dynamic_package(software)
            dynamic_winget = None if winget_id else self._find_dynamic_winget_id(software)
            has_local = bool(self.local_source and self.local_source.find_installer(key))
            has_provider = bool(package_name or winget_id or dynamic_choco or dynamic_winget or has_local)
            if has_provider:
                continue
            internal_state = self._run_internal_installer(software, installers, dry_run=True)
            if internal_state is None or internal_state.status == "Quelle erforderlich":
                missing[key] = internal_state.detail if internal_state is not None else "Keine gueltige Quelle gefunden"
        return missing

    def process(
        self,
        selected_keys: list[str],
        current_states: dict[str, SoftwareState],
        status_callback: Callable[[str, SoftwareState], None],
        progress_callback: Callable[[int, int], None],
        item_start_callback: Callable[[str], None] | None = None,
        resolution_callback: Callable[[str, str | None, str | None], None] | None = None,
        dry_run: bool = False,
        internal_installers: dict[str, dict[str, Any]] | None = None,
        download_progress_callback: DownloadProgressCallback | None = None,
        activity_callback: ActivityCallback | None = None,
    ) -> list[ReportEntry]:
        total = len(selected_keys)
        installers = internal_installers if internal_installers is not None else INTERNAL_INSTALLERS
        report_rows: list[ReportEntry] = []
        self._download_progress = download_progress_callback
        self._activity_callback = activity_callback

        try:
            for index, key in enumerate(selected_keys, start=1):
                software = self.software_by_key[key]
                self._current_software_key = key
                self._native_vendor_install_ui = key in NATIVE_VENDOR_INSTALL_UI_KEYS
                self._prefetched_internal_path = None

                progress_callback(index - 1, total)
                if item_start_callback is not None:
                    item_start_callback(key)
                self.logger.info("%s wird verarbeitet...", software.display_name)
                previous_state = current_states.get(key, SoftwareState("Nicht geprueft"))
                st0 = previous_state.status
                if st0 == "Update verfuegbar":
                    self._emit_activity(software, "Update …")
                elif st0 not in ("Aktuell", "Installiert", "Dry-Run (unveraendert)"):
                    self._emit_activity(software, "Installation …")

                try:
                    action, new_state = self._process_single(software, previous_state, dry_run, installers, resolution_callback)
                except Exception as exc:  # pylint: disable=broad-except
                    self.logger.exception("Fehler bei %s: %s", software.display_name, exc)
                    action = "Fehlerbehandlung"
                    new_state = SoftwareState(
                        "Fehler",
                        detail=str(exc),
                        package_name=previous_state.package_name,
                        installed_version=previous_state.installed_version,
                        available_version=previous_state.available_version,
                    )
                finally:
                    self._prefetched_internal_path = None

                current_states[key] = new_state
                status_callback(key, new_state)
                progress_callback(index, total)
                if action == "Keine Aktion erforderlich":
                    result = "OK"
                else:
                    result = "OK" if new_state.status not in {"Fehler", "Quelle erforderlich"} else "Fehler"
                    if dry_run and action.startswith("DRY-RUN"):
                        result = "DRY-RUN"
                report_rows.append(
                    ReportEntry(
                        software_key=key,
                        package_name=new_state.package_name or "",
                        status_before=previous_state.status,
                        action=action,
                        status_after=new_state.status,
                        result=result,
                        error_message=new_state.detail,
                        reboot_required="yes" if self.is_reboot_pending() else "no",
                        provider=new_state.provider or "",
                        installer_path=new_state.installer_path or "",
                        installer_sha256=new_state.installer_sha256 or "",
                        uninstall_method="",
                        uninstall_attempts_json="",
                        manual_reason="",
                    )
                )

            return report_rows
        finally:
            self._download_progress = None
            self._activity_callback = None
            self._prefetched_internal_path = None
            self._native_vendor_install_ui = False
            self._current_software_key = ""

    def _no_action_if_current(
        self,
        software: SoftwarePackage,
        prev: SoftwareState,
        st: str,
    ) -> tuple[str, SoftwareState] | None:
        if st not in ("Aktuell", "Installiert", "Dry-Run (unveraendert)"):
            return None
        if st in ("Aktuell", "Installiert"):
            self.logger.info("%s ist aktuell. Keine Aktion erforderlich.", software.display_name)
        else:
            self.logger.info("%s: Keine Aktion erforderlich (Dry-Run-Status unveraendert).", software.display_name)
        return (
            "Keine Aktion erforderlich",
            SoftwareState(
                st,
                package_name=prev.package_name,
                detail=prev.detail or "",
                installed_version=prev.installed_version,
                available_version=prev.available_version,
            ),
        )

    def _no_action_if_download_blocked(
        self,
        software: SoftwarePackage,
        prev: SoftwareState,
        st: str,
    ) -> tuple[str, SoftwareState] | None:
        if st != "Quelle erforderlich":
            return None
        self.logger.warning(
            "%s: Konfiguration oder Installer-Quelle pruefen (Quelle erforderlich).",
            software.display_name,
        )
        return (
            "Konfiguration/Quelle erforderlich",
            SoftwareState(
                "Quelle erforderlich",
                detail=prev.detail or "Interner Installer-Pfad oder Quelle pruefen",
                package_name=prev.package_name,
                installed_version=prev.installed_version,
                available_version=prev.available_version,
                provider="Quelle erforderlich",
            ),
        )

    def _resolve_choco_package_name(self, software: SoftwarePackage) -> str | None:
        self.logger.info("Quelle gesucht: %s", software.display_name)
        if not self.choco.is_choco_installed():
            self.logger.info("Chocolatey nicht installiert, ueberspringe Chocolatey-Aufloesung.")
            return None
        cfg = self.provider_configs.get(software.key, {})
        if not isinstance(cfg, dict):
            cfg = {}
        resolved = cfg.get("resolved", {})
        if isinstance(resolved, dict):
            resolved_pkg = str(resolved.get("choco_package", "") or "").strip()
            if resolved_pkg:
                self.logger.info("Chocolatey resolved Treffer: %s", resolved_pkg)
                return resolved_pkg
        defined_pkg = software.primary_package
        if defined_pkg:
            self.logger.info("Chocolatey Treffer: %s", defined_pkg)
            return defined_pkg
        if software.key == "avaya_workplace":
            return None
        self.logger.info("Chocolatey dynamische Suche: %s", software.display_name)
        return None

    def _resolve_winget_id(self, software: SoftwarePackage) -> str | None:
        cfg = self.provider_configs.get(software.key, {})
        if not isinstance(cfg, dict):
            cfg = {}
        resolved = cfg.get("resolved", {})
        if isinstance(resolved, dict):
            resolved_id = str(resolved.get("winget_id", "") or "").strip()
            if resolved_id:
                self.logger.info("WinGet resolved Treffer: %s", resolved_id)
                return resolved_id
        winget_id = software.winget_id
        if winget_id:
            self.logger.info("WinGet Treffer: %s", winget_id)
            return winget_id
        self.logger.info("WinGet dynamische Suche: %s", software.display_name)
        return None

    def _process_single(
        self,
        software: SoftwarePackage,
        state: SoftwareState | None,
        dry_run: bool,
        internal_installers: dict[str, dict[str, Any]],
        resolution_callback: Callable[[str, str | None, str | None], None] | None,
    ) -> tuple[str, SoftwareState]:
        prev = state or SoftwareState("Nicht geprueft")
        st = prev.status

        noop = self._no_action_if_current(software, prev, st)
        if noop is not None:
            return noop

        package_name = self._resolve_choco_package_name(software)
        winget_id = self._resolve_winget_id(software)
        dynamic_choco = None if (package_name or software.key in ("opentext", "opentext_core_endpoint")) else self._find_dynamic_package(software)
        if dynamic_choco:
            self.logger.info("Chocolatey dynamischer Treffer: %s", dynamic_choco)
            package_name = dynamic_choco
            if resolution_callback is not None:
                resolution_callback(software.key, dynamic_choco, None)
        dynamic_winget = None if (winget_id or software.key in ("opentext", "opentext_core_endpoint")) else self._find_dynamic_winget_id(software)
        if dynamic_winget:
            self.logger.info("WinGet dynamischer Treffer: %s", dynamic_winget)
            winget_id = dynamic_winget
            if resolution_callback is not None:
                resolution_callback(software.key, None, dynamic_winget)
        can_upgrade = st == "Update verfuegbar"
        can_install = st in ("Nicht installiert", "Nicht geprueft", "Quelle erforderlich", "Fehler")

        if can_upgrade:
            if software.key == "avaya_workplace":
                return self._attempt_update_chain_avaya(software, package_name, winget_id, prev, dry_run, internal_installers)
            if software.key == "opentext":
                return self._attempt_update_chain_opentext(software, package_name, winget_id, prev, dry_run, internal_installers)
            return self._attempt_update_chain(software, package_name, winget_id, prev, dry_run, internal_installers)
        if can_install:
            if software.key == "avaya_workplace":
                return self._attempt_install_chain_avaya(software, package_name, winget_id, prev, dry_run, internal_installers)
            if software.key == "opentext":
                return self._attempt_install_chain_opentext(software, package_name, winget_id, prev, dry_run, internal_installers)
            return self._attempt_install_chain(software, package_name, winget_id, prev, dry_run, internal_installers)

        return (
            "Manuelle Pruefung",
            SoftwareState(
                "Manuelle Pruefung noetig",
                detail=f"Scan-Status '{st}' erlaubt keine automatische Aktion.",
                package_name=prev.package_name,
                installed_version=prev.installed_version,
                available_version=prev.available_version,
            ),
        )

    def _attempt_update_chain(
        self,
        software: SoftwarePackage,
        package_name: str | None,
        winget_id: str | None,
        prev: SoftwareState,
        dry_run: bool,
        internal_installers: dict[str, dict[str, Any]],
    ) -> tuple[str, SoftwareState]:
        errors: list[str] = []
        if self.prefer_local_source:
            local = self._run_local_source_installer(software, prev, dry_run)
            if local is not None:
                action, state = local
                if dry_run or state.status != "Fehler":
                    return action, state
                errors.append(f"Lokal/USB: {state.detail}")
        if self.prefer_local_source:
            local = self._run_local_source_installer(software, prev, dry_run)
            if local is not None:
                action, state = local
                if dry_run or state.status != "Fehler":
                    return action, state
                errors.append(f"Lokal/USB: {state.detail}")
        if package_name:
            self.logger.info("Nutze Provider: Chocolatey")
            action, state = self._run_choco_upgrade(package_name, prev, dry_run)
            if dry_run or state.status != "Fehler":
                return action, state
            if software.key == "teamviewer" and self._is_hash_mismatch(state.detail):
                self.logger.warning("Chocolatey Hash mismatch, wechsel zu WinGet.")
            errors.append(f"Chocolatey: {state.detail}")
        if winget_id:
            self.logger.info("Nutze Provider: WinGet")
            action, state = self._run_winget_upgrade(winget_id, prev, dry_run)
            if dry_run or state.status != "Fehler":
                return action, state
            errors.append(f"WinGet: {state.detail}")
        installer_state = self._run_internal_installer(software, internal_installers, dry_run)
        if installer_state is not None:
            self.logger.info("Nutze Provider: Intern")
            if dry_run and installer_state.status == "Dry-Run (unveraendert)":
                return ("DRY-RUN: wuerde installieren", installer_state)
            if installer_state.status != "Fehler" and installer_state.status != "Quelle erforderlich":
                return ("Interner Installer", installer_state)
            errors.append(f"Intern: {installer_state.detail}")
            if installer_state.status == "Quelle erforderlich" and not errors[:-1]:
                return ("Quelle erforderlich", installer_state)
        if not self.prefer_local_source:
            local = self._run_local_source_installer(software, prev, dry_run)
            if local is not None:
                action, state = local
                if dry_run or state.status != "Fehler":
                    return action, state
                errors.append(f"Lokal/USB: {state.detail}")
        if not errors:
            return ("Quelle erforderlich", SoftwareState("Quelle erforderlich", detail="Keine gueltige Quelle gefunden", provider="Quelle erforderlich"))
        return ("Fehler", SoftwareState("Fehler", detail=" | ".join(errors)[:400], provider="Quelle erforderlich"))

    def _attempt_install_chain(
        self,
        software: SoftwarePackage,
        package_name: str | None,
        winget_id: str | None,
        prev: SoftwareState,
        dry_run: bool,
        internal_installers: dict[str, dict[str, Any]],
    ) -> tuple[str, SoftwareState]:
        errors: list[str] = []
        if self.prefer_local_source:
            local = self._run_local_source_installer(software, prev, dry_run)
            if local is not None:
                action, state = local
                if dry_run or state.status != "Fehler":
                    return action, state
                errors.append(f"Lokal/USB: {state.detail}")
        if package_name:
            self.logger.info("Nutze Provider: Chocolatey")
            action, state = self._run_choco_upgrade(package_name, prev, dry_run)
            if dry_run or state.status != "Fehler":
                return action, state
            if software.key == "teamviewer" and self._is_hash_mismatch(state.detail):
                self.logger.warning("Chocolatey Hash mismatch, wechsel zu WinGet.")
            errors.append(f"Chocolatey: {state.detail}")
        if winget_id:
            self.logger.info("Nutze Provider: WinGet")
            action, state = self._run_winget_install(winget_id, prev, dry_run)
            if dry_run or state.status != "Fehler":
                return action, state
            errors.append(f"WinGet: {state.detail}")
        installer_state = self._run_internal_installer(software, internal_installers, dry_run)
        if installer_state is not None:
            self.logger.info("Nutze Provider: Intern")
            if dry_run and installer_state.status == "Dry-Run (unveraendert)":
                return ("DRY-RUN: wuerde installieren", installer_state)
            if installer_state.status != "Fehler" and installer_state.status != "Quelle erforderlich":
                return ("Interner Installer", installer_state)
            errors.append(f"Intern: {installer_state.detail}")
            if installer_state.status == "Quelle erforderlich" and not errors[:-1]:
                return ("Quelle erforderlich", installer_state)
        if not self.prefer_local_source:
            local = self._run_local_source_installer(software, prev, dry_run)
            if local is not None:
                action, state = local
                if dry_run or state.status != "Fehler":
                    return action, state
                errors.append(f"Lokal/USB: {state.detail}")
        if not errors:
            return ("Quelle erforderlich", SoftwareState("Quelle erforderlich", detail="Keine gueltige Quelle gefunden", provider="Quelle erforderlich"))
        return ("Fehler", SoftwareState("Fehler", detail=" | ".join(errors)[:400], provider="Quelle erforderlich"))

    def _attempt_update_chain_avaya(
        self,
        software: SoftwarePackage,
        package_name: str | None,
        winget_id: str | None,
        prev: SoftwareState,
        dry_run: bool,
        internal_installers: dict[str, dict[str, Any]],
    ) -> tuple[str, SoftwareState]:
        errors: list[str] = []
        if winget_id:
            self.logger.info("Nutze Provider: WinGet (Avaya Prioritaet)")
            action, state = self._run_winget_upgrade(winget_id, prev, dry_run)
            if dry_run or state.status != "Fehler":
                return action, state
            errors.append(f"WinGet: {state.detail}")
        installer_state = self._run_internal_installer(software, internal_installers, dry_run)
        if installer_state is not None:
            self.logger.info("Nutze internen Installer...")
            if dry_run and installer_state.status == "Dry-Run (unveraendert)":
                return ("DRY-RUN: wuerde installieren", installer_state)
            if installer_state.status != "Fehler" and installer_state.status != "Quelle erforderlich":
                return ("Interner Installer", installer_state)
            errors.append(f"Intern: {installer_state.detail}")
            if installer_state.status == "Quelle erforderlich" and not errors[:-1]:
                return ("Quelle erforderlich", installer_state)
        if package_name:
            self.logger.info("Nutze Provider: Chocolatey")
            action, state = self._run_choco_upgrade(package_name, prev, dry_run)
            if dry_run or state.status != "Fehler":
                return action, state
            errors.append(f"Chocolatey: {state.detail}")
        if not self.prefer_local_source:
            local = self._run_local_source_installer(software, prev, dry_run)
            if local is not None:
                action, state = local
                if dry_run or state.status != "Fehler":
                    return action, state
                errors.append(f"Lokal/USB: {state.detail}")
        if not errors:
            return ("Quelle erforderlich", SoftwareState("Quelle erforderlich", detail="Keine gueltige Quelle gefunden", provider="Quelle erforderlich"))
        return ("Fehler", SoftwareState("Fehler", detail=" | ".join(errors)[:400], provider="Quelle erforderlich"))

    def _attempt_install_chain_avaya(
        self,
        software: SoftwarePackage,
        package_name: str | None,
        winget_id: str | None,
        prev: SoftwareState,
        dry_run: bool,
        internal_installers: dict[str, dict[str, Any]],
    ) -> tuple[str, SoftwareState]:
        errors: list[str] = []
        if self.prefer_local_source:
            local = self._run_local_source_installer(software, prev, dry_run)
            if local is not None:
                action, state = local
                if dry_run or state.status != "Fehler":
                    return action, state
                errors.append(f"Lokal/USB: {state.detail}")
        if winget_id:
            self.logger.info("Nutze Provider: WinGet (Avaya Prioritaet)")
            action, state = self._run_winget_install(winget_id, prev, dry_run)
            if dry_run or state.status != "Fehler":
                return action, state
            errors.append(f"WinGet: {state.detail}")
        installer_state = self._run_internal_installer(software, internal_installers, dry_run)
        if installer_state is not None:
            self.logger.info("Nutze internen Installer...")
            if dry_run and installer_state.status == "Dry-Run (unveraendert)":
                return ("DRY-RUN: wuerde installieren", installer_state)
            if installer_state.status != "Fehler" and installer_state.status != "Quelle erforderlich":
                return ("Interner Installer", installer_state)
            errors.append(f"Intern: {installer_state.detail}")
            if installer_state.status == "Quelle erforderlich" and not errors[:-1]:
                return ("Quelle erforderlich", installer_state)
        if package_name:
            self.logger.info("Nutze Provider: Chocolatey")
            action, state = self._run_choco_upgrade(package_name, prev, dry_run)
            if dry_run or state.status != "Fehler":
                return action, state
            errors.append(f"Chocolatey: {state.detail}")
        if not self.prefer_local_source:
            local = self._run_local_source_installer(software, prev, dry_run)
            if local is not None:
                action, state = local
                if dry_run or state.status != "Fehler":
                    return action, state
                errors.append(f"Lokal/USB: {state.detail}")
        if not errors:
            return ("Quelle erforderlich", SoftwareState("Quelle erforderlich", detail="Keine gueltige Quelle gefunden", provider="Quelle erforderlich"))
        return ("Fehler", SoftwareState("Fehler", detail=" | ".join(errors)[:400], provider="Quelle erforderlich"))

    def _attempt_update_chain_opentext(
        self,
        software: SoftwarePackage,
        package_name: str | None,
        winget_id: str | None,
        prev: SoftwareState,
        dry_run: bool,
        internal_installers: dict[str, dict[str, Any]],
    ) -> tuple[str, SoftwareState]:
        errors: list[str] = []
        if self.prefer_local_source:
            local = self._run_local_source_installer(software, prev, dry_run)
            if local is not None:
                action, state = local
                if dry_run or state.status != "Fehler":
                    return action, state
                errors.append(f"Lokal/USB: {state.detail}")
        internal_info = self._resolve_internal_installer_info(software, internal_installers)
        if internal_info.source:
            self._log_opentext_preflight(internal_info.source, internal_info.response_file)
        installer_state = self._run_internal_installer(software, internal_installers, dry_run)
        if installer_state is not None:
            self.logger.info("Nutze internen Installer...")
            if dry_run and installer_state.status == "Dry-Run (unveraendert)":
                return ("DRY-RUN: wuerde installieren", installer_state)
            if installer_state.status != "Fehler" and installer_state.status != "Quelle erforderlich":
                return ("Interner Installer", installer_state)
            errors.append(f"Intern: {installer_state.detail}")
            if installer_state.status == "Quelle erforderlich":
                if internal_info.source:
                    return ("Quelle erforderlich", installer_state)
                # optional fallback when no internal source is configured
        if winget_id:
            self.logger.info("Nutze Provider: WinGet")
            action, state = self._run_winget_upgrade(winget_id, prev, dry_run)
            if dry_run or state.status != "Fehler":
                return action, state
            errors.append(f"WinGet: {state.detail}")
        if package_name:
            self.logger.info("Nutze Provider: Chocolatey")
            action, state = self._run_choco_upgrade(package_name, prev, dry_run)
            if dry_run or state.status != "Fehler":
                return action, state
            errors.append(f"Chocolatey: {state.detail}")
        if not self.prefer_local_source:
            local = self._run_local_source_installer(software, prev, dry_run)
            if local is not None:
                action, state = local
                if dry_run or state.status != "Fehler":
                    return action, state
                errors.append(f"Lokal/USB: {state.detail}")
        if not errors:
            return ("Quelle erforderlich", SoftwareState("Quelle erforderlich", detail="Keine gueltige Quelle gefunden", provider="Quelle erforderlich"))
        return ("Fehler", SoftwareState("Fehler", detail=" | ".join(errors)[:400], provider="Quelle erforderlich"))

    def _attempt_install_chain_opentext(
        self,
        software: SoftwarePackage,
        package_name: str | None,
        winget_id: str | None,
        prev: SoftwareState,
        dry_run: bool,
        internal_installers: dict[str, dict[str, Any]],
    ) -> tuple[str, SoftwareState]:
        errors: list[str] = []
        if self.prefer_local_source:
            local = self._run_local_source_installer(software, prev, dry_run)
            if local is not None:
                action, state = local
                if dry_run or state.status != "Fehler":
                    return action, state
                errors.append(f"Lokal/USB: {state.detail}")
        internal_info = self._resolve_internal_installer_info(software, internal_installers)
        if internal_info.source:
            self._log_opentext_preflight(internal_info.source, internal_info.response_file)
        installer_state = self._run_internal_installer(software, internal_installers, dry_run)
        if installer_state is not None:
            self.logger.info("Nutze internen Installer...")
            if dry_run and installer_state.status == "Dry-Run (unveraendert)":
                return ("DRY-RUN: wuerde installieren", installer_state)
            if installer_state.status != "Fehler" and installer_state.status != "Quelle erforderlich":
                return ("Interner Installer", installer_state)
            errors.append(f"Intern: {installer_state.detail}")
            if installer_state.status == "Quelle erforderlich":
                if internal_info.source:
                    return ("Quelle erforderlich", installer_state)
                # optional fallback when no internal source is configured
        if winget_id:
            self.logger.info("Nutze Provider: WinGet")
            action, state = self._run_winget_install(winget_id, prev, dry_run)
            if dry_run or state.status != "Fehler":
                return action, state
            errors.append(f"WinGet: {state.detail}")
        if package_name:
            self.logger.info("Nutze Provider: Chocolatey")
            action, state = self._run_choco_upgrade(package_name, prev, dry_run)
            if dry_run or state.status != "Fehler":
                return action, state
            errors.append(f"Chocolatey: {state.detail}")
        if not self.prefer_local_source:
            local = self._run_local_source_installer(software, prev, dry_run)
            if local is not None:
                action, state = local
                if dry_run or state.status != "Fehler":
                    return action, state
                errors.append(f"Lokal/USB: {state.detail}")
        if not errors:
            return ("Quelle erforderlich", SoftwareState("Quelle erforderlich", detail="Keine gueltige Quelle gefunden", provider="Quelle erforderlich"))
        return ("Fehler", SoftwareState("Fehler", detail=" | ".join(errors)[:400], provider="Quelle erforderlich"))

    def _run_choco_upgrade(self, package_name: str, prev: SoftwareState, dry_run: bool) -> tuple[str, SoftwareState]:
        if dry_run:
            return ("DRY-RUN: wuerde upgraden", SoftwareState("Dry-Run (unveraendert)", package_name=package_name, provider="Chocolatey"))
        sw = self.software_by_key.get(self._current_software_key)
        if sw:
            self._emit_activity(sw, "Chocolatey (Upgrade) …")
        native = bool(getattr(self, "_native_vendor_install_ui", False))
        result = self._run_with_retry(package_name, "upgrade", package_name, native_ui=native)
        return ("Upgrade", SoftwareState("Aktuell" if result.ok else "Fehler", package_name=package_name, detail=self._combine_error(result), provider="Chocolatey"))

    def _run_winget_upgrade(self, winget_id: str, prev: SoftwareState, dry_run: bool) -> tuple[str, SoftwareState]:
        if dry_run:
            return ("DRY-RUN: wuerde upgraden", SoftwareState("Dry-Run (unveraendert)", package_name=winget_id, provider="WinGet"))
        if not self.winget.is_available():
            bootstrap_result = self.winget.ensure_installed()
            if not bootstrap_result.ok:
                return ("Upgrade", SoftwareState("Fehler", package_name=winget_id, detail=self._combine_error(bootstrap_result), provider="WinGet"))
        sw = self.software_by_key.get(self._current_software_key)
        if sw:
            self._emit_activity(sw, "WinGet (Upgrade) …")
        interactive = bool(getattr(self, "_native_vendor_install_ui", False))
        result = self._run_winget_with_lock_retry("upgrade", winget_id, interactive=interactive)
        return ("Upgrade", SoftwareState("Aktuell" if result.ok else "Fehler", package_name=winget_id, detail=self._combine_error(result), provider="WinGet"))

    def _run_winget_install(self, winget_id: str, prev: SoftwareState, dry_run: bool) -> tuple[str, SoftwareState]:
        if dry_run:
            return ("DRY-RUN: wuerde installieren", SoftwareState("Dry-Run (unveraendert)", package_name=winget_id, provider="WinGet"))
        if not self.winget.is_available():
            bootstrap_result = self.winget.ensure_installed()
            if not bootstrap_result.ok:
                return ("Install", SoftwareState("Fehler", package_name=winget_id, detail=self._combine_error(bootstrap_result), provider="WinGet"))
        sw = self.software_by_key.get(self._current_software_key)
        if sw:
            self._emit_activity(sw, "WinGet …")
        interactive = bool(getattr(self, "_native_vendor_install_ui", False))
        result = self._run_winget_with_lock_retry("install", winget_id, interactive=interactive)
        return ("Install", SoftwareState("Installiert" if result.ok else "Fehler", package_name=winget_id, detail=self._combine_error(result), provider="WinGet"))

    def _find_dynamic_package(self, software: SoftwarePackage) -> str | None:
        candidates: list[tuple[str, str]] = []
        for term in software.search_terms:
            results = self.choco.search(term)
            candidates.extend(results)

        if not candidates:
            return None

        keywords = [k.lower() for k in software.registry_keywords]
        ranked = []
        for name, version in candidates:
            lowered = name.lower()
            score = sum(1 for key in keywords if key in lowered)
            ranked.append((score, name, version))

        ranked.sort(reverse=True)
        if software.key == "opentext" and len(ranked) != 1:
            return None
        best_score, best_name, _ = ranked[0]
        if best_score <= 0:
            return None

        self.logger.info("Alternatives Paket gewaehlt fuer %s: %s", software.display_name, best_name)
        return best_name

    def _find_dynamic_winget_id(self, software: SoftwarePackage) -> str | None:
        if not self.winget.is_available():
            return None
        candidates: list[str] = []
        for term in software.search_terms:
            candidates.extend(self.winget.search_ids(term))
        if not candidates:
            return None
        keywords = [k.lower() for k in software.registry_keywords]
        ranked: list[tuple[int, str]] = []
        for candidate in candidates:
            lowered = candidate.lower()
            score = sum(1 for key in keywords if key in lowered)
            ranked.append((score, candidate))
        ranked.sort(reverse=True)
        if software.key == "opentext" and len(ranked) != 1:
            return None
        best_score, best_id = ranked[0]
        if best_score <= 0:
            return None
        self.logger.info("Alternative WinGet-ID gewaehlt fuer %s: %s", software.display_name, best_id)
        return best_id

    def _finalize_opentext_endpoint_exe(
        self,
        source_text: str,
        cmd_source: str,
        downloaded_file: str | None,
        is_url_source: bool,
        keycode: str,
    ) -> tuple[str, str | None, str]:
        """Keycode-EXE laut Hersteller; Rueckgabe (cmd_source, downloaded_file_fuer_cleanup, fehler_text)."""
        if is_url_source:
            exe_name = self._endpoint_keycode_exe_basename(keycode)
            if not exe_name or not downloaded_file or not os.path.isfile(downloaded_file):
                return ("", None, "endpoint_keycode fehlt oder ungueltig (Format XXXX-XXXX-XXXX-XXXX-XXXX).")
            dest = Path(downloaded_file).resolve().parent / exe_name
            if dest.resolve() != Path(downloaded_file).resolve():
                if dest.exists():
                    dest.unlink(missing_ok=True)
                shutil.move(downloaded_file, str(dest))
            self.logger.info("OpenText Core Endpoint: Agent als %s bereitgestellt (laut Keycode-Dateiname).", exe_name)
            return (str(dest), str(dest), "")
        src = Path(cmd_source)
        if not src.is_file():
            return ("", downloaded_file, f"Installer nicht gefunden: {cmd_source}")
        if self._is_endpoint_keycode_exe_filename(src.name):
            return (str(src.resolve()), None, "")
        exe_name = self._endpoint_keycode_exe_basename(keycode)
        if not exe_name:
            return (
                "",
                downloaded_file,
                "Lokaler Installer nicht im Keycode-Format: Datei umbenennen oder endpoint_keycode setzen.",
            )
        dest = Path(tempfile.gettempdir()) / exe_name
        if dest.exists():
            dest.unlink(missing_ok=True)
        shutil.copy2(src, str(dest))
        self.logger.info("OpenText Core Endpoint: Kopie nach %s fuer Hintergrund-Installation.", dest)
        return (str(dest), str(dest), "")

    def _run_internal_installer(
        self,
        software: SoftwarePackage,
        internal_installers: dict[str, dict[str, Any]],
        dry_run: bool,
    ) -> SoftwareState | None:
        internal_info = self._resolve_internal_installer_info(software, internal_installers)
        source = internal_info.source
        silent_args = internal_info.silent_args
        installer_type = internal_info.installer_type
        response_file = internal_info.response_file
        if not source:
            return None
        source_text = str(source).strip()
        is_url_source = self._is_url_source(source_text)
        use_endpoint = software.internal_installer_key == "opentext_endpoint"
        qc_detail = (
            "OpenText Core Endpoint: endpoint_keycode in Einstellungen setzen "
            "(Site-Key XXXX-XXXX-XXXX-XXXX-XXXX aus Management Console, Download Windows .exe)."
        )
        if not is_url_source:
            installer_path = Path(source_text)
            if not installer_path.exists():
                if software.key == "opentext" and source_text.startswith("\\\\"):
                    self.logger.warning("OpenText UNC-Quelle nicht erreichbar: %s", source_text)
                return SoftwareState("Quelle erforderlich", detail=source_text, installed_version="—", available_version="—", provider="Quelle erforderlich")

        cmd_source = source_text
        downloaded_file: str | None = None
        prefetched = getattr(self, "_prefetched_internal_path", None)
        if prefetched and os.path.isfile(prefetched):
            cmd_source = prefetched
            downloaded_file = prefetched
            self._emit_activity(software, "Setup (vorbereitet) …")
            self.logger.info("%s Installer (vorbereitet): %s", software.display_name, source_text)
        elif is_url_source and not dry_run:
            try:
                self._emit_activity(software, "Installer laden …")
                downloaded_file = self._download_installer_from_url(source_text, report_progress=True)
                cmd_source = downloaded_file
                self.logger.info("%s Installer aus URL geladen: %s", software.display_name, source_text)
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.error("Download fehlgeschlagen fuer %s: %s", software.display_name, exc)
                return SoftwareState("Quelle erforderlich", detail=source_text, installed_version="—", available_version="—", provider="Quelle erforderlich")

        if use_endpoint and dry_run:
            if is_url_source:
                if not self._endpoint_keycode_exe_basename(internal_info.endpoint_keycode):
                    return SoftwareState(
                        "Quelle erforderlich",
                        detail=qc_detail,
                        installed_version="—",
                        available_version="—",
                        provider="Quelle erforderlich",
                    )
                bn = self._endpoint_keycode_exe_basename(internal_info.endpoint_keycode)
                return SoftwareState(
                    "Dry-Run (unveraendert)",
                    detail=f"[DRY-RUN] Download {source_text}, Ausfuehrung als {bn} (Hintergrund-Installation)",
                    installed_version="—",
                    available_version="—",
                    provider="Intern",
                )
            lp = Path(cmd_source)
            if lp.is_file() and self._is_endpoint_keycode_exe_filename(lp.name):
                return SoftwareState(
                    "Dry-Run (unveraendert)",
                    detail=f"[DRY-RUN] Start {cmd_source}",
                    installed_version="—",
                    available_version="—",
                    provider="Intern",
                )
            if not self._endpoint_keycode_exe_basename(internal_info.endpoint_keycode):
                return SoftwareState(
                    "Quelle erforderlich",
                    detail=qc_detail + " Alternativ: Installer lokal als Keycode-EXE ablegen.",
                    installed_version="—",
                    available_version="—",
                    provider="Quelle erforderlich",
                )
            bn = self._endpoint_keycode_exe_basename(internal_info.endpoint_keycode)
            return SoftwareState(
                "Dry-Run (unveraendert)",
                detail=f"[DRY-RUN] Kopie nach %TEMP%\\{bn} und Start (Hintergrund-Installation)",
                installed_version="—",
                available_version="—",
                provider="Intern",
            )

        if use_endpoint and not dry_run:
            new_src, new_dl, err = self._finalize_opentext_endpoint_exe(
                source_text, cmd_source, downloaded_file, is_url_source, internal_info.endpoint_keycode
            )
            if err:
                if downloaded_file and os.path.isfile(downloaded_file):
                    try:
                        Path(downloaded_file).unlink(missing_ok=True)
                    except OSError:
                        pass
                return SoftwareState(
                    "Quelle erforderlich",
                    detail=err,
                    installed_version="—",
                    available_version="—",
                    provider="Quelle erforderlich",
                )
            cmd_source = new_src
            downloaded_file = new_dl

        is_msi = self._is_msi_installer(cmd_source, installer_type)
        if software.key == "avaya_workplace" and is_msi:
            self.logger.info("Avaya MSI Installer erkannt.")
            self._log_avaya_prereq_warnings()
        if software.key == "opentext" and is_msi:
            self.logger.info("OpenText MSI Installer erkannt.")
        if software.key == "opentext" and not is_msi:
            self.logger.info("OpenText EXE Installer erkannt.")
        if use_endpoint and not is_msi:
            self.logger.info("OpenText Core Endpoint EXE (Keycode-Name) wird gestartet.")
        cmd = self._build_internal_install_command(cmd_source, silent_args, is_msi)
        if software.key == "opentext" and not is_msi and not silent_args.strip():
            cmd = [cmd_source, "/qn"]
        if use_endpoint and not is_msi and not silent_args.strip():
            cmd = [cmd_source]
        if dry_run:
            self.logger.info("[DRY-RUN] Wuerde internen Installer starten: %s", " ".join(cmd))
            if software.key == "opentext" and response_file:
                self.logger.info("[DRY-RUN] Optionaler Configure-Schritt: EConfig.ps1 -rfile %s", response_file)
            return SoftwareState("Dry-Run (unveraendert)", detail=" ".join(cmd), installed_version="—", available_version="—", provider="Intern")

        if software.key in {"avaya_workplace", "opentext", "opentext_core_endpoint"}:
            self.logger.info("%s Installation gestartet...", software.display_name)
        self.logger.info("%s aus Firmenquelle installiert: %s", software.display_name, source_text)
        self.logger.info("EXEC: %s", " ".join(cmd))
        self._emit_activity(software, "Setup …")
        try:
            hide_win = self._hide_for_internal_subprocess(is_msi, bool(getattr(self, "_native_vendor_install_ui", False)))
            completed = self._run_subprocess_with_lock_retry(cmd, hide_window=hide_win)
        except subprocess.TimeoutExpired:
            if downloaded_file and os.path.exists(downloaded_file):
                Path(downloaded_file).unlink(missing_ok=True)
            return SoftwareState("Fehler", detail="Interner Installer Timeout", installed_version="—", available_version="—")
        finally:
            if downloaded_file and os.path.exists(downloaded_file):
                Path(downloaded_file).unlink(missing_ok=True)

        if completed.returncode == 0:
            if software.key == "opentext" and response_file:
                self._run_opentext_configure_step(cmd_source, response_file)
            if is_msi:
                product_code = self._detect_msi_product_code_for_display_name(software.display_name)
                if product_code:
                    uninstall_cmd = self._build_msi_uninstall_command(product_code)
                    self.logger.info("MSI Uninstall vorbereitet: %s", " ".join(uninstall_cmd))
            return SoftwareState("Installiert", detail="Interner Installer", installed_version="(intern)", available_version="(intern)", provider="Intern")
        return SoftwareState(
            "Fehler",
            detail=f"Installer Returncode {completed.returncode}",
            installed_version="—",
            available_version="—",
            provider="Intern",
        )

    def _run_local_source_installer(
        self,
        software: SoftwarePackage,
        prev: SoftwareState,
        dry_run: bool,
    ) -> tuple[str, SoftwareState] | None:
        if self.local_source is None:
            return None
        local_installer = self.local_source.find_installer(software.key)
        if local_installer is None:
            return None
        installer_type = self.local_source.detect_installer_type(local_installer)
        if installer_type not in {"exe", "msi", "ps1", "bat"}:
            return None
        if installer_type == "bat":
            return (
                "Quelle erforderlich",
                SoftwareState(
                    "Quelle erforderlich",
                    detail=f"Lokale Datei wird aus Sicherheitsgruenden nicht ausgefuehrt: {local_installer}",
                    provider="Lokal/USB",
                    installer_path=str(local_installer),
                ),
            )
        cfg = self.provider_configs.get(software.key, {})
        cfg_dict = cfg if isinstance(cfg, dict) else {}
        internal = cfg_dict.get("internal_installer", {})
        internal_dict = internal if isinstance(internal, dict) else {}
        silent_args = str(internal_dict.get("silent_args", "") or "").strip()
        if dry_run:
            self.logger.info("[DRY-RUN] wuerde lokalen Installer starten: %s", local_installer)
            return (
                "DRY-RUN: wuerde lokalen Installer starten",
                SoftwareState(
                    "Dry-Run (unveraendert)",
                    detail=f"DRY-RUN: wuerde lokalen Installer starten: {local_installer}",
                    package_name=prev.package_name,
                    provider="Lokal/USB",
                    installer_path=str(local_installer),
                ),
            )
        file_hash = LocalSourceService.sha256(local_installer)
        self.logger.info("Lokaler Installer: %s", local_installer)
        self.logger.info("Lokaler Installer SHA256: %s", file_hash)
        if installer_type == "msi":
            cmd = ["msiexec", "/i", str(local_installer), "/qn", "/norestart"]
        elif installer_type == "ps1":
            cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(local_installer)]
            self.logger.warning("PS1 Installer wird ausgefuehrt: %s", local_installer)
        else:
            args = self._parse_silent_args(silent_args)
            if not args:
                self.logger.warning("Keine Silent-Args konfiguriert, Installation kann interaktiv werden.")
            cmd = [str(local_installer), *args] if args else [str(local_installer)]
        try:
            completed = self._run_subprocess_with_lock_retry(cmd, hide_window=True)
        except subprocess.TimeoutExpired:
            return (
                "Lokal/USB",
                SoftwareState(
                    "Fehler",
                    detail="Lokaler Installer Timeout",
                    provider="Lokal/USB",
                    installer_path=str(local_installer),
                    installer_sha256=file_hash,
                ),
            )
        status = "Installiert" if completed.returncode == 0 else "Fehler"
        detail = "Lokaler Installer" if completed.returncode == 0 else f"Installer Returncode {completed.returncode}"
        return (
            "Lokal/USB",
            SoftwareState(
                status,
                detail=detail,
                provider="Lokal/USB",
                installed_version=prev.installed_version,
                available_version=prev.available_version,
                installer_path=str(local_installer),
                installer_sha256=file_hash,
            ),
        )

    @staticmethod
    def _is_url_source(source: str) -> bool:
        parsed = urllib.parse.urlparse(source)
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)

    def _download_installer_from_url(self, source: str, *, report_progress: bool = True) -> str:
        parsed = urllib.parse.urlparse(source)
        suffix = Path(parsed.path).suffix or ".exe"
        chunk_size = 256 * 1024
        progress_cb = self._download_progress if report_progress else None
        with urllib.request.urlopen(source, timeout=60) as response:
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            total: int | None = None
            cl = response.headers.get("Content-Length")
            if cl:
                try:
                    parsed_len = int(cl)
                    if parsed_len > 0:
                        total = parsed_len
                except ValueError:
                    pass
            read = 0
            tmp_path: str | None = None
            if progress_cb and total:
                progress_cb(0, total)
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
                    tmp_path = tmp_file.name
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        tmp_file.write(chunk)
                        read += len(chunk)
                        if progress_cb:
                            progress_cb(read, total)
                assert tmp_path is not None
                return tmp_path
            except Exception:
                if tmp_path and os.path.exists(tmp_path):
                    Path(tmp_path).unlink(missing_ok=True)
                raise
            finally:
                if progress_cb:
                    progress_cb(-1, None)

    def _run_with_retry(self, display_name: str, action: str, package_name: str, *, native_ui: bool = False):
        if not self.choco.is_choco_installed():
            bootstrap_result = self.choco.ensure_installed()
            if not bootstrap_result.ok:
                self.logger.error("Chocolatey Bootstrap fehlgeschlagen fuer %s", display_name)
                return bootstrap_result

        last_result = None
        for attempt in range(0, MAX_CHOCO_RETRIES + 1):
            last_result = self._run_choco_with_lock_retry(action, package_name, native_ui=native_ui)
            if last_result.ok:
                return last_result
            if self._is_package_not_found(last_result.stdout, last_result.stderr):
                self.logger.error("Kein Retry fuer %s: package not found.", display_name)
                return last_result
            if attempt < MAX_CHOCO_RETRIES:
                self.logger.warning("Fehler bei %s, erneuter Versuch %s/%s...", display_name, attempt + 1, MAX_CHOCO_RETRIES)
        return last_result

    def _run_choco_with_lock_retry(self, action: str, package_name: str, *, native_ui: bool = False):
        def run_once() -> Any:
            if action == "upgrade":
                return self.choco.upgrade(package_name, use_native_installer_ui=native_ui)
            return self.choco.install(package_name, use_native_installer_ui=native_ui)

        last_result = run_once()
        for _ in range(max(INSTALLER_LOCK_MAX_ATTEMPTS - 1, 0)):
            if not self._is_installer_lock_result(last_result.returncode, last_result.stdout, last_result.stderr):
                return last_result
            self.logger.warning("Installer-Lock erkannt, warte auf laufende Installation...")
            time.sleep(INSTALLER_LOCK_WAIT_SECONDS)
            last_result = run_once()
        return last_result

    def _run_winget_with_lock_retry(self, action: str, winget_id: str, *, interactive: bool = False):
        def run_once() -> Any:
            if action == "upgrade":
                return self.winget.upgrade(winget_id, interactive=interactive)
            return self.winget.install(winget_id, interactive=interactive)

        last_result = run_once()
        for _ in range(max(INSTALLER_LOCK_MAX_ATTEMPTS - 1, 0)):
            if not self._is_installer_lock_result(last_result.returncode, last_result.stdout, last_result.stderr):
                return last_result
            self.logger.warning("Installer-Lock erkannt, warte auf laufende Installation...")
            time.sleep(INSTALLER_LOCK_WAIT_SECONDS)
            last_result = run_once()
        return last_result

    def _run_subprocess_with_lock_retry(self, cmd: list[str], *, hide_window: bool = True) -> subprocess.CompletedProcess:
        sub_kw = self._subprocess_hidden_kwargs() if hide_window else {}
        completed = subprocess.run(
            cmd,
            check=False,
            timeout=INSTALL_TIMEOUT_SECONDS,
            shell=False,
            **sub_kw,
        )
        for _ in range(max(INSTALLER_LOCK_MAX_ATTEMPTS - 1, 0)):
            if not self._is_installer_lock_result(completed.returncode, "", ""):
                return completed
            self.logger.warning("Installer-Lock erkannt, warte auf laufende Installation...")
            time.sleep(INSTALLER_LOCK_WAIT_SECONDS)
            completed = subprocess.run(
                cmd,
                check=False,
                timeout=INSTALL_TIMEOUT_SECONDS,
                shell=False,
                **sub_kw,
            )
        return completed

    def _build_internal_install_command(self, cmd_source: str, silent_args: str, is_msi: bool) -> list[str]:
        extra_args = self._parse_silent_args(silent_args)
        if is_msi:
            self.logger.info("Nutze msiexec...")
            self.logger.info("MSI Installation gestartet...")
            return ["msiexec", "/i", cmd_source, "/qn", "/norestart", *extra_args]
        return [cmd_source, *extra_args] if extra_args else [cmd_source]

    class _InternalInstallerInfo:
        def __init__(
            self,
            source: str | None,
            silent_args: str,
            installer_type: str,
            response_file: str,
            endpoint_keycode: str = "",
        ) -> None:
            self.source = source
            self.silent_args = silent_args
            self.installer_type = installer_type
            self.response_file = response_file
            self.endpoint_keycode = endpoint_keycode

    @staticmethod
    def _endpoint_keycode_exe_basename(keycode: str) -> str | None:
        """Herstellerformat: XXXX-XXXX-XXXX-XXXX-XXXX.exe (Site-Key aus Console)."""
        s = (keycode or "").strip()
        if not s:
            return None
        if not s.lower().endswith(".exe"):
            s = f"{s}.exe"
        if re.match(r"^[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){4}\.exe$", s):
            return s
        return None

    @staticmethod
    def _is_endpoint_keycode_exe_filename(name: str) -> bool:
        return bool(InstallerService._endpoint_keycode_exe_basename(name))

    def _resolve_internal_installer_info(
        self,
        software: SoftwarePackage,
        internal_installers: dict[str, dict[str, Any]],
    ) -> _InternalInstallerInfo:
        source = software.installer_source
        silent_args = ""
        installer_type = "auto"
        response_file = ""
        endpoint_keycode = ""
        pc_raw = self.provider_configs.get(software.key, {})
        pc = pc_raw if isinstance(pc_raw, dict) else {}
        pc_int = pc.get("internal_installer", {})
        pc_int = pc_int if isinstance(pc_int, dict) else {}
        if software.internal_installer_key and software.internal_installer_key in internal_installers:
            internal = internal_installers[software.internal_installer_key]
            source = internal.get("path", source)
            silent_args = str(internal.get("silent_args", "")).strip()
            installer_type = str(internal.get("type", "auto") or "auto").strip().lower()
            response_file = str(internal.get("response_file", "") or "").strip()
            endpoint_keycode = str(internal.get("endpoint_keycode", "") or "").strip()
        if str(pc_int.get("endpoint_keycode", "") or "").strip():
            endpoint_keycode = str(pc_int.get("endpoint_keycode", "") or "").strip()
        return InstallerService._InternalInstallerInfo(source, silent_args, installer_type, response_file, endpoint_keycode)

    def _run_opentext_configure_step(self, installer_source: str, response_file: str) -> None:
        response_path = Path(response_file)
        if not response_path.exists():
            self.logger.warning("OpenText response_file nicht gefunden: %s", response_file)
            return
        script_path = Path(installer_source).resolve().parent / "EConfig.ps1"
        if not script_path.exists():
            self.logger.warning("OpenText Configure-Skript nicht gefunden: %s", script_path)
            return
        cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script_path), "-rfile", str(response_path)]
        self.logger.info("OpenText Configure-Schritt: EConfig.ps1 -rfile %s", response_file)
        try:
            completed = self._run_subprocess_with_lock_retry(cmd)
            if completed.returncode != 0:
                self.logger.warning("OpenText Configure-Schritt Returncode: %s", completed.returncode)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.warning("OpenText Configure-Schritt fehlgeschlagen: %s", exc)

    def _log_opentext_preflight(self, source: str, response_file: str) -> None:
        source_exists = self._is_url_source(source) or Path(source).exists()
        response_exists = True if not response_file else Path(response_file).exists()
        admin_ok = self._is_admin()
        ports = self._check_ldap_ports()
        self.logger.info(
            "OpenText Preflight: installer=%s, response_file=%s, admin=%s, port389=%s, port636=%s",
            "ok" if source_exists else "fehlt",
            "ok" if response_exists else "fehlt",
            "ja" if admin_ok else "nein",
            "ok" if ports[389] else "warnung",
            "ok" if ports[636] else "warnung",
        )
        if not ports[389] or not ports[636]:
            self.logger.warning("OpenText Preflight Warnung: Ports 389/636 pruefen (LDAP/LDAPS Erreichbarkeit).")

    @staticmethod
    def _is_admin() -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    @staticmethod
    def _check_ldap_ports() -> dict[int, bool]:
        results = {389: False, 636: False}
        for port in (389, 636):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                    results[port] = True
            except OSError:
                results[port] = False
        return results

    @staticmethod
    def _parse_silent_args(silent_args: str) -> list[str]:
        text = (silent_args or "").strip()
        if not text:
            return []
        try:
            return [arg for arg in shlex.split(text, posix=False) if arg]
        except ValueError:
            return [arg for arg in text.split() if arg]

    @staticmethod
    def _is_msi_installer(source: str, installer_type: str) -> bool:
        t = (installer_type or "auto").lower()
        if t == "msi":
            return True
        if t == "exe":
            return False
        return source.lower().endswith(".msi")

    def _log_avaya_prereq_warnings(self) -> None:
        if not self._is_dotnet_48_or_newer():
            self.logger.warning("Avaya Prereq Hinweis: .NET Framework 4.8+ nicht erkannt.")
        if not self._has_vc_redist():
            self.logger.warning("Avaya Prereq Hinweis: VC++ Redistributable nicht erkannt.")

    @staticmethod
    def _is_dotnet_48_or_newer() -> bool:
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full",
            ) as key:
                release, _ = winreg.QueryValueEx(key, "Release")
                return int(release) >= 528040
        except (OSError, ValueError, TypeError):
            return False

    @staticmethod
    def _has_vc_redist() -> bool:
        uninstall_paths = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        )
        for root in uninstall_paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as base:
                    sub_count = winreg.QueryInfoKey(base)[0]
                    for idx in range(sub_count):
                        try:
                            sub_name = winreg.EnumKey(base, idx)
                            with winreg.OpenKey(base, sub_name) as subkey:
                                display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                            text = str(display_name).lower()
                            if "visual c++" in text and "redistributable" in text:
                                return True
                        except OSError:
                            continue
            except OSError:
                continue
        return False

    @staticmethod
    def _build_msi_uninstall_command(product_code: str) -> list[str]:
        return ["msiexec", "/x", product_code, "/qn", "/norestart"]

    @staticmethod
    def _detect_msi_product_code_for_display_name(display_name: str) -> str | None:
        needle = (display_name or "").strip().lower()
        if not needle:
            return None
        uninstall_paths = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        )
        for root in uninstall_paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as base:
                    sub_count = winreg.QueryInfoKey(base)[0]
                    for idx in range(sub_count):
                        try:
                            sub_name = winreg.EnumKey(base, idx)
                            with winreg.OpenKey(base, sub_name) as subkey:
                                disp, _ = winreg.QueryValueEx(subkey, "DisplayName")
                                uninstall_string, _ = winreg.QueryValueEx(subkey, "UninstallString")
                            if needle not in str(disp).lower():
                                continue
                            text = str(uninstall_string)
                            if "msiexec" in text.lower():
                                start = text.find("{")
                                end = text.find("}", start + 1)
                                if start >= 0 and end > start:
                                    return text[start : end + 1]
                        except OSError:
                            continue
            except OSError:
                continue
        return None

    @staticmethod
    def _is_hash_mismatch(text: str) -> bool:
        lowered = (text or "").lower()
        return "hash" in lowered and "mismatch" in lowered

    def _is_installer_lock_result(self, returncode: int, stdout: str, stderr: str) -> bool:
        merged = f"{stdout}\n{stderr}".lower()
        if returncode == 1618 or "1618" in merged:
            return True
        if "another installation is already in progress" in merged:
            return True
        return self._has_installer_lock_processes()

    @staticmethod
    def _has_installer_lock_processes() -> bool:
        try:
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                shell=False,
                **InstallerService._subprocess_hidden_kwargs(),
            )
        except Exception:
            return False
        if completed.returncode != 0:
            return False
        lines = (completed.stdout or "").splitlines()
        for line in lines:
            clean = line.strip().strip('"')
            if not clean:
                continue
            process_name = clean.split('","')[0].lower()
            if process_name in LOCK_PROCESS_NAMES:
                return True
        return False

    @staticmethod
    def is_reboot_pending() -> bool:
        checks = (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending", None),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired", None),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager", "PendingFileRenameOperations"),
        )
        for hive, path, value_name in checks:
            try:
                with winreg.OpenKey(hive, path) as key:
                    if value_name is None:
                        return True
                    try:
                        value, _ = winreg.QueryValueEx(key, value_name)
                    except FileNotFoundError:
                        continue
                    if value:
                        return True
            except FileNotFoundError:
                continue
            except OSError:
                continue
        return False

    @staticmethod
    def _is_package_not_found(stdout: str, stderr: str) -> bool:
        text = f"{stdout}\n{stderr}".lower()
        markers = (
            "unable to find package",
            "package not found",
            "0 packages found",
            "not installed. the package was not found",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _combine_error(result) -> str:
        text = result.stderr or result.stdout
        return text[:220]
