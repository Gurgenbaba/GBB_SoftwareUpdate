"""Microsoft 365 removal: safe process stop, Get Help CLI, ODT (generated remove.xml), legacy SaRA, metadata."""

from __future__ import annotations

import logging
import os
import platform
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

EXCERPT_LEN = 600

RunCmd = Callable[..., Any]


def _console_encoding() -> str:
    """OEM-Konsolen-Codepage statt ANSI/CP1252 (z. B. Deutsch meist CP850) — sonst stirbt der
    subprocess-Reader-Thread mit UnicodeDecodeError und die Ausgabe geht verloren."""
    if platform.system() != "Windows":
        return "utf-8"
    try:
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "cp850"

# Safe Office apps to stop before removal (no ClickToRunSvc unless explicitly requested in config).
_OFFICE_KILL_NAMES: tuple[str, ...] = (
    "WINWORD.EXE",
    "EXCEL.EXE",
    "POWERPNT.EXE",
    "OUTLOOK.EXE",
    "ONENOTE.EXE",
    "MSACCESS.EXE",
    "MSPUB.EXE",
    "VISIO.EXE",
    "WINPROJ.EXE",
    "Teams.exe",
    "ms-teams.exe",
)


def _valid_file(p: str) -> Path | None:
    path = Path(p.strip())
    if not p.strip() or not path.is_file():
        return None
    return path


def office_exit_ok(returncode: int | None) -> tuple[bool, bool]:
    """(treat_as_success, reboot_may_be_required)."""
    if returncode is None:
        return False, False
    rc = int(returncode)
    if rc in (0, 1707):
        return True, False
    if rc in (3010, 1641):
        return True, True
    return False, False


def find_get_help_cmd(logger: logging.Logger | None = None) -> Path | None:
    """Locate GetHelpCmd.exe under Program Files (no network)."""
    roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
    ]
    static = (
        "Microsoft/GetHelp/GetHelpCmd.exe",
        "Windows/GetHelp/GetHelpCmd.exe",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for rel in static:
            cand = root / rel.replace("/", os.sep)
            if cand.is_file():
                if logger:
                    logger.info("[OFFICE] Get Help CLI located: %s", cand)
                return cand
    return None


def resolve_get_help_exe(office_tools: dict[str, Any], logger: logging.Logger) -> Path | None:
    p = str(office_tools.get("get_help_cmd_path", "") or "").strip()
    if p:
        exe = _valid_file(p)
        if exe:
            return exe
        logger.warning("[OFFICE] get_help_cmd_path set but not a file: %s", p[:200])
        return None
    return find_get_help_cmd(logger)


def stop_safe_office_processes(
    *,
    logger: logging.Logger,
    dry_run: bool,
    kill_click_to_run_service: bool,
    run_cmd: RunCmd | None = None,
) -> int:
    """
    Terminate known Office UI processes. Returns count of ``taskkill`` invocations that exited 0.
    Does not stop ClickToRunSvc unless ``kill_click_to_run_service`` is True.
    """
    run = run_cmd or subprocess.run
    if dry_run:
        logger.info("[OFFICE] Dry-Run: would stop safe Office processes (%d names)", len(_OFFICE_KILL_NAMES))
        return 0
    stopped = 0
    for name in _OFFICE_KILL_NAMES:
        try:
            cp = run(
                ["taskkill", "/IM", name, "/F"],
                capture_output=True,
                text=True,
                encoding=_console_encoding(),
                errors="replace",
                timeout=60,
                check=False,
                shell=False,
            )
            if cp.returncode == 0:
                stopped += 1
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("[OFFICE] taskkill %s: %s", name, exc)
    if kill_click_to_run_service:
        try:
            cp = run(
                ["sc", "stop", "ClickToRunSvc"],
                capture_output=True,
                text=True,
                encoding=_console_encoding(),
                errors="replace",
                timeout=120,
                check=False,
                shell=False,
            )
            if cp.returncode == 0:
                stopped += 1
                logger.info("[OFFICE] Stopped ClickToRunSvc (configured)")
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("[OFFICE] ClickToRunSvc stop failed: %s", exc)
    if stopped:
        logger.info("[OFFICE] Stopped %d Office process(es)", stopped)
    return stopped


def build_odt_remove_xml(*, display_level: Literal["Full", "None"]) -> str:
    return (
        "<Configuration>\n"
        '  <Remove All="TRUE" />\n'
        f'  <Display Level="{display_level}" AcceptEULA="TRUE" />\n'
        '  <Property Name="FORCEAPPSHUTDOWN" Value="TRUE" />\n'
        "</Configuration>\n"
    )


def odt_display_level_from_install_mode(mode: str) -> Literal["Full", "None"]:
    m = (mode or "auto").strip().lower()
    if m == "silent":
        return "None"
    return "Full"


def try_get_help_office_scrub(
    *,
    get_help_exe: Path,
    get_help_args: Sequence[str],
    logger: logging.Logger,
    dry_run: bool,
    run_cmd: RunCmd | None = None,
) -> tuple[bool, str, int | None, str]:
    """Run GetHelpCmd.exe with configured args (e.g. OfficeScrubScenario). Returns (ok, detail, rc, config_path_used)."""
    run = run_cmd or subprocess.run
    args = [str(a).strip() for a in get_help_args if str(a).strip()]
    if not args:
        args = ["OfficeScrubScenario"]
    cmd = [str(get_help_exe), *args]
    cfg_note = str(get_help_exe)
    if dry_run:
        logger.info("[OFFICE-GetHelp] Dry-Run: would run %s", " ".join(cmd))
        return True, f"DRY-RUN: would run Get Help: {' '.join(cmd)}", None, cfg_note
    try:
        cp = run(
            cmd,
            cwd=str(get_help_exe.parent),
            capture_output=True,
            text=True,
            encoding=_console_encoding(),
            errors="replace",
            timeout=7200,
            check=False,
            shell=False,
        )
        rc = int(cp.returncode)
        tail = ((cp.stdout or "") + (cp.stderr or ""))[-600:]
        ok, _reb = office_exit_ok(rc)
        return ok, tail.strip() or f"Get Help finished RC={rc}", rc, cfg_note
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("[OFFICE-GetHelp] execution failed: %s", exc)
        return False, str(exc), None, cfg_note


def try_odt_remove(
    *,
    odt_setup_path: str,
    odt_remove_config_path: str,
    logger: logging.Logger,
    dry_run: bool,
    display_level: Literal["Full", "None"] = "Full",
    run_cmd: RunCmd | None = None,
) -> tuple[bool, str, int | None, str]:
    """
    Run ``setup.exe /configure <xml>``. If ``odt_remove_config_path`` is empty but setup exists,
    writes a temporary remove-office.xml. Returns (ok, detail, rc, config_path_used).
    """
    run = run_cmd or subprocess.run
    setup = _valid_file(odt_setup_path)
    if not setup:
        return False, "ODT setup.exe path missing or not a file", None, ""
    temp_xml: Path | None = None
    xml_path: Path | None = None
    cfg_used = ""
    try:
        if str(odt_remove_config_path or "").strip():
            xml_path = _valid_file(odt_remove_config_path)
            if not xml_path:
                return False, "ODT remove configuration.xml path invalid or not a file", None, str(odt_remove_config_path)[:240]
            cfg_used = str(xml_path)
        else:
            body = build_odt_remove_xml(display_level=display_level)
            fd, tmp = tempfile.mkstemp(prefix="gbb_office_remove_", suffix=".xml", text=True)
            os.close(fd)
            temp_xml = Path(tmp)
            temp_xml.write_text(body, encoding="utf-8")
            xml_path = temp_xml
            cfg_used = str(temp_xml)
        cwd = str(setup.parent)
        cmd = [str(setup), "/configure", str(xml_path)]
        if dry_run:
            logger.info("[OFFICE-ODT] Dry-Run: would run %s (cwd=%s)", " ".join(cmd), cwd)
            return True, f"DRY-RUN: would run ODT: {' '.join(cmd)}", None, cfg_used
        cp = run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding=_console_encoding(),
            errors="replace",
            timeout=3600,
            check=False,
            shell=False,
        )
        rc = int(cp.returncode)
        tail = ((cp.stdout or "") + (cp.stderr or ""))[-600:]
        ok, _reb = office_exit_ok(rc)
        return ok, tail.strip() or f"ODT finished RC={rc}", rc, cfg_used
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("[OFFICE-ODT] execution failed: %s", exc)
        return False, str(exc), None, cfg_used or str(odt_remove_config_path)[:120]
    finally:
        if temp_xml is not None:
            try:
                if temp_xml.is_file():
                    temp_xml.unlink(missing_ok=True)  # type: ignore[arg-type]
            except OSError:
                pass


def _normalize_sara_args(raw: Any) -> str:
    if isinstance(raw, list):
        return " ".join(str(x).strip() for x in raw if str(x).strip())
    return str(raw or "").strip()


def try_sara_remove(
    *,
    sara_path: str,
    sara_args: str | list[str] | Any,
    logger: logging.Logger,
    dry_run: bool,
    run_cmd: RunCmd | None = None,
) -> tuple[bool, str, int | None, str]:
    """Run SaRA only when path is configured. Returns (ok, detail, rc, path_used)."""
    run = run_cmd or subprocess.run
    exe = _valid_file(sara_path)
    if not exe:
        return False, "SaRA executable path missing or not a file", None, ""
    sa = _normalize_sara_args(sara_args)
    parts: list[str] = [str(exe)]
    if sa:
        try:
            parts.extend(shlex.split(sa, posix=os.name != "nt"))
        except ValueError:
            parts.append(sa)
    cfg_used = str(exe)
    if dry_run:
        logger.info("[OFFICE-SaRA] Dry-Run: would run %s", " ".join(parts))
        return True, f"DRY-RUN: would run SaRA: {' '.join(parts)}", None, cfg_used
    try:
        cp = run(
            parts,
            cwd=str(exe.parent),
            capture_output=True,
            text=True,
            encoding=_console_encoding(),
            errors="replace",
            timeout=3600,
            check=False,
            shell=False,
        )
        rc = int(cp.returncode)
        tail = ((cp.stdout or "") + (cp.stderr or ""))[-600:]
        ok, _reb = office_exit_ok(rc)
        return ok, tail.strip() or f"SaRA finished RC={rc}", rc, cfg_used
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("[OFFICE-SaRA] execution failed: %s", exc)
        return False, str(exc), None, cfg_used


@dataclass
class OfficeRemovalChainOutcome:
    """Aggregated office-tool chain result for reporting."""

    any_tool_exit_ok: bool
    verified_absent_after_tool: bool
    summary_notes: str
    strategy_used: str
    exit_code: str
    config_path: str
    verification: str


def _verify_office_absent(scanner: Any, software: Any) -> tuple[bool, str]:
    if scanner is None:
        return False, "no_scanner"
    try:
        v = scanner.post_uninstall_verify(software)
        absent = not v.still_present
        return absent, (v.verification_status or "")[:120]
    except Exception as exc:  # pylint: disable=broad-except
        return False, f"verify_error:{exc}"[:120]


def run_office_removal_strategies(
    *,
    office_tools: dict[str, Any],
    logger: logging.Logger,
    dry_run: bool,
    add_att: Callable[..., None],
    scanner: Any | None,
    software: Any,
    install_mode: str,
    run_cmd: RunCmd | None = None,
) -> OfficeRemovalChainOutcome:
    """
    Run tools in ``prefer`` order: get_help, odt, sara (only if sara_path set), skip generic.
    After a tool exits successfully, runs strict verification; if Office absent, stops trying further tools.
    """
    prefer = office_tools.get("prefer") if isinstance(office_tools.get("prefer"), list) else []
    prefer_l = [str(x).strip().lower() for x in prefer if str(x).strip()]
    if not prefer_l:
        prefer_l = ["get_help", "odt", "sara", "generic"]
    kill_svc = bool(office_tools.get("kill_click_to_run_service", False))
    gh_args_raw = office_tools.get("get_help_args", ["OfficeScrubScenario"])
    if isinstance(gh_args_raw, list):
        get_help_args = [str(x) for x in gh_args_raw if str(x).strip()]
    else:
        get_help_args = [str(gh_args_raw).strip()] if str(gh_args_raw or "").strip() else ["OfficeScrubScenario"]
    if not get_help_args:
        get_help_args = ["OfficeScrubScenario"]
    odt_setup = str(office_tools.get("odt_setup_path", "") or "").strip()
    odt_xml = str(office_tools.get("odt_remove_config_path", "") or "").strip()
    sara_path = str(office_tools.get("sara_path", "") or "").strip()
    sara_args = office_tools.get("sara_args", "")
    disp = odt_display_level_from_install_mode(install_mode)
    notes: list[str] = []
    strategies_tried: list[str] = []
    last_strategy = ""
    last_rc = ""
    last_cfg = ""
    last_verify = ""
    any_ok = False
    absent_after = False
    sara_warned = False

    for step in prefer_l:
        if step == "generic":
            continue
        if step == "get_help":
            exe = resolve_get_help_exe(office_tools, logger)
            if not exe:
                msg = "Get Help CLI not found (set office_tools.get_help_cmd_path or install Get Help)"
                logger.info("[OFFICE] %s", msg)
                add_att("Office Get Help", "skipped", det=msg[:EXCERPT_LEN])
                notes.append(msg)
                continue
            ok, det, rc, cfg = try_get_help_office_scrub(
                get_help_exe=exe,
                get_help_args=get_help_args,
                logger=logger,
                dry_run=dry_run,
                run_cmd=run_cmd,
            )
            last_strategy, last_rc, last_cfg = "get_help", ("" if rc is None else str(rc)), cfg
            add_att("Office Get Help", "success" if ok else "failed", None if rc is None else int(rc), det=det[:EXCERPT_LEN])
            strategies_tried.append("get_help")
            if ok:
                any_ok = True
                if scanner and not dry_run:
                    absent, vs = _verify_office_absent(scanner, software)
                    last_verify = vs
                    notes.append(f"Get Help post-verify absent={absent} ({vs})")
                    if absent:
                        absent_after = True
                        break
            else:
                notes.append(f"Get Help: {det[:160]}")
        elif step == "odt":
            if not odt_setup:
                msg = "ODT not configured (odt_setup_path)"
                logger.info("[OFFICE] %s", msg)
                add_att("Office ODT", "skipped", det=msg[:EXCERPT_LEN])
                notes.append(msg)
                continue
            ok, det, rc, cfg = try_odt_remove(
                odt_setup_path=odt_setup,
                odt_remove_config_path=odt_xml,
                logger=logger,
                dry_run=dry_run,
                display_level=disp,
                run_cmd=run_cmd,
            )
            last_strategy, last_rc, last_cfg = "odt", ("" if rc is None else str(rc)), cfg
            add_att("Office ODT", "success" if ok else "failed", None if rc is None else int(rc), det=det[:EXCERPT_LEN])
            strategies_tried.append("odt")
            if ok:
                any_ok = True
                if scanner and not dry_run:
                    absent, vs = _verify_office_absent(scanner, software)
                    last_verify = vs
                    notes.append(f"ODT post-verify absent={absent} ({vs})")
                    if absent:
                        absent_after = True
                        break
            else:
                notes.append(f"ODT: {det[:160]}")
        elif step == "sara":
            if not sara_path:
                msg = "SaRA not configured (sara_path) — skipped (deprecated CLI; opt-in only)"
                logger.info("[OFFICE] %s", msg)
                add_att("Office SaRA", "skipped", det=msg[:EXCERPT_LEN])
                notes.append(msg)
                continue
            if not sara_warned:
                logger.warning(
                    "[OFFICE] SaRA command-line is deprecated; using only because configured (office_tools.sara_path)."
                )
                sara_warned = True
            ok, det, rc, cfg = try_sara_remove(
                sara_path=sara_path,
                sara_args=sara_args,
                logger=logger,
                dry_run=dry_run,
                run_cmd=run_cmd,
            )
            last_strategy, last_rc, last_cfg = "sara", ("" if rc is None else str(rc)), cfg
            add_att("Office SaRA", "success" if ok else "failed", None if rc is None else int(rc), det=det[:EXCERPT_LEN])
            strategies_tried.append("sara")
            if ok:
                any_ok = True
                if scanner and not dry_run:
                    absent, vs = _verify_office_absent(scanner, software)
                    last_verify = vs
                    notes.append(f"SaRA post-verify absent={absent} ({vs})")
                    if absent:
                        absent_after = True
                        break
            else:
                notes.append(f"SaRA: {det[:160]}")
    strat = ",".join(strategies_tried) if strategies_tried else ""
    return OfficeRemovalChainOutcome(
        any_tool_exit_ok=any_ok,
        verified_absent_after_tool=absent_after,
        summary_notes=" | ".join(notes)[:500],
        strategy_used=last_strategy or strat,
        exit_code=last_rc,
        config_path=last_cfg[:400],
        verification=last_verify,
    )


def office_removal_tools_available(office_tools: dict[str, Any], logger: logging.Logger) -> bool:
    """True if Get Help can be resolved, or ODT setup exists, or SaRA path is set (runtime discovery for Get Help)."""
    if resolve_get_help_exe(office_tools, logger) is not None:
        return True
    odt_setup = str(office_tools.get("odt_setup_path", "") or "").strip()
    if _valid_file(odt_setup):
        return True
    sara_path = str(office_tools.get("sara_path", "") or "").strip()
    return bool(_valid_file(sara_path))


def office_tools_configured(office_tools: dict[str, Any], *, logger: logging.Logger | None = None) -> bool:
    """True if Get Help path set, ODT setup (+xml or generatable), or explicit SaRA path (not auto-discovery)."""
    _ = logger
    gh = str(office_tools.get("get_help_cmd_path", "") or "").strip()
    if gh and _valid_file(gh):
        return True
    odt_setup = str(office_tools.get("odt_setup_path", "") or "").strip()
    odt_xml = str(office_tools.get("odt_remove_config_path", "") or "").strip()
    if _valid_file(odt_setup):
        if _valid_file(odt_xml):
            return True
        return True
    sara_path = str(office_tools.get("sara_path", "") or "").strip()
    return bool(_valid_file(sara_path))
