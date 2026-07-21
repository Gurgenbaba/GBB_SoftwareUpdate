from __future__ import annotations

import os
import platform
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover - Windows only
    winreg = None

from .config import SOFTWARE_ALIASES, SOFTWARE_CATALOG, SoftwarePackage
from .identity import detect_choco_with_identity, registry_match_with_identity
from .models import SoftwareState


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

# Phrase-only registry display matching (substring, lowercased). Avoids "teams" matching "TeamSpeak".
_REGISTRY_STRICT_DISPLAY_PHRASES: dict[str, tuple[str, ...]] = {
    "microsoft_teams": (
        "microsoft teams",
        "msteams",
        "ms teams",
        "teams machine-wide",
        "teams work or school",
    ),
    "teamspeak": ("teamspeak", "team speak"),
}


@dataclass(frozen=True)
class PostUninstallVerification:
    """Authoritative post-uninstall presence check (stricter than raw scan())."""

    still_present: bool
    verification_status: str
    evidence: str
    stale_evidence_ignored: str


class SoftwareScanner:
    def __init__(
        self,
        choco_client,
        winget_client,
        logger,
        catalog: tuple[SoftwarePackage, ...] | None = None,
        software_providers: dict[str, Any] | None = None,
    ) -> None:
        self.choco = choco_client
        self.winget = winget_client
        self.logger = logger
        self.catalog = catalog or SOFTWARE_CATALOG
        self._providers: dict[str, Any] = software_providers if isinstance(software_providers, dict) else {}

    def _provider_cfg(self, software: SoftwarePackage) -> dict[str, Any]:
        raw = self._providers.get(software.key, {})
        return raw if isinstance(raw, dict) else {}

    def scan(self) -> dict[str, SoftwareState]:
        local = self.choco.list_local()
        outdated_versions = self.choco.outdated_versions()
        winget_installed = self.winget.list_installed() if self.winget.is_available() else {}
        registry_entries = self._read_registry_program_entries()

        states: dict[str, SoftwareState] = {}
        for software in self.catalog:
            detected_pkg = self._detect_package_name_choco(software, local)
            if detected_pkg:
                installed_ver = local.get(detected_pkg, "")
                if detected_pkg in outdated_versions:
                    cur, avail = outdated_versions[detected_pkg]
                    states[software.key] = SoftwareState(
                        "Update verfuegbar",
                        detected_pkg,
                        installed_version=cur or installed_ver,
                        available_version=avail or "?",
                        provider="Chocolatey",
                    )
                else:
                    states[software.key] = SoftwareState(
                        "Aktuell",
                        detected_pkg,
                        installed_version=installed_ver,
                        available_version=installed_ver,
                        provider="Chocolatey",
                    )
                continue

            if software.winget_id and software.winget_id.lower() in winget_installed:
                ver = winget_installed.get(software.winget_id.lower(), "")
                states[software.key] = SoftwareState(
                    "Installiert",
                    software.winget_id,
                    detail="Ueber WinGet erkannt",
                    installed_version=ver or "(WinGet)",
                    available_version=ver or "(WinGet)",
                    provider="WinGet",
                )
                continue

            if self._registry_match(software, registry_entries, self._provider_cfg(software)):
                states[software.key] = SoftwareState(
                    "Installiert",
                    package_name=None,
                    detail="Ueber Registry erkannt (valider Deinstallationshinweis)",
                    installed_version="(Registry)",
                    available_version="—",
                    provider="Intern",
                    uninstall_hint="Manuelle Deinstallation noetig",
                )
                continue

            states[software.key] = SoftwareState(
                "Nicht installiert",
                installed_version="—",
                available_version="—",
                provider="Quelle erforderlich",
            )
        return states

    def _detect_package_name_choco(self, software: SoftwarePackage, local_packages: dict[str, str]) -> str | None:
        return detect_choco_with_identity(software, local_packages, self._provider_cfg(software))

    @staticmethod
    def _registry_phrase_list_matches(lowered: str, phrases: tuple[str, ...]) -> bool:
        return any(p and p in lowered for p in phrases)

    @staticmethod
    def _registry_term_matches(term: str, lowered: str) -> bool:
        """Multi-word terms: substring match. Single-token: whole-token match (no 'teams' inside 'teamspeak')."""
        t = term.strip().lower()
        if not t:
            return False
        if " " in t:
            return t in lowered
        return re.search(rf"(?<!\w){re.escape(t)}(?!\w)", lowered, flags=re.IGNORECASE) is not None

    @staticmethod
    def _legacy_registry_display_matches(software: SoftwarePackage, display_name: str) -> bool:
        lowered = str(display_name or "").lower()
        if not lowered:
            return False
        strict = _REGISTRY_STRICT_DISPLAY_PHRASES.get(software.key)
        if strict is not None:
            return SoftwareScanner._registry_phrase_list_matches(lowered, strict)
        keywords = tuple(word.lower() for word in software.registry_keywords)
        aliases = tuple(word.lower() for word in SOFTWARE_ALIASES.get(software.key, ()))
        for term in keywords + aliases:
            if SoftwareScanner._registry_term_matches(term, lowered):
                return True
        return False

    @staticmethod
    def _registry_display_matches(
        software: SoftwarePackage,
        display_name: str,
        provider_cfg: dict[str, Any] | None = None,
    ) -> bool:
        prov = provider_cfg if isinstance(provider_cfg, dict) else {}
        lowered = str(display_name or "").lower()
        im = registry_match_with_identity(software, lowered, prov)
        if im is not None:
            return im
        return SoftwareScanner._legacy_registry_display_matches(software, display_name)

    @staticmethod
    def registry_entry_signals_real_install(entry: dict[str, str]) -> bool:
        """True only if uninstall path, install dir, or icon path is actionable and exists on disk (or msiexec)."""

        def _uninstall_cmd_actionable(raw: str) -> bool:
            raw = (raw or "").strip()
            if not raw:
                return False
            try:
                parts = shlex.split(raw, posix=False)
            except ValueError:
                parts = [p for p in raw.split() if p]
            if not parts:
                return False
            exe0 = os.path.expandvars(parts[0].strip('"'))
            name = Path(exe0).name.lower()
            if name in ("msiexec.exe", "msiexec"):
                return True
            if name in ("rundll32.exe", "rundll32"):
                return Path(exe0).is_file()
            if not name.endswith(".exe"):
                return False
            try:
                return Path(exe0).is_file()
            except OSError:
                return False

        for raw in (entry.get("uninstall_string") or "", entry.get("quiet_uninstall_string") or ""):
            if _uninstall_cmd_actionable(raw):
                return True
        loc = os.path.expandvars(str(entry.get("install_location") or "").strip())
        if loc:
            try:
                if Path(loc).exists():
                    return True
            except OSError:
                pass
        di = str(entry.get("display_icon") or "").strip()
        if di:
            ip = os.path.expandvars(di.split(",")[0].strip().strip('"'))
            if ip:
                try:
                    if Path(ip).exists():
                        return True
                except OSError:
                    pass
        return False

    @staticmethod
    def _registry_match(software: SoftwarePackage, registry_entries: list[dict[str, str]], provider_cfg: dict[str, Any]) -> bool:
        for entry in registry_entries:
            if not SoftwareScanner._registry_display_matches(software, entry.get("display_name", ""), provider_cfg):
                continue
            if SoftwareScanner.registry_entry_signals_real_install(entry):
                return True
        return False

    def post_uninstall_verify(self, software: SoftwarePackage) -> PostUninstallVerification:
        """Re-evaluate installation after uninstall using stricter evidence than scan() alone."""
        local = self.choco.list_local()
        winget_installed = self.winget.list_installed() if self.winget.is_available() else {}
        registry_entries = self._read_registry_program_entries()

        stale: list[str] = []
        evidence: list[str] = []

        choco_pkg = self._detect_package_name_choco(software, local)
        wg_hit = bool(software.winget_id and software.winget_id.lower() in winget_installed)
        if wg_hit:
            evidence.append(f"WinGet inventory: {software.winget_id}")

        prov = self._provider_cfg(software)
        reg_real = [
            e
            for e in registry_entries
            if self._registry_display_matches(software, e.get("display_name", ""), prov) and self.registry_entry_signals_real_install(e)
        ]
        for e in reg_real[:3]:
            evidence.append(f"ARP/Registry (valid): {e.get('display_name', '')[:80]}")

        if software.key == "citrix_workspace":
            phys = self._citrix_workspace_physical_present()
            procs = self._citrix_related_processes_running()
            if choco_pkg and not phys:
                stale.append(f"Chocolatey lists '{choco_pkg}' but no Citrix Workspace/ICA binaries under Program Files")
            if choco_pkg and phys:
                evidence.append(f"Chocolatey + binaries: {choco_pkg}")
            elif choco_pkg and not phys:
                pass
            if procs:
                evidence.append(f"Citrix-related processes still running: {', '.join(procs)}")
            still = bool(phys or wg_hit or reg_real or procs)
            if not still and choco_pkg:
                stale.append(f"Ignored Chocolatey-only entry '{choco_pkg}' (no binaries, no WinGet, no valid ARP)")
            status = "absent" if not still else "present"
            return PostUninstallVerification(still, status, " | ".join(evidence)[:500], " | ".join(stale)[:500])

        if software.key == "microsoft_teams":
            classic = self._teams_classic_install_present()
            mwi = self._teams_machine_wide_install_present(registry_entries)
            appx = self._teams_appx_or_msix_present()
            if classic:
                evidence.append("Teams classic (per-user Update.exe or Teams.exe)")
            if mwi:
                evidence.append("Teams machine-wide installer (ARP)")
            if appx:
                evidence.append("Teams AppX/MSIX package still registered")
            if choco_pkg and not (classic or mwi or appx or wg_hit or reg_real):
                stale.append(f"Chocolatey lists '{choco_pkg}' but no classic/MWI/AppX/WinGet/valid ARP evidence")
            still = bool(classic or mwi or appx or wg_hit or reg_real)
            if not still and choco_pkg:
                stale.append(f"Ignored Chocolatey-only '{choco_pkg}'")
            status = "absent" if not still else "present"
            return PostUninstallVerification(still, status, " | ".join(evidence)[:500], " | ".join(stale)[:500])

        if software.key == "office365business":
            office_phys, office_msg = self._office365_physical_evidence()
            if office_phys:
                evidence.extend(office_msg)
            c2r_reg, access_denied, c2r_msg = self._office365_click_to_run_registry()
            if c2r_msg:
                evidence.extend(c2r_msg)
            if access_denied and not office_phys:
                return PostUninstallVerification(
                    True,
                    "access_denied",
                    " | ".join(evidence + ["Office Click-to-Run registry not readable (Zugriff verweigert)"])[:500],
                    " | ".join(stale)[:500],
                )
            if choco_pkg and not (office_phys or wg_hit or reg_real):
                stale.append(f"Chocolatey lists '{choco_pkg}' but no Office binaries/Click-to-Run/valid ARP")
            still = bool(office_phys or wg_hit or reg_real or c2r_reg)
            if not still and choco_pkg:
                stale.append(f"Ignored Chocolatey-only '{choco_pkg}'")
            status = "absent" if not still else "present"
            return PostUninstallVerification(still, status, " | ".join(evidence)[:500], " | ".join(stale)[:500])

        if choco_pkg:
            evidence.append(f"Chocolatey: {choco_pkg}")
        still = bool(choco_pkg or wg_hit or reg_real)
        status = "absent" if not still else "present"
        return PostUninstallVerification(still, status, " | ".join(evidence)[:500], " | ".join(stale)[:500])

    @staticmethod
    def _citrix_workspace_physical_present() -> bool:
        roots = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Citrix",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Citrix",
        ]
        markers = ("TrolleyExpress.exe", "CitrixWorkspaceApp.exe", "WFICA32.exe", "SelfServicePlugin.exe")
        for root in roots:
            if not root.is_dir():
                continue
            try:
                for sub in sorted(root.glob("Citrix Workspace*"), key=lambda p: str(p), reverse=True):
                    if sub.is_dir():
                        for m in markers:
                            try:
                                hit = next(sub.rglob(m), None)
                                if hit and hit.is_file():
                                    return True
                            except OSError:
                                continue
            except OSError:
                pass
            for sub_name in ("ICA Client", "Receiver", "Workspace"):
                p = root / sub_name
                if p.is_dir():
                    for m in markers:
                        try:
                            hit = next(p.rglob(m), None)
                            if hit and hit.is_file():
                                return True
                        except OSError:
                            continue
            for m in markers:
                try:
                    for hit in root.rglob(m):
                        if hit.is_file():
                            return True
                except OSError:
                    continue
        return False

    @staticmethod
    def _citrix_related_processes_running() -> list[str]:
        names = ("SelfService.exe", "wfica32.exe", "concentr.exe", "redirector.exe", "CitrixWorkspaceApp.exe")
        found: list[str] = []
        try:
            cp = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding=_console_encoding(),
                errors="replace",
                timeout=45,
                check=False,
                shell=False,
            )
            blob = (cp.stdout or "").lower()
            for n in names:
                if n.lower() in blob:
                    found.append(n)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return found

    @staticmethod
    def _teams_classic_install_present() -> bool:
        la = os.environ.get("LOCALAPPDATA", "")
        if not la:
            return False
        base = Path(la) / "Microsoft" / "Teams"
        # Citrix Workspace VDI optimization drops stubs in this directory that are
        # very small (< 512 KB). Real Teams Classic Update.exe (Squirrel) and
        # Teams.exe are both well above 1 MB. Require BOTH files to be present
        # and each to exceed the size threshold so the Citrix plugin is ignored.
        _MIN_BYTES = 512 * 1024
        update_exe = base / "Update.exe"
        teams_exe = base / "current" / "Teams.exe"
        try:
            if (
                update_exe.is_file()
                and update_exe.stat().st_size >= _MIN_BYTES
                and teams_exe.is_file()
                and teams_exe.stat().st_size >= _MIN_BYTES
            ):
                return True
        except OSError:
            pass
        return False

    @staticmethod
    def _teams_machine_wide_install_present(registry_entries: list[dict[str, str]]) -> bool:
        for entry in registry_entries:
            dn = str(entry.get("display_name", "")).lower()
            if "machine-wide" not in dn and "machine wide" not in dn:
                continue
            if not re.search(r"(?<!\w)teams(?!\w)", dn, flags=re.IGNORECASE):
                continue
            if SoftwareScanner.registry_entry_signals_real_install(entry):
                return True
        return False

    @staticmethod
    def _teams_appx_or_msix_present() -> bool:
        ps = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$c=(Get-AppxPackage -AllUsers | Where-Object { $_.Name -match 'MSTeams|MicrosoftTeams' } | Measure-Object).Count;"
            "$c2=(Get-AppxPackage | Where-Object { $_.Name -match 'MSTeams|MicrosoftTeams' } | Measure-Object).Count;"
            "Write-Output ($c+$c2)"
        )
        try:
            cp = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps],
                capture_output=True,
                text=True,
                encoding=_console_encoding(),
                errors="replace",
                timeout=45,
                check=False,
                shell=False,
            )
            out = (cp.stdout or "").strip().splitlines()
            if not out:
                return False
            n = int(out[-1].strip() or "0")
            return n > 0
        except (ValueError, OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _office365_physical_evidence() -> tuple[bool, list[str]]:
        msgs: list[str] = []
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        c2r = pf / "Common Files" / "Microsoft Shared" / "ClickToRun" / "OfficeClickToRun.exe"
        if c2r.is_file():
            msgs.append(f"OfficeClickToRun.exe present: {c2r}")
        root16 = pf / "Microsoft Office" / "root" / "Office16"
        for exe in ("WINWORD.EXE", "EXCEL.EXE", "POWERPNT.EXE", "OUTLOOK.EXE", "MSACCESS.EXE"):
            p = root16 / exe
            try:
                if p.is_file():
                    msgs.append(f"Office app binary: {p}")
                    return True, msgs
            except OSError:
                continue
        if c2r.is_file():
            return True, msgs
        alt = pf / "Microsoft Office"
        try:
            if alt.is_dir():
                for hit in alt.rglob("WINWORD.EXE"):
                    if hit.is_file():
                        msgs.append(f"Office WINWORD: {hit}")
                        return True, msgs
        except OSError:
            pass
        return False, msgs

    @staticmethod
    def _office_click_to_run_service_active() -> bool:
        try:
            cp = subprocess.run(
                ["sc", "query", "OfficeClickToRun"],
                capture_output=True,
                text=True,
                encoding=_console_encoding(),
                errors="replace",
                timeout=20,
                check=False,
                shell=False,
            )
            out = ((cp.stdout or "") + (cp.stderr or "")).upper()
            return "RUNNING" in out or "START_PENDING" in out
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _office365_click_to_run_registry() -> tuple[bool, bool, list[str]]:
        """Returns (c2r_registry_implies_installed, access_denied, messages)."""
        if winreg is None:
            return False, False, []
        msgs: list[str] = []
        access_denied = False
        key_path = r"SOFTWARE\Microsoft\Office\ClickToRun\Configuration"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
                def q(name: str) -> str:
                    try:
                        v, _ = winreg.QueryValueEx(k, name)
                        return str(v or "").strip()
                    except OSError:
                        return ""

                pr = q("ProductReleaseIds") or q("ProductToConsumerVersion")
                install_path = os.path.expandvars((q("InstallationPath") or q("ClientFolderToStaging") or "").strip())
                if install_path and Path(install_path).exists():
                    msgs.append(f"Office Click-to-Run InstallationPath exists: {install_path}")
                    return True, False, msgs
                if pr and len(pr) > 3 and SoftwareScanner._office_click_to_run_service_active():
                    msgs.append("Office Click-to-Run service active with product configuration in registry")
                    return True, False, msgs
        except OSError as exc:
            if getattr(exc, "winerror", None) == 5 or getattr(exc, "errno", None) in (13, 5):
                access_denied = True
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Office\16.0\Common\InstallRoot") as k2:
                path = ""
                try:
                    path, _ = winreg.QueryValueEx(k2, "Path")
                except OSError:
                    pass
                path = os.path.expandvars(str(path or "").strip())
                if path and Path(path).exists():
                    msgs.append(f"Office InstallRoot Path exists: {path}")
                    return True, False, msgs
        except OSError as exc:
            if getattr(exc, "winerror", None) == 5 or getattr(exc, "errno", None) in (13, 5):
                access_denied = True
        return False, access_denied, msgs

    def _read_registry_program_entries(self) -> list[dict[str, str]]:
        if winreg is None:
            return []
        entries: list[dict[str, str]] = []
        hives = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]
        paths = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ]
        for hive in hives:
            for path in paths:
                entries.extend(self._read_uninstall_key(hive, path))
        return entries

    def _read_uninstall_key(self, hive, path: str) -> list[dict[str, str]]:
        values: list[dict[str, str]] = []
        try:
            with winreg.OpenKey(hive, path) as root:
                index = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(root, sub_name) as sub_key:
                            def _get(name: str, _sk=sub_key) -> str:
                                try:
                                    value, _ = winreg.QueryValueEx(_sk, name)
                                    return str(value or "").strip()
                                except OSError:
                                    return ""

                            name = _get("DisplayName")
                            if name:
                                values.append(
                                    {
                                        "display_name": name,
                                        "uninstall_string": _get("UninstallString"),
                                        "quiet_uninstall_string": _get("QuietUninstallString"),
                                        "install_location": _get("InstallLocation"),
                                        "display_icon": _get("DisplayIcon"),
                                    }
                                )
                    except OSError:
                        continue
        except OSError:
            return values
        return values
