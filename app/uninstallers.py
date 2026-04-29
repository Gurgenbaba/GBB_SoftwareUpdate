from __future__ import annotations

import json
from typing import Any, Callable

from .config import SOFTWARE_BY_KEY, SoftwarePackage
from .models import ReportEntry, SoftwareState
from .uninstall_engine import MultiStageUninstallEngine, UninstallEngineResult


class UninstallerService:
    def __init__(
        self,
        choco_client,
        winget_client,
        logger,
        software_by_key: dict[str, SoftwarePackage] | None = None,
        scanner=None,
        software_providers: dict[str, Any] | None = None,
    ) -> None:
        self.choco = choco_client
        self.winget = winget_client
        self.logger = logger
        self.software_by_key = software_by_key or SOFTWARE_BY_KEY
        self.scanner = scanner
        self.software_providers = software_providers or {}

    @staticmethod
    def _attempts_json(eng: UninstallEngineResult) -> str:
        payload = [
            {
                "method": a.method,
                "outcome": a.outcome,
                "return_code": a.return_code,
                "detail": (a.detail or "")[:240],
            }
            for a in eng.attempts
        ]
        try:
            return json.dumps(payload, ensure_ascii=False)[:4000]
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _map_result_string(eng: UninstallEngineResult, dry_run: bool) -> str:
        if dry_run and eng.method_used == "dry_run":
            return "DRY-RUN"
        if eng.status in ("success", "skipped", "already_absent"):
            return "OK"
        if eng.status == "manual_required":
            return "Manuell"
        return "Fehler"

    def process(
        self,
        selected_keys: list[str],
        current_states: dict[str, SoftwareState],
        status_callback: Callable[[str, SoftwareState], None],
        progress_callback: Callable[[int, int], None],
        item_start_callback: Callable[[str], None] | None = None,
        dry_run: bool = False,
        scanner=None,
        software_providers: dict[str, Any] | None = None,
        method_progress_callback: Callable[[str, str], None] | None = None,
    ) -> list[ReportEntry]:
        scan = scanner if scanner is not None else self.scanner
        prov = software_providers if software_providers is not None else self.software_providers
        engine = MultiStageUninstallEngine(
            self.choco,
            self.winget,
            self.logger,
            self.software_by_key,
            scanner=scan,
            software_providers=prov,
        )
        total = len(selected_keys)
        rows: list[ReportEntry] = []
        for index, key in enumerate(selected_keys, start=1):
            progress_callback(index - 1, total)
            if item_start_callback is not None:
                item_start_callback(key)
            software = self.software_by_key[key]
            prev = current_states.get(key, SoftwareState("Nicht geprueft"))
            eng = engine.execute(software, prev, dry_run, method_progress_callback=method_progress_callback)
            new_state = eng.final_state or SoftwareState("Fehler", detail=eng.error_summary or eng.manual_reason)
            current_states[key] = new_state
            status_callback(key, new_state)
            progress_callback(index, total)
            result = self._map_result_string(eng, dry_run)
            rows.append(
                ReportEntry(
                    software_key=key,
                    package_name=new_state.package_name or "",
                    status_before=prev.status,
                    action=eng.ui_action,
                    status_after=new_state.status,
                    result=result,
                    error_message=new_state.detail,
                    reboot_required="yes" if eng.reboot_required else "no",
                    provider=new_state.provider or "",
                    uninstall_method=eng.method_used,
                    uninstall_attempts_json=self._attempts_json(eng),
                    manual_reason=eng.manual_reason or "",
                    verification_status=getattr(eng, "verification_status", "") or "",
                    verification_evidence=getattr(eng, "verification_evidence", "") or "",
                    stale_evidence_ignored=getattr(eng, "stale_evidence_ignored", "") or "",
                    cleanup_items_found="",
                    cleanup_items_removed="",
                    cleanup_classification="",
                )
            )
        return rows
