from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SoftwareState:
    status: str
    package_name: str | None = None
    detail: str = ""
    installed_version: str = ""
    available_version: str = ""
    provider: str = "Unbekannt"
    uninstall_hint: str = ""
    installer_path: str = ""
    installer_sha256: str = ""


@dataclass
class ReportEntry:
    software_key: str
    package_name: str
    status_before: str
    action: str
    status_after: str
    result: str
    error_message: str
    reboot_required: str = "no"
    provider: str = ""
    installer_path: str = ""
    installer_sha256: str = ""
    uninstall_method: str = ""
    uninstall_attempts_json: str = ""
    manual_reason: str = ""
    verification_status: str = ""
    verification_evidence: str = ""
    stale_evidence_ignored: str = ""
    cleanup_items_found: str = ""
    cleanup_items_removed: str = ""
    cleanup_classification: str = ""
    extended_metadata: str = ""
