from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path

from .config import SOFTWARE_BY_KEY


class LocalSourceService:
    def __init__(self, logger, software_providers: dict[str, object] | None = None) -> None:
        self.logger = logger
        self.software_providers = software_providers or {}
        self.root_path: Path | None = None
        self._files: list[Path] = []

    def validate_path(self, path: str) -> bool:
        raw = (path or "").strip()
        if not raw:
            return False
        try:
            return Path(raw).exists()
        except OSError:
            return False

    def scan_source(self, root_path: str) -> int:
        raw = (root_path or "").strip()
        if not raw:
            self.root_path = None
            self._files = []
            return 0
        root = Path(raw)
        if not root.exists() or not root.is_dir():
            self.root_path = root
            self._files = []
            return 0
        self.root_path = root
        found: list[Path] = []
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".exe", ".msi", ".ps1", ".bat"}:
                found.append(path)
        self._files = found
        self.logger.info("Lokale Quelle gescannt: %s (%s Dateien)", root, len(found))
        return len(found)

    def detect_installer_type(self, path: str | Path) -> str:
        ext = Path(path).suffix.lower()
        if ext in {".exe", ".msi", ".ps1", ".bat"}:
            return ext[1:]
        return ""

    def find_installer(self, software_key: str) -> Path | None:
        if not self._files:
            return None
        software = SOFTWARE_BY_KEY.get(software_key)
        if software is None:
            return None
        cfg_raw = self.software_providers.get(software_key, {})
        cfg = cfg_raw if isinstance(cfg_raw, dict) else {}

        patterns: list[str] = []
        local_patterns = cfg.get("local_patterns")
        if isinstance(local_patterns, list):
            patterns.extend(str(item).strip() for item in local_patterns if str(item).strip())
        patterns.extend(
            [
                f"*{software.display_name}*.exe",
                f"*{software.display_name}*.msi",
                f"*{software.key.replace('_', '')}*.exe",
                f"*{software.key.replace('_', '')}*.msi",
            ]
        )
        search_terms = cfg.get("search_terms", software.search_terms)
        if isinstance(search_terms, list):
            patterns.extend(f"*{str(term).strip()}*.exe" for term in search_terms if str(term).strip())
            patterns.extend(f"*{str(term).strip()}*.msi" for term in search_terms if str(term).strip())
        else:
            patterns.extend(f"*{term}*.exe" for term in software.search_terms)
            patterns.extend(f"*{term}*.msi" for term in software.search_terms)

        normalized_patterns = [p.lower() for p in patterns if p]
        normalized_key = software.key.lower().replace("_", "")
        normalized_name = software.display_name.lower()
        for file_path in self._files:
            filename = file_path.name.lower()
            rel = str(file_path).lower()
            if any(fnmatch.fnmatch(filename, p) for p in normalized_patterns):
                return file_path
            if any(fnmatch.fnmatch(rel, p) for p in normalized_patterns):
                return file_path
            if normalized_key in filename or normalized_name in filename:
                return file_path
        return None

    @staticmethod
    def sha256(path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
