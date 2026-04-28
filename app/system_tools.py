from __future__ import annotations

import getpass
import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
try:
    import winreg
except ImportError:  # pragma: no cover - Windows only
    winreg = None


@dataclass
class SystemActionResult:
    ok: bool
    lines: list[str]


class SystemToolsService:
    def __init__(self, logger) -> None:
        self.logger = logger

    @staticmethod
    def _hidden_kwargs() -> dict:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = None
        if creationflags and hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        return {"creationflags": creationflags, "startupinfo": startupinfo}

    def _run(self, cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            **self._hidden_kwargs(),
        )

    @staticmethod
    def _users_root() -> Path:
        drive = (os.environ.get("SystemDrive", "C:") or "C:").rstrip("\\/")
        return Path(f"{drive}\\Users")

    def run_account_safe_update(self, source_user: str, target_name: str, dry_run: bool) -> SystemActionResult:
        current_user = getpass.getuser()
        source = (source_user or "").strip() or current_user
        target = (target_name or "").strip()
        if not target:
            return SystemActionResult(False, ["Gewünschter neuer Name fehlt."])
        if not self._user_exists(source):
            return SystemActionResult(False, [f"Benutzer nicht gefunden: {source}"])

        lines = [f"Angemeldeter Benutzer: {current_user}", f"Gewünschter Benutzer: {source}", f"Gewünschter neuer Name: {target}"]
        effective_user = source
        if source.lower() != target.lower():
            if self._user_exists(target):
                return SystemActionResult(False, [*lines, f"FEHLER: Der gewünschte Benutzername existiert bereits: {target}"])
            if dry_run:
                lines.append(f"[DRY-RUN] würde lokalen Benutzernamen umbenennen: {source} -> {target}")
                effective_user = target
            else:
                source_ps = source.replace("'", "''")
                target_ps = target.replace("'", "''")
                rename_cmd = (
                    "Rename-LocalUser "
                    f"-Name '{source_ps}' "
                    f"-NewName '{target_ps}'"
                )
                rename_result = self._run(["powershell", "-NoProfile", "-Command", rename_cmd], timeout=60)
                if rename_result.returncode != 0:
                    lines.append("FEHLER: Lokaler Benutzername konnte nicht umbenannt werden.")
                    lines.append((rename_result.stderr or rename_result.stdout or "").strip()[:320])
                    return SystemActionResult(False, lines)
                lines.append("Lokaler Benutzername umbenannt.")
                effective_user = target

        if dry_run:
            lines.append(f"[DRY-RUN] würde Kontovollname setzen: net user {effective_user} /fullname:{target}")
        else:
            result = self._run(["net", "user", effective_user, f'/fullname:{target}'])
            if result.returncode != 0:
                lines.append("Kontovollname konnte nicht gesetzt werden.")
                lines.append((result.stderr or result.stdout or "").strip()[:280])
            else:
                lines.append("Kontovollname aktualisiert.")

        # Profile path may still use the old folder name until explicit migration.
        profile_user_for_checks = source
        struct = self._ensure_profile_structure(profile_user_for_checks, dry_run=dry_run)
        lines.extend(struct.lines)
        refs = self._scan_path_references(profile_user_for_checks)
        lines.extend(refs.lines)
        ok = all("FEHLER" not in line for line in lines)
        return SystemActionResult(ok, lines)

    def _ensure_profile_structure(self, source_user: str, dry_run: bool) -> SystemActionResult:
        user_home = self._users_root() / source_user
        if not user_home.exists():
            return SystemActionResult(False, [f"FEHLER: Profilpfad nicht gefunden: {user_home}"])
        folders = [
            user_home / "Desktop",
            user_home / "Documents",
            user_home / "Downloads",
            user_home / "Pictures",
            user_home / "Videos",
            user_home / "Music",
            user_home / "Work",
            user_home / "Software",
        ]
        lines: list[str] = ["Ordnerstruktur-Prüfung:"]
        ok = True
        for folder in folders:
            if folder.exists():
                lines.append(f"- OK vorhanden: {folder}")
                continue
            if dry_run:
                lines.append(f"- [DRY-RUN] wuerde erstellen: {folder}")
                continue
            try:
                folder.mkdir(parents=True, exist_ok=True)
                lines.append(f"- erstellt: {folder}")
            except OSError as exc:
                ok = False
                lines.append(f"- FEHLER bei {folder}: {exc}")
        return SystemActionResult(ok, lines)

    def _scan_path_references(self, old_name: str) -> SystemActionResult:
        needle = old_name.lower().strip()
        lines: list[str] = ["Pfadreferenz-Scan (safe, read-only):"]
        if not needle:
            return SystemActionResult(True, lines)

        startup_dirs = [
            Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup",
            Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs\Startup",
        ]
        hits = 0
        for root in startup_dirs:
            if not root.exists():
                continue
            for item in root.rglob("*"):
                if needle in str(item).lower():
                    hits += 1
                    lines.append(f"- Startup-Hinweis: {item}")

        reg_cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            r"Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' | Out-String",
        ]
        reg_out = self._run(reg_cmd, timeout=60)
        text = (reg_out.stdout or "") + "\n" + (reg_out.stderr or "")
        if needle in text.lower():
            hits += 1
            lines.append("- Registry Run enthaelt Verweise auf alten Namen (siehe Log).")

        if hits == 0:
            lines.append("- Keine offensichtlichen Altpfad-Referenzen gefunden.")
        return SystemActionResult(True, lines)

    def scan_windows_updates(self) -> SystemActionResult:
        script = (
            "$s=New-Object -ComObject Microsoft.Update.Session;"
            "$searcher=$s.CreateUpdateSearcher();"
            "$r=$searcher.Search(\"IsInstalled=0 and Type='Software'\");"
            "if($r.Updates.Count -eq 0){'Keine Updates gefunden.'} else {"
            "for($i=0;$i -lt $r.Updates.Count;$i++){"
            "$u=$r.Updates.Item($i);"
            "$kbs=($u.KBArticleIDs -join ',');"
            "Write-Output (\"[{0}] {1} KB:{2}\" -f $i,$u.Title,$kbs)"
            "}"
            "}"
        )
        res = self._run(["powershell", "-NoProfile", "-Command", script], timeout=300)
        lines = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
        if not lines:
            lines = [((res.stderr or "").strip() or "Keine Ausgabe.")]
        return SystemActionResult(res.returncode == 0, lines)

    def install_windows_updates(self, dry_run: bool) -> SystemActionResult:
        if dry_run:
            return SystemActionResult(True, ["[DRY-RUN] würde verfügbare Windows-Updates herunterladen und installieren."])
        script = (
            "$s=New-Object -ComObject Microsoft.Update.Session;"
            "$searcher=$s.CreateUpdateSearcher();"
            "$r=$searcher.Search(\"IsInstalled=0 and Type='Software'\");"
            "if($r.Updates.Count -eq 0){Write-Output 'Keine Updates zu installieren.'; exit 0};"
            "$c=New-Object -ComObject Microsoft.Update.UpdateColl;"
            "for($i=0;$i -lt $r.Updates.Count;$i++){[void]$c.Add($r.Updates.Item($i))};"
            "$d=$s.CreateUpdateDownloader();$d.Updates=$c;$dr=$d.Download();"
            "Write-Output ('Download Ergebnis: ' + $dr.ResultCode);"
            "$i2=$s.CreateUpdateInstaller();$i2.Updates=$c;$ir=$i2.Install();"
            "Write-Output ('Install Ergebnis: ' + $ir.ResultCode);"
            "Write-Output ('Reboot erforderlich: ' + $ir.RebootRequired)"
        )
        res = self._run(["powershell", "-NoProfile", "-Command", script], timeout=1800)
        lines = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
        if res.stderr:
            lines.append((res.stderr or "").strip()[:400])
        return SystemActionResult(res.returncode == 0, lines or ["Keine Ausgabe."])

    def profile_migration_precheck(self, source_user: str, target_profile_name: str) -> SystemActionResult:
        current_user = getpass.getuser()
        source = (source_user or "").strip() or current_user
        target = (target_profile_name or "").strip()
        lines: list[str] = ["Profil-Migrations-Precheck (safe, read-only):"]
        if not self._user_exists(source):
            return SystemActionResult(False, [*lines, f"Benutzer nicht gefunden: {source}"])
        if not target:
            return SystemActionResult(False, [*lines, "Gewünschter Profilname fehlt."])
        if target.lower() == source.lower():
            return SystemActionResult(False, [*lines, "Gewünschter Profilname entspricht Quellbenutzer."])

        current_profile = self._users_root() / source
        if not current_profile.exists():
            return SystemActionResult(False, [*lines, f"Profilpfad nicht gefunden: {current_profile}"])
        parent = current_profile.parent
        target_profile = parent / target
        lines.append(f"Angemeldeter Benutzer: {current_user}")
        lines.append(f"Gewünschter Benutzer: {source}")
        lines.append(f"Quell-Profilpfad: {current_profile}")
        lines.append(f"Gewünschter Profilpfad: {target_profile}")

        if target_profile.exists():
            lines.append("BLOCKER: Gewünschter Profilpfad existiert bereits.")
            return SystemActionResult(False, lines)

        current_is_admin = self._is_user_admin(current_user)
        source_is_admin = self._is_user_admin(source)
        if not current_is_admin:
            lines.append("BLOCKER: Angemeldeter Benutzer hat keine Adminrechte.")
        if source.lower() != current_user.lower():
            if current_is_admin:
                lines.append("OK: Migration eines anderen Benutzers durch Admin ist zulässig (kein zweites Admin-Konto erforderlich).")
        else:
            has_admin, admins = self._has_second_admin(current_user)
            if has_admin:
                lines.append(f"OK: Zweites Admin-Konto vorhanden ({', '.join(admins[:5])}).")
            else:
                lines.append("BLOCKER: Kein zweites Admin-Konto gefunden (nötig für Self-Migration).")
        if source_is_admin:
            lines.append("WARNUNG: Gewünschter Benutzer ist ebenfalls Admin. Änderung ist erlaubt, aber bitte besonders vorsichtig vorgehen.")

        path_hits = self._scan_profile_path_mentions(str(current_profile))
        lines.extend(path_hits.lines)

        blocked = any(line.startswith("BLOCKER") for line in lines)
        if blocked:
            lines.append("Ergebnis: Migration aktuell blockiert.")
        else:
            lines.append("Ergebnis: Voraussetzungen für kontrollierte Migration erfüllt.")
            lines.append("Hinweis: Die eigentliche Profilordner-Umbenennung erfolgt nicht im aktiven Konto.")
        return SystemActionResult(not blocked, lines)

    def execute_profile_migration(self, source_user: str, target_profile_name: str, dry_run: bool) -> SystemActionResult:
        pre = self.profile_migration_precheck(source_user, target_profile_name)
        lines = list(pre.lines)
        if not pre.ok:
            lines.append("Abbruch: Migration wird aus Sicherheitsgruenden nicht gestartet.")
            return SystemActionResult(False, lines)
        source = (source_user or "").strip() or getpass.getuser()
        target = (target_profile_name or "").strip()
        current = getpass.getuser().strip()
        source_profile = self._users_root() / source
        target_profile = self._users_root() / target
        if dry_run:
            lines.append("[DRY-RUN] würde Migrations-Skript für externes Admin-Konto vorbereiten.")
            lines.append(
                "[DRY-RUN] Schritte: Abmelden Zielkonto -> Profilordner umbenennen -> ProfileList anpassen -> Re-Login testen."
            )
            return SystemActionResult(True, lines)
        if source.lower() == current.lower():
            lines.append("BLOCKER: Self-Migration bleibt gesperrt. Bitte anderes Admin-Konto nutzen.")
            return SystemActionResult(False, lines)
        if self._is_user_logged_in(source):
            lines.append("BLOCKER: Zielbenutzer ist aktuell angemeldet. Bitte zuerst abmelden.")
            return SystemActionResult(False, lines)
        if self._is_user_admin(source):
            lines.append("WARNUNG: Gewünschter Benutzer ist Admin. Migration wird trotzdem ausgeführt (auf eigene Verantwortung).")
        if winreg is None:
            lines.append("BLOCKER: winreg nicht verfügbar.")
            return SystemActionResult(False, lines)

        sid, original_profile = self._find_profile_sid_for_path(source_profile)
        if not sid:
            lines.append(f"BLOCKER: Kein ProfileList-Eintrag gefunden für {source_profile}")
            return SystemActionResult(False, lines)
        if target_profile.exists():
            if source_profile.exists():
                lines.append(
                    f"BLOCKER: Zielpfad existiert bereits: {target_profile}. "
                    "Das sieht nach einem abgebrochenen Versuch aus. Zielordner zuerst bereinigen/umbenennen."
                )
            else:
                lines.append(
                    f"BLOCKER: Quellpfad fehlt, Zielpfad existiert bereits: {target_profile}. "
                    "Bitte Zustand manuell prüfen (ggf. Registry/ProfileList kontrollieren)."
                )
            return SystemActionResult(False, lines)

        lines.append(f"Produktive Migration gestartet: {source_profile} -> {target_profile}")
        try:
            # Strict directory rename (same volume): no recursive copy fallback, no partial move.
            source_profile.rename(target_profile)
            lines.append("OK: Profilordner umbenannt.")
        except Exception as exc:  # pylint: disable=broad-except
            lines.append(
                "FEHLER: Profilordner konnte nicht atomar umbenannt werden. "
                "Bitte sicherstellen, dass der Zielbenutzer vollständig abgemeldet ist und kein Handle mehr offen ist."
            )
            lines.append(f"Details: {exc}")
            return SystemActionResult(False, lines)

        try:
            self._update_profilelist_path(sid, str(target_profile))
            lines.append(f"OK: Registry ProfileImagePath aktualisiert ({sid}).")
        except Exception as exc:  # pylint: disable=broad-except
            # Rollback folder rename on registry failure
            try:
                if target_profile.exists() and not source_profile.exists():
                    target_profile.rename(source_profile)
                    lines.append("Rollback: Profilordner auf Originalname zurückgesetzt.")
            except Exception as rb_exc:  # pylint: disable=broad-except
                lines.append(f"Rollback-FEHLER: {rb_exc}")
            lines.append(f"FEHLER: Registry konnte nicht aktualisiert werden: {exc}")
            return SystemActionResult(False, lines)

        lines.append("Migration abgeschlossen. Bitte Zielbenutzer neu anmelden und Funktionen prüfen.")
        lines.append(f"Vorheriger ProfileList-Pfad: {original_profile}")
        lines.append(f"Neuer ProfileList-Pfad: {target_profile}")
        return SystemActionResult(True, lines)

    def _has_second_admin(self, current_user: str) -> tuple[bool, list[str]]:
        names = self._get_admin_group_members()
        candidates = [n for n in names if self._extract_account_name(n) != current_user.lower()]
        return (len(candidates) > 0, candidates)

    def _is_user_admin(self, username: str) -> bool:
        admins = self._get_admin_group_members()
        normalized = username.lower().strip()
        return any(self._extract_account_name(item) == normalized for item in admins)

    def _get_admin_group_members(self) -> list[str]:
        # Use well-known SID S-1-5-32-544 to avoid localization issues (Administrators/Administratoren).
        script = (
            "$g=Get-LocalGroup -SID 'S-1-5-32-544';"
            "Get-LocalGroupMember -SID $g.SID | Select-Object -ExpandProperty Name"
        )
        res = self._run(["powershell", "-NoProfile", "-Command", script], timeout=60)
        return [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]

    @staticmethod
    def _extract_account_name(identity: str) -> str:
        text = (identity or "").strip().lower()
        if "\\" in text:
            return text.split("\\")[-1]
        return text

    def _user_exists(self, username: str) -> bool:
        result = self._run(["net", "user", username], timeout=60)
        return result.returncode == 0

    def _is_user_logged_in(self, username: str) -> bool:
        result = self._run(["quser"], timeout=30)
        text = ((result.stdout or "") + "\n" + (result.stderr or "")).lower()
        return username.lower() in text

    def _find_profile_sid_for_path(self, profile_path: Path) -> tuple[str | None, str]:
        if winreg is None:
            return None, ""
        base = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
        wanted = str(profile_path).lower().rstrip("\\")
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
                count = winreg.QueryInfoKey(root)[0]
                for idx in range(count):
                    sid = winreg.EnumKey(root, idx)
                    key_path = f"{base}\\{sid}"
                    try:
                        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                            value, _ = winreg.QueryValueEx(key, "ProfileImagePath")
                            raw = str(value or "").strip()
                            if raw.lower().rstrip("\\") == wanted:
                                return sid, raw
                    except OSError:
                        continue
        except OSError:
            return None, ""
        return None, ""

    def _update_profilelist_path(self, sid: str, new_path: str) -> None:
        if winreg is None:
            raise RuntimeError("winreg unavailable")
        base = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
        key_path = f"{base}\\{sid}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ProfileImagePath", 0, winreg.REG_EXPAND_SZ, new_path)

    def get_computer_name(self) -> str:
        return (os.environ.get("COMPUTERNAME") or platform.node() or "").strip()

    @staticmethod
    def validate_computer_name(name: str) -> tuple[bool, str]:
        """NetBIOS-style rules (15 chars) — matches typical Windows Rename-Computer constraints."""
        n = (name or "").strip()
        if not n:
            return False, "Name fehlt."
        if len(n) > 15:
            return False, "Maximal 15 Zeichen (NetBIOS)."
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?", n):
            return False, "Erlaubt: Buchstaben, Ziffern, Bindestrich; nicht mit Bindestrich beginnen oder enden."
        if re.fullmatch(r"\d+", n):
            return False, "Name darf nicht nur aus Ziffern bestehen."
        return True, ""

    def rename_computer(self, new_name: str, dry_run: bool, restart_after: bool) -> SystemActionResult:
        cur = self.get_computer_name()
        new = (new_name or "").strip()
        lines = [f"Aktueller Computername: {cur}", f"Gewünschter neuer Name: {new}"]
        okv, err = self.validate_computer_name(new)
        if not okv:
            return SystemActionResult(False, [*lines, f"FEHLER: {err}"])
        if new.upper() == cur.upper():
            return SystemActionResult(False, [*lines, "FEHLER: Name entspricht bereits dem aktuellen Computernamen."])

        new_ps = new.replace("'", "''")
        if dry_run:
            ps = f"Rename-Computer -NewName '{new_ps}' -WhatIf"
        else:
            tail = "-Force -Restart" if restart_after else "-Force"
            ps = f"Rename-Computer -NewName '{new_ps}' {tail}"
        res = self._run(["powershell", "-NoProfile", "-Command", ps], timeout=180 if restart_after else 120)
        out = (res.stdout or "").strip()
        err_txt = (res.stderr or "").strip()
        if out:
            lines.extend(out.splitlines())
        if err_txt:
            lines.append(err_txt[:400])
        if res.returncode != 0:
            lines.append("FEHLER: Umbenennung fehlgeschlagen (Administratorrechte erforderlich?).")
            return SystemActionResult(False, lines)
        if dry_run:
            lines.append("[DRY-RUN] Simulation abgeschlossen.")
            return SystemActionResult(True, lines)
        if restart_after:
            lines.append("Neustart wurde angefordert — die Verbindung kann abbrechen.")
        else:
            lines.append("Hinweis: Vollständige Aktivierung des neuen Namens oft erst nach einem Neustart.")
        return SystemActionResult(True, lines)

    def list_local_users(self) -> list[str]:
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-LocalUser | Select-Object -ExpandProperty Name",
        ]
        res = self._run(cmd, timeout=60)
        names = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
        # Blend out obvious service/system accounts for operator safety.
        blocked_prefixes = ("defaultaccount", "wdagutilityaccount", "defaultuser")
        filtered = [n for n in names if not n.lower().startswith(blocked_prefixes)]
        filtered.sort(key=lambda x: x.lower())
        return filtered

    def get_user_details(self, username: str) -> dict[str, str]:
        user = (username or "").strip()
        if not user:
            return {"exists": "Nein", "admin": "Nein", "active": "Unbekannt", "profile_path": "—", "profile_exists": "Nein"}
        exists = self._user_exists(user)
        profile_path = str(self._users_root() / user)
        profile_exists = Path(profile_path).exists()
        active = "Unbekannt"
        if exists:
            result = self._run(["net", "user", user], timeout=60)
            text = ((result.stdout or "") + "\n" + (result.stderr or "")).lower()
            if "account active" in text:
                active = "Ja" if "account active               yes" in text else "Nein"
            elif "konto aktiv" in text:
                active = "Ja" if "konto aktiv                 ja" in text else "Nein"
        return {
            "exists": "Ja" if exists else "Nein",
            "admin": "Ja" if self._is_user_admin(user) else "Nein",
            "active": active,
            "profile_path": profile_path,
            "profile_exists": "Ja" if profile_exists else "Nein",
        }

    def _scan_profile_path_mentions(self, profile_path: str) -> SystemActionResult:
        needle = profile_path.lower()
        lines = ["Pfadreferenz-Check auf aktuelles Profil:"]
        hits = 0
        reg_cmds = [
            r"Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' | Out-String",
            r"Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run' | Out-String",
        ]
        for command in reg_cmds:
            res = self._run(["powershell", "-NoProfile", "-Command", command], timeout=60)
            text = ((res.stdout or "") + "\n" + (res.stderr or "")).lower()
            if needle in text:
                hits += 1
        startup_dirs = [
            Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup",
            Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs\Startup",
        ]
        for root in startup_dirs:
            if not root.exists():
                continue
            for item in root.rglob("*"):
                if needle in str(item).lower():
                    hits += 1
                    break
        if hits:
            lines.append(f"Hinweis: {hits} potentielle Altpfad-Referenz(en) gefunden.")
        else:
            lines.append("OK: Keine offensichtlichen Altpfad-Referenzen gefunden.")
        return SystemActionResult(True, lines)
