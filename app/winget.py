from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .choco import CommandResult
from .config import COMMAND_TIMEOUT_SECONDS, INSTALL_TIMEOUT_SECONDS

HEALTH_PROBE_TIMEOUT_SECONDS = 20
WINGET_INSTALL_TIMEOUT_SECONDS = 600


class WingetService:
    def __init__(self, logger) -> None:
        self.logger = logger

    @staticmethod
    def _windows_local_winget_exe() -> Path | None:
        la = (os.environ.get("LOCALAPPDATA") or "").strip()
        if not la:
            return None
        p = Path(la) / "Microsoft" / "WindowsApps" / "winget.exe"
        return p if p.is_file() else None

    @staticmethod
    def is_available() -> bool:
        if shutil.which("winget"):
            return True
        if platform.system() != "Windows":
            return False
        p = WingetService._windows_local_winget_exe()
        return p is not None

    @staticmethod
    def _winget_argv0() -> str:
        w = shutil.which("winget")
        if w:
            return w
        if platform.system() == "Windows":
            p = WingetService._windows_local_winget_exe()
            if p is not None:
                return str(p)
        return "winget"

    @staticmethod
    def _hidden_subprocess_kwargs() -> dict[str, object]:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = None
        if creationflags and hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        return {"creationflags": creationflags, "startupinfo": startupinfo}

    def _run(
        self,
        args: list[str],
        timeout: int = COMMAND_TIMEOUT_SECONDS,
        *,
        hide_console: bool = True,
    ) -> CommandResult:
        cmd = [self._winget_argv0(), *args]
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
                stdout=(completed.stdout or "").strip(),
                stderr=(completed.stderr or "").strip(),
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
            self.logger.error("Timeout bei Kommando: %s", rendered)
            return CommandResult(command=rendered, returncode=1, stdout=stdout, stderr=stderr, timed_out=True)

    def ensure_installed(self) -> CommandResult:
        if self.is_available():
            return CommandResult("winget --version", 0, "WinGet bereits installiert", "")
        script = (
            "$ProgressPreference='SilentlyContinue';"
            "$tmp=Join-Path $env:TEMP 'Microsoft.DesktopAppInstaller.msixbundle';"
            "Invoke-WebRequest -Uri 'https://aka.ms/getwinget' -OutFile $tmp -UseBasicParsing;"
            "Add-AppxPackage -Path $tmp;"
            "Write-Output 'winget-bootstrap-done';"
        )
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]
        rendered = " ".join(shlex.quote(part) for part in cmd)
        self.logger.info("WinGet fehlt, starte Bootstrap: %s", rendered)
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=WINGET_INSTALL_TIMEOUT_SECONDS,
                check=False,
                shell=False,
                **self._hidden_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=rendered,
                returncode=1,
                stdout=(exc.stdout or "").strip() if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "").strip() if isinstance(exc.stderr, str) else "WinGet-Bootstrap Timeout",
                timed_out=True,
            )
        result = CommandResult(
            command=rendered,
            returncode=completed.returncode,
            stdout=(completed.stdout or "").strip(),
            stderr=(completed.stderr or "").strip(),
            timed_out=False,
        )
        if result.ok and self.is_available():
            self.logger.info("WinGet Bootstrap erfolgreich abgeschlossen.")
            return result
        if result.returncode == 0 and not self.is_available():
            return CommandResult(rendered, 1, result.stdout, "WinGet-Bootstrap meldet Erfolg, winget aber nicht gefunden")
        return result

    def version(self) -> str | None:
        result = self._run(["--version"], timeout=HEALTH_PROBE_TIMEOUT_SECONDS)
        if not result.ok:
            return None
        return result.stdout.splitlines()[0].strip() if result.stdout else None

    def source_available(self) -> bool:
        result = self._run(["source", "list", "--accept-source-agreements"], timeout=HEALTH_PROBE_TIMEOUT_SECONDS)
        if not result.ok:
            return False
        text = f"{result.stdout}\n{result.stderr}".lower()
        return "msstore" in text or "winget" in text

    def list_installed(self) -> dict[str, str]:
        result = self._run(["list", "--accept-source-agreements"])
        packages: dict[str, str] = {}
        if not result.ok:
            return packages
        for line in result.stdout.splitlines():
            raw = line.strip()
            if not raw or "---" in raw:
                continue
            parts = raw.split()
            pkg_id = parts[-2] if len(parts) >= 3 and "." in parts[-2] else None
            version = parts[-1] if parts else ""
            if pkg_id:
                packages[pkg_id.lower()] = version
        return packages

    def search(self, query: str) -> CommandResult:
        return self._run(["search", query, "--accept-source-agreements"])

    def search_ids(self, query: str) -> list[str]:
        result = self.search(query)
        if not result.ok:
            return []
        ids: list[str] = []
        for line in result.stdout.splitlines():
            raw = line.strip()
            if not raw or "---" in raw:
                continue
            if raw.lower().startswith("name") and "id" in raw.lower():
                continue
            # Typical table format: Name <spaces> Id <spaces> Version <spaces> Source
            parts = [p.strip() for p in re.split(r"\s{2,}", raw) if p.strip()]
            if len(parts) >= 2:
                candidate_id = parts[1]
                if "." in candidate_id:
                    ids.append(candidate_id)
        return ids

    def info(self, package_id: str) -> CommandResult:
        return self._run(["show", "-e", "--id", package_id, "--accept-source-agreements"])

    def id_exists(self, package_id: str) -> bool:
        info_result = self.info(package_id)
        if not info_result.ok:
            return False
        text = f"{info_result.stdout}\n{info_result.stderr}".lower()
        return package_id.lower() in text

    def install(self, package_id: str, *, interactive: bool = False) -> CommandResult:
        if interactive:
            args = [
                "install",
                "-e",
                "--id",
                package_id,
                "--interactive",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ]
        else:
            args = [
                "install",
                "-e",
                "--id",
                package_id,
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ]
        return self._run(args, timeout=INSTALL_TIMEOUT_SECONDS, hide_console=not interactive)

    def upgrade(self, package_id: str, *, interactive: bool = False) -> CommandResult:
        if interactive:
            args = [
                "upgrade",
                "-e",
                "--id",
                package_id,
                "--interactive",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ]
        else:
            args = [
                "upgrade",
                "-e",
                "--id",
                package_id,
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ]
        return self._run(args, timeout=INSTALL_TIMEOUT_SECONDS, hide_console=not interactive)

    def uninstall(self, package_id: str) -> CommandResult:
        return self._run(
            ["uninstall", "-e", "--id", package_id, "--silent", "--accept-source-agreements"],
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
