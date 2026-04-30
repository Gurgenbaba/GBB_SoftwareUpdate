from __future__ import annotations

import os
import platform
import shlex
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import COMMAND_TIMEOUT_SECONDS

HEALTH_PROBE_TIMEOUT_SECONDS = 20
CHOCO_INSTALL_TIMEOUT_SECONDS = 600


@dataclass
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    verified_after_timeout: bool = False
    recovery_exhausted: bool = False

    @property
    def ok(self) -> bool:
        if self.verified_after_timeout:
            return True
        return self.returncode == 0 and not self.timed_out


class ChocoClient:
    def __init__(self, logger) -> None:
        self.logger = logger

    @staticmethod
    def _bootstrap_skipped_existing_install(combined_log_lower: str) -> bool:
        """Offizielles install.ps1 bricht ab, wenn unter ProgramData schon Reste liegen."""
        needles = (
            "existing chocolatey installation was detected",
            "installation will not continue",
            "files from a previous installation of chocolatey",
        )
        return any(n in combined_log_lower for n in needles)

    @staticmethod
    def _windows_default_choco_exe() -> Path:
        root = (os.environ.get("ChocolateyInstall") or "").strip()
        base = Path(root) if root else Path(r"C:\ProgramData\chocolatey")
        return base / "bin" / "choco.exe"

    @staticmethod
    def is_choco_installed() -> bool:
        if shutil.which("choco"):
            return True
        if platform.system() != "Windows":
            return False
        return ChocoClient._windows_default_choco_exe().is_file()

    def _choco_argv0(self) -> str:
        w = shutil.which("choco")
        if w:
            return w
        if platform.system() == "Windows":
            p = self._windows_default_choco_exe()
            if p.is_file():
                return str(p)
        return "choco"

    @staticmethod
    def has_network() -> bool:
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=2)
            return True
        except OSError:
            return False

    @staticmethod
    def _hidden_subprocess_kwargs() -> dict[str, object]:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = None
        if creationflags and hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        return {"creationflags": creationflags, "startupinfo": startupinfo}

    def run(
        self,
        args: list[str],
        timeout: int = COMMAND_TIMEOUT_SECONDS,
        *,
        hide_console: bool = True,
    ) -> CommandResult:
        cmd = [self._choco_argv0(), *args]
        rendered = " ".join(shlex.quote(part) for part in cmd)
        self.logger.info("EXEC: %s", rendered)
        sub_kw: dict[str, object] = self._hidden_subprocess_kwargs() if hide_console else {}
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
                **sub_kw,
            )
            return CommandResult(
                command=rendered,
                returncode=completed.returncode,
                stdout=completed.stdout.strip(),
                stderr=completed.stderr.strip(),
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
            self.logger.warning(
                "Kommando-Timeout (Kind-Installer kann noch laufen; ggf. verifiziert die App nach): %s",
                rendered,
            )
            return CommandResult(
                command=rendered,
                returncode=1,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )

    def ensure_installed(self) -> CommandResult:
        if self.is_choco_installed():
            return CommandResult("choco --version", 0, "Chocolatey bereits installiert", "")
        if not self.has_network():
            return CommandResult("choco bootstrap", 1, "", "Keine Netzwerkverbindung für Chocolatey-Installation")
        install_script = (
            "Set-ExecutionPolicy Bypass -Scope Process -Force; "
            "[System.Net.ServicePointManager]::SecurityProtocol = "
            "[System.Net.ServicePointManager]::SecurityProtocol -bor 3072; "
            "iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))"
        )
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", install_script]
        rendered = " ".join(shlex.quote(part) for part in cmd)
        self.logger.info("Chocolatey fehlt, starte Bootstrap: %s", rendered)
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CHOCO_INSTALL_TIMEOUT_SECONDS,
                check=False,
                shell=False,
                **self._hidden_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=rendered,
                returncode=1,
                stdout=(exc.stdout or "").strip() if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "").strip() if isinstance(exc.stderr, str) else "Chocolatey-Bootstrap Timeout",
                timed_out=True,
            )
        result = CommandResult(
            command=rendered,
            returncode=completed.returncode,
            stdout=(completed.stdout or "").strip(),
            stderr=(completed.stderr or "").strip(),
            timed_out=False,
        )
        combined_log = f"{result.stdout}\n{result.stderr}".strip().lower()
        if self._bootstrap_skipped_existing_install(combined_log):
            if self.is_choco_installed():
                self.logger.info("Chocolatey: Setup uebersprungen, choco.exe ist nutzbar.")
                return CommandResult(
                    rendered,
                    0,
                    "Chocolatey ist bereits installiert (Setup hat eine vorhandene Installation nicht ueberschrieben).",
                    (result.stdout + ("\n" + result.stderr if result.stderr else "")).strip(),
                )
            hint = (
                "Unter C:\\ProgramData\\chocolatey liegen Dateien einer frueheren Chocolatey-Installation - "
                "das offizielle Setup bricht deshalb ab und choco.exe wurde nicht gefunden.\n\n"
                "Moegliche Schritte (Administrator-Eingabeaufforderung):\n"
                "-  choco upgrade chocolatey   ausfuehren, falls choco schon teilweise funktioniert, oder\n"
                "- Inhalt von C:\\ProgramData\\chocolatey sichern, Ordner leeren/loeschen und diese Installation erneut starten."
            )
            return CommandResult(rendered, 1, result.stdout, hint)

        if result.ok and self.is_choco_installed():
            self.logger.info("Chocolatey Bootstrap erfolgreich abgeschlossen.")
            return result

        if result.returncode == 0 and not self.is_choco_installed():
            return CommandResult(
                rendered,
                1,
                result.stdout,
                "Chocolatey-Bootstrap beendet ohne erkennbare choco-Installation (siehe Log). "
                "Pruefen Sie C:\\ProgramData\\chocolatey und ggf. PATH / Neustart der Anwendung.",
            )
        return result

    def version(self) -> str | None:
        result = self.run(["-v"], timeout=HEALTH_PROBE_TIMEOUT_SECONDS)
        if not result.ok:
            return None
        return result.stdout.splitlines()[0].strip() if result.stdout else None

    def list_local(self) -> dict[str, str]:
        result = self.run(["list", "--local-only", "--limit-output"])
        packages: dict[str, str] = {}
        if not result.ok:
            return packages
        for line in result.stdout.splitlines():
            if "|" not in line:
                continue
            name, version = line.split("|", 1)
            packages[name.strip().lower()] = version.strip()
        return packages

    def outdated(self) -> set[str]:
        return set(self.outdated_versions().keys())

    def outdated_versions(self) -> dict[str, tuple[str, str]]:
        """Paketname (lower) -> (installierte Version, verfuegbare Version)."""
        result = self.run(["outdated", "--limit-output"])
        mapping: dict[str, tuple[str, str]] = {}
        if not result.ok:
            return mapping
        for line in result.stdout.splitlines():
            if "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if not parts or not parts[0]:
                continue
            pkg = parts[0].lower()
            if len(parts) >= 3:
                mapping[pkg] = (parts[1], parts[2])
            elif len(parts) == 2:
                mapping[pkg] = (parts[1], "?")
            else:
                mapping[pkg] = ("", "")
        return mapping

    def source_list(self) -> CommandResult:
        return self.run(["source", "list", "--limit-output"], timeout=HEALTH_PROBE_TIMEOUT_SECONDS)

    def source_list_contains_url(self, url: str) -> bool:
        needle = url.strip().lower()
        if not needle:
            return False
        result = self.source_list()
        if not result.ok:
            return False
        return needle in result.stdout.lower()

    def info_exists(self, package_name: str) -> bool:
        result = self.run(["info", package_name, "--limit-output"])
        return result.ok and package_name.lower() in result.stdout.lower()

    def search(self, term: str) -> list[tuple[str, str]]:
        result = self.run(["search", term, "--limit-output"])
        matches: list[tuple[str, str]] = []
        if not result.ok:
            return matches
        for line in result.stdout.splitlines():
            if "|" not in line:
                continue
            name, version = line.split("|", 1)
            name_clean = name.strip()
            version_clean = version.strip()
            if name_clean:
                matches.append((name_clean, version_clean))
        return matches

    def install(self, package_name: str, *, use_native_installer_ui: bool = False) -> CommandResult:
        extra = ["--notSilent"] if use_native_installer_ui else []
        # choco.exe immer ohne eigenes Konsolenfenster; --notSilent betrifft nur das Kind-Setup.
        return self.run(["install", package_name, "-y", *extra], hide_console=True)

    def upgrade(self, package_name: str, *, use_native_installer_ui: bool = False) -> CommandResult:
        extra = ["--notSilent"] if use_native_installer_ui else []
        return self.run(["upgrade", package_name, "-y", *extra], hide_console=True)

    def uninstall_remove_metadata_only(self, package_name: str) -> CommandResult:
        """Remove Chocolatey lib metadata when the app is already gone (ghost entry)."""
        return self.run(["uninstall", package_name, "-y", "--skip-autouninstaller"], hide_console=True)
