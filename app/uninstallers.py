from __future__ import annotations

import shlex
import subprocess
import os
import shutil
import fnmatch
import re
from pathlib import Path
from typing import Callable

try:
    import winreg
except ImportError:  # pragma: no cover - Windows only
    winreg = None

from .config import INSTALL_TIMEOUT_SECONDS, SOFTWARE_ALIASES, SOFTWARE_BY_KEY, SoftwarePackage
from .models import ReportEntry, SoftwareState


class UninstallerService:
    @staticmethod
    def _contains_term(name: str, term: str) -> bool:
        clean_name = (name or "").lower()
        clean_term = (term or "").strip().lower()
        if not clean_name or not clean_term:
            return False
        if re.search(rf"(?<![a-z0-9]){re.escape(clean_term)}(?![a-z0-9])", clean_name):
            return True
        return False

    def __init__(self, choco_client, winget_client, logger, software_by_key: dict[str, SoftwarePackage] | None = None) -> None:
        self.choco = choco_client
        self.winget = winget_client
        self.logger = logger
        self.software_by_key = software_by_key or SOFTWARE_BY_KEY

    def process(
        self,
        selected_keys: list[str],
        current_states: dict[str, SoftwareState],
        status_callback: Callable[[str, SoftwareState], None],
        progress_callback: Callable[[int, int], None],
        item_start_callback: Callable[[str], None] | None = None,
        dry_run: bool = False,
    ) -> list[ReportEntry]:
        total = len(selected_keys)
        rows: list[ReportEntry] = []
        for index, key in enumerate(selected_keys, start=1):
            progress_callback(index - 1, total)
            if item_start_callback is not None:
                item_start_callback(key)
            software = self.software_by_key[key]
            prev = current_states.get(key, SoftwareState("Nicht geprueft"))
            action, new_state = self._remove_single(software, prev, dry_run)
            current_states[key] = new_state
            status_callback(key, new_state)
            progress_callback(index, total)
            result = "DRY-RUN" if dry_run and action.startswith("DRY-RUN") else ("Fehler" if "Fehler" in new_state.status else "OK")
            rows.append(
                ReportEntry(
                    software_key=key,
                    package_name=new_state.package_name or "",
                    status_before=prev.status,
                    action=action,
                    status_after=new_state.status,
                    result=result,
                    error_message=new_state.detail,
                    provider=new_state.provider or "",
                )
            )
        return rows

    def _remove_single(self, software: SoftwarePackage, prev: SoftwareState, dry_run: bool) -> tuple[str, SoftwareState]:
        if prev.status == "Nicht installiert":
            return ("Keine Aktion erforderlich", prev)

        cleanup_messages: list[str] = []
        hard_failures: list[str] = []
        choco_failed = False
        citrix_native_ok = False
        citrix_native_attempted = False
        if prev.provider == "Chocolatey" and prev.package_name:
            if dry_run:
                return ("DRY-RUN: wuerde entfernen", SoftwareState("Dry-Run (unveraendert)", package_name=prev.package_name, provider="Chocolatey"))
            result = self.choco.run(["uninstall", prev.package_name, "-y"], timeout=INSTALL_TIMEOUT_SECONDS)
            choco_msg = (result.stderr or result.stdout or "").strip()
            if choco_msg:
                cleanup_messages.append(choco_msg)
            choco_failed = result.returncode != 0
            if choco_failed and software.key == "citrix_workspace" and not dry_run:
                citrix_native_attempted = True
                native_ok, native_msg = self._try_citrix_workspace_native_uninstall(dry_run)
                if native_msg:
                    cleanup_messages.append(native_msg)
                if native_ok:
                    citrix_native_ok = True
                    choco_failed = False
            if choco_failed:
                hard_failures.append(f"Chocolatey RC={result.returncode}")

        if software.winget_id and self.winget.is_available():
            if dry_run:
                return ("DRY-RUN: wuerde entfernen", SoftwareState("Dry-Run (unveraendert)", package_name=software.winget_id, provider="WinGet"))
            result = self.winget.uninstall(software.winget_id)
            winget_msg = (result.stderr or result.stdout or "").strip()
            if winget_msg:
                cleanup_messages.append(winget_msg)
            if result.returncode != 0:
                winget_msg_l = winget_msg.lower()
                not_found_hint = (
                    "no installed package found" in winget_msg_l
                    or "kein installiertes paket gefunden" in winget_msg_l
                    or "es wurde kein installiertes paket gefunden" in winget_msg_l
                )
                if not not_found_hint:
                    hard_failures.append(f"WinGet RC={result.returncode}")

        if software.key == "citrix_workspace" and not dry_run and not citrix_native_ok and not citrix_native_attempted:
            citrix_native_attempted = True
            native_ok, native_msg = self._try_citrix_workspace_native_uninstall(dry_run)
            if native_msg:
                cleanup_messages.append(native_msg)
            if native_ok:
                citrix_native_ok = True
                hard_failures = [h for h in hard_failures if not (h.startswith("Chocolatey RC=") or h.startswith("WinGet RC="))]

        # Restlose Entfernung: versuche gefundene Registry-Deinstallationsstrings auszufuehren.
        if dry_run:
            return (
                "DRY-RUN: wuerde restlos entfernen",
                SoftwareState(
                    "Dry-Run (unveraendert)",
                    detail="DRY-RUN: Registry-/UninstallString-Cleanup wuerde ausgefuehrt",
                    provider="Intern",
                ),
            )

        killed, kill_detail = self._terminate_related_processes(software, dry_run=dry_run)
        if killed > 0:
            cleanup_messages.append(f"Lock-Cleanup: {killed} Prozess(e) beendet")
        if kill_detail:
            cleanup_messages.append(kill_detail)

        removed_count, cleanup_detail = self._force_registry_cleanup(software, prev)
        if removed_count > 0:
            cleanup_messages.append(f"Registry-Cleanup ausgefuehrt: {removed_count} Eintrag/Eintraege")
        if cleanup_detail:
            cleanup_messages.append(cleanup_detail)

        deep_removed, deep_detail = self._deep_cleanup_leftovers(software, dry_run=dry_run)
        if deep_removed > 0:
            cleanup_messages.append(f"Deep-Cleanup entfernt: {deep_removed} Rest(e)")
        if deep_detail:
            cleanup_messages.append(deep_detail)

        self._append_access_denied_hint(cleanup_messages)

        # Verifiziere nach Cleanup, ob noch ein valider uninstallbarer Treffer vorhanden ist.
        if self._registry_has_actionable_match(software, prev):
            return (
                "Manuelle Deinstallation",
                SoftwareState(
                    "Fehler: Manuelle Deinstallation nötig",
                    detail=" | ".join(m for m in cleanup_messages if m).strip()[:500] or "Rest-Eintraege vorhanden",
                    provider="Intern",
                ),
            )
        if hard_failures:
            failure_detail = " | ".join([*hard_failures, *[m for m in cleanup_messages if m]])[:500]
            return (
                "Entfernen",
                SoftwareState(
                    "Fehler: Deinstallation fehlgeschlagen",
                    detail=failure_detail,
                    provider="Intern",
                ),
            )
        return (
            "Entfernen",
            SoftwareState(
                "Nicht installiert",
                detail=" | ".join(m for m in cleanup_messages if m).strip()[:500],
                provider="Intern",
            ),
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
        exe = UninstallerService._exe_from_cmd(cmd)
        lower = [arg.lower() for arg in cmd]
        joined_l = " ".join(cmd).lower()
        # Microsoft Teams (per-user): Update.exe nutzt --uninstall -s; blindes /S erzeugt WinError 87.
        if exe == "update.exe" and "teams" in joined_l:
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
                                    def _get(name: str) -> str:
                                        try:
                                            value, _ = winreg.QueryValueEx(sk, name)
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
                                        }
                                    )
                            except OSError:
                                continue
                except OSError:
                    continue
        return entries

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
                for p in root.rglob("TrolleyExpress.exe"):
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
            try:
                cp = subprocess.run(
                    [str(exe_path), "/silent", "/uninstall"],
                    check=False,
                    timeout=900,
                    capture_output=True,
                    text=True,
                    shell=False,
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
    def _append_access_denied_hint(messages: list[str]) -> None:
        blob = " ".join(messages).lower()
        if "zugriff verweigert" in blob or "winerror 5" in blob or "access is denied" in blob:
            messages.append(
                "Hinweis: Office/365-Deinstall oft nur als Administrator oder mit Microsoft "
                "Support and Recovery Assistant (SaRA) moeglich."
            )

    def _matches_prev(self, software: SoftwarePackage, prev: SoftwareState, display_name: str) -> bool:
        name = (display_name or "").lower()
        if not name:
            return False
        if software.key == "microsoft_teams" and "teamspeak" in name:
            return False
        if software.key == "microsoft_teams":
            if "machine-wide" in name and "teams" in name:
                return True
            if "teams" in name and "work or school" in name:
                return True
        pkg = (prev.package_name or "").lower()
        if pkg and self._contains_term(name, pkg):
            return True
        if self._contains_term(name, software.display_name):
            return True
        software_key_compact = software.key.lower().replace("_", "")
        if software_key_compact and software_key_compact in name.replace(" ", ""):
            return True
        aliases = SOFTWARE_ALIASES.get(software.key, ())
        if any(self._contains_term(name, alias) for alias in aliases):
            return True
        return False

    def _force_registry_cleanup(self, software: SoftwarePackage, prev: SoftwareState) -> tuple[int, str]:
        removed = 0
        messages: list[str] = []
        for entry in self._iter_uninstall_entries():
            if not self._matches_prev(software, prev, entry.get("display_name", "")):
                continue
            raw_cmd = entry.get("quiet_uninstall_string") or entry.get("uninstall_string")
            cmd = self._to_silent_uninstall(self._parse_cmdline(raw_cmd))
            if not cmd:
                continue
            try:
                completed = subprocess.run(
                    cmd,
                    check=False,
                    timeout=INSTALL_TIMEOUT_SECONDS,
                    capture_output=True,
                    text=True,
                    shell=False,
                )
                if completed.returncode == 0:
                    removed += 1
                elif completed.returncode in (1605, 1614):
                    # MSI: product already uninstalled / removed.
                    removed += 1
                else:
                    messages.append(f"{entry.get('display_name')}: RC={completed.returncode}")
            except Exception as exc:  # pylint: disable=broad-except
                messages.append(f"{entry.get('display_name')}: {exc}")
        return removed, " | ".join(messages)[:350]

    def _registry_has_actionable_match(self, software: SoftwarePackage, prev: SoftwareState) -> bool:
        for entry in self._iter_uninstall_entries():
            if not self._matches_prev(software, prev, entry.get("display_name", "")):
                continue
            if entry.get("quiet_uninstall_string") or entry.get("uninstall_string"):
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

    def _terminate_related_processes(self, software: SoftwarePackage, dry_run: bool) -> tuple[int, str]:
        patterns = self._process_patterns(software)
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
