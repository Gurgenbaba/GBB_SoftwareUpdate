from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .config import LOG_DIR
from .json_config import CONFIG_JSON_PATH, get_config_dict


def _path_exists_bounded(path: Path, timeout_sec: float = 3.0) -> bool | None:
    """Prüft Existenz ohne endlos zu hängen (z. B. UNC/offline Netzlaufwerk). None = Timeout."""
    result: list[bool | None] = [None]

    def probe() -> None:
        try:
            result[0] = path.exists()
        except OSError:
            result[0] = False

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        return None
    return result[0]


@dataclass
class HealthResult:
    """Self-health outcome. `passed` is false only when `warnings` is non-empty."""

    passed: bool
    warnings: list[str]
    config_hints: list[str]
    details: list[str]
    errors: list[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    @property
    def config_hint_count(self) -> int:
        return len(self.config_hints)


def run_self_health_check(choco_client, winget_client, runtime_settings, logger) -> HealthResult:
    warnings: list[str] = []
    config_hints: list[str] = []
    details: list[str] = []
    errors: list[str] = []

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        probe = LOG_DIR / ".write_test.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        details.append("Schreibrechte auf logs/: OK")
    except OSError as exc:
        msg = f"Schreibrechte auf logs/ fehlen ({exc})"
        warnings.append(msg)

    try:
        cfg = get_config_dict(logger)
        if cfg:
            details.append(f"config.json lesbar: {CONFIG_JSON_PATH}")
        else:
            warnings.append("config.json ist leer oder ungueltig")
    except Exception as exc:  # pragma: no cover
        warnings.append(f"config.json Fehler: {exc}")

    if not choco_client.has_network():
        warnings.append("Keine Netzwerkverbindung erkannt")

    if choco_client.is_choco_installed():
        version = choco_client.version() or "Unbekannt"
        details.append(f"Chocolatey erreichbar (Version {version})")
    else:
        warnings.append("Chocolatey nicht installiert/erreichbar")

    if choco_client.is_choco_installed() and runtime_settings.chocolatey_source and runtime_settings.chocolatey_source.configured:
        url = runtime_settings.chocolatey_source.url
        if choco_client.source_list_contains_url(url):
            details.append(f"Chocolatey Source URL in choco source list: {url}")
        else:
            warnings.append(f"Chocolatey Source URL nicht in choco source list: {url}")

    if winget_client.is_available():
        wv = winget_client.version() or "Unbekannt"
        details.append(f"WinGet erreichbar (Version {wv})")
        if not winget_client.source_available():
            config_hints.append("WinGet Source nicht verifizierbar (Hinweis)")
    else:
        config_hints.append("WinGet nicht verfuegbar (optional, kein fataler Fehler)")

    for key, data in runtime_settings.internal_installers.items():
        path = str(data.get("path", "") or "").strip()
        if not path:
            config_hints.append(f"Interner Installer '{key}' ohne Pfad konfiguriert")
            continue
        parsed = urlparse(path)
        if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
            details.append(f"Interner Installer '{key}' als URL konfiguriert")
            continue
        exists = _path_exists_bounded(Path(path))
        if exists is True:
            details.append(f"Interner Installer '{key}' gefunden")
        elif exists is False:
            config_hints.append(f"Interner Installer '{key}' nicht gefunden: {path}")
        else:
            config_hints.append(f"Interner Installer '{key}': Pfadprüfung Timeout (Netzwerk/UNC?) — {path}")

    local_source_path = (runtime_settings.local_source_last_path or "").strip()
    if local_source_path:
        loc_exists = _path_exists_bounded(Path(local_source_path))
        if loc_exists is True:
            details.append(f"Lokale Quelle erreichbar: {local_source_path}")
        elif loc_exists is False:
            config_hints.append(f"Lokale Quelle nicht erreichbar: {local_source_path}")
        else:
            config_hints.append(f"Lokale Quelle: Pfadprüfung Timeout (Netzwerk/UNC?) — {local_source_path}")

    passed = len(warnings) == 0

    for detail in details:
        logger.info("[HEALTH] %s", detail)
    for w in warnings:
        logger.warning("[HEALTH] %s", w)
    for hint in config_hints:
        logger.info("[CONFIG] %s", hint)

    return HealthResult(
        passed=passed,
        warnings=warnings,
        config_hints=config_hints,
        details=details,
        errors=errors,
    )
