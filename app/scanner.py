from __future__ import annotations

from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - Windows only
    winreg = None

from .config import SOFTWARE_ALIASES, SOFTWARE_CATALOG, SoftwarePackage
from .models import SoftwareState


class SoftwareScanner:
    def __init__(self, choco_client, winget_client, logger, catalog: tuple[SoftwarePackage, ...] | None = None) -> None:
        self.choco = choco_client
        self.winget = winget_client
        self.logger = logger
        self.catalog = catalog or SOFTWARE_CATALOG

    def scan(self) -> dict[str, SoftwareState]:
        local = self.choco.list_local()
        outdated_versions = self.choco.outdated_versions()
        winget_installed = self.winget.list_installed() if self.winget.is_available() else {}
        registry_entries = self._read_registry_program_entries()

        states: dict[str, SoftwareState] = {}
        for software in self.catalog:
            detected_pkg = self._detect_package_name(software, local)
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

            if self._registry_match(software, registry_entries):
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

    @staticmethod
    def _detect_package_name(software: SoftwarePackage, local_packages: dict[str, str]) -> str | None:
        if software.primary_package and software.primary_package.lower() in local_packages:
            return software.primary_package.lower()

        aliases = SOFTWARE_ALIASES.get(software.key, ())
        for term in software.search_terms:
            term_lower = term.lower()
            for pkg in local_packages:
                if term_lower in pkg:
                    return pkg
        for alias in aliases:
            alias_lower = alias.lower()
            for pkg in local_packages:
                if alias_lower in pkg:
                    return pkg
        return None

    @staticmethod
    def _registry_match(software: SoftwarePackage, registry_entries: list[dict[str, str]]) -> bool:
        keywords = tuple(word.lower() for word in software.registry_keywords)
        aliases = tuple(word.lower() for word in SOFTWARE_ALIASES.get(software.key, ()))
        for entry in registry_entries:
            lowered = str(entry.get("display_name", "")).lower()
            if not lowered:
                continue
            if any(k in lowered for k in keywords) or any(a in lowered for a in aliases):
                uninstall = str(entry.get("uninstall_string", "")).strip()
                quiet_uninstall = str(entry.get("quiet_uninstall_string", "")).strip()
                install_location = str(entry.get("install_location", "")).strip()
                display_icon = str(entry.get("display_icon", "")).strip()
                if uninstall or quiet_uninstall:
                    return True
                if install_location and Path(install_location).exists():
                    return True
                if display_icon:
                    icon_path = display_icon.split(",")[0].strip().strip('"')
                    if icon_path and Path(icon_path).exists():
                        return True
                # Reine Registry-Leichen ohne validen Hinweis nicht als installiert werten.
        return False

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
                            def _get(name: str) -> str:
                                try:
                                    value, _ = winreg.QueryValueEx(sub_key, name)
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
