from __future__ import annotations

import ctypes
import os
import platform
import queue
import shutil
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tkinter import PhotoImage, filedialog, messagebox
from typing import Callable

import customtkinter as ctk
from PIL import Image, ImageTk

from .choco import ChocoClient
from .config import APP_DIR, APP_NAME, BUNDLE_DIR, EXE_PARENT, REPORT_DIR, SOFTWARE_BY_KEY, SOFTWARE_CATALOG
from .health import run_self_health_check
from .installers import InstallerService
from .local_source import LocalSourceService
from .uninstallers import UninstallerService
from .winget import WingetService
from .json_config import (
    CONFIG_JSON_PATH,
    default_software_providers,
    get_config_dict,
    load_runtime_settings,
    save_config_dict,
    update_provider_resolved_source,
)
from .logger import build_logger
from .models import ReportEntry, SoftwareState
from .reporting import ReportWriter, pdf_export_available, write_summary_pdf
from .result_normalization import dialog_detail_lines, format_install_summary_lines, format_scan_summary_lines
from .residue_cleanup import apply_selected_cleanup, run_residue_cleanup_phase
from .scanner import SoftwareScanner
from .system_tools import SystemActionResult, SystemToolsService


class UpdaterApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self._apply_display_scaling()
        self.ogx_colors = {
            "bg": "#0f1115",
            "panel": "#171b22",
            "panel_alt": "#1d232d",
            "border": "#2a3340",
            "text": "#e6edf7",
            "muted": "#a4b0c0",
            "accent": "#4c6ef5",
            "accent_hover": "#3f5bd6",
            "danger": "#cc6677",
        }
        self.configure(fg_color=self.ogx_colors["bg"])
        self._apply_window_size()

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.logger, self.log_file = build_logger(self._enqueue_log)
        self.runtime = load_runtime_settings(self.logger)
        self.logger.info("Laufzeitkonfiguration: %s", CONFIG_JSON_PATH)

        self.choco = ChocoClient(self.logger)
        self.winget = WingetService(self.logger)
        self.scanner = SoftwareScanner(self.choco, self.winget, self.logger, self.runtime.visible_catalog)
        self.local_source = LocalSourceService(self.logger, self.runtime.software_providers)
        if self.runtime.local_source_last_path:
            self.local_source.scan_source(self.runtime.local_source_last_path)
        runtime_by_key = {s.key: s for s in self.runtime.visible_catalog}
        self.installer = InstallerService(
            self.choco,
            self.winget,
            self.logger,
            runtime_by_key,
            provider_configs=self.runtime.software_providers,
            local_source_service=self.local_source,
            prefer_local_source=self.runtime.local_source_prefer_local,
            scanner=self.scanner,
            installer_settle_wait_seconds=self.runtime.installer_settle_wait_seconds,
            installer_verify_after_timeout=self.runtime.installer_verify_after_timeout,
            installer_verify_poll_interval_seconds=self.runtime.installer_verify_poll_interval_seconds,
            installer_verify_poll_max_seconds=self.runtime.installer_verify_poll_max_seconds,
        )
        self.uninstaller = UninstallerService(
            self.choco,
            self.winget,
            self.logger,
            runtime_by_key,
            scanner=self.scanner,
            software_providers=self.runtime.software_providers,
        )
        self.report_writer = ReportWriter(self.logger)
        self.system_tools = SystemToolsService(self.logger)

        self.checkbox_vars: dict[str, ctk.BooleanVar] = {}
        self.row_frames: dict[str, ctk.CTkFrame] = {}
        self.badge_labels: dict[str, ctk.CTkLabel] = {}
        self.ver_inst_labels: dict[str, ctk.CTkLabel] = {}
        self.ver_avail_labels: dict[str, ctk.CTkLabel] = {}
        self.provider_labels: dict[str, ctk.CTkLabel] = {}
        self.row_progress_bars: dict[str, ctk.CTkProgressBar] = {}
        self.current_states: dict[str, SoftwareState] = {}
        self._operation_lock = threading.Lock()
        self.row_visible: dict[str, bool] = {}
        self.last_report_file: Path | None = None
        self.history_scroll: ctk.CTkScrollableFrame | None = None
        self.logo_image: PhotoImage | None = None
        self.settings_btn: ctk.CTkButton | None = None
        self._progress_phase = ""
        self._last_status_message = "Bereit"
        self._download_progress_suffix = ""
        self._filezilla_buchhaltung: bool | None = None
        self._install_activity_line = ""
        self._active_item_display = ""
        self._last_prog_done = 0
        self._last_prog_total = 0
        self.filter_segment: ctk.CTkSegmentedButton | None = None
        self.search_var = ctk.StringVar(value="")
        self.health_scroll: ctk.CTkScrollableFrame | None = None
        self.health_details_label: ctk.CTkLabel | None = None
        self.health_warnings_label: ctk.CTkLabel | None = None
        self.health_config_label: ctk.CTkLabel | None = None
        self.health_summary_frame: ctk.CTkScrollableFrame | None = None
        self.health_toggle_btn: ctk.CTkButton | None = None
        self._health_details_expanded = False
        self.search_entry: ctk.CTkEntry | None = None
        self.software_header_frame: ctk.CTkFrame | None = None
        self.select_missing_btn: ctk.CTkButton | None = None
        self.select_updates_btn: ctk.CTkButton | None = None
        self.patch_run_btn: ctk.CTkButton | None = None
        self.system_tools_btn: ctk.CTkButton | None = None
        self.system_tools_output_box: ctk.CTkTextbox | None = None
        self.system_tools_tabview: ctk.CTkTabview | None = None
        self.system_tools_status_labels: dict[str, ctk.CTkLabel] = {}
        self._tooltip_toplevel: ctk.CTkToplevel | None = None
        self._tooltip_label: ctk.CTkLabel | None = None
        self._tooltip_after_id: str | None = None
        init_local = self.runtime.local_source_last_path or "keine"
        self.local_source_path_var = ctk.StringVar(value=f"Aktive lokale Quelle: {init_local}")
        self.prefer_local_var = ctk.BooleanVar(value=self.runtime.local_source_prefer_local)
        self.software_list_frame: ctk.CTkScrollableFrame | None = None
        self.history_frame: ctk.CTkFrame | None = None
        self.history_toggle_btn: ctk.CTkButton | None = None
        self.content_frame: ctk.CTkFrame | None = None
        self.filter_bar_frame: ctk.CTkFrame | None = None
        self.log_frame: ctk.CTkFrame | None = None
        self.top_frame: ctk.CTkFrame | None = None
        self.quick_panel: ctk.CTkFrame | None = None
        self.quick_panel_label: ctk.CTkLabel | None = None
        self.local_source_path_label: ctk.CTkLabel | None = None
        self.button_host: ctk.CTkFrame | None = None
        self.button_frame: ctk.CTkFrame | None = None
        self.install_choco_btn: ctk.CTkButton | None = None
        self.install_winget_btn: ctk.CTkButton | None = None
        self.energy_screensaver_btn: ctk.CTkButton | None = None
        self._toolbar_buttons: list[ctk.CTkButton] = []
        self._toolbar_cols: int | None = None
        self._filter_heading: ctk.CTkLabel | None = None
        self._search_heading: ctk.CTkLabel | None = None
        self._filter_bar_stacked: bool | None = None
        self._scale_baseline: float = 1.0
        self._active_wheel_handler: Callable[[int], None] | None = None
        self._compact_mode: bool | None = None
        self._single_column_mode: bool | None = None
        self._log_wide_split_active: bool | None = None
        self._history_expanded = False
        self._layout_warmup_tries = 0

        self._ensure_runtime_catalog()
        self._apply_title()
        self._build_layout()
        self._apply_ogx_style(self)
        self.bind("<Configure>", self._on_window_resize, add="+")
        self.bind_all("<MouseWheel>", self._on_global_mousewheel, add="+")
        self._poll_queues()
        self._run_startup_checks()
        self.after(300, self._refresh_report_history)
        self.after(120, self._initial_layout_refresh)

    def _initial_layout_refresh(self) -> None:
        """Nach dem ersten Map: echte winfo-Werte abwarten (sonst falsche Toolbar-/Spaltenlogik)."""
        self._layout_warmup_tries += 1
        tries = self._layout_warmup_tries
        try:
            w, h = self.winfo_width(), self.winfo_height()
        except Exception:  # pragma: no cover
            w, h = 0, 0
        if (w < 520 or h < 420) and tries < 30:
            self.after(100, self._initial_layout_refresh)
            return
        eff_w = w if w >= 520 else 1020
        eff_h = h if h >= 420 else 700
        self._apply_responsive_layout(eff_w, eff_h)

    def _apply_display_scaling(self) -> None:
        screen_w = max(self.winfo_screenwidth(), 1024)
        screen_h = max(self.winfo_screenheight(), 768)
        adaptive_scale = 1.0
        if screen_w <= 1366 or screen_h <= 768:
            adaptive_scale = 0.93
        elif screen_w >= 2560 or screen_h >= 1440:
            adaptive_scale = 1.06

        area_factor = ((screen_w * screen_h) / (1920 * 1080)) ** 0.16
        area_factor = max(0.92, min(1.12, area_factor))

        dpi_scale = 1.0
        try:
            dpi = int(ctypes.windll.user32.GetDpiForSystem())
            dpi_scale = max(0.9, min(1.35, dpi / 96.0))
        except Exception:
            dpi_scale = 1.0

        # Kompakt aber stabil: keine zusätzliche dynamische Fensterbreiten-Skalierung (die frisst Lesbarkeit).
        final_scale = max(0.88, min(1.1, adaptive_scale * area_factor * dpi_scale))
        self._scale_baseline = final_scale
        ctk.set_widget_scaling(final_scale)
        ctk.set_window_scaling(final_scale)

    def _apply_window_size(self) -> None:
        """Use a screen-aware default size so content is not cut off."""
        self.update_idletasks()
        screen_w = max(self.winfo_screenwidth(), 1024)
        screen_h = max(self.winfo_screenheight(), 768)
        aspect = screen_w / max(screen_h, 1)
        if aspect >= 2.0:
            desired_w, desired_h = 1680, 900
        elif aspect <= 1.45:
            desired_w, desired_h = 1240, 900
        else:
            desired_w, desired_h = 1360, 900
        margin_w, margin_h = 48, 80
        width = min(desired_w, max(1020, screen_w - margin_w))
        height = min(desired_h, max(680, screen_h - margin_h))

        x = max((screen_w - width) // 2, 0)
        y = max((screen_h - height) // 2, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")
        # Unterhalb dieser Größe wird die Liste scrollbar — kein „Mini-Fenster“-Start mehr.
        self.minsize(960, 640)

    def _apply_title(self) -> None:
        if self.runtime.company_name:
            self.title(f"{APP_NAME} - {self.runtime.company_name}")
        else:
            self.title(APP_NAME)

    def _ensure_runtime_catalog(self) -> None:
        if self.runtime.visible_catalog:
            return
        self.logger.warning("Keine Software aus Konfiguration geladen, fallback auf Defaults.")
        self.runtime = replace(
            self.runtime,
            visible_catalog=tuple(SOFTWARE_CATALOG),
            enabled_software_keys=None,
        )

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1, minsize=260)

        self.health_frame = ctk.CTkFrame(self, corner_radius=10)
        self.health_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 4))
        self.health_frame.grid_columnconfigure(0, weight=1)
        self.health_frame.grid_columnconfigure(1, weight=0)
        self.health_frame.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(self.health_frame, text="Health Summary", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(6, 2)
        )
        self.health_toggle_btn = ctk.CTkButton(
            self.health_frame,
            text="Details anzeigen",
            width=118,
            height=22,
            font=ctk.CTkFont(size=11),
            command=self._toggle_health_details,
        )
        self.health_toggle_btn.grid(row=0, column=1, sticky="e", padx=(4, 8), pady=(4, 2))
        self.health_main_label = ctk.CTkLabel(
            self.health_frame, text="Health Check: wird ausgeführt ...", anchor="w", font=ctk.CTkFont(size=11)
        )
        self.health_main_label.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 2))
        self.health_scroll = ctk.CTkScrollableFrame(self.health_frame, label_text="", height=68)
        self.health_scroll.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=6, pady=(0, 6))
        self.health_summary_frame = self.health_scroll
        self.health_scroll.grid_columnconfigure(0, weight=1)
        cap = ctk.CTkFont(size=11)
        ctk.CTkLabel(self.health_scroll, text="Checks / Details", font=ctk.CTkFont(size=11, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=4, pady=(0, 2)
        )
        self.health_details_label = ctk.CTkLabel(
            self.health_scroll,
            text="—",
            anchor="w",
            justify="left",
            wraplength=1220,
            font=cap,
            text_color=("gray20", "gray80"),
        )
        self.health_details_label.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 10))
        ctk.CTkLabel(self.health_scroll, text="Warnungen", font=ctk.CTkFont(weight="bold")).grid(row=2, column=0, sticky="w", padx=4, pady=(0, 2))
        self.health_warnings_label = ctk.CTkLabel(
            self.health_scroll,
            text="",
            anchor="w",
            justify="left",
            wraplength=1220,
            font=cap,
            text_color=("gray20", "gray80"),
        )
        self.health_warnings_label.grid(row=3, column=0, sticky="ew", padx=4, pady=(0, 10))
        ctk.CTkLabel(self.health_scroll, text="Konfiguration", font=ctk.CTkFont(weight="bold")).grid(row=4, column=0, sticky="w", padx=4, pady=(0, 2))
        self.health_config_label = ctk.CTkLabel(
            self.health_scroll,
            text="",
            anchor="w",
            justify="left",
            wraplength=1220,
            font=cap,
            text_color=("gray20", "gray80"),
        )
        self.health_config_label.grid(row=5, column=0, sticky="ew", padx=4, pady=(0, 4))

        top_frame = ctk.CTkFrame(self, corner_radius=12)
        self.top_frame = top_frame
        top_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 4))
        top_frame.grid_columnconfigure(1, weight=1)
        top_frame.grid_columnconfigure(2, weight=1)

        logo_path = EXE_PARENT / "assets" / "logo.png"
        if not logo_path.exists():
            logo_path = BUNDLE_DIR / "assets" / "logo.png"
        if logo_path.exists():
            try:
                pil_logo = Image.open(logo_path).convert("RGBA")
                max_px = 100
                pil_logo.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
                self.logo_image = ImageTk.PhotoImage(pil_logo)
                # Also set the window/app icon when logo is available.
                self.iconphoto(True, self.logo_image)
                ctk.CTkLabel(top_frame, text="", image=self.logo_image).grid(row=0, column=0, rowspan=5, padx=8, pady=8)
            except Exception as exc:  # pragma: no cover
                self.logger.warning("Logo konnte nicht geladen werden: %s", exc)
        else:
            self.logger.info("Kein assets/logo.png gefunden; UI-Logo/Icon wird uebersprungen.")

        ctk.CTkLabel(top_frame, text=APP_NAME, font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=1, sticky="w", padx=6, pady=(4, 0)
        )
        self.company_label = ctk.CTkLabel(top_frame, text=f"Company: {self.runtime.company_name or 'Nicht gesetzt'}")
        self.company_label.configure(font=ctk.CTkFont(size=10))
        self.company_label.grid(row=1, column=1, sticky="w", padx=6, pady=(0, 2))
        self.dry_run_var = ctk.BooleanVar(value=False)
        self.mandatory_install_var = ctk.BooleanVar(value=False)
        opts_row = ctk.CTkFrame(top_frame, fg_color="transparent")
        opts_row.grid(row=2, column=1, sticky="ew", padx=6, pady=(0, 2))
        opts_row.grid_columnconfigure(0, weight=0)
        opts_row.grid_columnconfigure(1, weight=0)
        opts_row.grid_columnconfigure(2, weight=1)
        dry_run_cb = ctk.CTkCheckBox(opts_row, text="Testmodus / Dry-Run", variable=self.dry_run_var, font=ctk.CTkFont(size=10))
        mandatory_cb = ctk.CTkCheckBox(opts_row, text="Pflichtsoftware erzwingen", variable=self.mandatory_install_var, font=ctk.CTkFont(size=10))
        dry_run_cb.grid(row=0, column=0, sticky="w", padx=(0, 16))
        mandatory_cb.grid(row=0, column=1, sticky="w", padx=(0, 8))
        self._attach_tooltip(dry_run_cb, "Führt alle Schritte nur als Simulation aus, ohne echte Installation oder Deinstallation.")
        self._attach_tooltip(mandatory_cb, "Installiert alle aktivierten Standardprogramme im Best-Effort-Modus.")
        src_row = ctk.CTkFrame(top_frame, fg_color="transparent")
        src_row.grid(row=3, column=1, sticky="ew", padx=6, pady=(0, 2))
        src_row.grid_columnconfigure(0, weight=0)
        src_row.grid_columnconfigure(1, weight=0)
        src_row.grid_columnconfigure(2, weight=0)
        src_row.grid_columnconfigure(3, weight=1)
        local_source_btn = ctk.CTkButton(
            src_row, text="Installationsquelle wählen", command=self._select_local_source, height=20, font=ctk.CTkFont(size=10)
        )
        scan_source_btn = ctk.CTkButton(
            src_row, text="Quelle scannen", command=self._scan_local_source, height=20, font=ctk.CTkFont(size=10)
        )
        prefer_local_cb = ctk.CTkCheckBox(
            src_row,
            text="Lokale Quelle bevorzugen",
            variable=self.prefer_local_var,
            command=lambda: self._persist_local_source_settings(None),
            font=ctk.CTkFont(size=10),
        )
        local_source_btn.grid(row=0, column=0, sticky="w", padx=(0, 8))
        scan_source_btn.grid(row=0, column=1, sticky="w", padx=(0, 8))
        prefer_local_cb.grid(row=0, column=2, sticky="w", padx=(0, 8))
        self._attach_tooltip(local_source_btn, "Wählt einen lokalen Ordner oder USB-Pfad als Installationsquelle.")
        self._attach_tooltip(scan_source_btn, "Durchsucht die gewählte Quelle nach passenden Installern für bekannte Software.")
        self._attach_tooltip(prefer_local_cb, "Wenn aktiv, wird zuerst lokaler Installer/USB versucht, bevor Online-Provider genutzt werden.")
        self.local_source_path_label = ctk.CTkLabel(
            top_frame,
            textvariable=self.local_source_path_var,
            anchor="w",
            justify="left",
            wraplength=780,
            font=ctk.CTkFont(size=10),
            text_color=self.ogx_colors["muted"],
        )
        self.local_source_path_label.grid(row=4, column=1, sticky="ew", padx=6, pady=(0, 4))

        quick_panel = ctk.CTkFrame(top_frame, corner_radius=8)
        self.quick_panel = quick_panel
        quick_panel.grid(row=0, column=2, rowspan=5, sticky="nsew", padx=(8, 6), pady=6)
        quick_panel.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(quick_panel, text="Übersicht", font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(6, 2)
        )
        choco_ok = self.choco.is_choco_installed()
        winget_ok = self.winget.is_available()
        source_text = str(self.runtime.local_source_last_path or "nicht gesetzt")
        provider_text = (
            f"Provider-Status\n"
            f"- Chocolatey: {'bereit' if choco_ok else 'nicht installiert'}\n"
            f"- WinGet: {'bereit' if winget_ok else 'nicht verfügbar'}\n"
            f"- Lokale Quelle: {source_text}\n\n"
            "Hinweis\n"
            "- Ohne Chocolatey wird automatisch WinGet/Intern/Lokal genutzt.\n"
            "- 'Quelle scannen' aktualisiert lokale Installer direkt."
        )
        self.quick_panel_label = ctk.CTkLabel(
            quick_panel,
            text=provider_text,
            justify="left",
            anchor="w",
            wraplength=520,
            font=ctk.CTkFont(size=10),
        )
        self.quick_panel_label.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 6))

        content = ctk.CTkFrame(self)
        self.content_frame = content
        content.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 4))
        # Softwareliste deutlich breiter als Log-Spalte (Aktionen liegen über dem Log).
        content.grid_columnconfigure(0, weight=5)
        content.grid_columnconfigure(1, weight=1)
        content.grid_rowconfigure(0, weight=0)
        content.grid_rowconfigure(1, weight=1, minsize=200)
        content.grid_rowconfigure(2, weight=0)

        filter_bar = ctk.CTkFrame(content)
        self.filter_bar_frame = filter_bar
        filter_bar.grid(row=0, column=0, sticky="ew", padx=(6, 4), pady=(4, 4))
        filter_bar.grid_columnconfigure(1, weight=1)
        filter_bar.grid_columnconfigure(3, weight=1)
        filter_bar.grid_rowconfigure(0, weight=0)
        filter_bar.grid_rowconfigure(1, weight=0)
        self._filter_heading = ctk.CTkLabel(filter_bar, text="Filter", font=ctk.CTkFont(size=10, weight="bold"))
        self._filter_heading.grid(row=0, column=0, padx=6, pady=4, sticky="w")
        self.filter_segment = ctk.CTkSegmentedButton(
            filter_bar,
            values=["Alle", "Updates", "Fehlend", "Fehler"],
            command=self._on_filter_change,
            font=ctk.CTkFont(size=10),
            height=20,
        )
        self.filter_segment.set("Alle")
        self.filter_segment.grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        self._search_heading = ctk.CTkLabel(filter_bar, text="Suche", font=ctk.CTkFont(size=10, weight="bold"))
        self._search_heading.grid(row=0, column=2, padx=(12, 4), pady=4, sticky="w")
        self.search_entry = ctk.CTkEntry(
            filter_bar, textvariable=self.search_var, placeholder_text="Programmname filtern ...", height=20, font=ctk.CTkFont(size=10)
        )
        self.search_entry.grid(row=0, column=3, sticky="ew", padx=6, pady=4)

        software_frame = ctk.CTkScrollableFrame(content, label_text="Softwarestatus", scrollbar_button_hover_color=("gray70", "gray30"))
        self.software_list_frame = software_frame
        software_frame.grid(row=1, column=0, sticky="nsew", padx=(6, 4), pady=(0, 6))
        software_frame.grid_columnconfigure(0, weight=1)
        self._setup_scrollableframe_mousewheel(software_frame)

        header = ctk.CTkFrame(software_frame, fg_color="transparent")
        self.software_header_frame = header
        header.grid(row=0, column=0, columnspan=7, sticky="ew", padx=2, pady=(0, 2))
        header.grid_columnconfigure(0, weight=3, minsize=130)
        header.grid_columnconfigure(1, weight=1, minsize=60)
        header.grid_columnconfigure(2, weight=1, minsize=60)
        header.grid_columnconfigure(3, weight=1, minsize=60)
        header.grid_columnconfigure(4, weight=1, minsize=60)
        header.grid_columnconfigure(5, weight=1, minsize=76)
        _hf = ctk.CTkFont(size=10, weight="bold")
        ctk.CTkLabel(header, text="Programm", font=_hf).grid(row=0, column=0, sticky="w", padx=4)
        ctk.CTkLabel(header, text="Status", font=_hf).grid(row=0, column=1, padx=2)
        ctk.CTkLabel(header, text="Provider", font=_hf).grid(row=0, column=2, padx=2)
        ctk.CTkLabel(header, text="Installiert", font=_hf).grid(row=0, column=3, padx=2)
        ctk.CTkLabel(header, text="Verfuegbar", font=_hf).grid(row=0, column=4, padx=2)
        ctk.CTkLabel(header, text="Fortschritt", font=_hf).grid(row=0, column=5, padx=2)

        for idx, software in enumerate(self.runtime.visible_catalog, start=1):
            key = software.key
            rowf = ctk.CTkFrame(software_frame, fg_color="transparent")
            rowf.grid(row=idx, column=0, columnspan=7, sticky="ew", pady=1)
            rowf.grid_columnconfigure(0, weight=3, minsize=130)
            rowf.grid_columnconfigure(1, weight=1, minsize=60)
            rowf.grid_columnconfigure(2, weight=1, minsize=60)
            rowf.grid_columnconfigure(3, weight=1, minsize=60)
            rowf.grid_columnconfigure(4, weight=1, minsize=60)
            rowf.grid_columnconfigure(5, weight=1, minsize=76)
            self.row_frames[key] = rowf
            self._setup_scroll_hover_target(rowf, self._software_list_wheel)

            var = ctk.BooleanVar(value=False)
            self.checkbox_vars[key] = var
            row_name = (
                f"{software.display_name} (Server-Komponente (interner Installer))"
                if key == "opentext"
                else f"{software.display_name} (Webroot-Agent; Keycode aus Console)"
                if key == "opentext_core_endpoint"
                else software.display_name
            )
            ctk.CTkCheckBox(rowf, text=row_name, variable=var, width=180, font=ctk.CTkFont(size=10)).grid(
                row=0, column=0, sticky="w", padx=2, pady=0
            )

            badge = ctk.CTkLabel(rowf, text="OFFEN", width=92, height=20, corner_radius=5, anchor="center", font=ctk.CTkFont(size=10))
            badge.grid(row=0, column=1, padx=2, pady=0)
            self.badge_labels[key] = badge

            pv = ctk.CTkLabel(rowf, text="—", width=96, anchor="w", font=ctk.CTkFont(size=10))
            pv.grid(row=0, column=2, padx=2, pady=0)
            self.provider_labels[key] = pv

            vi = ctk.CTkLabel(rowf, text="—", width=88, anchor="w", font=ctk.CTkFont(size=10))
            vi.grid(row=0, column=3, padx=2, pady=0)
            self.ver_inst_labels[key] = vi

            va = ctk.CTkLabel(rowf, text="—", width=88, anchor="w", font=ctk.CTkFont(size=10))
            va.grid(row=0, column=4, padx=2, pady=0)
            self.ver_avail_labels[key] = va

            pb = ctk.CTkProgressBar(rowf, width=100)
            pb.grid(row=0, column=5, padx=2, pady=0)
            pb.set(0)
            self.row_progress_bars[key] = pb

            self.row_visible[key] = True
            init_state = SoftwareState("Nicht geprueft")
            self.current_states[key] = init_state
            self._apply_row_state(key, init_state)

        self.search_var.trace_add("write", lambda *_: self._on_search_change())
        self._sync_row_visibility()

        log_frame = ctk.CTkFrame(content)
        self.log_frame = log_frame
        log_frame.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(4, 6), pady=6)
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=0)
        log_frame.grid_rowconfigure(1, weight=1)

        # Toolbar über dem Log (rechte Spalte), damit die Softwareliste mehr vertikale Fläche hat.
        button_host = ctk.CTkFrame(log_frame, fg_color="transparent")
        self.button_host = button_host
        button_host.grid(row=0, column=0, sticky="ew", padx=2, pady=(0, 4))
        button_host.grid_columnconfigure(0, weight=1)
        button_host.grid_columnconfigure(1, weight=0)
        button_host.grid_columnconfigure(2, weight=1)
        button_frame = ctk.CTkFrame(button_host, fg_color="transparent")
        self.button_frame = button_frame
        button_frame.grid(row=0, column=1, sticky="n")

        _btn_kw: dict = {"height": 20, "font": ctk.CTkFont(size=10)}
        all_btn = ctk.CTkButton(button_frame, text="Alle Pakete", command=self._select_all, **_btn_kw)
        all_btn.grid(row=0, column=0, padx=2, pady=1, sticky="ew")
        self._attach_tooltip(all_btn, "Markiert alle aktuell sichtbaren Programme in der Liste.")
        self.check_btn = ctk.CTkButton(button_frame, text="Prüfen", command=self._scan_selected, **_btn_kw)
        self.check_btn.grid(row=0, column=1, padx=2, pady=1, sticky="ew")
        self._attach_tooltip(self.check_btn, "Prüft ausgewählte Programme auf installiert/Update verfügbar.")
        self.install_btn = ctk.CTkButton(button_frame, text="Install / Update", command=self._install_selected, **_btn_kw)
        self.install_btn.grid(row=0, column=2, padx=2, pady=1, sticky="ew")
        self._attach_tooltip(self.install_btn, "Installiert oder aktualisiert ausgewählte Programme gemäß Provider-Kette.")
        self.remove_btn = ctk.CTkButton(button_frame, text="Ausgewählte entfernen", command=self._remove_selected, **_btn_kw)
        self.remove_btn.grid(row=0, column=3, padx=2, pady=1, sticky="ew")
        self._attach_tooltip(self.remove_btn, "Deinstalliert ausgewählte Programme inklusive Deep-Cleanup von Resten.")
        save_log_btn = ctk.CTkButton(button_frame, text="Log speichern", command=self._save_log, **_btn_kw)
        save_log_btn.grid(row=1, column=0, padx=2, pady=1, sticky="ew")
        self._attach_tooltip(save_log_btn, "Speichert das aktuelle Laufprotokoll als Datei.")
        self.report_btn = ctk.CTkButton(button_frame, text="CSV Report öffnen", command=self._open_report_folder, **_btn_kw)
        self.report_btn.grid(row=1, column=1, padx=2, pady=1, sticky="ew")
        self._attach_tooltip(self.report_btn, "Oeffnet den Report-Ordner mit CSV/PDF-Ergebnissen.")
        self.settings_btn = ctk.CTkButton(button_frame, text="Einstellungen", command=self._open_settings_dialog, **_btn_kw)
        self.settings_btn.grid(row=1, column=2, padx=2, pady=1, sticky="ew")
        self._attach_tooltip(self.settings_btn, "Öffnet die Konfiguration für Provider, Installer und Suchmuster.")
        about_btn = ctk.CTkButton(button_frame, text="About", command=self._show_about_dialog, **_btn_kw)
        about_btn.grid(row=1, column=3, padx=2, pady=1, sticky="ew")
        self._attach_tooltip(about_btn, "Zeigt Versions-, Build- und Runtime-Informationen.")
        self.select_missing_btn = ctk.CTkButton(button_frame, text="Nur fehlend", command=self._select_missing_only, **_btn_kw)
        self.select_missing_btn.grid(row=2, column=0, padx=2, pady=(0, 1), sticky="ew")
        self.select_updates_btn = ctk.CTkButton(button_frame, text="Nur Updates", command=self._select_updates_only, **_btn_kw)
        self.select_updates_btn.grid(row=2, column=1, padx=2, pady=(0, 1), sticky="ew")
        self.patch_run_btn = ctk.CTkButton(button_frame, text="Standard Patch Run", command=self._standard_patch_run, **_btn_kw)
        self.patch_run_btn.grid(row=2, column=2, padx=2, pady=(0, 1), sticky="ew")
        self._attach_tooltip(self.patch_run_btn, "Wählt automatisch Programme mit fehlender Installation oder verfügbarem Update.")
        self.system_tools_btn = ctk.CTkButton(button_frame, text="Systemverwaltung", command=self._open_system_tools_dialog, **_btn_kw)
        self.system_tools_btn.grid(row=2, column=3, padx=2, pady=(0, 1), sticky="ew")
        self._attach_tooltip(self.system_tools_btn, "Öffnet Safe-Systemtools für Benutzerverwaltung, Profil-Migration und Windows-Updates.")
        self.energy_screensaver_btn = ctk.CTkButton(
            button_frame,
            text="Energie & Bildschirmschoner",
            command=self._apply_energy_screensaver_defaults,
            **_btn_kw,
        )
        self.energy_screensaver_btn.grid(row=3, column=2, padx=2, pady=(0, 1), sticky="ew")
        self._attach_tooltip(
            self.energy_screensaver_btn,
            "Deckel zu = keine Aktion, Standby aus, Bildschirmschoner 15 Min. "
            "Energie: aktives Schema (powercfg); Schoner: aktueller Benutzer. Admin oft nötig.",
        )
        self.install_choco_btn = ctk.CTkButton(
            button_frame, text="Chocolatey installieren", command=self._bootstrap_chocolatey, **_btn_kw
        )
        self.install_choco_btn.grid(row=3, column=0, padx=2, pady=(0, 1), sticky="ew")
        self._attach_tooltip(
            self.install_choco_btn,
            "Installiert Chocolatey per offiziellem PowerShell-Skript (Internet, Admin empfohlen).",
        )
        self.install_winget_btn = ctk.CTkButton(
            button_frame, text="WinGet installieren", command=self._bootstrap_winget, **_btn_kw
        )
        self.install_winget_btn.grid(row=3, column=1, padx=2, pady=(0, 1), sticky="ew")
        self._attach_tooltip(
            self.install_winget_btn,
            "Installiert die App-Installer-Paketquelle (aka.ms/getwinget). Nach Neustart oder neuer Shell oft verfügbar.",
        )
        self._toolbar_buttons = [
            all_btn,
            self.check_btn,
            self.install_btn,
            self.remove_btn,
            save_log_btn,
            self.report_btn,
            self.settings_btn,
            about_btn,
            self.select_missing_btn,
            self.select_updates_btn,
            self.patch_run_btn,
            self.system_tools_btn,
            self.energy_screensaver_btn,
            self.install_choco_btn,
            self.install_winget_btn,
        ]
        if platform.system() != "Windows":
            self.energy_screensaver_btn.configure(state="disabled")

        self.log_box = ctk.CTkTextbox(log_frame, wrap="word", font=ctk.CTkFont(size=10))
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self._setup_scroll_hover_target(self.log_box, self._logbox_wheel)

        self.history_frame = ctk.CTkFrame(self)
        self.history_frame.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 4))
        self.history_frame.grid_columnconfigure(1, weight=1)
        self.history_frame.grid_columnconfigure(2, weight=0)
        ctk.CTkLabel(self.history_frame, text="Letzte Läufe (max. 10 Reports: CSV / PDF)", font=ctk.CTkFont(size=10, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=6, pady=6
        )
        ctk.CTkButton(
            self.history_frame, text="Report-Ordner öffnen", command=self._open_reports_dir, height=22, font=ctk.CTkFont(size=10)
        ).grid(
            row=0, column=1, sticky="e", padx=6, pady=6
        )
        self.history_toggle_btn = ctk.CTkButton(
            self.history_frame,
            text="Reports anzeigen",
            width=118,
            height=20,
            font=ctk.CTkFont(size=10),
            command=self._toggle_history_section,
        )
        self.history_toggle_btn.grid(row=0, column=2, sticky="e", padx=(0, 6), pady=6)
        self.history_scroll = ctk.CTkScrollableFrame(self.history_frame, height=48)
        self.history_scroll.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        self.history_scroll.grid_columnconfigure(0, weight=1)
        self._set_history_details_visible(False)

        bottom_frame = ctk.CTkFrame(self)
        bottom_frame.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 4))
        bottom_frame.grid_columnconfigure(0, weight=1)
        bottom_frame.grid_columnconfigure(1, weight=0)

        self.progress = ctk.CTkProgressBar(bottom_frame)
        self.progress.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 2))
        self.progress.set(0)
        self.progress_count_label = ctk.CTkLabel(bottom_frame, text="0/0", width=220, anchor="e", font=ctk.CTkFont(size=10))
        self.progress_count_label.grid(row=0, column=1, sticky="e", padx=6, pady=(4, 2))

        self.status_line = ctk.CTkLabel(bottom_frame, text="Bereit", font=ctk.CTkFont(size=10))
        self.status_line.grid(row=1, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 2))
        self.hint_line = ctk.CTkLabel(
            bottom_frame, text="Hinweis: Für Installationen sind Administratorrechte empfohlen.", font=ctk.CTkFont(size=10)
        )
        self.hint_line.grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 4))
        # Kein _apply_responsive_layout hier: winfo ist oft 1×1 vor dem Map → falsche Einspalten-/Toolbar-Logik.

    def _attach_tooltip(self, widget, text: str) -> None:
        def on_enter(event) -> None:
            self._schedule_tooltip(event, text)

        def on_move(event) -> None:
            self._schedule_tooltip(event, text)

        def on_leave(_event) -> None:
            self._cancel_scheduled_tooltip()
            self._hide_tooltip()

        widget.bind("<Enter>", on_enter, add="+")
        widget.bind("<Motion>", on_move, add="+")
        widget.bind("<Leave>", on_leave, add="+")

    def _schedule_tooltip(self, event, text: str) -> None:
        self._cancel_scheduled_tooltip()
        x_root = int(getattr(event, "x_root", 0))
        y_root = int(getattr(event, "y_root", 0))
        self._tooltip_after_id = self.after(250, lambda: self._show_tooltip_at(x_root, y_root, text))

    def _cancel_scheduled_tooltip(self) -> None:
        if self._tooltip_after_id is not None:
            try:
                self.after_cancel(self._tooltip_after_id)
            except Exception:  # pragma: no cover
                pass
            self._tooltip_after_id = None

    def _show_tooltip(self, event, text: str) -> None:
        if not text:
            return
        if self._tooltip_toplevel is None or not self._tooltip_toplevel.winfo_exists():
            self._tooltip_toplevel = ctk.CTkToplevel(self)
            self._tooltip_toplevel.overrideredirect(True)
            self._tooltip_toplevel.attributes("-topmost", True)
            self._tooltip_label = ctk.CTkLabel(
                self._tooltip_toplevel,
                text=text,
                justify="left",
                anchor="w",
                corner_radius=8,
                fg_color=("gray90", "gray20"),
                text_color=("black", "white"),
                padx=10,
                pady=6,
                wraplength=360,
            )
            self._tooltip_label.pack(fill="both", expand=True)
        elif self._tooltip_label is not None:
            self._tooltip_label.configure(text=text)
        x = event.x_root + 14
        y = event.y_root + 14
        self._tooltip_toplevel.geometry(f"+{x}+{y}")
        self._tooltip_toplevel.deiconify()
        self._tooltip_after_id = None

    def _show_tooltip_at(self, x_root: int, y_root: int, text: str) -> None:
        class _EventProxy:
            def __init__(self, x: int, y: int) -> None:
                self.x_root = x
                self.y_root = y

        self._show_tooltip(_EventProxy(x_root, y_root), text)

    def _hide_tooltip(self) -> None:
        if self._tooltip_toplevel is not None and self._tooltip_toplevel.winfo_exists():
            self._tooltip_toplevel.withdraw()

    def _set_system_tab_status(self, tab_name: str, state: str) -> None:
        label = self.system_tools_status_labels.get(tab_name)
        if label is None:
            return
        normalized = state.strip().lower()
        if normalized == "läuft":
            label.configure(text="läuft", fg_color="#7a6a2b", text_color="#f5f1e0")
            return
        if normalized == "fehler":
            label.configure(text="fehler", fg_color="#7a3240", text_color="#f8e9ed")
            return
        label.configure(text="bereit", fg_color="#2c5a4a", text_color="#e6f7ef")

    def _on_window_resize(self, event) -> None:
        if event.widget is not self:
            return
        self._apply_responsive_layout(int(event.width), int(event.height))

    def _toolbar_reference_width(self, window_width: int) -> int:
        """Toolbar liegt über dem Log; Spaltenanzahl nach Log-Breite, nicht nach voller Fensterbreite."""
        lf = self.log_frame
        if lf is not None:
            try:
                w = int(lf.winfo_width())
                if w > 160:
                    return w
            except Exception:
                pass
        if window_width < 800:
            return max(480, window_width - 48)
        return max(360, int(window_width * 0.28))

    def _apply_toolbar_layout(self, layout_w: int) -> None:
        bf = self.button_frame
        if bf is None or not self._toolbar_buttons:
            return
        # Breit: 6×2, mittel: 4×4, schmal: 2×… — bezogen auf Log-Spalte (button_host im log_frame).
        if layout_w >= 1240:
            cols = 6
        elif layout_w >= 720:
            cols = 4
        else:
            cols = 2
        if self._toolbar_cols == cols:
            return
        self._toolbar_cols = cols
        px, py = 2, 1
        for c in range(6):
            bf.grid_columnconfigure(c, weight=1 if c < cols else 0, minsize=0)
        for btn in self._toolbar_buttons:
            btn.grid_forget()
        for i, btn in enumerate(self._toolbar_buttons):
            r, c = divmod(i, cols)
            btn.grid(row=r, column=c, padx=px, pady=py, sticky="ew")

    def _apply_filter_bar_layout(self, layout_w: int) -> None:
        fb = self.filter_bar_frame
        if (
            fb is None
            or self.filter_segment is None
            or self.search_entry is None
            or self._filter_heading is None
            or self._search_heading is None
        ):
            return
        stacked = layout_w < 640
        if self._filter_bar_stacked is not None and stacked == self._filter_bar_stacked:
            return
        self._filter_bar_stacked = stacked
        for w in (self._filter_heading, self.filter_segment, self._search_heading, self.search_entry):
            w.grid_forget()
        if stacked:
            self._filter_heading.grid(row=0, column=0, sticky="w", padx=6, pady=2)
            self.filter_segment.grid(row=0, column=1, columnspan=3, sticky="ew", padx=6, pady=2)
            self._search_heading.grid(row=1, column=0, sticky="w", padx=6, pady=(4, 2))
            self.search_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=6, pady=(0, 2))
        else:
            self._filter_heading.grid(row=0, column=0, padx=6, pady=4, sticky="w")
            self.filter_segment.grid(row=0, column=1, sticky="ew", padx=6, pady=4)
            self._search_heading.grid(row=0, column=2, padx=(12, 4), pady=4, sticky="w")
            self.search_entry.grid(row=0, column=3, sticky="ew", padx=6, pady=4)

    def _apply_responsive_layout(self, width: int, height: int) -> None:
        self._apply_top_section_layout(width, height)
        self._update_wrap_lengths(width)
        self._apply_software_column_layout(width)
        self._apply_filter_bar_layout(width)
        self._apply_width_layout(width)
        self._apply_toolbar_layout(self._toolbar_reference_width(width))
        compact = height < 760
        if self._compact_mode is not None and compact == self._compact_mode:
            return
        self._compact_mode = compact
        self._set_health_details_visible((not compact) and self._health_details_expanded)
        if compact:
            if self.history_frame is not None:
                self.history_frame.grid_remove()
        else:
            if self.history_frame is not None:
                self.history_frame.grid()
            self._set_history_details_visible(self._history_expanded)

    def _apply_top_section_layout(self, width: int, height: int) -> None:
        if self.top_frame is None or self.quick_panel is None:
            return
        # Notebook/touchpad usability: prefer less top-area content
        # unless there is enough horizontal and vertical space.
        # Übersicht nur bei sehr viel Platz — spart Höhe und wirkt auf schmalen Fenstern aufgeräumter.
        if width < 1320 or height < 820:
            self.quick_panel.grid_remove()
            self.top_frame.grid_columnconfigure(2, weight=0, minsize=0)
        else:
            self.quick_panel.grid()
            self.top_frame.grid_columnconfigure(2, weight=1, minsize=300)

    def _apply_software_column_layout(self, width: int) -> None:
        if width >= 1900:
            col0, other, progress = 400, 118, 164
        elif width >= 1600:
            col0, other, progress = 340, 104, 148
        elif width >= 1300:
            col0, other, progress = 300, 92, 132
        elif width >= 1100:
            col0, other, progress = 260, 82, 118
        elif width >= 900:
            col0, other, progress = 220, 72, 104
        elif width >= 700:
            col0, other, progress = 190, 64, 92
        elif width >= 520:
            col0, other, progress = 165, 56, 80
        else:
            col0, other, progress = 120, 48, 68

        targets: list[ctk.CTkFrame] = []
        if self.software_header_frame is not None:
            targets.append(self.software_header_frame)
        targets.extend(self.row_frames.values())
        for frame in targets:
            frame.grid_columnconfigure(0, weight=3, minsize=col0)
            frame.grid_columnconfigure(1, weight=1, minsize=other)
            frame.grid_columnconfigure(2, weight=1, minsize=other)
            frame.grid_columnconfigure(3, weight=1, minsize=other)
            frame.grid_columnconfigure(4, weight=1, minsize=other)
            frame.grid_columnconfigure(5, weight=1, minsize=progress)

    def _update_wrap_lengths(self, width: int) -> None:
        health_wrap = max(180, min(1500, width - 48))
        info_wrap = max(220, min(760, int(width * 0.44)))
        source_wrap = max(200, min(980, int(width * 0.9)))
        if self.health_details_label is not None:
            self.health_details_label.configure(wraplength=health_wrap)
        if self.health_warnings_label is not None:
            self.health_warnings_label.configure(wraplength=health_wrap)
        if self.health_config_label is not None:
            self.health_config_label.configure(wraplength=health_wrap)
        if self.quick_panel_label is not None:
            self.quick_panel_label.configure(wraplength=info_wrap)
        if self.local_source_path_label is not None:
            self.local_source_path_label.configure(wraplength=source_wrap)

    def _set_history_details_visible(self, visible: bool) -> None:
        if self.history_scroll is not None:
            if visible:
                self.history_scroll.grid()
            else:
                self.history_scroll.grid_remove()
        if self.history_toggle_btn is not None:
            self.history_toggle_btn.configure(text="Reports ausblenden" if visible else "Reports anzeigen")

    def _toggle_history_section(self) -> None:
        self._history_expanded = not self._history_expanded
        if self._compact_mode:
            self._set_history_details_visible(False)
            return
        self._set_history_details_visible(self._history_expanded)

    def _set_health_details_visible(self, visible: bool) -> None:
        if self.health_summary_frame is not None:
            if visible:
                self.health_summary_frame.grid()
            else:
                self.health_summary_frame.grid_remove()
        if self.health_toggle_btn is not None:
            self.health_toggle_btn.configure(text="Details ausblenden" if visible else "Details anzeigen")

    def _toggle_health_details(self) -> None:
        self._health_details_expanded = not self._health_details_expanded
        self._set_health_details_visible((not bool(self._compact_mode)) and self._health_details_expanded)

    def _apply_width_layout(self, width: int) -> None:
        # Ab dieser Breite: Log rechts neben Filter+Liste (gleiche Zeilenhöhe wie Software — „rutscht nach oben“).
        single_column = width < 800
        if self.content_frame is None or self.filter_bar_frame is None or self.software_list_frame is None or self.log_frame is None:
            return

        content = self.content_frame
        want_wide_split = not single_column and width >= 1600
        prev_mode = self._single_column_mode
        prev_split = self._log_wide_split_active
        layout_mode_changed = prev_mode != single_column
        split_only_changed = (
            not single_column
            and prev_mode is False
            and bool(prev_split) != bool(want_wide_split)
        )

        if not layout_mode_changed:
            if single_column:
                return
            if not split_only_changed:
                return
            self._log_wide_split_active = want_wide_split
            if want_wide_split:
                content.grid_columnconfigure(0, weight=7)
                content.grid_columnconfigure(1, weight=1)
            else:
                content.grid_columnconfigure(0, weight=5)
                content.grid_columnconfigure(1, weight=1)
            return

        self._single_column_mode = single_column
        self._log_wide_split_active = want_wide_split if not single_column else None

        if single_column:
            content.grid_columnconfigure(0, weight=1)
            content.grid_columnconfigure(1, weight=0)
            content.grid_rowconfigure(0, weight=0)
            content.grid_rowconfigure(1, weight=2, minsize=160)
            content.grid_rowconfigure(2, weight=1, minsize=120)
            self.filter_bar_frame.grid(row=0, column=0, columnspan=1, sticky="ew", padx=6, pady=(4, 4))
            self.software_list_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 4))
            self.log_frame.grid(row=2, column=0, rowspan=1, sticky="nsew", padx=6, pady=(0, 6))
        else:
            if want_wide_split:
                content.grid_columnconfigure(0, weight=7)
                content.grid_columnconfigure(1, weight=1)
            else:
                content.grid_columnconfigure(0, weight=5)
                content.grid_columnconfigure(1, weight=1)
            content.grid_rowconfigure(0, weight=0)
            content.grid_rowconfigure(1, weight=1, minsize=200)
            content.grid_rowconfigure(2, weight=0, minsize=0)
            self.filter_bar_frame.grid(row=0, column=0, columnspan=1, sticky="ew", padx=(6, 4), pady=(4, 4))
            self.software_list_frame.grid(row=1, column=0, sticky="nsew", padx=(6, 4), pady=(0, 6))
            self.log_frame.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(4, 6), pady=6)

    def _on_filter_change(self, value: str) -> None:
        self._sync_row_visibility()

    def _on_search_change(self, *_args: object) -> None:
        self._sync_row_visibility()

    @staticmethod
    def _set_health_caption(lb: ctk.CTkLabel | None, text: str) -> None:
        if lb is not None:
            lb.configure(text=text or "—")

    def _current_filter_mode(self) -> str:
        if self.filter_segment is None:
            return "Alle"
        v = self.filter_segment.get()
        return str(v)

    def _row_matches_filter(self, key: str) -> bool:
        mode = self._current_filter_mode()
        state = self.current_states.get(key, SoftwareState("Nicht geprueft"))
        st = state.status
        if mode == "Alle":
            return True
        if mode == "Updates":
            return st == "Update verfuegbar"
        if mode == "Fehlend":
            return st == "Nicht installiert"
        if mode == "Fehler":
            if st in ("Quelle erforderlich", "PRUEFT"):
                return False
            return "Fehler" in st
        return True

    def _row_matches_search(self, key: str) -> bool:
        q = (self.search_var.get() or "").strip().lower()
        if not q:
            return True
        for sw in self.runtime.visible_catalog:
            if sw.key == key:
                return q in sw.display_name.lower() or q in key.lower()
        return True

    def _filters_relaxed(self) -> bool:
        if (self.search_var.get() or "").strip():
            return False
        return self._current_filter_mode() == "Alle"

    def _sync_row_visibility(self) -> None:
        relaxed = self._filters_relaxed()
        for key, frame in self.row_frames.items():
            if relaxed:
                show = True
            else:
                show = self._row_matches_filter(key) and self._row_matches_search(key)
            self.row_visible[key] = show
            if show:
                frame.grid()
            else:
                frame.grid_remove()

    def _apply_filter(self) -> None:
        self._sync_row_visibility()

    def _apply_row_state(self, key: str, state: SoftwareState) -> None:
        self.current_states[key] = state
        badge_text, bg, fg = self._status_badge_style(state)
        self.badge_labels[key].configure(text=badge_text, fg_color=bg, text_color=fg)
        self.provider_labels[key].configure(text=state.provider or "—")
        self.ver_inst_labels[key].configure(text=state.installed_version or "—")
        self.ver_avail_labels[key].configure(text=state.available_version or "—")
        self._progress_phase = "bearbeitet"
        if state.status == "PRUEFT":
            self._set_row_progress_start(key)
        else:
            self._set_row_progress_done(key)

    @staticmethod
    def _status_badge_style(state: SoftwareState) -> tuple[str, str, str]:
        st = state.status
        if st == "PRUEFT":
            return "PRUEFT", "#8a6a2a", "#fff8e6"
        if st == "Quelle erforderlich":
            return "QUELLE", "#7a6a2b", "#f5f1e0"
        if "fehler" in st.lower():
            return "FEHLER", "#7a3240", "#f8e9ed"
        if "dry-run" in st.lower():
            return "DRY-RUN", "#756847", "#f7f2e6"
        if st == "Update verfuegbar":
            return "UPDATE", "#6e5d2f", "#f6f0df"
        if st == "Nicht installiert":
            return "FEHLT", "#4b5563", "#e8edf5"
        if st == "Manuelle Pruefung noetig" or "manuelle" in st.lower():
            return "PRUEFEN", "#6f6138", "#f6f1e2"
        if st == "Installiert":
            return "OK", "#2c5a4a", "#e6f7ef"
        if st == "Aktuell":
            return "AKTUELL", "#244c40", "#e4f4ee"
        if st == "Nicht geprueft":
            return "OFFEN", "#405164", "#e7eef6"
        return st[:10].upper(), "#3f4a5b", "#e7eef6"

    def _run_startup_checks(self) -> None:
        self._set_actions_enabled(False)

        def worker() -> None:
            admin_ok = self._is_admin()
            self._queue_status("Prüfe Administratorrechte...")
            if admin_ok:
                self.logger.info("Programm laeuft mit Administratorrechten.")
            else:
                self.logger.warning("Programm laeuft NICHT mit Administratorrechten.")
                self.ui_queue.put(("warning", "Keine Administratorrechte erkannt. Einige Aktionen können fehlschlagen."))

            choco_installed = self.choco.is_choco_installed()
            choco_ver = "—"
            choco_warn_msg: str | None = None
            self._queue_status("Prüfe Chocolatey...")
            if choco_installed:
                choco_ver = self.choco.version() or "Unbekannt"
                self.logger.info("Chocolatey Version: %s", choco_ver)
            else:
                self.logger.error("Chocolatey wurde nicht gefunden.")
                choco_warn_msg = "Chocolatey ist nicht installiert oder nicht im PATH."

            self._queue_status("Prüfe Netzwerk...")
            net_ok = self.choco.has_network()
            if net_ok:
                self.logger.info("Netzwerk ist verfuegbar.")

            self._queue_status("Self Health Check...")
            health = run_self_health_check(self.choco, self.winget, self.runtime, self.logger)

            detail_line = " | ".join(health.details[:4])
            if len(health.details) > 4:
                detail_line += " | ..."
            warn_line = " | ".join(health.warnings[:3])
            if len(health.warnings) > 3:
                warn_line += " | ..."
            hint_line = " | ".join(health.config_hints[:3])
            if len(health.config_hints) > 3:
                hint_line += " | ..."
            self.ui_queue.put(
                (
                    "health_ui",
                    {
                        "passed": health.passed,
                        "admin": admin_ok,
                        "choco": choco_installed,
                        "choco_ver": choco_ver,
                        "network": net_ok,
                        "err_n": health.error_count,
                        "warn_n": health.warning_count,
                        "hint_n": health.config_hint_count,
                        "details": detail_line,
                        "warnings": warn_line,
                        "hints": hint_line,
                    },
                )
            )

            self._queue_status("Startpruefung abgeschlossen.")
            self.ui_queue.put(("enable_actions", True))

            # Hinweise nach Health-UI und Freigabe der Buttons (Dialoge blockieren nicht mehr die Queue).
            if health.passed:
                if health.config_hint_count:
                    self.ui_queue.put(("hint", "Health Check Passed (configuration hints)"))
                else:
                    self.ui_queue.put(("hint", "Health Check Passed"))
            else:
                self.ui_queue.put(("hint", "Warnings detected"))
                self.ui_queue.put(("warning", "Self-Health-Check meldet Warnungen. Details stehen im Log."))
            if choco_warn_msg:
                self.ui_queue.put(("warning", choco_warn_msg))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_provider_quick_panel(self) -> None:
        if self.quick_panel_label is None:
            return
        choco_ok = self.choco.is_choco_installed()
        winget_ok = self.winget.is_available()
        source_text = str(self.runtime.local_source_last_path or "nicht gesetzt")
        provider_text = (
            f"Provider-Status\n"
            f"- Chocolatey: {'bereit' if choco_ok else 'nicht installiert'}\n"
            f"- WinGet: {'bereit' if winget_ok else 'nicht verfügbar'}\n"
            f"- Lokale Quelle: {source_text}\n\n"
            "Hinweis\n"
            "- Ohne Chocolatey wird automatisch WinGet/Intern/Lokal genutzt.\n"
            "- 'Quelle scannen' aktualisiert lokale Installer direkt."
        )
        self.quick_panel_label.configure(text=provider_text)

    def _bootstrap_chocolatey(self) -> None:
        if self.choco.is_choco_installed():
            messagebox.showinfo(APP_NAME, "Chocolatey ist bereits installiert.", parent=self)
            return
        if not messagebox.askokcancel(
            APP_NAME,
            "Chocolatey wird mit dem offiziellen Installations-Skript aus dem Internet geladen. "
            "Bitte als Administrator ausführen. Fortfahren?",
            parent=self,
        ):
            return
        self._set_actions_enabled(False)

        def worker() -> None:
            self._queue_status("Chocolatey wird installiert …")
            res = self.choco.ensure_installed()
            ok = bool(res.ok and self.choco.is_choco_installed())
            detail = "\n".join(x for x in (res.stdout, res.stderr) if x).strip()
            if not detail:
                detail = "OK" if ok else "Keine Ausgabe"
            self.ui_queue.put(("bootstrap_done", {"ok": ok, "title": "Chocolatey", "detail": detail}))

        threading.Thread(target=worker, daemon=True).start()

    def _bootstrap_winget(self) -> None:
        if self.winget.is_available():
            messagebox.showinfo(APP_NAME, "WinGet ist bereits verfügbar.", parent=self)
            return
        if not messagebox.askokcancel(
            APP_NAME,
            "WinGet (App Installer) wird von Microsoft geladen und installiert. "
            "Bei älterem Windows kann ein Neustart nötig sein. Fortfahren?",
            parent=self,
        ):
            return
        self._set_actions_enabled(False)

        def worker() -> None:
            self._queue_status("WinGet wird installiert …")
            res = self.winget.ensure_installed()
            ok = bool(res.ok and self.winget.is_available())
            detail = "\n".join(x for x in (res.stdout, res.stderr) if x).strip()
            if not detail:
                detail = "OK" if ok else "Keine Ausgabe"
            self.ui_queue.put(("bootstrap_done", {"ok": ok, "title": "WinGet", "detail": detail}))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_energy_screensaver_defaults(self) -> None:
        if platform.system() != "Windows":
            messagebox.showinfo(APP_NAME, "Nur unter Windows verfügbar.", parent=self)
            return
        if not messagebox.askokcancel(
            APP_NAME,
            "Folgendes wird am aktiven Energieschema gesetzt (oft Administrator nötig):\n\n"
            "• Deckel zu: keine Aktion (Netz/Batterie)\n"
            "• Standby: nie (Netz/Batterie)\n"
            "• Bildschirmschoner: ein, 15 Minuten (aktueller Benutzer)\n\n"
            "Fortfahren?",
            parent=self,
        ):
            return
        self._set_actions_enabled(False)
        self._queue_status("Energie & Bildschirmschoner …")

        def worker() -> None:
            result = self.system_tools.apply_energy_and_screensaver_defaults()
            self.ui_queue.put(("energy_screensaver_done", result))

        threading.Thread(target=worker, daemon=True).start()

    def _energy_screensaver_finished(self, result: SystemActionResult) -> None:
        try:
            for line in result.lines:
                self.logger.info("%s", line)
            self._queue_status("Bereit" if result.ok else "Energie/Schoner: Fehler")
            detail = "\n".join(result.lines)
            if len(detail) > 900:
                detail = detail[:900] + "\n…"
            if result.ok:
                messagebox.showinfo(APP_NAME, detail or "OK", parent=self)
            else:
                messagebox.showwarning(APP_NAME, detail or "Fehler", parent=self)
        finally:
            self._set_actions_enabled(True)

    def _show_about_dialog(self) -> None:
        version_path = EXE_PARENT / "VERSION.txt"
        if not version_path.exists():
            version_path = BUNDLE_DIR / "VERSION.txt"
        version = "Unbekannt"
        build_date = "Unbekannt"
        if version_path.exists():
            version = version_path.read_text(encoding="utf-8").strip() or "Unbekannt"
            build_date = datetime.fromtimestamp(version_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        py_runtime = platform.python_version()
        choco_version = self.choco.version() or "Nicht verfuegbar"
        winget_version = self.winget.version() or "Nicht verfuegbar"
        messagebox.showinfo(
            "About",
            (
                f"Version: {version}\n"
                f"Build Date: {build_date}\n"
                f"Python Runtime: {py_runtime}\n"
                f"Chocolatey Version: {choco_version}\n"
                f"WinGet Version: {winget_version}"
            ),
        )

    def _open_system_tools_dialog(self) -> None:
        dialog = ctk.CTkToplevel(self)
        dialog.title("Systemverwaltung (Safe)")
        dialog.geometry("980x760")
        dialog.configure(fg_color=self.ogx_colors["bg"])
        dialog.grab_set()

        frame = ctk.CTkFrame(dialog)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            frame,
            text="Safe-Modus: Keine riskante Live-Profil-Umbenennung. Erst Precheck und Erklärung, dann gezielt ausführen.",
            anchor="w",
            justify="left",
            text_color=("gray30", "gray75"),
        ).grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 8))

        top_row = ctk.CTkFrame(frame, fg_color="transparent")
        top_row.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        top_row.grid_columnconfigure(0, weight=0)
        top_row.grid_columnconfigure(1, weight=0)
        top_row.grid_columnconfigure(2, weight=1)
        top_row.grid_columnconfigure(3, weight=0)
        top_row.grid_columnconfigure(4, weight=0)

        ctk.CTkLabel(top_row, text="Gewünschter Benutzer").grid(row=0, column=0, sticky="w", padx=(0, 8))
        users = self.system_tools.list_local_users()
        source_user_var = ctk.StringVar(value=(users[0] if users else ""))
        source_user_menu = ctk.CTkOptionMenu(
            top_row,
            variable=source_user_var,
            values=users if users else ["(keine lokalen Benutzer)"],
            width=220,
        )
        source_user_menu.grid(row=0, column=1, sticky="w")
        self._attach_tooltip(source_user_menu, "Wählt den gewünschten lokalen Benutzer aus, der geändert oder migriert werden soll.")
        user_info_var = ctk.StringVar(value="Benutzerdetails: —")
        ctk.CTkLabel(top_row, textvariable=user_info_var, anchor="w", justify="left", wraplength=680).grid(
            row=0, column=2, sticky="ew", padx=(10, 10)
        )

        def _refresh_user_info() -> None:
            selected = source_user_var.get().strip()
            if not selected or selected.startswith("("):
                user_info_var.set("Benutzerdetails: —")
                return
            details = self.system_tools.get_user_details(selected)
            user_info_var.set(
                "Benutzerdetails: "
                f"Existiert: {details['exists']} | "
                f"Admin: {details['admin']} | "
                f"Aktiv: {details['active']} | "
                f"Profil: {details['profile_exists']} ({details['profile_path']})"
            )

        def _reload_users() -> None:
            refreshed = self.system_tools.list_local_users()
            if not refreshed:
                refreshed = ["(keine lokalen Benutzer)"]
            source_user_menu.configure(values=refreshed)
            if source_user_var.get() not in refreshed:
                source_user_var.set(refreshed[0])
            _refresh_user_info()
        reload_users_btn = ctk.CTkButton(top_row, text="Benutzerliste neu laden", command=_reload_users, width=180)
        reload_users_btn.grid(
            row=0, column=3, sticky="e"
        )
        self._attach_tooltip(reload_users_btn, "Lädt die lokalen Benutzerkonten neu vom System.")
        source_user_menu.configure(command=lambda _value: _refresh_user_info())
        _refresh_user_info()

        tabview = ctk.CTkTabview(frame)
        tabview.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        tabview.add("Account")
        tabview.add("Computer")
        tabview.add("Updates")
        tabview.add("Migration")
        tabview.add("Protokoll")
        self.system_tools_tabview = tabview
        account_tab = tabview.tab("Account")
        computer_tab = tabview.tab("Computer")
        updates_tab = tabview.tab("Updates")
        migration_tab = tabview.tab("Migration")
        log_tab = tabview.tab("Protokoll")
        for tab in (account_tab, computer_tab, updates_tab, migration_tab):
            tab.grid_columnconfigure(1, weight=1)
        log_tab.grid_columnconfigure(0, weight=1)
        log_tab.grid_rowconfigure(1, weight=1)

        account_name_var = ctk.StringVar(value="")
        ctk.CTkLabel(account_tab, text="Neuer Kontoname/Anzeigename").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 6))
        ctk.CTkEntry(account_tab, textvariable=account_name_var).grid(row=0, column=1, sticky="ew", padx=8, pady=(8, 6))
        ctk.CTkLabel(
            account_tab,
            text="Ändert den Kontovollnamen und sorgt für eine saubere Basis-Ordnerstruktur beim gewählten Benutzer.",
            anchor="w",
            justify="left",
            text_color=("gray35", "gray70"),
            wraplength=760,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))

        current_pc_var = ctk.StringVar(value=self.system_tools.get_computer_name())
        ctk.CTkLabel(computer_tab, text="Aktueller Computername").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 6))
        ctk.CTkLabel(computer_tab, textvariable=current_pc_var, anchor="w").grid(row=0, column=1, sticky="w", padx=8, pady=(8, 6))
        pc_new_name_var = ctk.StringVar(value="")
        ctk.CTkLabel(computer_tab, text="Neuer Computername").grid(row=1, column=0, sticky="w", padx=8, pady=(8, 6))
        ctk.CTkEntry(computer_tab, textvariable=pc_new_name_var).grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 6))
        ctk.CTkLabel(
            computer_tab,
            text="Max. 15 Zeichen, Buchstaben/Ziffern/Bindestrich (NetBIOS). Administratorrechte nötig. Dry-Run nutzt -WhatIf.",
            anchor="w",
            justify="left",
            text_color=("gray35", "gray70"),
            wraplength=760,
        ).grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        restart_after_rename_var = ctk.BooleanVar(value=False)
        restart_rename_cb = ctk.CTkCheckBox(
            computer_tab,
            text="Nach Umbenennung sofort neu starten (-Restart)",
            variable=restart_after_rename_var,
        )
        restart_rename_cb.grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))
        self._attach_tooltip(
            restart_rename_cb,
            "Führt Rename-Computer mit -Restart aus. Ohne Häkchen bleibt ein manueller Neustart meist nötig.",
        )

        profile_target_var = ctk.StringVar(value="")
        ctk.CTkLabel(migration_tab, text="Ziel-Profilname").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 6))
        ctk.CTkEntry(migration_tab, textvariable=profile_target_var).grid(row=0, column=1, sticky="ew", padx=8, pady=(8, 6))
        ctk.CTkLabel(
            migration_tab,
            text="Precheck zuerst ausführen. Die Aktion blockiert automatisch bei unsicheren Voraussetzungen.",
            anchor="w",
            justify="left",
            text_color=("gray35", "gray70"),
            wraplength=760,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))

        dry_var = ctk.BooleanVar(value=True)
        sys_dry_cb = ctk.CTkCheckBox(top_row, text="Dry-Run", variable=dry_var)
        sys_dry_cb.grid(row=0, column=4, sticky="e", padx=(10, 0))
        self._attach_tooltip(sys_dry_cb, "Simuliert Systemaktionen ohne echte Änderungen auf dem Betriebssystem.")

        ctk.CTkLabel(
            updates_tab,
            text="Windows-Updates können hier erst gescannt und danach installiert werden. Installation kann Neustart erfordern.",
            anchor="w",
            justify="left",
            text_color=("gray35", "gray70"),
            wraplength=760,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 8))

        account_status = ctk.CTkLabel(account_tab, text="bereit", width=70, corner_radius=8, fg_color="#2c5a4a", text_color="#e6f7ef")
        account_status.grid(row=0, column=2, padx=8, pady=(8, 6), sticky="e")
        updates_status = ctk.CTkLabel(updates_tab, text="bereit", width=70, corner_radius=8, fg_color="#2c5a4a", text_color="#e6f7ef")
        updates_status.grid(row=0, column=2, padx=8, pady=(8, 6), sticky="e")
        migration_status = ctk.CTkLabel(migration_tab, text="bereit", width=70, corner_radius=8, fg_color="#2c5a4a", text_color="#e6f7ef")
        migration_status.grid(row=0, column=2, padx=8, pady=(8, 6), sticky="e")
        pc_status = ctk.CTkLabel(computer_tab, text="bereit", width=70, corner_radius=8, fg_color="#2c5a4a", text_color="#e6f7ef")
        pc_status.grid(row=0, column=2, rowspan=2, padx=8, pady=(8, 6), sticky="ne")
        self.system_tools_status_labels = {
            "Account": account_status,
            "Computer": pc_status,
            "Updates": updates_status,
            "Migration": migration_status,
        }

        ctk.CTkLabel(log_tab, text="Protokoll / Details", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 4)
        )
        output = ctk.CTkTextbox(log_tab, wrap="word")
        output.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        def _append(lines: list[str]) -> None:
            if not lines:
                return
            output.insert("end", "\n".join(lines) + "\n")
            output.see("end")

        def _run_in_thread(fn, tab_name: str) -> None:
            self._set_system_tab_status(tab_name, "läuft")
            if self.system_tools_tabview is not None:
                self.system_tools_tabview.set("Protokoll")

            def worker() -> None:
                try:
                    result = fn()
                    self.ui_queue.put(("system_tools_output", {"lines": result.lines, "tab": tab_name, "ok": bool(result.ok)}))
                except Exception as exc:  # pylint: disable=broad-except
                    self.ui_queue.put(("system_tools_output", {"lines": [f"Fehler: {exc}"], "tab": tab_name, "ok": False}))

            threading.Thread(target=worker, daemon=True).start()

        def run_safe_account() -> None:
            source_user = source_user_var.get().strip()
            if not source_user or source_user.startswith("("):
                messagebox.showwarning(APP_NAME, "Bitte 'Gewünschter Benutzer' auswählen.")
                return
            target = account_name_var.get().strip()
            if not target:
                messagebox.showwarning(APP_NAME, "Bitte gewünschten neuen Namen eintragen.")
                return
            _append(["Starte Safe-Account-Aktion..."])
            _run_in_thread(lambda: self.system_tools.run_account_safe_update(source_user, target, dry_var.get()), "Account")

        def scan_updates() -> None:
            _append(["Suche Windows Updates..."])
            _run_in_thread(self.system_tools.scan_windows_updates, "Updates")

        def install_updates() -> None:
            _append(["Starte Windows Update Installation..."])
            _run_in_thread(lambda: self.system_tools.install_windows_updates(dry_var.get()), "Updates")

        def migration_precheck() -> None:
            source_user = source_user_var.get().strip()
            if not source_user or source_user.startswith("("):
                messagebox.showwarning(APP_NAME, "Bitte 'Gewünschter Benutzer' auswählen.")
                return
            target = profile_target_var.get().strip()
            if not target:
                messagebox.showwarning(APP_NAME, "Bitte gewünschten Profilnamen eintragen.")
                return
            _append(["Starte Profil-Migrations-Precheck..."])
            _run_in_thread(lambda: self.system_tools.profile_migration_precheck(source_user, target), "Migration")

        def migration_execute() -> None:
            source_user = source_user_var.get().strip()
            if not source_user or source_user.startswith("("):
                messagebox.showwarning(APP_NAME, "Bitte 'Gewünschter Benutzer' auswählen.")
                return
            target = profile_target_var.get().strip()
            if not target:
                messagebox.showwarning(APP_NAME, "Bitte gewünschten Profilnamen eintragen.")
                return
            _append(["Starte Profil-Migrations-Vorbereitung..."])
            _run_in_thread(lambda: self.system_tools.execute_profile_migration(source_user, target, dry_var.get()), "Migration")

        def rename_pc() -> None:
            newn = pc_new_name_var.get().strip()
            if not newn:
                messagebox.showwarning(APP_NAME, "Bitte neuen Computernamen eintragen.")
                return
            restart = bool(restart_after_rename_var.get())
            dry = bool(dry_var.get())
            if not dry and restart:
                if not messagebox.askyesno(
                    APP_NAME,
                    "Der PC wird nach erfolgreicher Umbenennung neu gestartet. Nicht gespeicherte Arbeit speichern. Fortfahren?",
                ):
                    return
            _append(["Starte Computer-Umbenennung..."])
            _run_in_thread(lambda: self.system_tools.rename_computer(newn, dry, restart), "Computer")

        safe_account_btn = ctk.CTkButton(account_tab, text="Account + Struktur (Safe)", command=run_safe_account)
        safe_account_btn.grid(row=2, column=0, columnspan=2, padx=8, pady=(0, 8), sticky="ew")
        self._attach_tooltip(safe_account_btn, "Ändert Kontovollname und prüft/erstellt die Basis-Ordnerstruktur des gewählten Benutzers.")

        rename_pc_btn = ctk.CTkButton(computer_tab, text="PC umbenennen", command=rename_pc)
        rename_pc_btn.grid(row=4, column=0, columnspan=2, padx=8, pady=(0, 8), sticky="ew")
        self._attach_tooltip(rename_pc_btn, "Benennt den Windows-Computer um (Rename-Computer). Erfordert typischerweise Administratorrechte.")

        scan_updates_btn = ctk.CTkButton(updates_tab, text="Windows-Updates scannen", command=scan_updates)
        scan_updates_btn.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="ew")
        self._attach_tooltip(scan_updates_btn, "Listet verfügbare Windows-Software-Updates.")
        install_updates_btn = ctk.CTkButton(updates_tab, text="Windows-Updates installieren", command=install_updates)
        install_updates_btn.grid(row=1, column=1, padx=8, pady=(0, 8), sticky="ew")
        self._attach_tooltip(install_updates_btn, "Installiert gefundene Windows-Updates; kann Neustart erfordern.")

        migration_precheck_btn = ctk.CTkButton(migration_tab, text="Migrations-Precheck", command=migration_precheck)
        migration_precheck_btn.grid(row=2, column=0, padx=8, pady=(0, 8), sticky="ew")
        self._attach_tooltip(migration_precheck_btn, "Prüft vorab, ob eine sichere Profil-Migration für den Benutzer möglich ist.")
        migration_execute_btn = ctk.CTkButton(migration_tab, text="Migration ausführen (safe)", command=migration_execute)
        migration_execute_btn.grid(row=2, column=1, padx=8, pady=(0, 8), sticky="ew")
        self._attach_tooltip(migration_execute_btn, "Führt nur kontrollierte, sichere Migrationsschritte aus und blockiert bei Risiko.")
        ctk.CTkLabel(
            migration_tab,
            text="Empfohlen: Erst Migrations-Precheck, dann Ausführung starten.",
            anchor="w",
            text_color=("gray35", "gray70"),
        ).grid(row=3, column=0, columnspan=2, padx=8, pady=(0, 8), sticky="w")

        self.system_tools_output_box = output
        self._apply_ogx_style(dialog)

    def _open_settings_dialog(self) -> None:
        cfg = get_config_dict(self.logger)
        dialog = ctk.CTkToplevel(self)
        dialog.title("Einstellungen")
        dialog.geometry("920x820")
        dialog.configure(fg_color=self.ogx_colors["bg"])
        dialog.grab_set()

        frame = ctk.CTkScrollableFrame(dialog)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(frame, text="Firmenname").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        company_var = ctk.StringVar(value=str(cfg.get("company_name", "")))
        ctk.CTkEntry(frame, textvariable=company_var).grid(row=0, column=1, sticky="ew", padx=8, pady=8)

        source = cfg.get("chocolatey_source", {})
        src_url = source.get("url", "") if isinstance(source, dict) else ""
        src_name = source.get("name", "chocolatey") if isinstance(source, dict) else "chocolatey"
        ctk.CTkLabel(frame, text="Chocolatey Source URL").grid(row=1, column=0, sticky="w", padx=8, pady=8)
        source_url_var = ctk.StringVar(value=str(src_url))
        ctk.CTkEntry(frame, textvariable=source_url_var).grid(row=1, column=1, sticky="ew", padx=8, pady=8)
        local_cfg = cfg.get("local_source", {})
        local_cfg = local_cfg if isinstance(local_cfg, dict) else {}
        ctk.CTkLabel(frame, text="Lokale Installationsquelle (Pfad)").grid(row=2, column=0, sticky="w", padx=8, pady=8)
        local_path_var = ctk.StringVar(value=str(local_cfg.get("last_path", "") or ""))
        ctk.CTkEntry(frame, textvariable=local_path_var).grid(row=2, column=1, sticky="ew", padx=8, pady=8)
        local_pref_var = ctk.BooleanVar(value=bool(local_cfg.get("prefer_local", True)))
        ctk.CTkCheckBox(frame, text="Lokale Quelle bevorzugen", variable=local_pref_var).grid(row=3, column=1, sticky="w", padx=8, pady=(0, 8))

        ctk.CTkLabel(frame, text="Software Provider Einstellungen (pro Programm)").grid(
            row=4, column=0, columnspan=2, sticky="w", padx=8, pady=(12, 6)
        )
        providers_cfg = cfg.get("software_providers")
        if not isinstance(providers_cfg, dict):
            providers_cfg = default_software_providers()
        provider_vars: dict[str, dict[str, object]] = {}
        row = 5
        for software in SOFTWARE_CATALOG:
            current = providers_cfg.get(software.key, {}) if isinstance(providers_cfg.get(software.key), dict) else {}
            internal = current.get("internal_installer", {}) if isinstance(current.get("internal_installer"), dict) else {}
            enabled_var = ctk.BooleanVar(value=bool(current.get("enabled", True)))
            display_var = ctk.StringVar(value=str(current.get("display_name", software.display_name)))
            choco_var = ctk.StringVar(value=str(current.get("choco_package", software.primary_package or "")))
            winget_var = ctk.StringVar(value=str(current.get("winget_id", software.winget_id or "")))
            path_var = ctk.StringVar(value=str(internal.get("path", software.installer_source or "")))
            args_var = ctk.StringVar(value=str(internal.get("silent_args", "")))
            type_var = ctk.StringVar(value=str(internal.get("type", "auto") or "auto").lower())
            response_var = ctk.StringVar(value=str(internal.get("response_file", "")))
            terms_raw = current.get("search_terms", list(software.search_terms))
            terms_var = ctk.StringVar(value=", ".join(str(x).strip() for x in terms_raw if str(x).strip()) if isinstance(terms_raw, list) else str(terms_raw))
            patterns_raw = current.get("local_patterns", [])
            patterns_var = ctk.StringVar(value=", ".join(str(x).strip() for x in patterns_raw if str(x).strip()) if isinstance(patterns_raw, list) else "")

            card = ctk.CTkFrame(frame)
            card.grid(row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=6)
            card.grid_columnconfigure(1, weight=1)
            ctk.CTkCheckBox(card, text=f"{software.key} aktiviert", variable=enabled_var).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 4))
            ctk.CTkLabel(card, text="Anzeigename").grid(row=1, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkEntry(card, textvariable=display_var).grid(row=1, column=1, sticky="ew", padx=8, pady=4)
            ctk.CTkLabel(card, text="Chocolatey Paketname").grid(row=2, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkEntry(card, textvariable=choco_var).grid(row=2, column=1, sticky="ew", padx=8, pady=4)
            ctk.CTkLabel(card, text="WinGet ID").grid(row=3, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkEntry(card, textvariable=winget_var).grid(row=3, column=1, sticky="ew", padx=8, pady=4)
            ctk.CTkLabel(card, text="Interner Installerpfad").grid(row=4, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkEntry(card, textvariable=path_var).grid(row=4, column=1, sticky="ew", padx=8, pady=4)
            ctk.CTkLabel(card, text="Installer-Typ (Auto/MSI/EXE)").grid(row=5, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkOptionMenu(card, variable=type_var, values=["auto", "msi", "exe"]).grid(row=5, column=1, sticky="w", padx=8, pady=4)
            ctk.CTkLabel(card, text="Response File (optional)").grid(row=6, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkEntry(card, textvariable=response_var).grid(row=6, column=1, sticky="ew", padx=8, pady=4)
            ctk.CTkLabel(card, text="Silent Args").grid(row=7, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkEntry(card, textvariable=args_var).grid(row=7, column=1, sticky="ew", padx=8, pady=4)
            ctk.CTkLabel(card, text="Suchbegriffe (Komma-getrennt)").grid(row=8, column=0, sticky="w", padx=8, pady=(4, 8))
            ctk.CTkEntry(card, textvariable=terms_var).grid(row=8, column=1, sticky="ew", padx=8, pady=(4, 8))
            ctk.CTkLabel(card, text="Lokale Dateipatterns (Komma-getrennt)").grid(row=9, column=0, sticky="w", padx=8, pady=(0, 8))
            ctk.CTkEntry(card, textvariable=patterns_var).grid(row=9, column=1, sticky="ew", padx=8, pady=(0, 8))

            keycode_var: ctk.StringVar | None = None
            if software.key == "opentext_core_endpoint":
                keycode_var = ctk.StringVar(value=str(internal.get("endpoint_keycode", "") or ""))
                ctk.CTkLabel(
                    card,
                    text="Endpoint Site-Keycode (XXXX-XXXX-… aus Console; stilles Setup per Dateiname)",
                    anchor="w",
                ).grid(row=10, column=0, sticky="w", padx=8, pady=(4, 8))
                ctk.CTkEntry(card, textvariable=keycode_var).grid(row=10, column=1, sticky="ew", padx=8, pady=(4, 8))

            pv: dict[str, object] = {
                "enabled": enabled_var,
                "display_name": display_var,
                "choco_package": choco_var,
                "winget_id": winget_var,
                "path": path_var,
                "installer_type": type_var,
                "response_file": response_var,
                "silent_args": args_var,
                "search_terms": terms_var,
                "local_patterns": patterns_var,
            }
            if keycode_var is not None:
                pv["endpoint_keycode"] = keycode_var
            provider_vars[software.key] = pv
            row += 1

        def save_settings() -> None:
            new_config = dict(cfg)
            new_config["company_name"] = company_var.get().strip()
            new_config["chocolatey_source"] = {"name": str(src_name), "url": source_url_var.get().strip()}
            providers_out: dict[str, object] = {}
            enabled_keys: list[str] = []
            internal_installers: dict[str, object] = dict(new_config.get("internal_installers", {}))
            for key, vals in provider_vars.items():
                enabled = bool(vals["enabled"].get())  # type: ignore[index]
                if enabled:
                    enabled_keys.append(key)
                terms = [t.strip() for t in str(vals["search_terms"].get()).split(",") if t.strip()]  # type: ignore[index]
                local_patterns = [t.strip() for t in str(vals["local_patterns"].get()).split(",") if t.strip()]  # type: ignore[index]
                ii: dict[str, str] = {
                    "path": str(vals["path"].get()).strip(),  # type: ignore[index]
                    "type": str(vals["installer_type"].get()).strip().lower() or "auto",  # type: ignore[index]
                    "response_file": str(vals["response_file"].get()).strip(),  # type: ignore[index]
                    "silent_args": str(vals["silent_args"].get()).strip(),  # type: ignore[index]
                }
                if "endpoint_keycode" in vals:
                    ii["endpoint_keycode"] = str(vals["endpoint_keycode"].get()).strip()  # type: ignore[index]
                providers_out[key] = {
                    "enabled": enabled,
                    "display_name": str(vals["display_name"].get()).strip(),  # type: ignore[index]
                    "choco_package": str(vals["choco_package"].get()).strip(),  # type: ignore[index]
                    "winget_id": str(vals["winget_id"].get()).strip(),  # type: ignore[index]
                    "internal_installer": ii,
                    "search_terms": terms,
                    "local_patterns": local_patterns,
                }
                if key == "avaya_workplace":
                    internal_installers["avaya"] = {
                        "path": str(vals["path"].get()).strip(),
                        "type": str(vals["installer_type"].get()).strip().lower() or "auto",
                        "response_file": str(vals["response_file"].get()).strip(),
                        "silent_args": str(vals["silent_args"].get()).strip(),
                        "display_name": "Avaya Workplace",
                    }  # type: ignore[index]
                if key == "opentext":
                    internal_installers["opentext"] = {
                        "path": str(vals["path"].get()).strip(),
                        "type": str(vals["installer_type"].get()).strip().lower() or "auto",
                        "response_file": str(vals["response_file"].get()).strip(),
                        "silent_args": str(vals["silent_args"].get()).strip(),
                        "display_name": "OpenText",
                    }  # type: ignore[index]
                if key == "opentext_core_endpoint":
                    internal_installers["opentext_endpoint"] = {
                        "path": str(vals["path"].get()).strip(),
                        "type": str(vals["installer_type"].get()).strip().lower() or "auto",
                        "response_file": str(vals["response_file"].get()).strip(),
                        "silent_args": str(vals["silent_args"].get()).strip(),
                        "display_name": "OpenText Core Endpoint Protection",
                        "endpoint_keycode": str(vals["endpoint_keycode"].get()).strip() if "endpoint_keycode" in vals else "",
                    }  # type: ignore[index]
            new_config["software_providers"] = providers_out
            new_config["enabled_standard_software"] = enabled_keys
            new_config["internal_installers"] = internal_installers
            new_config["local_source"] = {
                "last_path": local_path_var.get().strip(),
                "prefer_local": bool(local_pref_var.get()),
            }
            save_config_dict(new_config, self.logger)
            self.runtime = load_runtime_settings(self.logger)
            self.local_source = LocalSourceService(self.logger, self.runtime.software_providers)
            if self.runtime.local_source_last_path:
                self.local_source.scan_source(self.runtime.local_source_last_path)
                self.local_source_path_var.set(f"Aktive lokale Quelle: {self.runtime.local_source_last_path}")
            runtime_by_key = {s.key: s for s in self.runtime.visible_catalog}
            self.scanner = SoftwareScanner(self.choco, self.winget, self.logger, self.runtime.visible_catalog)
            self.installer = InstallerService(
                self.choco,
                self.winget,
                self.logger,
                runtime_by_key,
                provider_configs=self.runtime.software_providers,
                local_source_service=self.local_source,
                prefer_local_source=self.runtime.local_source_prefer_local,
                scanner=self.scanner,
                installer_settle_wait_seconds=self.runtime.installer_settle_wait_seconds,
                installer_verify_after_timeout=self.runtime.installer_verify_after_timeout,
                installer_verify_poll_interval_seconds=self.runtime.installer_verify_poll_interval_seconds,
                installer_verify_poll_max_seconds=self.runtime.installer_verify_poll_max_seconds,
            )
            self.uninstaller = UninstallerService(
                self.choco,
                self.winget,
                self.logger,
                runtime_by_key,
                scanner=self.scanner,
                software_providers=self.runtime.software_providers,
            )
            self._apply_title()
            self.company_label.configure(text=f"Company: {self.runtime.company_name or 'Nicht gesetzt'}")
            messagebox.showinfo(APP_NAME, "Einstellungen gespeichert. Bitte Programm neu starten, damit alle Änderungen aktiv werden.")
            dialog.destroy()

        ctk.CTkButton(frame, text="Speichern", command=save_settings).grid(
            row=row + 1, column=0, columnspan=2, sticky="ew", padx=8, pady=12
        )
        self._apply_ogx_style(dialog)

    def _apply_ogx_style(self, root_widget) -> None:
        for child in root_widget.winfo_children():
            try:
                if isinstance(child, ctk.CTkFrame):
                    frame_color = str(child.cget("fg_color"))
                    if frame_color.lower() != "transparent":
                        child.configure(fg_color=self.ogx_colors["panel"], border_width=1, border_color=self.ogx_colors["border"])
                elif isinstance(child, ctk.CTkScrollableFrame):
                    child.configure(fg_color=self.ogx_colors["panel_alt"], border_width=1, border_color=self.ogx_colors["border"])
                elif isinstance(child, ctk.CTkButton):
                    child.configure(
                        fg_color=self.ogx_colors["accent"],
                        hover_color=self.ogx_colors["accent_hover"],
                        text_color="#f4f7fd",
                        border_width=0,
                        corner_radius=6,
                        height=20,
                        font=ctk.CTkFont(size=10),
                    )
                elif isinstance(child, ctk.CTkEntry):
                    child.configure(
                        fg_color=self.ogx_colors["panel_alt"],
                        border_color=self.ogx_colors["border"],
                        text_color=self.ogx_colors["text"],
                    )
                elif isinstance(child, ctk.CTkTextbox):
                    child.configure(
                        fg_color=self.ogx_colors["panel_alt"],
                        border_color=self.ogx_colors["border"],
                        text_color=self.ogx_colors["text"],
                    )
                elif isinstance(child, ctk.CTkOptionMenu):
                    child.configure(
                        fg_color=self.ogx_colors["panel_alt"],
                        button_color=self.ogx_colors["accent"],
                        button_hover_color=self.ogx_colors["accent_hover"],
                        text_color=self.ogx_colors["text"],
                    )
                elif isinstance(child, ctk.CTkCheckBox):
                    child.configure(
                        fg_color=self.ogx_colors["accent"],
                        hover_color=self.ogx_colors["accent_hover"],
                        border_color=self.ogx_colors["border"],
                        checkmark_color="#03131a",
                        text_color=self.ogx_colors["text"],
                    )
                elif isinstance(child, ctk.CTkSegmentedButton):
                    child.configure(
                        fg_color=self.ogx_colors["panel_alt"],
                        selected_color=self.ogx_colors["accent"],
                        selected_hover_color=self.ogx_colors["accent_hover"],
                        unselected_color=self.ogx_colors["panel"],
                        unselected_hover_color="#1a2632",
                        text_color=self.ogx_colors["text"],
                        height=20,
                        font=ctk.CTkFont(size=10),
                    )
                elif isinstance(child, ctk.CTkTabview):
                    child.configure(
                        fg_color=self.ogx_colors["panel"],
                        segmented_button_fg_color=self.ogx_colors["panel_alt"],
                        segmented_button_selected_color=self.ogx_colors["accent"],
                        segmented_button_selected_hover_color=self.ogx_colors["accent_hover"],
                        segmented_button_unselected_color=self.ogx_colors["panel"],
                        segmented_button_unselected_hover_color="#1a2632",
                        text_color=self.ogx_colors["text"],
                    )
                elif isinstance(child, ctk.CTkLabel):
                    current_color = child.cget("text_color")
                    if current_color in ("#ffffff", "white", "#000000", "black") or isinstance(current_color, tuple):
                        child.configure(text_color=self.ogx_colors["text"])
            except Exception:  # pragma: no cover - UI style should never block features
                pass
            self._apply_ogx_style(child)

    @staticmethod
    def _is_admin() -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # pragma: no cover
            return False

    def _selected_keys(self) -> list[str]:
        return [key for key, var in self.checkbox_vars.items() if var.get()]

    def _select_all(self) -> None:
        selected_filezilla = False
        for software in self.runtime.visible_catalog:
            key = software.key
            if self.row_visible.get(key, True):
                self.checkbox_vars[key].set(True)
                if key == "filezilla":
                    selected_filezilla = True
        if not selected_filezilla:
            self._filezilla_buchhaltung = None
            return
        buchhaltung = messagebox.askyesno(
            APP_NAME,
            "Alle Programme sind ausgewählt — inklusive FileZilla.\n\n"
            "Soll FileZilla für die Buchhaltung mit installiert werden?\n\n"
            "Ja = Buchhaltung (FileZilla bleibt angehakt)\n"
            "Nein = keine Buchhaltung (FileZilla wird abgewählt, kein Download/Install dafür)",
            parent=self,
        )
        if buchhaltung:
            self._filezilla_buchhaltung = True
            self.logger.info("Alle Pakete: FileZilla fuer Buchhaltung mit ausgewaehlt.")
            self.hint_line.configure(
                text="FileZilla: Buchhaltung — bleibt in der Auswahl; Hinweis bei Installation im Log."
            )
        else:
            self._filezilla_buchhaltung = None
            fz_var = self.checkbox_vars.get("filezilla")
            if fz_var is not None:
                fz_var.set(False)
            self.logger.info("Alle Pakete: FileZilla abgewaehlt (keine Buchhaltung).")
            self.hint_line.configure(
                text="FileZilla: abgewaehlt (nur bei Buchhaltung mit Alle Pakete auswaehlen)."
            )

    def _select_missing_only(self) -> None:
        for software in self.runtime.visible_catalog:
            key = software.key
            st = self.current_states.get(key, SoftwareState("Nicht geprueft")).status
            self.checkbox_vars[key].set(st == "Nicht installiert")

    def _select_updates_only(self) -> None:
        for software in self.runtime.visible_catalog:
            key = software.key
            st = self.current_states.get(key, SoftwareState("Nicht geprueft")).status
            self.checkbox_vars[key].set(st == "Update verfuegbar")

    def _standard_patch_run(self) -> None:
        """Waehlt nur Eintraege mit fehlender Installation oder verfuegbarem Update (nicht 'Aktuell'/'Installiert')."""
        need: set[str] = set()
        unchecked = 0
        for software in self.runtime.visible_catalog:
            key = software.key
            st = self.current_states.get(key, SoftwareState("Nicht geprueft")).status
            if st == "Nicht geprueft":
                unchecked += 1
            elif st == "Nicht installiert" or st == "Update verfuegbar":
                need.add(key)
        if unchecked and not need:
            messagebox.showinfo(
                APP_NAME,
                "Noch keine Prüfergebnisse. Bitte zuerst 'Ausgewählte prüfen' ausführen (z. B. alle Standardprogramme auswählen).",
            )
            return
        for software in self.runtime.visible_catalog:
            key = software.key
            self.checkbox_vars[key].set(key in need)
        self._queue_status(f"Standard Patch Run: {len(need)} Programme ausgewählt.")
        if need:
            self.hint_line.configure(text=f"Standard Patch: {len(need)} ausgewählt — jetzt prüfen und/oder installieren.")
        else:
            self.hint_line.configure(text="Standard Patch: keine fehlenden Programme oder Updates laut letzter Prüfung.")

    def _scan_selected(self) -> None:
        keys = self._selected_keys()
        if not keys:
            messagebox.showwarning(APP_NAME, "Bitte mindestens ein Programm auswählen.")
            return

        self._set_actions_enabled(False)
        self._progress_phase = "geprueft"
        total = len(keys)
        self.ui_queue.put(("progress", {"frac": 0.0, "done": 0, "total": total}))
        self._queue_status("Prüfe installierte Programme...")

        def worker() -> None:
            try:
                all_states = self.scanner.scan()
                for i, key in enumerate(keys, start=1):
                    self.ui_queue.put(("progress", {"frac": (i - 1) / total if total else 0.0, "done": i - 1, "total": total}))
                    state = all_states[key]
                    self.current_states[key] = state
                    self.ui_queue.put(("software_row", (key, state)))
                self.ui_queue.put(("progress", {"frac": 1.0, "done": total, "total": total}))
                rows = self.report_writer.from_scan(self.current_states, keys)
                self.last_report_file = self.report_writer.write_report(rows, "scan_report")
                self.logger.info("Prüfung abgeschlossen.")
                self._queue_status("Prüfung abgeschlossen.")
                summary_txt = self._scan_summary_text(rows)
                self.ui_queue.put(
                    (
                        "summary_dialog",
                        {
                            "title": "Abschluss — Prüfung",
                            "summary": summary_txt,
                            "entries": rows,
                            "report_csv": self.last_report_file,
                            "dry_run": False,
                            "kind": "scan",
                        },
                    )
                )
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.exception("Fehler während der Prüfung: %s", exc)
                self._queue_status("Fehler in der Prüfung.")
            finally:
                self.ui_queue.put(("enable_actions", True))
                self.ui_queue.put(("refresh_history", None))

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _scan_summary_text(rows: list[ReportEntry]) -> str:
        return "\n".join(format_scan_summary_lines(rows))

    @staticmethod
    def _install_summary_message(
        rows: list[ReportEntry],
        dry_run: bool,
        mandatory: bool = False,
        operation: str = "install",
    ) -> str:
        return "\n".join(
            format_install_summary_lines(rows, dry_run=dry_run, mandatory=mandatory, operation=operation),
        )

    def _show_completion_dialog(self, data: dict) -> None:
        title = str(data.get("title", "Abschluss"))
        summary = str(data.get("summary", ""))
        entries = data.get("entries") or []
        report_csv = data.get("report_csv")
        kind = str(data.get("kind", "install"))

        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.geometry("620x540")
        dialog.transient(self)
        dialog.grab_set()

        body = ctk.CTkScrollableFrame(dialog)
        body.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        tb = ctk.CTkTextbox(body, wrap="word", font=ctk.CTkFont(size=13))
        tb.pack(fill="both", expand=True)
        lines = [summary.strip(), "", "Details (Auszug):"]
        if isinstance(entries, list):
            re_list = [r for r in entries[:120] if isinstance(r, ReportEntry)]
            for line in dialog_detail_lines(re_list, limit=len(re_list)):
                lines.append(line)
        tb.insert("1.0", "\n".join(lines))
        tb.configure(state="disabled")

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 12))

        def close_d() -> None:
            try:
                dialog.grab_release()
            except Exception:  # pragma: no cover
                pass
            dialog.destroy()

        def open_csv_dir() -> None:
            path = Path(report_csv) if report_csv else None
            if path and path.exists():
                os.startfile(str(path.parent))  # type: ignore[attr-defined]
            else:
                REPORT_DIR.mkdir(parents=True, exist_ok=True)
                os.startfile(str(REPORT_DIR))  # type: ignore[attr-defined]

        def export_pdf() -> None:
            if not isinstance(entries, list) or not entries:
                messagebox.showinfo(APP_NAME, "Keine Zeilen für PDF.")
                return
            rt = "scan_summary" if kind == "scan" else "install_summary"
            pdf_path = write_summary_pdf(entries, rt, title)
            if pdf_path:
                self.logger.info("PDF-Zusammenfassung: %s", pdf_path)
                messagebox.showinfo(APP_NAME, f"PDF gespeichert:\n{pdf_path}")
                self.ui_queue.put(("refresh_history", None))
            else:
                messagebox.showwarning(APP_NAME, "fpdf2 ist nicht installiert. Hinweis: pip install fpdf2")

        ctk.CTkButton(btn_row, text="Schliessen", command=close_d).pack(side="left", padx=4)
        ctk.CTkButton(btn_row, text="CSV-Ordner", command=open_csv_dir).pack(side="left", padx=4)
        pdf_btn = ctk.CTkButton(btn_row, text="PDF exportieren", command=export_pdf)
        pdf_btn.pack(side="left", padx=4)
        if not pdf_export_available():
            pdf_btn.configure(state="disabled")

    def _run_residue_cleanup_dialog_payload(self, payload: dict) -> None:
        """Main-thread dialog: scan results with optional deletion (TuneUp-style)."""
        rq = payload.get("response_q")
        cr = payload.get("cleanup")
        title_name = str(payload.get("display_name", "Software"))
        if rq is None or cr is None:
            return

        total_found = len(cr.paths_found) + len(cr.registry_keys_found) + len(cr.shortcuts_found)
        if total_found > 0 and len(cr.removable_items) == 0:
            msg = (
                f"Es wurden {total_found} Restobjekt(e) gefunden, aber keine liegen unter den "
                "erlaubten Standardpfaden (Program Files, ProgramData, AppData …).\n\n"
                "Bitte manuell im Explorer oder in der Registrierung prüfen."
            )
            if cr.skipped_items:
                msg += "\n\nNicht als sicher löschbar eingestuft:\n"
                msg += "\n".join(f"- {(s.label or s.target)[:100]}" for s in cr.skipped_items[:12])
                if len(cr.skipped_items) > 12:
                    msg += "\n…"
            messagebox.showinfo(APP_NAME, msg, parent=self)
            rq.put({"cancelled": False, "removed_ok": 0, "failed": 0, "selected_count": 0, "info_only": True})
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title("Reste gefunden")
        dialog.geometry("700x520")
        dialog.transient(self)
        dialog.grab_set()
        fg = self.ogx_colors.get("panel", "#171b22")
        dialog.configure(fg_color=self.ogx_colors.get("bg", "#0f1115"))

        head = ctk.CTkLabel(
            dialog,
            text=f"{title_name}: {total_found} Restobjekt(e). Wählen Sie Einträge zum Löschen.",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=self.ogx_colors.get("text", "#e6edf7"),
        )
        head.pack(anchor="w", padx=14, pady=(12, 4))

        scroll = ctk.CTkScrollableFrame(dialog, fg_color=fg, height=300)
        scroll.pack(fill="both", expand=True, padx=12, pady=8)

        vars_by_id: dict[str, ctk.BooleanVar] = {}
        for it in cr.removable_items:
            v = ctk.BooleanVar(value=True)
            vars_by_id[it.id] = v
            row_f = ctk.CTkFrame(scroll, fg_color="transparent")
            row_f.pack(fill="x", pady=1)
            ctk.CTkCheckBox(row_f, text="", variable=v, width=28).pack(side="left", padx=(0, 4))
            lbl = f"[{it.kind}] {(it.label or it.target)[:95]}"
            ctk.CTkLabel(row_f, text=lbl, anchor="w", font=ctk.CTkFont(size=12)).pack(side="left", fill="x", expand=True)

        if cr.skipped_items:
            ctk.CTkLabel(
                scroll,
                text="Nicht automatisch löschbar (Auszug):",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=self.ogx_colors.get("muted", "#a4b0c0"),
            ).pack(anchor="w", pady=(8, 2))
            for s in cr.skipped_items[:20]:
                ctk.CTkLabel(
                    scroll,
                    text=f"- {(s.label or s.target)[:100]} — {s.skip_reason or 'unsicher'}",
                    anchor="w",
                    font=ctk.CTkFont(size=11),
                    text_color=self.ogx_colors.get("muted", "#a4b0c0"),
                ).pack(anchor="w")

        btn_bar = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_bar.pack(fill="x", padx=12, pady=(4, 12))

        def select_all_fn() -> None:
            for v in vars_by_id.values():
                v.set(True)

        def select_none_fn() -> None:
            for v in vars_by_id.values():
                v.set(False)

        def finish(cancelled: bool, removed_ok: int = 0, failed: int = 0, selected_count: int = 0) -> None:
            try:
                dialog.grab_release()
            except Exception:  # pragma: no cover
                pass
            dialog.destroy()
            rq.put(
                {
                    "cancelled": cancelled,
                    "removed_ok": removed_ok,
                    "failed": failed,
                    "selected_count": selected_count,
                    "info_only": False,
                }
            )

        def on_clean() -> None:
            selected = [it for it in cr.removable_items if vars_by_id.get(it.id) and vars_by_id[it.id].get()]
            if not selected:
                messagebox.showwarning(APP_NAME, "Bitte mindestens ein Objekt auswählen.", parent=dialog)
                return
            if not messagebox.askyesno(
                APP_NAME,
                f"Ausgewählte {len(selected)} Objekt(e) unwiderruflich löschen?",
                parent=dialog,
            ):
                return
            rok, fl = apply_selected_cleanup(selected, self.logger)
            finish(cancelled=False, removed_ok=rok, failed=fl, selected_count=len(selected))

        ctk.CTkButton(btn_bar, text="Alle auswählen", command=select_all_fn, width=120).pack(side="left", padx=2)
        ctk.CTkButton(btn_bar, text="Keine", command=select_none_fn, width=80).pack(side="left", padx=2)
        ctk.CTkButton(btn_bar, text="Ausgewählte bereinigen", command=on_clean, fg_color=self.ogx_colors.get("accent", "#4c6ef5")).pack(
            side="left", padx=12
        )
        ctk.CTkButton(btn_bar, text="Schliessen (ohne Löschen)", command=lambda: finish(cancelled=True)).pack(side="right", padx=2)

        def on_x() -> None:
            finish(cancelled=True)

        dialog.protocol("WM_DELETE_WINDOW", on_x)

    def _install_selected(self) -> None:
        self._run_action("install")

    def _remove_selected(self) -> None:
        self._run_action("remove")

    def _run_action(self, mode: str) -> None:
        if not self._operation_lock.acquire(blocking=False):
            messagebox.showwarning(APP_NAME, "Ein Vorgang läuft bereits. Bitte warten.", parent=self)
            return
        mandatory = mode == "install" and self.mandatory_install_var.get()
        keys = [s.key for s in self.runtime.visible_catalog] if mandatory else self._selected_keys()
        if not keys:
            self._operation_lock.release()
            messagebox.showwarning(APP_NAME, "Bitte mindestens ein Programm auswählen.")
            return

        if mode == "remove":
            if not self.dry_run_var.get() and not messagebox.askyesno(
                APP_NAME,
                "Diese Programme werden vom System entfernt. Fortfahren?",
            ):
                return
        elif not self.dry_run_var.get() and not messagebox.askyesno(
            APP_NAME,
            "Produktivmodus: Es werden Änderungen am System vorgenommen. Fortfahren?",
            ):
                self._operation_lock.release()
                return

        self._set_actions_enabled(False)
        self._progress_phase = "bearbeitet"
        self.ui_queue.put(("row_progress_reset", keys))
        total = len(keys)
        self.ui_queue.put(("progress", {"frac": 0.0, "done": 0, "total": total}))
        self._queue_status("Vorgang gestartet..." if mode == "install" else "Entfernen gestartet...")

        def status_callback(key: str, state: SoftwareState) -> None:
            self.ui_queue.put(("software_row", (key, state)))

        def progress_callback(done: int, total_count: int) -> None:
            self.ui_queue.put(
                ("progress", {"frac": 0 if total_count == 0 else done / total_count, "done": done, "total": total_count})
            )

        def download_progress_callback(read: int, total: int | None) -> None:
            self.ui_queue.put(("download_progress", {"read": read, "total": total}))

        def activity_callback(display_name: str, detail: str, progress_phase: str | None = None) -> None:
            self.ui_queue.put(("install_activity", (display_name, detail, progress_phase)))

        def item_start_callback(key: str) -> None:
            self.ui_queue.put(("row_progress_start", key))

        def method_progress_callback(key: str, label: str) -> None:
            st0 = self.current_states.get(key, SoftwareState("Installiert"))
            status_callback(
                key,
                SoftwareState(
                    st0.status,
                    package_name=st0.package_name,
                    detail=label[:220],
                    provider=st0.provider or "",
                    installed_version=st0.installed_version,
                    available_version=st0.available_version,
                ),
            )

        def resolution_callback(key: str, choco_package: str | None, winget_id: str | None) -> None:
            self.ui_queue.put(("resolved_source", {"key": key, "choco": choco_package, "winget": winget_id}))

        dry = self.dry_run_var.get()

        def worker() -> None:
            try:
                if mode == "install" and "filezilla" in keys:
                    if self._filezilla_buchhaltung is True:
                        self.logger.info("FileZilla: Installation im Buchhaltungskontext (per Alle Pakete: Ja).")
                    else:
                        self.logger.info("FileZilla: Installation ohne Buchhaltungs-Markierung (manuell oder anderer Auswahlweg).")
                if mode == "install" and InstallerService.is_reboot_pending():
                    self.logger.warning("Reboot Pending erkannt: Neustart empfohlen.")
                    self.ui_queue.put(("hint", "Neustart empfohlen"))
                    if mandatory:
                        self.ui_queue.put(("warning", "Neustart empfohlen. Pflichtlauf wird trotzdem fortgesetzt."))
                if mode in ("install", "remove"):
                    self._queue_status("Installationsstatus wird aktualisiert...")
                    fresh = self.scanner.scan()
                    self.current_states.update(fresh)
                    for key in keys:
                        if key in fresh:
                            self.ui_queue.put(("software_row", (key, fresh[key])))
                if mode == "install" and mandatory:
                    missing_sources = self.installer.precheck_sources(keys, self.runtime.internal_installers)
                    if missing_sources:
                        self.logger.warning("Mandatory Source-Check: %s Quelle(n) fehlen.", len(missing_sources))
                        self.ui_queue.put(
                            (
                                "warning",
                                f"Mandatory Source-Check: {len(missing_sources)} Quelle(n) fehlen. Diese Eintraege werden als 'Quelle erforderlich' markiert, falls keine Providerquelle verfuegbar ist.",
                            )
                        )
                rows = (
                    self.installer.process(
                        keys,
                        self.current_states,
                        status_callback,
                        progress_callback,
                        item_start_callback=item_start_callback,
                        resolution_callback=resolution_callback,
                        dry_run=dry,
                        internal_installers=self.runtime.internal_installers,
                        download_progress_callback=download_progress_callback,
                        activity_callback=activity_callback,
                    )
                    if mode == "install"
                    else self.uninstaller.process(
                        keys,
                        self.current_states,
                        status_callback,
                        progress_callback,
                        item_start_callback=item_start_callback,
                        dry_run=dry,
                        scanner=self.scanner,
                        software_providers=self.runtime.software_providers,
                        method_progress_callback=method_progress_callback,
                    )
                )
                if mode == "remove" and not dry:
                    rows = run_residue_cleanup_phase(
                        rows,
                        self.runtime.software_providers,
                        lambda k: SOFTWARE_BY_KEY[k].display_name if k in SOFTWARE_BY_KEY else k,
                        self.logger,
                        self.ui_queue,
                    )
                self.last_report_file = self.report_writer.write_report(rows, "install_report")
                if mode == "remove" and not dry:
                    self._queue_status("Installationsstatus wird aktualisiert...")
                    fresh_remove = self.scanner.scan()
                    self.current_states.update(fresh_remove)
                    for rk in keys:
                        if rk in fresh_remove:
                            self.ui_queue.put(("software_row", (rk, fresh_remove[rk])))
                pending_after = any((r.reboot_required or "no").lower() == "yes" for r in rows)
                if pending_after:
                    self.ui_queue.put(("hint", "Neustart empfohlen"))
                self.logger.info("Vorgang abgeschlossen.")
                self._queue_status("OK - Vorgang abgeschlossen.")
                summary = self._install_summary_message(rows, dry, mandatory=mandatory, operation=mode)
                outro = (
                    "Bitte unnötige Installationsdateien oder Downloads löschen und installierte Programme kurz testen."
                    if mode == "install"
                    else "Bitte entfernte Programme stichprobenartig prüfen."
                )
                full_summary = (
                    "OK - Vorgang abgeschlossen.\n\n"
                    f"{summary}\n\n"
                    f"{outro}"
                )
                self.ui_queue.put(
                    (
                        "summary_dialog",
                        {
                            "title": (
                                "Abschluss — Installation (Dry-Run)"
                                if dry and mode == "install"
                                else "Abschluss — Entfernen (Dry-Run)"
                                if dry and mode == "remove"
                                else "Abschluss — Installation"
                                if mode == "install"
                                else "Abschluss — Entfernen"
                            ),
                            "summary": full_summary,
                            "entries": rows,
                            "report_csv": self.last_report_file,
                            "dry_run": dry,
                            "kind": "install",
                        },
                    )
                )
                self.ui_queue.put(("refresh_history", None))
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.exception("Fehler im Vorgang (%s): %s", mode, exc)
                self._queue_status("Fehler im Vorgang.")
            finally:
                self._operation_lock.release()
                self.ui_queue.put(("enable_actions", True))

        threading.Thread(target=worker, daemon=True).start()

    def _save_log(self) -> None:
        target = filedialog.asksaveasfilename(
            title="Log speichern",
            defaultextension=".log",
            filetypes=[("Logdateien", "*.log"), ("Alle Dateien", "*.*")],
        )
        if not target:
            return
        shutil.copy2(self.log_file, Path(target))
        messagebox.showinfo(APP_NAME, f"Log gespeichert:\n{target}")

    def _persist_local_source_settings(self, path: str | None = None) -> None:
        cfg = get_config_dict(self.logger)
        raw = cfg.get("local_source", {})
        local_cfg = raw if isinstance(raw, dict) else {}
        if path is not None:
            local_cfg["last_path"] = path
        local_cfg["prefer_local"] = bool(self.prefer_local_var.get())
        cfg["local_source"] = local_cfg
        save_config_dict(cfg, self.logger)
        self.installer.prefer_local_source = bool(self.prefer_local_var.get())

    def _select_local_source(self) -> None:
        selected = filedialog.askdirectory(title="Installationsquelle wählen")
        if not selected:
            return
        self.local_source_path_var.set(f"Aktive lokale Quelle: {selected}")
        self._persist_local_source_settings(selected)
        self._scan_local_source()

    def _scan_local_source(self) -> None:
        text = self.local_source_path_var.get().replace("Aktive lokale Quelle:", "", 1).strip()
        if text == "keine" or not text:
            messagebox.showwarning(APP_NAME, "Bitte zuerst eine lokale Quelle wählen.")
            return
        if not self.local_source.validate_path(text):
            messagebox.showwarning(APP_NAME, f"Lokale Quelle nicht erreichbar:\n{text}")
            return
        count = self.local_source.scan_source(text)
        self._persist_local_source_settings(text)
        self.logger.info("Lokale Quelle gescannt: %s Treffer", count)
        self.hint_line.configure(text=f"Lokale Quelle aktiv: {text} ({count} Dateien)")

    def _open_report_folder(self) -> None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.last_report_file if self.last_report_file and self.last_report_file.exists() else REPORT_DIR))  # type: ignore[attr-defined]

    def _open_reports_dir(self) -> None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(REPORT_DIR))  # type: ignore[attr-defined]

    def _open_report_path(self, path: Path) -> None:
        if path.exists():
            os.startfile(str(path))  # type: ignore[attr-defined]

    def _refresh_report_history(self) -> None:
        if self.history_scroll is None:
            return
        for child in self.history_scroll.winfo_children():
            child.destroy()
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        merged = list(REPORT_DIR.glob("*.csv")) + list(REPORT_DIR.glob("*.pdf"))
        files = sorted(merged, key=lambda p: p.stat().st_mtime, reverse=True)[:10]
        if not files:
            ctk.CTkLabel(self.history_scroll, text="Noch keine Reports (CSV/PDF).").grid(row=0, column=0, sticky="w", padx=4, pady=4)
            return
        for idx, report_path in enumerate(files):
            row = ctk.CTkFrame(self.history_scroll)
            row.grid(row=idx, column=0, sticky="ew", pady=2)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=report_path.name, anchor="w").grid(row=0, column=0, sticky="ew", padx=4)
            ctk.CTkButton(row, text="Report öffnen", width=120, command=lambda p=report_path: self._open_report_path(p)).grid(
                row=0, column=1, padx=4
            )
            ctk.CTkButton(row, text="Report-Ordner öffnen", width=160, command=lambda p=report_path: self._open_report_path(p.parent)).grid(
                row=0, column=2, padx=4
            )

    def _set_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for btn in self._toolbar_buttons:
            btn.configure(state=state)
        if self.filter_segment is not None:
            self.filter_segment.configure(state=state)
        if self.search_entry is not None:
            self.search_entry.configure(state=state)

    def _setup_scroll_hover_target(self, widget, handler: Callable[[int], None]) -> None:
        widget.bind("<Enter>", lambda _e: self._set_active_wheel_handler(handler), add="+")
        widget.bind("<Leave>", lambda _e: self._clear_active_wheel_handler(handler), add="+")

    def _setup_scrollableframe_mousewheel(self, frame: ctk.CTkScrollableFrame) -> None:
        self._setup_scroll_hover_target(frame, self._software_list_wheel)
        canvas = getattr(frame, "_parent_canvas", None)
        if canvas is not None:
            self._setup_scroll_hover_target(canvas, self._software_list_wheel)

    def _set_active_wheel_handler(self, handler: Callable[[int], None]) -> None:
        self._active_wheel_handler = handler

    def _clear_active_wheel_handler(self, handler: Callable[[int], None]) -> None:
        if self._active_wheel_handler == handler:
            self._active_wheel_handler = None

    def _on_global_mousewheel(self, event) -> None:
        delta_units = int(-1 * (event.delta / 120)) if getattr(event, "delta", 0) else 0
        if delta_units == 0 or self._active_wheel_handler is None:
            return
        self._active_wheel_handler(delta_units)

    def _software_list_wheel(self, delta_units: int) -> None:
        if self.software_list_frame is None:
            return
        canvas = getattr(self.software_list_frame, "_parent_canvas", None)
        if canvas is not None:
            canvas.yview_scroll(delta_units, "units")

    def _logbox_wheel(self, delta_units: int) -> None:
        textbox = getattr(self.log_box, "_textbox", None)
        if textbox is not None:
            textbox.yview_scroll(delta_units, "units")

    def _set_row_progress_reset(self, key: str) -> None:
        bar = self.row_progress_bars.get(key)
        if bar is not None:
            bar.stop()
            bar.set(0)

    def _set_row_progress_start(self, key: str) -> None:
        bar = self.row_progress_bars.get(key)
        if bar is not None:
            bar.set(0.08)
            bar.start()

    def _set_row_progress_done(self, key: str) -> None:
        bar = self.row_progress_bars.get(key)
        if bar is not None:
            bar.stop()
            bar.set(1.0)

    def _apply_status_line(self) -> None:
        base = self._last_status_message
        parts = [base]
        act = (self._install_activity_line or "").strip()
        if act:
            parts.append(act)
        self.status_line.configure(text=" · ".join(parts))

    def _refresh_progress_count_label(self) -> None:
        done, total = self._last_prog_done, self._last_prog_total
        phase = self._progress_phase or "bearbeitet"
        cur = (self._active_item_display or "").strip()
        dl = (self._download_progress_suffix or "").strip()
        if cur:
            text = f"{done}/{total} · {cur} · {phase}"
        else:
            text = f"{done}/{total} Programme {phase}"
        if dl:
            text = f"{text} · {dl}"
        self.progress_count_label.configure(text=text)

    def _queue_status(self, text: str) -> None:
        self.ui_queue.put(("status_line", text))
        self.logger.info(text)

    def _enqueue_log(self, line: str) -> None:
        self.log_queue.put(line)

    def _poll_queues(self) -> None:
        log_burst = 0
        while log_burst < 400:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_box.insert("end", line + "\n")
            self.log_box.see("end")
            log_burst += 1

        ui_burst = 0
        max_ui_burst = 32
        while ui_burst < max_ui_burst:
            try:
                action, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            ui_burst += 1

            if action == "status_line":
                self._last_status_message = str(payload)
                self._apply_status_line()
            elif action == "install_activity" and isinstance(payload, tuple) and len(payload) >= 2:
                dn, det = str(payload[0]), str(payload[1])
                self._install_activity_line = f"{dn}: {det}" if dn else det
                if len(payload) >= 3 and payload[2]:
                    self._progress_phase = str(payload[2])
                else:
                    self._progress_phase = "bearbeitet"
                self._apply_status_line()
                self._refresh_progress_count_label()
            elif action == "download_progress" and isinstance(payload, dict):
                read = int(payload.get("read", 0))
                if read < 0:
                    self._download_progress_suffix = ""
                else:
                    raw_total = payload.get("total")
                    total_bytes: int | None
                    if raw_total is None:
                        total_bytes = None
                    else:
                        try:
                            total_bytes = int(raw_total)
                        except (TypeError, ValueError):
                            total_bytes = None
                    if total_bytes is not None and total_bytes > 0:
                        rem = max(0.0, (total_bytes - read) / (1024 * 1024))
                        self._download_progress_suffix = f"noch ca. {rem:.1f} MB"
                    else:
                        loaded = read / (1024 * 1024)
                        self._download_progress_suffix = f"{loaded:.1f} MB"
                self._refresh_progress_count_label()
            elif action == "software_row":
                key, state = payload  # type: ignore[misc]
                self._apply_row_state(str(key), state)
                self._apply_filter()
            elif action == "row_progress_reset":
                keys = payload if isinstance(payload, list) else []
                self._active_item_display = ""
                self._install_activity_line = ""
                self._download_progress_suffix = ""
                self._apply_status_line()
                self._refresh_progress_count_label()
                for key in keys:
                    self._set_row_progress_reset(str(key))
            elif action == "row_progress_start":
                self._download_progress_suffix = ""
                k = str(payload)
                pkg = SOFTWARE_BY_KEY.get(k)
                self._active_item_display = pkg.display_name if pkg else k
                self._apply_status_line()
                self._refresh_progress_count_label()
                self._set_row_progress_start(k)
            elif action == "resolved_source" and isinstance(payload, dict):
                update_provider_resolved_source(
                    str(payload.get("key", "")),
                    choco_package=str(payload.get("choco") or "") or None,
                    winget_id=str(payload.get("winget") or "") or None,
                    logger=self.logger,
                )
            elif action == "progress":
                if isinstance(payload, dict):
                    self._download_progress_suffix = ""
                    self._apply_status_line()
                    frac = float(payload.get("frac", 0.0))
                    done = int(payload.get("done", 0))
                    total = int(payload.get("total", 0))
                    self._last_prog_done = done
                    self._last_prog_total = total
                    self.progress.set(frac)
                    self._refresh_progress_count_label()
                else:
                    self.progress.set(float(payload))
            elif action == "warning":
                # Nicht blockierend: sonst bleibt "Health Check: wird ausgeführt …" stehen,
                # bis der User ein evtl. verdecktes Dialogfenster schließt.
                warn_msg = str(payload)

                def _show_warn(m: str = warn_msg) -> None:
                    try:
                        messagebox.showwarning(APP_NAME, m, parent=self)
                    except Exception:  # pragma: no cover
                        self.logger.warning("Startup-Warnung (Dialog fehlgeschlagen): %s", m)

                self.after(0, _show_warn)
            elif action == "done_info":
                info_msg = str(payload)

                def _show_info(m: str = info_msg) -> None:
                    try:
                        messagebox.showinfo(APP_NAME, m, parent=self)
                    except Exception:  # pragma: no cover
                        self.logger.info("Info (Dialog fehlgeschlagen): %s", m)

                self.after(0, _show_info)
            elif action == "residue_cleanup_dialog" and isinstance(payload, dict):
                self._run_residue_cleanup_dialog_payload(payload)
            elif action == "summary_dialog" and isinstance(payload, dict):
                self._show_completion_dialog(payload)
            elif action == "energy_screensaver_done" and isinstance(payload, SystemActionResult):
                self.after(0, lambda r=payload: self._energy_screensaver_finished(r))
            elif action == "bootstrap_done" and isinstance(payload, dict):
                self._set_actions_enabled(True)
                self._refresh_provider_quick_panel()
                ok = bool(payload.get("ok"))
                title = str(payload.get("title", "Installation"))
                detail = str(payload.get("detail", "")).strip()
                msg = detail if detail else ("OK" if ok else "Keine Details")
                if len(msg) > 1800:
                    msg = msg[:1800] + "\n…"

                def _show_bootstrap(o: bool = ok, t: str = title, m: str = msg) -> None:
                    if o:
                        messagebox.showinfo(APP_NAME, f"{t}: abgeschlossen.\n\n{m}", parent=self)
                    else:
                        messagebox.showerror(APP_NAME, f"{t}: fehlgeschlagen oder nicht verifizierbar.\n\n{m}", parent=self)

                self.after(0, _show_bootstrap)
            elif action == "enable_actions":
                if bool(payload):
                    self._install_activity_line = ""
                    self._active_item_display = ""
                    self._download_progress_suffix = ""
                    self._apply_status_line()
                    self._refresh_progress_count_label()
                self._set_actions_enabled(bool(payload))
            elif action == "hint":
                self.hint_line.configure(text=str(payload))
            elif action == "refresh_history":
                self._refresh_report_history()
            elif action == "health_ui":
                data = payload  # type: ignore[assignment]
                passed = bool(data.get("passed"))
                admin = bool(data.get("admin"))
                choco = bool(data.get("choco"))
                choco_ver = str(data.get("choco_ver", "—"))
                net = bool(data.get("network"))
                err_n = int(data.get("err_n", 0))
                warn_n = int(data.get("warn_n", 0))
                hint_n = int(data.get("hint_n", 0))
                if passed:
                    main = "Health Check Passed (configuration hints)" if hint_n else "Health Check Passed"
                else:
                    main = "Warnings detected"
                self.health_main_label.configure(
                    text=(
                        f"{main}  |  Admin: {'Ja' if admin else 'Nein'}  |  Chocolatey: {'Ja' if choco else 'Nein'} ({choco_ver})  |  "
                        f"Netzwerk: {'Ja' if net else 'Nein'}  |  Fehler: {err_n}  |  Warnungen: {warn_n}  |  Konfig-Hinweise: {hint_n}"
                    )
                )
                d1 = str(data.get("details", ""))
                d2 = str(data.get("hints", ""))
                d3 = str(data.get("warnings", ""))
                self._set_health_caption(self.health_details_label, d1.strip() or "—")
                self._set_health_caption(self.health_warnings_label, d3.strip() or "Keine Warnungen.")
                self._set_health_caption(self.health_config_label, d2.strip() or "Keine Konfigurationshinweise.")
            elif action == "system_tools_output":
                lines: list[str]
                tab_name = ""
                ok = True
                if isinstance(payload, dict):
                    raw_lines = payload.get("lines")
                    lines = raw_lines if isinstance(raw_lines, list) else [str(raw_lines)]
                    tab_name = str(payload.get("tab") or "")
                    ok = bool(payload.get("ok", True))
                else:
                    lines = payload if isinstance(payload, list) else [str(payload)]
                if self.system_tools_output_box is not None:
                    self.system_tools_output_box.insert("end", "\n".join(str(x) for x in lines) + "\n")
                    self.system_tools_output_box.see("end")
                if tab_name:
                    self._set_system_tab_status(tab_name, "bereit" if ok else "fehler")

        if ui_burst >= max_ui_burst:
            self.update_idletasks()
            self.after(1, self._poll_queues)
        else:
            self.after(40, self._poll_queues)
