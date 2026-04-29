from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _exe_parent() -> Path:
    return Path(sys.executable).resolve().parent


def _is_onefile_bundle() -> bool:
    """True wenn eine einzelne .exe ohne Mitliefer-Ordner (_internal)."""
    if not getattr(sys, "frozen", False):
        return False
    return not (_exe_parent() / "_internal").is_dir()


APP_NAME = "GBB Software Updater"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

if getattr(sys, "frozen", False):
    EXE_PARENT = _exe_parent()
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", str(EXE_PARENT)))
    APP_DIR = EXE_PARENT
else:
    APP_DIR = _PROJECT_ROOT
    BUNDLE_DIR = _PROJECT_ROOT
    EXE_PARENT = _PROJECT_ROOT

LOG_DIR = APP_DIR / "logs"
REPORT_DIR = LOG_DIR / "reports"
# APP_DIR: beschreibbarer Bereich (bei EXE-Betrieb: Ordner der .exe)
# BUNDLE_DIR: eingebettete Ressourcen (_MEIPASS bzw. Projektroot in Dev)
# EXE_PARENT: Ordner der .exe (portable Zusatzdateien z. B. assets\ neben EXE)

COMMAND_TIMEOUT_SECONDS = 180
INSTALL_TIMEOUT_SECONDS = 1800

AVAYA_INSTALLER_SOURCE = r"\\fileserver\software\Avaya\AvayaWorkplaceSetup.exe"
OPENTEXT_INSTALLER_SOURCE = r"\\fileserver\software\OpenText\OpenTextSetup.exe"

MAX_CHOCO_RETRIES = 2

INTERNAL_INSTALLERS = {
    "avaya": {
        "path": AVAYA_INSTALLER_SOURCE,
        "silent_args": "/S",
        "display_name": "Avaya Workplace",
    },
    "opentext": {
        "path": OPENTEXT_INSTALLER_SOURCE,
        "silent_args": "/quiet /norestart",
        "display_name": "OpenText",
    },
}

SOFTWARE_ALIASES: dict[str, tuple[str, ...]] = {
    "citrix_workspace": ("citrix", "workspace", "citrix receiver", "citrixworkspace"),
    "adobe_reader": ("adobe", "acrobat", "reader", "acrobat reader"),
    "teamviewer": ("teamviewer", "team viewer"),
    "microsoft_teams": ("microsoft teams", "ms teams", "teams machine-wide", "teams work or school"),
    "office365business": ("microsoft 365", "office", "office365", "m365", "o365"),
    "opentext": ("opentext", "open text", "opentext content"),
    "avaya_workplace": ("avaya", "avaya workplace", "workplace"),
    "filezilla": ("filezilla", "file zilla"),
    "firefox": ("firefox", "mozilla", "mozilla firefox"),
}


@dataclass(frozen=True)
class SoftwarePackage:
    key: str
    display_name: str
    primary_package: str | None
    search_terms: tuple[str, ...]
    registry_keywords: tuple[str, ...]
    winget_id: str | None = None
    installer_source: str | None = None
    internal_installer_key: str | None = None
    allow_dynamic_search: bool = False


SOFTWARE_CATALOG: tuple[SoftwarePackage, ...] = (
    SoftwarePackage(
        key="citrix_workspace",
        display_name="Citrix Workspace",
        primary_package="citrix-workspace",
        winget_id="Citrix.Workspace",
        search_terms=("citrix workspace", "citrix"),
        registry_keywords=("citrix", "workspace"),
    ),
    SoftwarePackage(
        key="adobe_reader",
        display_name="Adobe Acrobat Reader",
        primary_package="adobereader",
        winget_id="Adobe.Acrobat.Reader.64-bit",
        search_terms=("adobe reader", "acrobat reader"),
        registry_keywords=("adobe", "acrobat", "reader"),
    ),
    SoftwarePackage(
        key="teamviewer",
        display_name="TeamViewer",
        primary_package="teamviewer",
        winget_id="TeamViewer.TeamViewer",
        search_terms=("teamviewer",),
        registry_keywords=("teamviewer",),
    ),
    SoftwarePackage(
        key="microsoft_teams",
        display_name="Microsoft Teams",
        primary_package="microsoft-teams",
        winget_id="Microsoft.Teams",
        search_terms=("microsoft teams", "teams"),
        registry_keywords=("teams", "microsoft teams"),
    ),
    SoftwarePackage(
        key="office365business",
        display_name="Microsoft 365 Business",
        primary_package="Office365Business",
        winget_id="Microsoft.Office",
        search_terms=("office 365 business", "m365", "microsoft 365"),
        registry_keywords=("microsoft 365", "office", "m365"),
    ),
    SoftwarePackage(
        key="opentext",
        display_name="OpenText",
        primary_package=None,
        winget_id=None,
        search_terms=("opentext", "open text"),
        registry_keywords=("opentext", "open text"),
        installer_source=OPENTEXT_INSTALLER_SOURCE,
        internal_installer_key="opentext",
        allow_dynamic_search=True,
    ),
    SoftwarePackage(
        key="avaya_workplace",
        display_name="Avaya Workplace",
        primary_package=None,
        winget_id="Avaya.AvayaWorkplace",
        search_terms=("avaya workplace", "avaya"),
        registry_keywords=("avaya", "workplace"),
        installer_source=AVAYA_INSTALLER_SOURCE,
        internal_installer_key="avaya",
        allow_dynamic_search=True,
    ),
    SoftwarePackage(
        key="filezilla",
        display_name="FileZilla",
        primary_package="filezilla",
        winget_id="TimKosse.FileZilla.Client",
        search_terms=("filezilla",),
        registry_keywords=("filezilla",),
    ),
    SoftwarePackage(
        key="firefox",
        display_name="Firefox",
        primary_package="firefox",
        winget_id="Mozilla.Firefox",
        search_terms=("firefox", "mozilla firefox"),
        registry_keywords=("mozilla", "firefox"),
    ),
)

SOFTWARE_BY_KEY = {item.key: item for item in SOFTWARE_CATALOG}
