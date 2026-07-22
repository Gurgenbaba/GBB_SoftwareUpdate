from __future__ import annotations

import ctypes
import os
import platform
import queue
import sys
import shutil
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tkinter import Menu, PhotoImage, filedialog, messagebox
from typing import Callable

import customtkinter as ctk
from PIL import Image, ImageTk

from .choco import ChocoClient
from .config import APP_DIR, APP_NAME, BUNDLE_DIR, EXE_PARENT, REPORT_DIR, SOFTWARE_BY_KEY, SOFTWARE_CATALOG
from .enterprise import apply_choco_ghost_cleanup_after_uninstall, try_restore_point_or_registry_export
from .health import run_self_health_check
from .installers import InstallerService
from .local_source import LocalSourceService
from .office_uninstall import office_removal_tools_available
from .uninstallers import UninstallerService
from .winget import WingetService
from .json_config import (
    CONFIG_JSON_PATH,
    add_custom_software,
    default_software_providers,
    get_config_dict,
    load_runtime_settings,
    remove_custom_software,
    save_config_dict,
    set_builtin_software_enabled,
    update_provider_resolved_source,
)
from .logger import build_logger
from .models import ReportEntry, SoftwareState
from .reporting import ReportWriter, pdf_export_available, write_html_report, write_summary_pdf
from .result_normalization import dialog_detail_lines, format_install_summary_lines, format_scan_summary_lines
from .residue_cleanup import apply_selected_cleanup, run_residue_cleanup_phase
from .scanner import SoftwareScanner
from .system_settings import (
    BatteryChargeCapability,
    DEFAULT_SYSTEM_SETTINGS,
    DEFAULT_UI_COLUMNS,
    MIN_UI_COLUMNS,
    SettingState,
    UI_COLUMN_KEYS,
    ToggleReadResult,
    apply_adaptive_brightness_state,
    apply_display_timeout,
    apply_lid_close_action,
    apply_power_button_action,
    apply_power_mode,
    apply_sleep_timeout,
    apply_show_battery_percent_state,
    apply_screensaver_state,
    apply_usb_power_saving_state,
    coerce_system_settings,
    format_energy_status_rows,
    merge_ui_columns,
    read_battery_charge_limit_capability,
    read_battery_percent,
    read_battery_saver_state,
    read_dark_mode,
    read_display_timeout,
    read_lid_close_action,
    read_power_button_action,
    read_power_mode_ac_dc,
    read_screensaver_state,
    read_show_battery_percent_state,
    read_sleep_timeout,
    read_adaptive_brightness_state,
    read_usb_power_saving_state,
    apply_dark_mode,
    read_show_file_extensions,
    apply_show_file_extensions,
)
from .system_tools import SystemActionResult, SystemToolsService


class UpdaterApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self._apply_display_scaling()
        # Palette aus compexx-finanz.de abgeleitet: dunkles Marken-Navy, Marken-Blau als
        # Interaktionsfarbe, Gold als Zweitakzent, Rot/Farbverlaufs-Cyan aus dem Farbstreifen-Logo.
        self.ogx_colors = {
            "bg":           "#111827",
            "panel":        "#14313F",
            "panel_alt":    "#1B3B4A",
            "table":        "#1F4356",
            "table_alt":    "#274F63",
            "border":       "#2E5470",
            "text":         "#F8FAFC",
            "muted":        "#B7C4D1",
            "accent":       "#0D6EFD",   # compexx Marken-Blau
            "accent_hover": "#3D8BFD",
            "gold":         "#F1A71E",   # compexx Gold-Zweitakzent
            "gold_hover":   "#F5BB4A",
            "cyan":         "#4AA8CC",
            "danger":       "#DC2626",
            "danger_hover": "#B91C1C",
        }
        self._resize_after_id: str | None = None
        self.configure(fg_color=self.ogx_colors["bg"])
        self._apply_window_size()

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.logger, self.log_file = build_logger(self._enqueue_log)
        self.runtime = load_runtime_settings(self.logger)
        self.logger.info("Laufzeitkonfiguration: %s", CONFIG_JSON_PATH)
        _is_packaged = getattr(sys, "frozen", False)
        _is_onedir = _is_packaged and (EXE_PARENT / "_internal").is_dir()
        _build_mode = "onedir" if _is_onedir else ("onefile" if _is_packaged else "source")
        _version = "unbekannt"
        try:
            _ver_file = BUNDLE_DIR / "VERSION.txt"
            if _ver_file.is_file():
                _version = _ver_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        self.logger.info(
            "[STARTUP] app=%s version=%s packaged=%s build=%s dry_run=%s",
            APP_NAME, _version, _is_packaged, _build_mode,
            getattr(self.runtime, "dry_run_default", False),
        )
        if getattr(self.runtime, "dry_run_default", False):
            self.dry_run_var.set(True)

        self.choco = ChocoClient(self.logger)
        self.winget = WingetService(self.logger)
        self.scanner = SoftwareScanner(
            self.choco,
            self.winget,
            self.logger,
            self.runtime.visible_catalog,
            software_providers=self.runtime.software_providers,
        )
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
            local_source_strict=self.runtime.local_source_strict,
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
            office_tools=self.runtime.office_tools,
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
        self.app_icon_image: PhotoImage | None = None
        self._progress_phase = ""
        self._last_status_message = "Bereit"
        self._download_progress_suffix = ""
        self._filezilla_buchhaltung: bool | None = None
        self._install_activity_line = ""
        self._active_item_display = ""
        self._last_prog_done = 0
        self._last_prog_total = 0
        self.filter_segment: ctk.CTkSegmentedButton | None = None  # kept for compat; use _active_filter_mode
        self._active_filter_mode: str = "Alle"
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
        self.add_software_btn: ctk.CTkButton | None = None
        self.system_tools_btn: ctk.CTkButton | None = None
        self.system_tools_output_box: ctk.CTkTextbox | None = None
        self.system_tools_tabview: ctk.CTkTabview | None = None
        self.system_tools_status_labels: dict[str, ctk.CTkLabel] = {}
        self._device_settings_output_box: ctk.CTkTextbox | None = None
        self._device_settings_tabview: ctk.CTkTabview | None = None
        self._device_settings_status_labels: dict[str, ctk.CTkLabel] = {}
        self._tooltip_toplevel: ctk.CTkToplevel | None = None
        self._tooltip_label: ctk.CTkLabel | None = None
        self._tooltip_after_id: str | None = None
        init_local = self.runtime.local_source_last_path or "keine"
        self.local_source_path_var = ctk.StringVar(value=f"Aktive lokale Quelle: {init_local}")
        self.prefer_local_var = ctk.BooleanVar(value=self.runtime.local_source_prefer_local)
        self.software_list_frame: ctk.CTkScrollableFrame | None = None
        self.history_frame: ctk.CTkFrame | None = None
        self.filter_bar_frame: ctk.CTkFrame | None = None
        self.log_frame: ctk.CTkFrame | None = None
        self.quick_panel_label: ctk.CTkLabel | None = None
        self.local_source_path_label: ctk.CTkLabel | None = None
        self.install_choco_btn: ctk.CTkButton | None = None
        self.install_winget_btn: ctk.CTkButton | None = None
        self.energy_screensaver_btn: ctk.CTkButton | None = None
        self.device_settings_btn: ctk.CTkButton | None = None
        self._toolbar_buttons: list[ctk.CTkButton] = []
        self._search_heading: ctk.CTkLabel | None = None
        self._scale_baseline: float = 1.0
        self._active_wheel_handler: Callable[[int], None] | None = None
        self._layout_warmup_tries = 0
        self._ui_column_widths: dict[str, int] = merge_ui_columns(get_config_dict(self.logger).get("ui_columns"))
        self._col_resize_drag: tuple[str, str, int] | None = None
        # Sidebar-Navigation
        self.sidebar: ctk.CTkFrame | None = None
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self._pages: dict[str, ctk.CTkFrame] = {}
        self._active_page: str = "software"
        self._system_overview: ctk.CTkFrame | None = None
        self._system_detail: ctk.CTkFrame | None = None
        self.software_count_badge: ctk.CTkLabel | None = None
        self.bulk_selection_label: ctk.CTkLabel | None = None
        self.log_drawer_toggle_btn: ctk.CTkButton | None = None
        self._log_drawer_expanded = False
        self._energy_value_labels: dict[str, ctk.CTkLabel] = {}
        self._energy_status_pills: dict[str, ctk.CTkLabel] = {}
        self._energy_action_buttons: dict[str, ctk.CTkButton] = {}
        self._battery_guidance_label: ctk.CTkLabel | None = None
        self.program_name_labels: dict[str, ctk.CTkLabel] = {}
        self.checkbox_widgets: dict[str, ctk.CTkCheckBox] = {}
        self.row_progress_cells: dict[str, ctk.CTkFrame] = {}
        self.progress_cell_frame_header: ctk.CTkFrame | None = None
        self._header_grip_widgets: list[ctk.CTkFrame] = []
        self._header_cell_frames: dict[str, ctk.CTkFrame] = {}
        self._row_cell_frames: dict[str, dict[str, ctk.CTkFrame]] = {}
        self._filter_count_labels: dict[str, ctk.CTkLabel] = {}

        self._ensure_runtime_catalog()
        self._apply_title()
        self._build_layout()
        # Kein self._apply_ogx_style(self) hier: die rekursive Generic-Styling-Passage würde alle
        # bewusst unterschiedlich eingefärbten Buttons (Sidebar-Nav, Gold/Danger-Karten, Chips) auf
        # eine einheitliche Akzentfarbe zurücksetzen. _build_layout färbt bereits alles explizit ein;
        # _apply_ogx_style(dialog) bleibt für die separat geöffneten Dialoge unverändert im Einsatz.
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

        # CustomTkinter skaliert Breite/Hoehe in geometry() intern mit dem window_scaling-Faktor
        # (siehe set_window_scaling() in _apply_display_scaling()), laesst x/y dabei aber
        # unveraendert durch. Berechnen wir x/y anhand der UNskalierten width/height, landet das
        # tatsaechlich gerenderte (skalierte) Fenster je nach Skalierungsfaktor sichtbar
        # rechts/unten neben der Bildschirmmitte. Deshalb hier mit der tatsaechlichen (skalierten)
        # Fenstergroesse zentrieren, nicht mit der Rohgroesse, die an geometry() uebergeben wird.
        scale = getattr(self, "_scale_baseline", 1.0) or 1.0
        rendered_w = round(width * scale)
        rendered_h = round(height * scale)
        x = max((screen_w - rendered_w) // 2, 0)
        y = max((screen_h - rendered_h) // 2, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")
        # Unterhalb dieser Größe wird die Liste scrollbar — kein „Mini-Fenster“-Start mehr.
        self.minsize(960, 640)
        # Ohne explizites maxsize() begrenzt Tk unter Windows die maximale Fenstergroesse (per
        # WM_GETMINMAXINFO) implizit auf die aktuell "angeforderte" Groesse (im Wesentlichen das,
        # was geometry() oben gesetzt hat) — SW_MAXIMIZE/state("zoomed") kann das Fenster dann NICHT
        # groesser als diese Groesse ziehen, selbst wenn Windows den Vorgang als erfolgreich meldet.
        # Deshalb hier grosszuegig ueber die Bildschirmgroesse hinaus freigeben.
        self.maxsize(screen_w + 200, screen_h + 200)
        # App soll beim Start immer maximiert erscheinen. Tk's eigenes state("zoomed") wird in der
        # per PyInstaller gebauten EXE zuverlaessig ignoriert (vermutlich Zusammenspiel aus
        # CustomTkinter-Skalierung und Timing beim ersten Map des Fensters) — deshalb stattdessen
        # direkt per Win32 ShowWindow(SW_MAXIMIZE) auf das echte Fenster-Handle, das ist
        # zuverlaessiger als der Tk-Layer. Mehrere verzoegerte Versuche, falls das Fenster beim
        # ersten Versuch noch nicht vollstaendig gemappt ist.
        #
        # Wichtig: ctypes.windll.user32.GetParent liefert ohne explizites restype einen 32-Bit
        # "int" zurueck. Auf 64-Bit-Python wird das echte (64-Bit-)Fenster-Handle dabei
        # stillschweigend abgeschnitten/verfaelscht -> ShowWindow bekommt ein ungueltiges Handle
        # und tut nichts, ohne einen Fehler zu werfen. Deshalb restype/argtypes explizit als
        # c_void_p deklarieren.
        get_parent = ctypes.windll.user32.GetParent
        get_parent.restype = ctypes.c_void_p
        get_parent.argtypes = [ctypes.c_void_p]
        show_window = ctypes.windll.user32.ShowWindow
        show_window.restype = ctypes.c_bool
        show_window.argtypes = [ctypes.c_void_p, ctypes.c_int]

        def _maximize(attempt: int = 0) -> None:
            try:
                self.update_idletasks()
                hwnd = get_parent(ctypes.c_void_p(self.winfo_id()))
                if not hwnd:
                    hwnd = self.winfo_id()
                sw_maximize = 3
                show_window(ctypes.c_void_p(hwnd), sw_maximize)
            except Exception:  # pragma: no cover - Plattform-/Umgebungsabhaengig
                pass
            if attempt < 3:
                self.after(150, lambda: _maximize(attempt + 1))

        self.after(50, _maximize)

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
        self.grid_rowconfigure(1, weight=1)

        # Marken-Farbstreifen (compexx-finanz.de Farbverlauf) als dekorativer Kopfbalken.
        brand_stripe = ctk.CTkFrame(self, fg_color="transparent", height=6)
        brand_stripe.pack_propagate(False)
        brand_stripe.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 0))
        for stripe_color in (
            "#9FD6F5", "#4A9ECD", "#3F68AE", "#3C4D9D", "#3F2B74",
            "#6B205B", "#A0326A", "#A62B27", "#DF643A", "#E4803A", "#F6C452",
        ):
            ctk.CTkFrame(brand_stripe, fg_color=stripe_color, corner_radius=0).pack(
                side="left", fill="both", expand=True
            )

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(4, 4))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self._build_sidebar(body)

        self.page_host = ctk.CTkFrame(body, fg_color="transparent")
        self.page_host.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self.page_host.grid_columnconfigure(0, weight=1)
        self.page_host.grid_rowconfigure(0, weight=1)

        self._build_software_page(self.page_host)
        self._build_system_page(self.page_host)
        self._build_setup_page(self.page_host)
        self._build_reports_page(self.page_host)
        self._build_settings_page(self.page_host)
        self._switch_page("software")

        bottom_frame = ctk.CTkFrame(self)
        bottom_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
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
        # Kein _apply_responsive_layout hier: winfo ist oft 1×1 vor dem Map → falsche Spaltenlogik.

    def _build_sidebar(self, parent) -> None:
        sidebar = ctk.CTkFrame(parent, corner_radius=10, width=208, fg_color=self.ogx_colors["panel"])
        self.sidebar = sidebar
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)

        logo_path = EXE_PARENT / "assets" / "logo.png"
        if not logo_path.exists():
            logo_path = BUNDLE_DIR / "assets" / "logo.png"
        if logo_path.exists():
            try:
                pil_logo = Image.open(logo_path).convert("RGBA")
                pil_logo.thumbnail((168, 50), Image.Resampling.LANCZOS)
                self.logo_image = ImageTk.PhotoImage(pil_logo)
                ctk.CTkLabel(sidebar, text="", image=self.logo_image).pack(anchor="w", padx=14, pady=(16, 4))
            except Exception as exc:  # pragma: no cover
                self.logger.warning("Logo konnte nicht geladen werden: %s", exc)
        else:
            self.logger.info("Kein assets/logo.png gefunden; UI-Logo wird uebersprungen.")
            ctk.CTkLabel(sidebar, text=APP_NAME, font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=14, pady=(16, 4))

        icon_path = EXE_PARENT / "assets" / "logo.ico"
        if not icon_path.exists():
            icon_path = BUNDLE_DIR / "assets" / "logo.ico"
        if icon_path.exists():
            try:
                # Quadratisches Badge-Icon fuer Fenster/Taskleiste (separat von der breiten Wortmarke).
                pil_icon = Image.open(icon_path).convert("RGBA")
                self.app_icon_image = ImageTk.PhotoImage(pil_icon)
                self.iconphoto(True, self.app_icon_image)
            except Exception as exc:  # pragma: no cover
                self.logger.warning("Icon konnte nicht geladen werden: %s", exc)

        self.company_label = ctk.CTkLabel(
            sidebar, text=f"Company: {self.runtime.company_name or 'Nicht gesetzt'}",
            font=ctk.CTkFont(size=10), text_color=self.ogx_colors["muted"], anchor="w",
        )
        self.company_label.pack(anchor="w", padx=14, pady=(0, 12))

        nav_font = ctk.CTkFont(size=12, weight="bold")
        for key, label in (
            ("software", "Software"),
            ("system", "System & Geräte"),
            ("setup", "Setup"),
            ("reports", "Berichte & Verlauf"),
        ):
            btn = ctk.CTkButton(
                sidebar, text=label, font=nav_font, anchor="w", height=34, corner_radius=7,
                fg_color="transparent", text_color=self.ogx_colors["muted"],
                hover_color=self.ogx_colors["panel_alt"],
                command=lambda k=key: self._switch_page(k),
            )
            btn.pack(fill="x", padx=8, pady=1)
            self._nav_buttons[key] = btn
        self.software_count_badge = self._nav_buttons["software"]

        sep = ctk.CTkFrame(sidebar, height=1, fg_color=self.ogx_colors["border"])
        sep.pack(fill="x", padx=12, pady=8)
        for label, cmd in (("Einstellungen", lambda: self._switch_page("settings")), ("About", self._show_about_dialog)):
            ctk.CTkButton(
                sidebar, text=label, font=nav_font, anchor="w", height=34, corner_radius=7,
                fg_color="transparent", text_color=self.ogx_colors["muted"],
                hover_color=self.ogx_colors["panel_alt"], command=cmd,
            ).pack(fill="x", padx=8, pady=1)

        spacer = ctk.CTkFrame(sidebar, fg_color="transparent")
        spacer.pack(fill="both", expand=True)

        self.dry_run_var = ctk.BooleanVar(value=False)
        self.mandatory_install_var = ctk.BooleanVar(value=False)
        opts_row = ctk.CTkFrame(sidebar, fg_color="transparent")
        opts_row.pack(fill="x", padx=12, pady=(0, 8))
        dry_run_cb = ctk.CTkCheckBox(
            opts_row, text="Testmodus / Dry-Run", variable=self.dry_run_var, font=ctk.CTkFont(size=10),
            fg_color=self.ogx_colors["accent"], hover_color=self.ogx_colors["accent_hover"],
        )
        mandatory_cb = ctk.CTkCheckBox(
            opts_row, text="Pflichtsoftware erzwingen", variable=self.mandatory_install_var, font=ctk.CTkFont(size=10),
            fg_color=self.ogx_colors["accent"], hover_color=self.ogx_colors["accent_hover"],
        )
        dry_run_cb.pack(anchor="w", pady=2)
        mandatory_cb.pack(anchor="w", pady=2)
        self._attach_tooltip(dry_run_cb, "Führt alle Schritte nur als Simulation aus, ohne echte Installation oder Deinstallation.")
        self._attach_tooltip(mandatory_cb, "Installiert alle aktivierten Standardprogramme im Best-Effort-Modus.")

        provider_card = ctk.CTkFrame(sidebar, fg_color=self.ogx_colors["panel_alt"], corner_radius=8)
        provider_card.pack(fill="x", padx=10, pady=(0, 12))
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
            provider_card, text=provider_text, justify="left", anchor="w",
            wraplength=176, font=ctk.CTkFont(size=10),
        )
        self.quick_panel_label.pack(fill="x", padx=10, pady=10)

    def _switch_page(self, name: str) -> None:
        self._active_page = name
        for key, page in self._pages.items():
            if key == name:
                page.grid()
            else:
                page.grid_remove()
        for key, btn in self._nav_buttons.items():
            active = key == name
            btn.configure(
                fg_color=self.ogx_colors["accent"] if active else "transparent",
                text_color=self.ogx_colors["text"] if active else self.ogx_colors["muted"],
            )

    def _build_software_page(self, parent) -> None:
        page = ctk.CTkFrame(parent, fg_color="transparent")
        self._pages["software"] = page
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(3, weight=1, minsize=200)

        # --- Health Summary ---
        self.health_frame = ctk.CTkFrame(page, corner_radius=10)
        self.health_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.health_frame.grid_columnconfigure(0, weight=1)
        self.health_frame.grid_columnconfigure(1, weight=0)
        self.health_frame.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(self.health_frame, text="Health Summary", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(6, 2)
        )
        self.health_toggle_btn = ctk.CTkButton(
            self.health_frame, text="Details anzeigen", width=118, height=22,
            font=ctk.CTkFont(size=11), command=self._toggle_health_details,
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
            self.health_scroll, text="—", anchor="w", justify="left", wraplength=1220, font=cap,
            text_color=self.ogx_colors["text"],
        )
        self.health_details_label.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 10))
        ctk.CTkLabel(self.health_scroll, text="Warnungen", font=ctk.CTkFont(weight="bold")).grid(row=2, column=0, sticky="w", padx=4, pady=(0, 2))
        self.health_warnings_label = ctk.CTkLabel(
            self.health_scroll, text="", anchor="w", justify="left", wraplength=1220, font=cap,
            text_color=self.ogx_colors["text"],
        )
        self.health_warnings_label.grid(row=3, column=0, sticky="ew", padx=4, pady=(0, 10))
        ctk.CTkLabel(self.health_scroll, text="Konfiguration", font=ctk.CTkFont(weight="bold")).grid(row=4, column=0, sticky="w", padx=4, pady=(0, 2))
        self.health_config_label = ctk.CTkLabel(
            self.health_scroll, text="", anchor="w", justify="left", wraplength=1220, font=cap,
            text_color=self.ogx_colors["text"],
        )
        self.health_config_label.grid(row=5, column=0, sticky="ew", padx=4, pady=(0, 4))
        self._set_health_details_visible(False)

        # --- Filter/Suche-Zeile ---
        filter_bar = ctk.CTkFrame(page, fg_color="transparent", height=32)
        filter_bar.pack_propagate(False)
        self.filter_bar_frame = filter_bar
        filter_bar.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        _fbf = ctk.CTkFont(size=10)
        _py = 5
        for _mode in ("Alle", "Updates", "Fehlend", "Fehler"):
            _is_active = _mode == "Alle"
            _btn = ctk.CTkButton(
                filter_bar, text=_mode, width=76, height=22, font=_fbf,
                fg_color=self.ogx_colors["accent"] if _is_active else self.ogx_colors["panel_alt"],
                hover_color=self.ogx_colors["accent_hover"] if _is_active else self.ogx_colors["border"],
                command=lambda m=_mode: self._on_filter_change(m),
            )
            _btn.pack(side="left", padx=(0, 2), pady=_py)
            self._filter_count_labels[_mode] = _btn
        _sep = ctk.CTkFrame(filter_bar, width=1, height=20, fg_color=self.ogx_colors["border"])
        _sep.pack(side="left", padx=(6, 6), pady=_py)
        self._search_heading = ctk.CTkLabel(filter_bar, text="Suche", font=_fbf)
        self._search_heading.pack(side="left", padx=(0, 4), pady=_py)
        self.search_entry = ctk.CTkEntry(
            filter_bar, textvariable=self.search_var, placeholder_text="Programmname ...", height=22, font=_fbf, width=160
        )
        self.search_entry.pack(side="left", pady=_py)
        _sep2 = ctk.CTkFrame(filter_bar, width=1, height=20, fg_color=self.ogx_colors["border"])
        _sep2.pack(side="left", padx=(8, 6), pady=_py)
        reset_cols = ctk.CTkButton(
            filter_bar, text="Spalten: Standard", width=118, height=22, font=_fbf,
            command=self._reset_ui_columns_to_defaults,
        )
        reset_cols.pack(side="left", pady=_py)
        self._attach_tooltip(reset_cols, "Setzt alle Spaltenbreiten auf die Standardwerte aus der Konfiguration.")

        # --- Auswahl-Helfer ---
        quick_row = ctk.CTkFrame(page, fg_color="transparent", height=32)
        quick_row.pack_propagate(False)
        quick_row.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        _qbtn_kw = {"height": 24, "font": ctk.CTkFont(size=10), "corner_radius": 999}
        all_btn = ctk.CTkButton(
            quick_row, text="Alle auswählen", command=self._select_all,
            fg_color="transparent", border_width=1, border_color=self.ogx_colors["border"],
            text_color=self.ogx_colors["muted"], hover_color=self.ogx_colors["panel_alt"], **_qbtn_kw,
        )
        self._attach_tooltip(all_btn, "Markiert alle aktuell sichtbaren Programme in der Liste.")
        self.select_missing_btn = ctk.CTkButton(
            quick_row, text="Nur fehlend", command=self._select_missing_only,
            fg_color="transparent", border_width=1, border_color=self.ogx_colors["border"],
            text_color=self.ogx_colors["muted"], hover_color=self.ogx_colors["panel_alt"], **_qbtn_kw,
        )
        self.select_updates_btn = ctk.CTkButton(
            quick_row, text="Nur Updates", command=self._select_updates_only,
            fg_color="transparent", border_width=1, border_color=self.ogx_colors["border"],
            text_color=self.ogx_colors["muted"], hover_color=self.ogx_colors["panel_alt"], **_qbtn_kw,
        )
        self.patch_run_btn = ctk.CTkButton(
            quick_row, text="Standard Patch Run", command=self._standard_patch_run,
            fg_color="transparent", border_width=1, border_color=self.ogx_colors["gold"],
            text_color=self.ogx_colors["gold"], hover_color=self.ogx_colors["panel_alt"], **_qbtn_kw,
        )
        self._attach_tooltip(self.patch_run_btn, "Wählt automatisch Programme mit fehlender Installation oder verfügbarem Update.")
        for b in (all_btn, self.select_missing_btn, self.select_updates_btn, self.patch_run_btn):
            b.pack(side="left", padx=(0, 6))
        self.add_software_btn = ctk.CTkButton(
            quick_row, text="+ Anwendung hinzufügen", command=self._open_add_software_dialog,
            height=24, font=ctk.CTkFont(size=10, weight="bold"), corner_radius=999,
        )
        self.add_software_btn.pack(side="right")
        self._attach_tooltip(
            self.add_software_btn,
            "Fügt ein beliebiges Programm per WinGet-ID/Chocolatey-Paket zur Liste hinzu (Rechtsklick auf eine Zeile zum Entfernen).",
        )

        # --- Softwaretabelle (volle Breite) ---
        software_frame = ctk.CTkScrollableFrame(
            page, label_text="Softwarestatus",
            fg_color=self.ogx_colors["panel_alt"],
            scrollbar_button_color=self.ogx_colors["border"],
            scrollbar_button_hover_color=self.ogx_colors["accent"],
        )
        self.software_list_frame = software_frame
        software_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 6))
        software_frame.grid_columnconfigure(0, weight=1)
        self._setup_scrollableframe_mousewheel(software_frame)

        header = ctk.CTkFrame(software_frame, fg_color=self.ogx_colors["table"], height=28)
        self.software_header_frame = header
        header.pack_propagate(False)
        header.grid(row=0, column=0, sticky="ew", padx=2, pady=(0, 1))
        self._header_grip_widgets.clear()
        self._header_cell_frames.clear()
        _hf = ctk.CTkFont(size=10, weight="bold")
        _wmap = self._ui_column_widths
        _titles = {"checkbox": "", "program": "Programm", "status": "Status",
                   "provider": "Provider", "installed": "Installiert",
                   "available": "Verfügbar", "progress": "Fortschritt"}
        _grip_after = {"checkbox", "status", "provider", "installed", "available"}

        for key in UI_COLUMN_KEYS:
            title = _titles[key]
            if key == "program":
                cell = ctk.CTkFrame(header, fg_color="transparent")
                cell.pack(side="left", fill="x", expand=True)
            else:
                cell = ctk.CTkFrame(header, fg_color="transparent", width=_wmap[key], height=26)
                cell.pack_propagate(False)
                cell.pack(side="left")
            self._header_cell_frames[key] = cell
            ctk.CTkLabel(cell, text=title, font=_hf, anchor="w").pack(side="left", padx=(6 if key == "checkbox" else 4, 0))
            if key in _grip_after:
                grip = ctk.CTkFrame(cell, width=5, fg_color=self.ogx_colors["border"], corner_radius=1)
                grip.pack(side="right", fill="y", pady=4)
                self._header_grip_widgets.append(grip)
                grip.bind("<ButtonPress-1>", lambda e, lk=key: self._on_column_resize_start(e, lk, ""))
                grip.bind("<B1-Motion>", lambda e, lk=key: self._on_column_resize_motion(e, lk, ""))
                grip.bind("<ButtonRelease-1>", lambda _e: self._on_column_resize_end())
                grip.bind("<Double-Button-1>", lambda _e, lk=key: self._reset_one_column_width(lk))
                grip.bind("<Enter>", lambda _e, g=grip: g.configure(fg_color=self.ogx_colors["accent"]))
                grip.bind("<Leave>", lambda _e, g=grip: g.configure(fg_color=self.ogx_colors["border"]))
                try:
                    grip.configure(cursor="sb_h_double_arrow")
                except Exception:
                    pass

        for idx, software in enumerate(self.runtime.visible_catalog, start=1):
            self._create_software_row(software, idx)

        self._apply_software_column_widths()
        self.search_var.trace_add("write", lambda *_: self._on_search_change())
        self._sync_row_visibility()

        # --- Aktionsleiste (Bulk-Aktionen auf Auswahl) ---
        bulk_bar = ctk.CTkFrame(page, fg_color=self.ogx_colors["panel"], corner_radius=8, height=44)
        bulk_bar.pack_propagate(False)
        bulk_bar.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        self.bulk_selection_label = ctk.CTkLabel(
            bulk_bar, text="0 von 0 ausgewählt", font=ctk.CTkFont(size=11, weight="bold"),
        )
        self.bulk_selection_label.pack(side="left", padx=12)
        self.remove_btn = ctk.CTkButton(
            bulk_bar, text="Ausgewählte entfernen", command=self._remove_selected,
            fg_color=self.ogx_colors["danger"], hover_color=self.ogx_colors["danger_hover"],
            height=26, font=ctk.CTkFont(size=10),
        )
        self.remove_btn.pack(side="right", padx=(0, 10), pady=8)
        self._attach_tooltip(self.remove_btn, "Deinstalliert ausgewählte Programme inklusive Deep-Cleanup von Resten.")
        self.install_btn = ctk.CTkButton(
            bulk_bar, text="Install / Update", command=self._install_selected,
            height=26, font=ctk.CTkFont(size=10, weight="bold"),
        )
        self.install_btn.pack(side="right", padx=(0, 8), pady=8)
        self._attach_tooltip(self.install_btn, "Installiert oder aktualisiert ausgewählte Programme gemäß Provider-Kette.")
        self.check_btn = ctk.CTkButton(
            bulk_bar, text="Prüfen", command=self._scan_selected,
            fg_color=self.ogx_colors["panel_alt"], hover_color=self.ogx_colors["border"],
            height=26, font=ctk.CTkFont(size=10),
        )
        self.check_btn.pack(side="right", padx=(0, 8), pady=8)
        self._attach_tooltip(self.check_btn, "Prüft ausgewählte Programme auf installiert/Update verfügbar.")

        self._toolbar_buttons = [
            all_btn, self.check_btn, self.install_btn, self.remove_btn,
            self.select_missing_btn, self.select_updates_btn, self.patch_run_btn, self.add_software_btn,
        ]

        # --- Log-Schublade (einklappbar) ---
        drawer_head = ctk.CTkFrame(page, fg_color="transparent", height=24)
        drawer_head.pack_propagate(False)
        drawer_head.grid(row=5, column=0, sticky="ew")
        self.log_drawer_toggle_btn = ctk.CTkButton(
            drawer_head, text="▾ Log", command=self._toggle_log_drawer, anchor="w",
            fg_color="transparent", text_color=self.ogx_colors["muted"], hover_color=self.ogx_colors["panel_alt"],
            height=22, font=ctk.CTkFont(size=10, weight="bold"),
        )
        self.log_drawer_toggle_btn.pack(side="left")
        save_log_btn = ctk.CTkButton(
            drawer_head, text="Log speichern", command=self._save_log, width=110,
            fg_color="transparent", border_width=1, border_color=self.ogx_colors["border"],
            text_color=self.ogx_colors["muted"], height=20, font=ctk.CTkFont(size=9),
        )
        save_log_btn.pack(side="right")
        self._attach_tooltip(save_log_btn, "Speichert das aktuelle Laufprotokoll als Datei.")

        self.log_box = ctk.CTkTextbox(page, wrap="word", font=ctk.CTkFont(size=10), height=130)
        self.log_box.grid(row=6, column=0, sticky="ew", pady=(2, 0))
        self._setup_scroll_hover_target(self.log_box, self._logbox_wheel)
        self.log_box.grid_remove()
        self.log_drawer_toggle_btn.configure(text="▸ Log")

    def _toggle_log_drawer(self) -> None:
        self._log_drawer_expanded = not self._log_drawer_expanded
        if self._log_drawer_expanded:
            self.log_box.grid()
            self.log_drawer_toggle_btn.configure(text="▾ Log")
        else:
            self.log_box.grid_remove()
            self.log_drawer_toggle_btn.configure(text="▸ Log")

    def _update_bulk_selection_label(self) -> None:
        if self.bulk_selection_label is None:
            return
        total = len(self.checkbox_vars)
        selected = sum(1 for v in self.checkbox_vars.values() if v.get())
        self.bulk_selection_label.configure(text=f"{selected} von {total} ausgewählt")

    def _build_system_page(self, parent) -> None:
        page = ctk.CTkFrame(parent, fg_color="transparent")
        self._pages["system"] = page
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)

        overview = ctk.CTkFrame(page, fg_color="transparent")
        self._system_overview = overview
        overview.grid(row=0, column=0, sticky="nsew")
        overview.grid_columnconfigure((0, 1), weight=1)

        detail = ctk.CTkFrame(page, fg_color="transparent")
        self._system_detail = detail
        detail.grid(row=0, column=0, sticky="nsew")
        detail.grid_columnconfigure(0, weight=1)
        detail.grid_rowconfigure(1, weight=1)
        detail.grid_remove()

        def _card(title: str, desc: str, command, *, danger=False, good=False, row=0, column=0):
            c = ctk.CTkFrame(overview, fg_color=self.ogx_colors["panel"], corner_radius=10)
            c.grid(row=row, column=column, sticky="nsew", padx=6, pady=6)
            ctk.CTkLabel(c, text=title, font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=16, pady=(14, 4))
            ctk.CTkLabel(
                c, text=desc, font=ctk.CTkFont(size=11), text_color=self.ogx_colors["muted"],
                anchor="w", justify="left", wraplength=280,
            ).pack(anchor="w", padx=16, pady=(0, 12))
            fg = self.ogx_colors["danger"] if danger else ("#22C55E" if good else self.ogx_colors["accent"])
            hover = self.ogx_colors["danger_hover"] if danger else ("#16A34A" if good else self.ogx_colors["accent_hover"])
            btn = ctk.CTkButton(c, text="Öffnen", command=command, fg_color=fg, hover_color=hover, width=100)
            btn.pack(anchor="w", padx=16, pady=(0, 14))
            return btn

        self.system_tools_btn = _card(
            "Systemverwaltung",
            "Benutzerverwaltung, Profil-Migration und Windows-Updates über sichere Systemtools.",
            lambda: self._show_system_detail("tools"), danger=True, row=0, column=0,
        )
        self.energy_screensaver_btn = _card(
            "Energie & Bildschirmschoner",
            "Energie-Dashboard: Profil, Timeouts, Bildschirmschoner, empfohlenes Setup anwenden/zurücksetzen.",
            lambda: self._show_system_detail("energy"), good=True, row=0, column=1,
        )
        self.device_settings_btn = _card(
            "Geräte-Einstellungen",
            "Computernamen ändern und Windows-Updates verwalten.",
            lambda: self._show_system_detail("device"), danger=True, row=1, column=0,
        )
        if platform.system() != "Windows":
            self.energy_screensaver_btn.configure(state="disabled")

    _SYSTEM_DETAIL_TITLES = {
        "tools": "Systemverwaltung",
        "energy": "Energie & Bildschirmschoner",
        "device": "Geräte-Einstellungen",
    }

    def _show_system_detail(self, which: str) -> None:
        for child in self._system_detail.winfo_children():
            child.destroy()
        self._energy_value_labels.clear()
        self._energy_status_pills.clear()
        self._energy_action_buttons.clear()
        self._battery_guidance_label = None
        self.system_tools_output_box = None
        self.system_tools_tabview = None
        self.system_tools_status_labels = {}
        self._device_settings_output_box = None
        self._device_settings_tabview = None
        self._device_settings_status_labels = {}

        head = ctk.CTkFrame(self._system_detail, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        back_btn = ctk.CTkButton(
            head, text="← Zurück zur Übersicht", width=180, height=28,
            fg_color=self.ogx_colors["panel_alt"], hover_color=self.ogx_colors["border"],
            command=self._show_system_overview,
        )
        back_btn.pack(side="left")
        ctk.CTkLabel(
            head, text=self._SYSTEM_DETAIL_TITLES.get(which, ""),
            font=ctk.CTkFont(size=14, weight="bold"), text_color=self.ogx_colors["text"],
        ).pack(side="left", padx=(14, 0))

        body = ctk.CTkFrame(self._system_detail, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        if which == "tools":
            self._build_system_tools_panel(body)
        elif which == "energy":
            self._build_energy_settings_panel(body)
        elif which == "device":
            self._build_device_settings_panel(body)

        self._system_overview.grid_remove()
        self._system_detail.grid()

    def _show_system_overview(self) -> None:
        self._system_detail.grid_remove()
        self._system_overview.grid()

    def _build_setup_page(self, parent) -> None:
        page = ctk.CTkFrame(parent, fg_color="transparent")
        self._pages["setup"] = page
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure((0, 1), weight=1)

        choco_card = ctk.CTkFrame(page, fg_color=self.ogx_colors["panel"], corner_radius=10)
        choco_card.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        ctk.CTkLabel(choco_card, text="Chocolatey installieren", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            choco_card, text="Offizielles PowerShell-Skript (Internet, Admin empfohlen).",
            font=ctk.CTkFont(size=11), text_color=self.ogx_colors["muted"], anchor="w", wraplength=280,
        ).pack(anchor="w", padx=16, pady=(0, 12))
        self.install_choco_btn = ctk.CTkButton(choco_card, text="Installieren", command=self._bootstrap_chocolatey, width=120)
        self.install_choco_btn.pack(anchor="w", padx=16, pady=(0, 14))
        self._attach_tooltip(
            self.install_choco_btn, "Installiert Chocolatey per offiziellem PowerShell-Skript (Internet, Admin empfohlen)."
        )

        winget_card = ctk.CTkFrame(page, fg_color=self.ogx_colors["panel"], corner_radius=10)
        winget_card.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        ctk.CTkLabel(winget_card, text="WinGet installieren", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            winget_card, text="App-Installer-Paketquelle (aka.ms/getwinget). Nach Neustart/neuer Shell oft verfügbar.",
            font=ctk.CTkFont(size=11), text_color=self.ogx_colors["muted"], anchor="w", justify="left", wraplength=280,
        ).pack(anchor="w", padx=16, pady=(0, 12))
        self.install_winget_btn = ctk.CTkButton(winget_card, text="Installieren", command=self._bootstrap_winget, width=120)
        self.install_winget_btn.pack(anchor="w", padx=16, pady=(0, 14))
        self._attach_tooltip(
            self.install_winget_btn,
            "Installiert die App-Installer-Paketquelle (aka.ms/getwinget). Nach Neustart oder neuer Shell oft verfügbar.",
        )

        source_card = ctk.CTkFrame(page, fg_color=self.ogx_colors["panel"], corner_radius=10)
        source_card.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=6, pady=6)
        ctk.CTkLabel(source_card, text="Installationsquelle", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            source_card, text="Lokalen Ordner oder USB-Pfad als Installationsquelle wählen und scannen.",
            font=ctk.CTkFont(size=11), text_color=self.ogx_colors["muted"], anchor="w",
        ).pack(anchor="w", padx=16, pady=(0, 10))
        src_row = ctk.CTkFrame(source_card, fg_color="transparent")
        src_row.pack(anchor="w", padx=16, pady=(0, 6))
        local_source_btn = ctk.CTkButton(src_row, text="Installationsquelle wählen", command=self._select_local_source, height=26)
        scan_source_btn = ctk.CTkButton(src_row, text="Quelle scannen", command=self._scan_local_source, height=26)
        self.prefer_local_var = ctk.BooleanVar(value=self.runtime.local_source_prefer_local)
        prefer_local_cb = ctk.CTkCheckBox(
            src_row, text="Lokale Quelle bevorzugen", variable=self.prefer_local_var,
            command=lambda: self._persist_local_source_settings(None),
        )
        local_source_btn.pack(side="left", padx=(0, 8))
        scan_source_btn.pack(side="left", padx=(0, 8))
        prefer_local_cb.pack(side="left", padx=(0, 8))
        self._attach_tooltip(local_source_btn, "Wählt einen lokalen Ordner oder USB-Pfad als Installationsquelle.")
        self._attach_tooltip(scan_source_btn, "Durchsucht die gewählte Quelle nach passenden Installern für bekannte Software.")
        self._attach_tooltip(prefer_local_cb, "Wenn aktiv, wird zuerst lokaler Installer/USB versucht, bevor Online-Provider genutzt werden.")
        init_local = self.runtime.local_source_last_path or "keine"
        self.local_source_path_var = ctk.StringVar(value=f"Aktive lokale Quelle: {init_local}")
        self.local_source_path_label = ctk.CTkLabel(
            source_card, textvariable=self.local_source_path_var, anchor="w", justify="left",
            wraplength=600, font=ctk.CTkFont(size=10), text_color=self.ogx_colors["muted"],
        )
        self.local_source_path_label.pack(anchor="w", padx=16, pady=(0, 14))

    def _build_reports_page(self, parent) -> None:
        page = ctk.CTkFrame(parent, fg_color="transparent")
        self._pages["reports"] = page
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        self.history_frame = ctk.CTkFrame(page, fg_color=self.ogx_colors["panel"], corner_radius=10)
        self.history_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.history_frame.grid_columnconfigure(0, weight=1)
        head_row = ctk.CTkFrame(self.history_frame, fg_color="transparent")
        head_row.grid(row=0, column=0, sticky="ew", padx=12, pady=10)
        ctk.CTkLabel(head_row, text="Letzte Läufe (max. 10 Reports: CSV / PDF)", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        report_btn = ctk.CTkButton(head_row, text="Report-Ordner öffnen", command=self._open_reports_dir, height=26)
        report_btn.pack(side="right", padx=(6, 0))
        self._attach_tooltip(report_btn, "Oeffnet den Report-Ordner mit CSV/PDF-Ergebnissen.")
        save_log_btn2 = ctk.CTkButton(
            head_row, text="Log speichern", command=self._save_log, height=26,
            fg_color=self.ogx_colors["panel_alt"], hover_color=self.ogx_colors["border"],
        )
        save_log_btn2.pack(side="right")
        self._attach_tooltip(save_log_btn2, "Speichert das aktuelle Laufprotokoll als Datei.")

        self.history_scroll = ctk.CTkScrollableFrame(page, fg_color=self.ogx_colors["panel_alt"], corner_radius=10)
        self.history_scroll.grid(row=1, column=0, sticky="nsew")
        self.history_scroll.grid_columnconfigure(0, weight=1)

    def _create_software_row(self, software: SoftwarePackage, idx: int) -> None:
        """Baut eine einzelne Tabellenzeile auf — wiederverwendbar fuer den initialen Aufbau und
        fuer per '+ Anwendung hinzufuegen' live ergaenzte Zeilen (kein Neustart noetig)."""
        key = software.key
        wmap = self._ui_column_widths
        row_bg = self.ogx_colors["table"] if idx % 2 == 0 else self.ogx_colors["table_alt"]
        rowf = ctk.CTkFrame(self.software_list_frame, fg_color=row_bg, height=28)
        rowf.pack_propagate(False)
        rowf.grid(row=idx, column=0, sticky="ew", pady=1)
        self.row_frames[key] = rowf
        self._row_cell_frames[key] = {}
        self._setup_scroll_hover_target(rowf, self._software_list_wheel)
        rowf.bind("<Button-3>", lambda e, k=key: self._show_row_context_menu(e, k))

        row_name = (
            f"{software.display_name} (Server-Komponente)"
            if key == "opentext"
            else f"{software.display_name} (Webroot-Agent)"
            if key == "opentext_core_endpoint"
            else software.display_name
        )

        # checkbox cell
        cb_cell = ctk.CTkFrame(rowf, fg_color="transparent", width=wmap["checkbox"], height=24)
        cb_cell.pack_propagate(False)
        cb_cell.pack(side="left")
        self._row_cell_frames[key]["checkbox"] = cb_cell
        var = ctk.BooleanVar(value=False)
        self.checkbox_vars[key] = var
        var.trace_add("write", lambda *_: self._update_bulk_selection_label())
        cb = ctk.CTkCheckBox(cb_cell, text="", variable=var, width=max(28, wmap["checkbox"] - 8),
                             checkbox_width=16, checkbox_height=16, font=ctk.CTkFont(size=10))
        cb.pack(side="left", padx=4)
        self.checkbox_widgets[key] = cb

        # program cell (flex)
        prog_cell = ctk.CTkFrame(rowf, fg_color="transparent")
        prog_cell.pack(side="left", fill="x", expand=True)
        self._row_cell_frames[key]["program"] = prog_cell
        nm = ctk.CTkLabel(prog_cell, text=row_name, anchor="w", font=ctk.CTkFont(size=10))
        nm.pack(side="left", fill="x", expand=True, padx=(4, 2))
        self.program_name_labels[key] = nm
        nm.bind("<Button-3>", lambda e, k=key: self._show_row_context_menu(e, k))

        # status cell
        st_cell = ctk.CTkFrame(rowf, fg_color="transparent", width=wmap["status"], height=24)
        st_cell.pack_propagate(False)
        st_cell.pack(side="left")
        self._row_cell_frames[key]["status"] = st_cell
        badge = ctk.CTkLabel(st_cell, text="OFFEN", height=20, corner_radius=5, anchor="center", font=ctk.CTkFont(size=10),
                             fg_color="#334155", text_color="#E2E8F0")
        badge.pack(fill="x", padx=2, pady=2)
        self.badge_labels[key] = badge

        # provider cell
        prov_cell = ctk.CTkFrame(rowf, fg_color="transparent", width=wmap["provider"], height=24)
        prov_cell.pack_propagate(False)
        prov_cell.pack(side="left")
        self._row_cell_frames[key]["provider"] = prov_cell
        pv = ctk.CTkLabel(prov_cell, text="—", anchor="w", font=ctk.CTkFont(size=10))
        pv.pack(side="left", padx=4)
        self.provider_labels[key] = pv

        # installed version cell
        inst_cell = ctk.CTkFrame(rowf, fg_color="transparent", width=wmap["installed"], height=24)
        inst_cell.pack_propagate(False)
        inst_cell.pack(side="left")
        self._row_cell_frames[key]["installed"] = inst_cell
        vi = ctk.CTkLabel(inst_cell, text="—", anchor="w", font=ctk.CTkFont(size=10))
        vi.pack(side="left", padx=4)
        self.ver_inst_labels[key] = vi

        # available version cell
        avail_cell = ctk.CTkFrame(rowf, fg_color="transparent", width=wmap["available"], height=24)
        avail_cell.pack_propagate(False)
        avail_cell.pack(side="left")
        self._row_cell_frames[key]["available"] = avail_cell
        va = ctk.CTkLabel(avail_cell, text="—", anchor="w", font=ctk.CTkFont(size=10))
        va.pack(side="left", padx=4)
        self.ver_avail_labels[key] = va

        # progress cell
        pcell = ctk.CTkFrame(rowf, fg_color="transparent", width=wmap["progress"], height=24)
        pcell.pack_propagate(False)
        pcell.pack(side="left")
        self._row_cell_frames[key]["progress"] = pcell
        pb = ctk.CTkProgressBar(pcell, height=10, width=max(40, wmap["progress"] - 8))
        pb.pack(side="left", padx=4, pady=7)
        pb.set(0)
        self.row_progress_cells[key] = pcell
        self.row_progress_bars[key] = pb

        self.row_visible[key] = True
        init_state = SoftwareState("Nicht geprueft")
        self.current_states[key] = init_state
        self._apply_row_state(key, init_state)
        self._update_bulk_selection_label()
        self._update_software_nav_badge()

    def _update_software_nav_badge(self) -> None:
        btn = self._nav_buttons.get("software")
        if btn is not None:
            btn.configure(text=f"Software  {len(self.row_frames)}")

    def _regrid_software_rows(self) -> None:
        """Nach Entfernen einer Zeile: verbleibende Zeilen luecken- und farbwechsel-korrekt neu anordnen."""
        for idx, software in enumerate(self.runtime.visible_catalog, start=1):
            rowf = self.row_frames.get(software.key)
            if rowf is None:
                continue
            row_bg = self.ogx_colors["table"] if idx % 2 == 0 else self.ogx_colors["table_alt"]
            try:
                rowf.configure(fg_color=row_bg)
                rowf.grid(row=idx, column=0, sticky="ew", pady=1)
            except Exception:
                pass

    def _show_row_context_menu(self, event: object, key: str) -> None:
        software = next((s for s in self.runtime.visible_catalog if s.key == key), None)
        if software is None:
            return
        menu = Menu(self, tearoff=0)
        menu.add_command(
            label=f"„{software.display_name}“ aus Liste entfernen",
            command=lambda: self._remove_software_row(key),
        )
        try:
            menu.tk_popup(int(getattr(event, "x_root", 0)), int(getattr(event, "y_root", 0)))
        finally:
            menu.grab_release()

    def _remove_software_row(self, key: str) -> bool:
        software = next((s for s in self.runtime.visible_catalog if s.key == key), None)
        if software is None:
            # Karten in den Einstellungen zeigen auch bereits deaktivierte Built-ins (dort per
            # Default "aktiviert" vorbelegt, solange noch keine explizite Provider-Config
            # existiert) — die stehen dann NICHT in visible_catalog. Ohne diesen Fallback wuerde
            # "Löschen" dafuer stillschweigend nichts tun.
            software = SOFTWARE_BY_KEY.get(key)
        if software is None:
            return False
        if not messagebox.askyesno(
            APP_NAME,
            f"„{software.display_name}“ aus der Liste entfernen?\n\n"
            "Über „+ Anwendung hinzufügen“ lässt es sich jederzeit wieder hinzufügen.",
            parent=self,
        ):
            return False
        cfg = get_config_dict(self.logger)
        if key in self.runtime.custom_software_keys:
            remove_custom_software(cfg, key, self.logger)
        else:
            set_builtin_software_enabled(cfg, key, False, self.logger)

        rowf = self.row_frames.pop(key, None)
        if rowf is not None:
            rowf.destroy()
        for store in (
            self._row_cell_frames, self.checkbox_vars, self.checkbox_widgets,
            self.program_name_labels, self.badge_labels, self.provider_labels,
            self.ver_inst_labels, self.ver_avail_labels, self.row_progress_cells,
            self.row_progress_bars, self.row_visible, self.current_states,
        ):
            store.pop(key, None)
        self.runtime.visible_catalog = tuple(s for s in self.runtime.visible_catalog if s.key != key)
        self.runtime.custom_software_keys = frozenset(k for k in self.runtime.custom_software_keys if k != key)

        self._regrid_software_rows()
        self._update_filter_counts()
        self._update_bulk_selection_label()
        self._update_software_nav_badge()
        self.logger.info("Software aus Liste entfernt: %s (%s)", software.display_name, key)
        return True

    def _browse_installer_path(self, path_var: ctk.StringVar) -> None:
        """Installer-Datei auswählen — funktioniert für jeden erreichbaren Laufwerksbuchstaben
        (Netzwerkfreigabe, lokale Platte oder z. B. ein USB-Stick unter D:\\...)."""
        initial = path_var.get().strip()
        initialdir = None
        if initial:
            try:
                cand = Path(initial).parent
                if cand.exists():
                    initialdir = str(cand)
            except (OSError, ValueError):
                initialdir = None
        selected = filedialog.askopenfilename(
            parent=self,
            title="Installer auswählen",
            initialdir=initialdir,
            filetypes=[("Installer", "*.exe *.msi"), ("Alle Dateien", "*.*")],
        )
        if selected:
            path_var.set(selected)

    def _open_add_software_dialog(self) -> None:
        dialog = ctk.CTkToplevel(self)
        dialog.title("Anwendung hinzufügen")
        dialog.geometry("440x360")
        dialog.transient(self)
        dialog.grab_set()
        dialog.configure(fg_color=self.ogx_colors["bg"])

        frame = ctk.CTkFrame(dialog, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=18, pady=18)

        ctk.CTkLabel(frame, text="Anwendung hinzufügen", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            frame,
            text="Erscheint sofort in der Liste dieses PCs und bleibt gespeichert.",
            anchor="w", font=ctk.CTkFont(size=10), text_color=self.ogx_colors["muted"],
        ).pack(anchor="w", pady=(2, 12))

        def _field(label_text: str, placeholder: str = "") -> ctk.CTkEntry:
            ctk.CTkLabel(frame, text=label_text, anchor="w", font=ctk.CTkFont(size=11)).pack(fill="x", pady=(6, 2))
            entry = ctk.CTkEntry(frame, placeholder_text=placeholder)
            entry.pack(fill="x")
            return entry

        name_entry = _field("Anzeigename", "z. B. Spotify")
        winget_entry = _field("WinGet-ID", "z. B. Spotify.Spotify")
        choco_entry = _field("Chocolatey-Paketname (optional)", "z. B. spotify")
        terms_entry = _field("Suchbegriffe, Komma-getrennt (optional)", "spotify, spotify music")

        ctk.CTkLabel(
            frame,
            text="Mindestens WinGet-ID oder Chocolatey-Paketname angeben.",
            anchor="w", justify="left", font=ctk.CTkFont(size=10),
            text_color=self.ogx_colors["muted"], wraplength=390,
        ).pack(fill="x", pady=(8, 0))

        btn_row = ctk.CTkFrame(frame, fg_color="transparent")
        btn_row.pack(fill="x", pady=(16, 0))

        def on_submit() -> None:
            display_name = name_entry.get().strip()
            winget_id = winget_entry.get().strip()
            choco_pkg = choco_entry.get().strip()
            terms = [t.strip() for t in terms_entry.get().split(",") if t.strip()]
            if not display_name or (not winget_id and not choco_pkg):
                messagebox.showwarning(
                    APP_NAME, "Bitte Anzeigename sowie WinGet-ID oder Chocolatey-Paketname angeben.", parent=dialog
                )
                return

            existing_builtin = next(
                (s for s in SOFTWARE_CATALOG if s.display_name.strip().lower() == display_name.lower()), None
            )
            if existing_builtin is not None:
                if any(s.key == existing_builtin.key for s in self.runtime.visible_catalog):
                    messagebox.showinfo(APP_NAME, f"„{existing_builtin.display_name}“ ist bereits in der Liste.", parent=dialog)
                    return
                cfg = get_config_dict(self.logger)
                set_builtin_software_enabled(cfg, existing_builtin.key, True, self.logger)
                new_pkg = existing_builtin
            else:
                cfg = get_config_dict(self.logger)
                ok, msg, new_pkg = add_custom_software(
                    cfg, display_name=display_name, winget_id=winget_id, choco_package=choco_pkg,
                    search_terms=terms, logger=self.logger,
                )
                if not ok or new_pkg is None:
                    messagebox.showwarning(APP_NAME, msg, parent=dialog)
                    return
                self.runtime.custom_software_keys = self.runtime.custom_software_keys | {new_pkg.key}

            idx = len(self.runtime.visible_catalog) + 1
            self.runtime.visible_catalog = self.runtime.visible_catalog + (new_pkg,)
            self._create_software_row(new_pkg, idx)
            self._apply_software_column_widths()
            self._update_filter_counts()
            self._sync_row_visibility()
            self.logger.info("Software hinzugefuegt: %s (%s)", new_pkg.display_name, new_pkg.key)
            dialog.destroy()

        ctk.CTkButton(btn_row, text="Hinzufügen", command=on_submit).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_row, text="Abbrechen", command=dialog.destroy, fg_color=self.ogx_colors["panel_alt"]
        ).pack(side="left")

        self._apply_ogx_style(dialog)

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
        if label is None or not label.winfo_exists():
            return
        normalized = state.strip().lower()
        if normalized == "läuft":
            label.configure(text="läuft", fg_color="#7a6a2b", text_color="#f5f1e0")
            return
        if normalized == "fehler":
            label.configure(text="fehler", fg_color="#7a3240", text_color="#f8e9ed")
            return
        label.configure(text="bereit", fg_color="#2c5a4a", text_color="#e6f7ef")

    def _set_device_tab_status(self, tab_name: str, state: str) -> None:
        label = self._device_settings_status_labels.get(tab_name)
        if label is None or not label.winfo_exists():
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
        if self._resize_after_id is not None:
            try:
                self.after_cancel(self._resize_after_id)
            except Exception:
                pass
        self._resize_after_id = self.after(
            80, lambda w=int(event.width), h=int(event.height): self._apply_responsive_layout(w, h)
        )

    def _apply_responsive_layout(self, width: int, height: int) -> None:
        self._update_wrap_lengths(width)
        self._apply_software_column_layout(width)

    def _apply_software_column_layout(self, width: int) -> None:
        """Horizontal layout reacts to window size; column min widths come from config (ui_columns)."""
        _ = width
        self._fit_columns_to_available_width()
        self._apply_software_column_widths()

    def _apply_software_column_widths(self) -> None:
        wmap = self._ui_column_widths
        _fixed_keys = ("checkbox", "status", "provider", "installed", "available", "progress")
        # Update header cells
        for key in _fixed_keys:
            cell = self._header_cell_frames.get(key)
            if cell is not None:
                try:
                    cell.configure(width=wmap[key])
                except Exception:
                    pass
        # Update all data row cells
        for row_cells in self._row_cell_frames.values():
            for key in _fixed_keys:
                cell = row_cells.get(key)
                if cell is not None:
                    try:
                        cell.configure(width=wmap[key])
                    except Exception:
                        pass
        # Checkbox widget width
        cb_w = wmap["checkbox"]
        for cb in self.checkbox_widgets.values():
            try:
                cb.configure(width=max(28, cb_w - 8))
            except Exception:
                pass
        # Progress bar inner width
        progress_w = wmap["progress"]
        for pb in self.row_progress_bars.values():
            try:
                pb.configure(width=max(40, progress_w - 8))
            except Exception:
                pass

    def _total_column_width(self) -> int:
        return sum(int(self._ui_column_widths.get(k, 0)) for k in UI_COLUMN_KEYS)

    def _fit_columns_to_available_width(self, avail_override: int | None = None) -> None:
        if avail_override is not None:
            avail = avail_override
        else:
            if self.software_list_frame is None:
                return
            canvas = getattr(self.software_list_frame, "_parent_canvas", None)
            if canvas is None:
                return
            try:
                avail = int(canvas.winfo_width()) - 18
            except Exception:
                return
            if avail < 320:
                return
        total = self._total_column_width()
        if total <= avail:
            return
        over = total - avail
        # On narrow windows, shrink non-program columns first, then program last.
        order = ("provider", "installed", "available", "status", "progress", "checkbox", "program")
        for key in order:
            if over <= 0:
                break
            cur = int(self._ui_column_widths[key])
            lo = int(MIN_UI_COLUMNS[key])
            room = max(0, cur - lo)
            if room <= 0:
                continue
            take = min(room, over)
            self._ui_column_widths[key] = cur - take
            over -= take

    def _on_column_resize_start(self, event: object, left_key: str, right_key: str) -> None:
        self._col_resize_drag = (left_key, right_key, int(getattr(event, "x_root", 0)))

    def _on_column_resize_motion(self, event: object, left_key: str, right_key: str) -> None:
        drag = self._col_resize_drag
        if drag is None or drag[0] != left_key:
            return
        x0 = drag[2]
        cur = int(getattr(event, "x_root", x0))
        delta = cur - x0
        if delta == 0:
            return
        self._col_resize_drag = (left_key, right_key, cur)
        new_w = max(MIN_UI_COLUMNS[left_key], self._ui_column_widths[left_key] + delta)
        self._ui_column_widths[left_key] = int(new_w)
        self._apply_software_column_widths()

    def _on_column_resize_end(self) -> None:
        if self._col_resize_drag is not None:
            self._col_resize_drag = None
            try:
                cfg = get_config_dict(self.logger)
                cfg["ui_columns"] = {k: int(self._ui_column_widths[k]) for k in UI_COLUMN_KEYS}
                save_config_dict(cfg, self.logger, backup=False)
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.warning("ui_columns speichern fehlgeschlagen: %s", exc)

    def _reset_one_column_width(self, key: str) -> None:
        self._ui_column_widths[key] = int(DEFAULT_UI_COLUMNS[key])
        self._apply_software_column_widths()
        self._on_column_resize_end()

    def _reset_ui_columns_to_defaults(self) -> None:
        self._ui_column_widths = {k: int(DEFAULT_UI_COLUMNS[k]) for k in UI_COLUMN_KEYS}
        self._fit_columns_to_available_width()
        self._apply_software_column_widths()
        self._on_column_resize_end()

    def _update_wrap_lengths(self, width: int) -> None:
        health_wrap = max(180, min(1500, width - 48))
        if self.health_details_label is not None:
            self.health_details_label.configure(wraplength=health_wrap)
        if self.health_warnings_label is not None:
            self.health_warnings_label.configure(wraplength=health_wrap)
        if self.health_config_label is not None:
            self.health_config_label.configure(wraplength=health_wrap)
        wrap_prog = max(120, int(self._ui_column_widths.get("program", 220) * 4))
        for lb in self.program_name_labels.values():
            try:
                lb.configure(wraplength=wrap_prog)
            except Exception:
                pass

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
        self._set_health_details_visible(self._health_details_expanded)

    def _on_filter_change(self, value: str) -> None:
        self._active_filter_mode = value
        for mode, btn in self._filter_count_labels.items():
            active = mode == value
            try:
                btn.configure(
                    fg_color=self.ogx_colors["accent"] if active else self.ogx_colors["panel_alt"],
                    hover_color=self.ogx_colors["accent_hover"] if active else self.ogx_colors["border"],
                )
            except Exception:
                pass
        self._sync_row_visibility()

    def _on_search_change(self, *_args: object) -> None:
        self._sync_row_visibility()

    def _update_filter_counts(self) -> None:
        counts: dict[str, int] = {"Alle": 0, "Updates": 0, "Fehlend": 0, "Fehler": 0}
        for key in self.row_frames:
            st = self.current_states.get(key)
            s = st.status if st else "Nicht geprueft"
            counts["Alle"] += 1
            if s == "Update verfuegbar":
                counts["Updates"] += 1
            elif s == "Nicht installiert":
                counts["Fehlend"] += 1
            elif "Fehler" in s and s not in ("Quelle erforderlich", "PRUEFT"):
                counts["Fehler"] += 1
        for mode, btn in self._filter_count_labels.items():
            try:
                btn.configure(text=f"{mode}  {counts[mode]}")
            except Exception:
                pass

    @staticmethod
    def _set_health_caption(lb: ctk.CTkLabel | None, text: str) -> None:
        if lb is not None:
            lb.configure(text=text or "—")

    def _current_filter_mode(self) -> str:
        return getattr(self, "_active_filter_mode", "Alle")

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
        return getattr(self, "_active_filter_mode", "Alle") == "Alle"

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
            return "PRUEFT",  "#1E3A8A", "#BFDBFE"
        if st == "Quelle erforderlich":
            return "QUELLE",  "#92400E", "#FFEDD5"
        if st == "Warnung":
            return "HINWEIS", "#78350F", "#FDE68A"
        if "fehler" in st.lower():
            return "FEHLER",  "#7F1D1D", "#FECACA"
        if "dry-run" in st.lower():
            return "DRY-RUN", "#44403C", "#E7E5E4"
        if st == "Update verfuegbar":
            return "UPDATE",  "#78350F", "#FDE68A"
        if st == "Nicht installiert":
            return "FEHLT",   "#334155", "#E2E8F0"
        if st == "Manuelle Pruefung noetig" or "manuelle" in st.lower():
            return "PRUEFEN", "#78350F", "#FDE68A"
        if st == "Installiert":
            return "OK",      "#14532D", "#BBF7D0"
        if st == "Aktuell":
            return "AKTUELL", "#14532D", "#BBF7D0"
        if st == "Nicht geprueft":
            return "OFFEN",   "#334155", "#E2E8F0"
        return st[:10].upper(), "#334155", "#E2E8F0"

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

    def _build_device_settings_panel(self, parent) -> None:
        frame = ctk.CTkFrame(parent)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        dry_var = ctk.BooleanVar(value=True)
        top_row = ctk.CTkFrame(frame, fg_color="transparent")
        top_row.grid(row=0, column=0, sticky="ew", padx=8, pady=(4, 8))
        sys_dry_cb = ctk.CTkCheckBox(top_row, text="Dry-Run (Simulation ohne echte Änderungen)", variable=dry_var)
        sys_dry_cb.pack(side="right")

        tabview = ctk.CTkTabview(frame)
        tabview.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        tabview.add("Computer")
        tabview.add("Updates")
        tabview.add("Protokoll")
        self._device_settings_tabview = tabview

        computer_tab = tabview.tab("Computer")
        updates_tab = tabview.tab("Updates")
        log_tab = tabview.tab("Protokoll")
        computer_tab.grid_columnconfigure(1, weight=1)
        updates_tab.grid_columnconfigure(1, weight=1)
        log_tab.grid_columnconfigure(0, weight=1)
        log_tab.grid_rowconfigure(1, weight=1)

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
            wraplength=720,
        ).grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        restart_after_rename_var = ctk.BooleanVar(value=False)
        restart_rename_cb = ctk.CTkCheckBox(
            computer_tab,
            text="Nach Umbenennung sofort neu starten (-Restart)",
            variable=restart_after_rename_var,
        )
        restart_rename_cb.grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))

        pc_status = ctk.CTkLabel(computer_tab, text="bereit", width=70, corner_radius=8, fg_color="#2c5a4a", text_color="#e6f7ef")
        pc_status.grid(row=0, column=2, rowspan=2, padx=8, pady=(8, 6), sticky="ne")

        ctk.CTkLabel(
            updates_tab,
            text="Windows-Updates können hier erst gescannt und danach installiert werden. Installation kann Neustart erfordern.",
            anchor="w",
            justify="left",
            text_color=("gray35", "gray70"),
            wraplength=720,
        ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 8))
        updates_status = ctk.CTkLabel(updates_tab, text="bereit", width=70, corner_radius=8, fg_color="#2c5a4a", text_color="#e6f7ef")
        updates_status.grid(row=0, column=2, padx=8, pady=(8, 6), sticky="e")

        self._device_settings_status_labels = {
            "Computer": pc_status,
            "Updates": updates_status,
        }

        ctk.CTkLabel(log_tab, text="Protokoll / Details", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 4)
        )
        output = ctk.CTkTextbox(log_tab, wrap="word")
        output.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self._device_settings_output_box = output

        def _append(lines: list[str]) -> None:
            if not lines:
                return
            output.insert("end", "\n".join(lines) + "\n")
            output.see("end")

        def _run_in_thread(fn, tab_name: str) -> None:
            self._set_device_tab_status(tab_name, "läuft")
            if self._device_settings_tabview is not None:
                self._device_settings_tabview.set("Protokoll")

            def worker() -> None:
                try:
                    result = fn()
                    self.ui_queue.put(("device_settings_output", {"lines": result.lines, "tab": tab_name, "ok": bool(result.ok)}))
                except Exception as exc:  # pylint: disable=broad-except
                    self.ui_queue.put(("device_settings_output", {"lines": [f"Fehler: {exc}"], "tab": tab_name, "ok": False}))

            threading.Thread(target=worker, daemon=True).start()

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

        def scan_updates() -> None:
            _append(["Suche Windows Updates..."])
            _run_in_thread(self.system_tools.scan_windows_updates, "Updates")

        def install_updates() -> None:
            _append(["Starte Windows Update Installation..."])
            _run_in_thread(lambda: self.system_tools.install_windows_updates(dry_var.get()), "Updates")

        rename_pc_btn = ctk.CTkButton(computer_tab, text="PC umbenennen", command=rename_pc)
        rename_pc_btn.grid(row=4, column=0, columnspan=2, padx=8, pady=(0, 8), sticky="ew")
        self._attach_tooltip(rename_pc_btn, "Benennt den Windows-Computer um (Rename-Computer). Erfordert typischerweise Administratorrechte.")

        # --- Dateiendungen anzeigen ---
        sep = ctk.CTkFrame(computer_tab, height=1, fg_color=self.ogx_colors["border"])
        sep.grid(row=5, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))

        ctk.CTkLabel(computer_tab, text="Dateiendungen anzeigen", anchor="w", font=ctk.CTkFont(size=11, weight="bold")).grid(
            row=6, column=0, sticky="w", padx=8, pady=(4, 2)
        )
        ctk.CTkLabel(
            computer_tab,
            text="Zeigt Dateiendungen im Explorer an (z. B. .pdf, .exe). Ändert HKCU\\...\\Explorer\\Advanced\\HideFileExt.",
            anchor="w",
            justify="left",
            text_color=("gray35", "gray70"),
            wraplength=600,
        ).grid(row=7, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 6))

        file_ext_status_lbl = ctk.CTkLabel(computer_tab, text="...", width=100, corner_radius=6)
        file_ext_status_lbl.grid(row=6, column=1, sticky="w", padx=8)

        def _refresh_file_ext_status() -> None:
            r = read_show_file_extensions()
            if not r.available:
                file_ext_status_lbl.configure(text="N/V", fg_color="#5b3742", text_color="#f7e9ed")
            elif r.on:
                file_ext_status_lbl.configure(text="Sichtbar", fg_color="#2f7d4f", text_color="#e8fff0")
            else:
                file_ext_status_lbl.configure(text="Versteckt", fg_color="#495261", text_color="#f0f4f8")

        def _toggle_file_extensions() -> None:
            cur = read_show_file_extensions()
            ok, msg = apply_show_file_extensions(not cur.on)
            if not ok:
                messagebox.showerror(APP_NAME, f"Fehler: {msg}", parent=self)
            _refresh_file_ext_status()

        file_ext_btn = ctk.CTkButton(computer_tab, text="Umschalten", width=120, command=_toggle_file_extensions)
        file_ext_btn.grid(row=6, column=2, padx=8, pady=(4, 2), sticky="e")
        self._attach_tooltip(file_ext_btn, "Wechselt zwischen 'Endungen anzeigen' und 'Endungen verstecken' für den aktuellen Benutzer.")
        _refresh_file_ext_status()

        scan_updates_btn = ctk.CTkButton(updates_tab, text="Windows-Updates scannen", command=scan_updates)
        scan_updates_btn.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="ew")
        self._attach_tooltip(scan_updates_btn, "Listet verfügbare Windows-Software-Updates.")
        install_updates_btn = ctk.CTkButton(updates_tab, text="Windows-Updates installieren", command=install_updates)
        install_updates_btn.grid(row=1, column=1, padx=8, pady=(0, 8), sticky="ew")
        self._attach_tooltip(install_updates_btn, "Installiert gefundene Windows-Updates; kann Neustart erfordern.")

        self._apply_ogx_style(parent)

    def _build_energy_settings_panel(self, parent) -> None:
        outer = ctk.CTkScrollableFrame(parent, fg_color=self.ogx_colors["bg"])
        outer.pack(fill="both", expand=True, padx=14, pady=14)

        top_bar = ctk.CTkFrame(outer, fg_color="transparent")
        top_bar.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(top_bar, text="Energie-Dashboard", font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        refresh_btn = ctk.CTkButton(
            top_bar, text="Aktualisieren", width=120, height=30,
            fg_color=self.ogx_colors["panel_alt"], hover_color=self.ogx_colors["border"],
            command=self._refresh_energy_dashboard_async,
        )
        refresh_btn.pack(side="right")
        self._energy_action_buttons["refresh"] = refresh_btn
        restore_btn = ctk.CTkButton(
            top_bar,
            text="Energieeinstellungen zurücksetzen",
            width=240,
            height=30,
            fg_color=self.ogx_colors["danger"],
            hover_color=self.ogx_colors["danger_hover"],
            command=lambda: self._energy_action_clicked("restore_defaults"),
        )
        restore_btn.pack(side="right", padx=(0, 8))
        self._energy_action_buttons["restore_defaults"] = restore_btn
        apply_btn = ctk.CTkButton(
            top_bar,
            text="Empfohlenes Energie-Setup anwenden",
            width=280,
            height=30,
            fg_color="#2f7d4f",
            hover_color="#25643f",
            command=lambda: self._energy_action_clicked("apply_recommended"),
        )
        apply_btn.pack(side="right", padx=(0, 8))
        self._energy_action_buttons["apply_recommended"] = apply_btn

        section_icons = {
            "Energiestatus": "⚡",
            "Eingesteckt": "🔌",
            "Akku": "🔋",
            "Anzeige & Komfort": "🖥️",
            "Akku schonen": "🌿",
        }
        for title in ("Energiestatus", "Eingesteckt", "Akku", "Anzeige & Komfort", "Akku schonen"):
            sec = ctk.CTkFrame(
                outer,
                fg_color=self.ogx_colors["panel"],
                corner_radius=10,
                border_width=1,
                border_color=self.ogx_colors["border"],
            )
            sec.pack(fill="x", pady=8)
            header = ctk.CTkFrame(sec, fg_color="transparent")
            header.pack(fill="x", padx=14, pady=(12, 6))
            ctk.CTkLabel(
                header,
                text=f"{section_icons.get(title, '')}  {title}",
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color=self.ogx_colors["text"],
            ).pack(anchor="w")
            ctk.CTkFrame(sec, fg_color=self.ogx_colors["border"], height=1).pack(fill="x", padx=14, pady=(0, 4))
            setattr(self, f"_sec_{title.lower().replace(' ', '_').replace('&', 'und')}", sec)

        self._add_energy_row(getattr(self, "_sec_energiestatus"), "status_ac_mode", "Eingesteckt", "", action_key="apply_ac_power_mode")
        self._add_energy_row(getattr(self, "_sec_energiestatus"), "status_dc_mode", "Akku", "", action_key="apply_dc_power_mode")
        self._add_energy_row(getattr(self, "_sec_energiestatus"), "status_battery_percent_setting", "Akkuprozentsatz anzeigen", "", action_key="toggle_battery_percent")

        self._add_energy_row(getattr(self, "_sec_eingesteckt"), "ac_power_button", "Netzschalter-Aktion", "", action_key="set_ac_power_sleep")
        self._add_energy_row(getattr(self, "_sec_eingesteckt"), "ac_lid_action", "Deckel schließen", "", action_key="set_ac_lid_none")
        self._add_energy_row(getattr(self, "_sec_eingesteckt"), "ac_display_timeout", "Bildschirm ausschalten nach", "", action_key="apply_ac_display")
        self._add_energy_row(getattr(self, "_sec_eingesteckt"), "ac_sleep_timeout", "Standby nach", "", action_key="apply_ac_sleep")

        self._add_energy_row(getattr(self, "_sec_akku"), "dc_power_button", "Netzschalter-Aktion", "", action_key="set_dc_power_sleep")
        self._add_energy_row(getattr(self, "_sec_akku"), "dc_lid_action", "Deckel schließen", "", action_key="set_dc_lid_none")
        self._add_energy_row(getattr(self, "_sec_akku"), "dc_display_timeout", "Bildschirm ausschalten nach", "", action_key="apply_dc_display")
        self._add_energy_row(getattr(self, "_sec_akku"), "dc_sleep_timeout", "Standby nach", "", action_key="apply_dc_sleep")

        self._add_energy_row(getattr(self, "_sec_anzeige_und_komfort"), "dark_mode", "Darkmode", "", action_key="toggle_dark_mode")
        self._add_energy_row(getattr(self, "_sec_anzeige_und_komfort"), "screensaver_disabled", "Bildschirmschoner deaktivieren", "", action_key="toggle_screensaver")
        self._add_energy_row(
            getattr(self, "_sec_anzeige_und_komfort"),
            "adaptive_brightness",
            "Adaptive Helligkeit / Inhaltsadaptive Helligkeit",
            "Sparen Sie Energie, indem Bildschirmkontrast und Helligkeit an angezeigte Inhalte optimiert werden",
        )
        self._add_energy_row(
            getattr(self, "_sec_anzeige_und_komfort"),
            "usb_power_saving",
            "USB-Geräte beim ausgeschalteten Bildschirm beenden",
            "USB-Geräte beenden, wenn der Bildschirm ausgeschaltet ist, um Akkuverbrauch zu verringern",
            action_key="apply_usb_power",
        )

        self._add_energy_row(getattr(self, "_sec_akku_schonen"), "battery_percent", "Aktueller Akkustand", "")
        self._add_energy_row(getattr(self, "_sec_akku_schonen"), "battery_saver", "Energiesparstatus / Schonmodus", "")
        self._add_energy_row(getattr(self, "_sec_akku_schonen"), "battery_capability", "Ladebegrenzung", "")
        self._battery_guidance_label = ctk.CTkLabel(
            getattr(self, "_sec_akku_schonen"),
            text="",
            justify="left",
            anchor="w",
            wraplength=760,
            text_color=self.ogx_colors["muted"],
        )
        self._battery_guidance_label.pack(fill="x", padx=14, pady=(2, 8))
        for _sec_name in ("energiestatus", "eingesteckt", "akku", "anzeige_und_komfort", "akku_schonen"):
            ctk.CTkFrame(getattr(self, f"_sec_{_sec_name}"), fg_color="transparent", height=6).pack(fill="x")
        self._refresh_energy_dashboard_async()

    def _add_energy_row(self, section, key: str, label: str, description: str, action_key: str | None = None) -> None:
        row = ctk.CTkFrame(section, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=4)
        row.grid_columnconfigure(0, weight=1)
        row.grid_columnconfigure(1, minsize=190)
        row.grid_columnconfigure(2, minsize=92)
        row.grid_columnconfigure(3, minsize=130)
        text_host = ctk.CTkFrame(row, fg_color="transparent")
        text_host.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            text_host, text=label, anchor="w", font=ctk.CTkFont(size=12, weight="bold"),
            text_color=self.ogx_colors["text"],
        ).pack(anchor="w")
        if description:
            ctk.CTkLabel(text_host, text=description, anchor="w", wraplength=620, text_color=self.ogx_colors["muted"]).pack(anchor="w")
        value = ctk.CTkLabel(row, text="Lädt ...", anchor="w", width=190, text_color=self.ogx_colors["muted"])
        value.grid(row=0, column=1, sticky="w", padx=(8, 6))
        pill = ctk.CTkLabel(row, text="...", width=92, height=24, corner_radius=8, font=ctk.CTkFont(size=10, weight="bold"))
        pill.grid(row=0, column=2, sticky="e", padx=(4, 8))
        self._energy_value_labels[key] = value
        self._energy_status_pills[key] = pill
        if action_key:
            btn = ctk.CTkButton(
                row,
                text=self._energy_action_button_text(action_key),
                width=130,
                height=24,
                fg_color=self.ogx_colors["accent"],
                hover_color=self.ogx_colors["accent_hover"],
                command=lambda k=action_key: self._energy_action_clicked(k),
            )
            btn.grid(row=0, column=3, sticky="e")
            self._energy_action_buttons[action_key] = btn

    @staticmethod
    def _energy_action_button_text(action_key: str) -> str:
        text_map = {
            "toggle_dark_mode": "Umschalten",
            "toggle_screensaver": "Umschalten",
            "toggle_battery_percent": "Umschalten",
            "apply_ac_display": "Anwenden",
            "apply_dc_display": "Anwenden",
            "apply_ac_sleep": "Anwenden",
            "apply_dc_sleep": "Anwenden",
            "set_ac_lid_none": "Auf Keine Aktion",
            "set_dc_lid_none": "Auf Keine Aktion",
            "set_ac_power_sleep": "Auf Standbymodus",
            "set_dc_power_sleep": "Auf Standbymodus",
            "apply_ac_power_mode": "Auf Beste Leistung",
            "apply_dc_power_mode": "Auf Ausbalanciert",
            "apply_usb_power": "Anwenden",
            "apply_recommended": "Empfohlen anwenden",
            "restore_defaults": "Zurücksetzen",
            "refresh": "Aktualisieren",
        }
        return text_map.get(action_key, "Anwenden")

    _ENERGY_DASHBOARD_KEYS: tuple[str, ...] = (
        "status_ac_mode", "status_dc_mode", "status_battery_percent_setting",
        "ac_power_button", "ac_lid_action", "ac_display_timeout", "ac_sleep_timeout",
        "dc_power_button", "dc_lid_action", "dc_display_timeout", "dc_sleep_timeout",
        "dark_mode", "screensaver_disabled", "adaptive_brightness", "usb_power_saving",
        "battery_percent", "battery_saver", "battery_capability",
    )

    def _refresh_energy_dashboard_async(self) -> None:
        def worker() -> None:
            # Grosszuegiges try/except: auf unbekannten Windows-Versionen/Builds koennen die
            # powercfg-/Registry-Aufrufe unerwartet fehlschlagen (nicht nur "nicht verfuegbar",
            # sondern z. B. auch neue/veraenderte Ausgabeformate). Ohne diesen Rahmen wuerde eine
            # unerwartete Ausnahme den Hintergrund-Thread beenden und das Dashboard dauerhaft auf
            # "Laedt ..." haengen lassen, ohne dass der Nutzer je eine Rueckmeldung bekommt.
            try:
                _energy_dashboard_worker_body()
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.exception("Energie-Dashboard: unerwarteter Fehler beim Aktualisieren")
                err = SettingState(f"Fehler: {exc}", available=False)
                fallback = {key: err for key in self._ENERGY_DASHBOARD_KEYS}
                fallback["battery_guidance"] = ""
                self.ui_queue.put(("energy_dashboard_data", fallback))

        def _energy_dashboard_worker_body() -> None:
            cfg = get_config_dict(self.logger)
            ac_mode, dc_mode = read_power_mode_ac_dc()
            battery_percent_toggle = read_show_battery_percent_state()
            dark = read_dark_mode()
            screensaver = read_screensaver_state()
            adaptive = read_adaptive_brightness_state()
            usb = read_usb_power_saving_state()
            battery_pct = read_battery_percent()
            battery_saver = read_battery_saver_state()
            battery_cap = read_battery_charge_limit_capability(cfg)
            data = {
                "status_ac_mode": ac_mode,
                "status_dc_mode": dc_mode,
                "status_battery_percent_setting": self._to_setting_state_from_toggle(battery_percent_toggle, true_label="AN", false_label="AUS"),
                "ac_power_button": read_power_button_action(True),
                "ac_lid_action": read_lid_close_action(True),
                "ac_display_timeout": read_display_timeout(True),
                "ac_sleep_timeout": read_sleep_timeout(True),
                "dc_power_button": read_power_button_action(False),
                "dc_lid_action": read_lid_close_action(False),
                "dc_display_timeout": read_display_timeout(False),
                "dc_sleep_timeout": read_sleep_timeout(False),
                "dark_mode": self._to_setting_state_from_toggle(dark, true_label="AN", false_label="AUS"),
                "screensaver_disabled": self._to_setting_state_from_toggle(
                    ToggleReadResult(screensaver.available, not screensaver.on, screensaver.hint, screensaver.detail),
                    true_label="JA",
                    false_label="NEIN",
                ),
                "adaptive_brightness": adaptive,
                "usb_power_saving": usb,
                "battery_percent": battery_pct,
                "battery_saver": battery_saver,
                "battery_capability": self._to_setting_state_from_battery_cap(battery_cap),
                "battery_guidance": battery_cap.guidance,
            }
            self.ui_queue.put(("energy_dashboard_data", data))

        self._queue_status("Energieübersicht wird aktualisiert …")
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _to_setting_state_from_toggle(t: ToggleReadResult, *, true_label: str = "AN", false_label: str = "AUS") -> SettingState:
        if not t.available:
            return SettingState("Nicht verfügbar", available=False, hint=t.hint or t.detail)
        return SettingState(true_label if t.on else false_label, available=True, hint=t.hint or t.detail)

    @staticmethod
    def _to_setting_state_from_battery_cap(cap: BatteryChargeCapability) -> SettingState:
        if not cap.available:
            return SettingState("Nicht verfügbar – Hersteller-Tool erforderlich", available=False, hint=cap.guidance)
        label = f"{cap.vendor}: erkannt"
        return SettingState(label, available=True, hint=cap.guidance)

    def _desired_system_settings(self) -> dict:
        cfg = get_config_dict(self.logger)
        raw = cfg.get("system_settings", {})
        merged = dict(DEFAULT_SYSTEM_SETTINGS)
        if isinstance(raw, dict):
            merged.update(raw)
        return merged

    @staticmethod
    def _to_bool(value: object, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in ("1", "true", "yes", "ja", "an", "on", "enabled"):
            return True
        if text in ("0", "false", "no", "nein", "aus", "off", "disabled"):
            return False
        return default

    def _energy_action_clicked(self, action_key: str) -> None:
        def worker() -> None:
            # Bewusst grosszuegiges try/except: einzelne Windows-Versionen/Builds koennen
            # unerwartete powercfg-/Registry-Antworten liefern (nicht nur "Einstellung fehlt",
            # sondern z. B. auch andere Werttypen). Ohne diesen Rahmen wuerde eine unerwartete
            # Ausnahme den Hintergrund-Thread stillschweigend beenden und den Button dauerhaft
            # auf "..." (deaktiviert) haengen lassen, ohne dass der Nutzer je eine Rueckmeldung
            # bekommt.
            try:
                _energy_action_worker_body()
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.exception("Energie-Aktion %s: unerwarteter Fehler", action_key)
                self.ui_queue.put(("energy_dashboard_action_done", {"ok": False, "msg": str(exc), "action": action_key}))

        def _energy_action_worker_body() -> None:
            desired = self._desired_system_settings()
            changes: list[str] = []
            ok = True
            msg = "OK"

            def _apply_single(name: str, fn) -> None:
                nonlocal ok, msg
                try:
                    a_ok, a_msg = fn()
                except Exception as exc:  # pylint: disable=broad-except
                    a_ok, a_msg = False, str(exc)
                if a_ok:
                    changes.append(f"{name}: angewendet")
                else:
                    changes.append(f"{name}: Nicht verfügbar/Fehler ({a_msg})")
                    ok = False
                    msg = a_msg

            if action_key == "toggle_dark_mode":
                cur = read_dark_mode()
                _apply_single("Darkmode", lambda: apply_dark_mode(not cur.on, self.logger))
            elif action_key == "toggle_screensaver":
                cur = read_screensaver_state()
                _apply_single("Bildschirmschoner", lambda: apply_screensaver_state(not cur.on))
            elif action_key == "toggle_battery_percent":
                cur = read_show_battery_percent_state()
                _apply_single("Akkuprozentsatz", lambda: apply_show_battery_percent_state(not cur.on))
            elif action_key == "apply_ac_display":
                sec = int(desired.get("display_timeout_seconds", 30))
                _apply_single("Display AC", lambda: apply_display_timeout(True, sec))
            elif action_key == "apply_dc_display":
                sec = int(desired.get("display_timeout_seconds", 30))
                _apply_single("Display DC", lambda: apply_display_timeout(False, sec))
            elif action_key == "apply_ac_sleep":
                mins = 0 if self._to_bool(desired.get("ac_standby_disabled", False)) else int(desired.get("sleep_timeout_minutes", 3))
                _apply_single("Standby AC", lambda: apply_sleep_timeout(True, mins))
            elif action_key == "apply_dc_sleep":
                mins = 0 if self._to_bool(desired.get("dc_standby_disabled", False)) else int(desired.get("sleep_timeout_minutes", 3))
                _apply_single("Standby DC", lambda: apply_sleep_timeout(False, mins))
            elif action_key == "set_ac_lid_none":
                _apply_single("Deckel AC", lambda: apply_lid_close_action(True, "none"))
            elif action_key == "set_dc_lid_none":
                _apply_single("Deckel DC", lambda: apply_lid_close_action(False, "none"))
            elif action_key == "set_ac_power_sleep":
                _apply_single("Netzschalter AC", lambda: apply_power_button_action(True, "sleep"))
            elif action_key == "set_dc_power_sleep":
                _apply_single("Netzschalter DC", lambda: apply_power_button_action(False, "sleep"))
            elif action_key == "apply_ac_power_mode":
                level = str(desired.get("ac_power_mode", "best_performance"))
                _apply_single("Energiemodus AC", lambda: apply_power_mode(True, level))
            elif action_key == "apply_dc_power_mode":
                level = str(desired.get("dc_power_mode", "balanced"))
                _apply_single("Energiemodus DC", lambda: apply_power_mode(False, level))
            elif action_key == "apply_usb_power":
                _apply_single("USB-Energiesparen", lambda: apply_usb_power_saving_state(self._to_bool(desired.get("usb_power_saving_enabled", True), True)))
            elif action_key == "restore_defaults":
                from .system_settings import _run as _ss_run
                res = _ss_run(["powercfg", "/restoredefaultschemes"], timeout=60)
                if res.returncode == 0:
                    changes.append("Energieschemas: Windows-Standard wiederhergestellt")
                else:
                    tail = ((res.stderr or "") + (res.stdout or "")).strip()[:300]
                    changes.append(f"Energieschemas: Fehler ({tail or f'Code {res.returncode}'})")
                    ok = False
                    msg = tail or "powercfg /restoredefaultschemes failed"
            elif action_key == "apply_recommended":
                sec = int(desired.get("display_timeout_seconds", 30))
                mins = int(desired.get("sleep_timeout_minutes", 3))
                _apply_single("Energiemodus AC", lambda: apply_power_mode(True, str(desired.get("ac_power_mode", "best_performance"))))
                _apply_single("Energiemodus DC", lambda: apply_power_mode(False, str(desired.get("dc_power_mode", "balanced"))))
                _apply_single("Darkmode", lambda: apply_dark_mode(self._to_bool(desired.get("dark_mode_enabled", False)), self.logger))
                _apply_single("Bildschirmschoner", lambda: apply_screensaver_state(not self._to_bool(desired.get("screensaver_disabled", False))))
                _apply_single("Akkuprozentsatz", lambda: apply_show_battery_percent_state(self._to_bool(desired.get("show_battery_percent", False))))
                _apply_single("Display AC", lambda: apply_display_timeout(True, sec))
                _apply_single("Display DC", lambda: apply_display_timeout(False, sec))
                _apply_single("Standby AC", lambda: apply_sleep_timeout(True, 0 if self._to_bool(desired.get("ac_standby_disabled", False)) else mins))
                _apply_single("Standby DC", lambda: apply_sleep_timeout(False, 0 if self._to_bool(desired.get("dc_standby_disabled", False)) else mins))
                _apply_single("Deckel AC", lambda: apply_lid_close_action(True, str(desired.get("ac_lid_action", "none"))))
                _apply_single("Deckel DC", lambda: apply_lid_close_action(False, str(desired.get("dc_lid_action", "none"))))
                _apply_single("Netzschalter AC", lambda: apply_power_button_action(True, str(desired.get("ac_power_button_action", "sleep"))))
                _apply_single("Netzschalter DC", lambda: apply_power_button_action(False, str(desired.get("dc_power_button_action", "sleep"))))
                _apply_single("Adaptive Helligkeit", lambda: apply_adaptive_brightness_state(self._to_bool(desired.get("adaptive_brightness_enabled", False))))
                _apply_single("USB-Energiesparen", lambda: apply_usb_power_saving_state(self._to_bool(desired.get("usb_power_saving_enabled", True), True)))
            else:
                ok, msg = False, "Unbekannte Aktion"
            for line in changes:
                self.logger.info("Energie-Aktion: %s", line)
            if not ok and not changes:
                self.logger.warning("Energie-Aktion %s fehlgeschlagen: %s", action_key, msg)
            self.ui_queue.put(("energy_dashboard_action_done", {"ok": ok, "msg": msg, "action": action_key}))

        btn = self._energy_action_buttons.get(action_key)
        if btn is not None:
            btn.configure(state="disabled", text="...")
        self._queue_status("Wird angewendet …")
        threading.Thread(target=worker, daemon=True).start()

    def _apply_energy_dashboard_data(self, data: dict) -> None:
        rows = [(k, v) for k, v in data.items() if isinstance(v, SettingState)]
        for key, value, pill_kind in format_energy_status_rows(rows):
            lb = self._energy_value_labels.get(key)
            pill = self._energy_status_pills.get(key)
            if lb is not None and lb.winfo_exists():
                lb.configure(text=value)
            if pill is not None and pill.winfo_exists():
                if pill_kind == "on":
                    pill.configure(text="AKTIV",   fg_color="#14532D", text_color="#BBF7D0")
                elif pill_kind == "off":
                    pill.configure(text="INAKTIV", fg_color="#334155", text_color="#E2E8F0")
                elif pill_kind == "na":
                    pill.configure(text="N/V",     fg_color="#78350F", text_color="#FDE68A")
                else:
                    pill.configure(text="INFO", fg_color="#424d5d", text_color="#e7eef6")
        guidance = str(data.get("battery_guidance", "")).strip()
        if self._battery_guidance_label is not None and self._battery_guidance_label.winfo_exists():
            self._battery_guidance_label.configure(text=guidance)
        self._queue_status("Bereit")

    def _apply_energy_action_done(self, payload: dict) -> None:
        action = str(payload.get("action", ""))
        btn = self._energy_action_buttons.get(action)
        if btn is not None and btn.winfo_exists():
            btn.configure(state="normal", text=self._energy_action_button_text(action))
        if not bool(payload.get("ok")):
            self._queue_status("Fehler: siehe Log")
        self._refresh_energy_dashboard_async()

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

    def _build_system_tools_panel(self, parent) -> None:
        frame = ctk.CTkFrame(parent)
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
        tabview.add("Migration")
        tabview.add("Protokoll")
        self.system_tools_tabview = tabview
        account_tab = tabview.tab("Account")
        migration_tab = tabview.tab("Migration")
        log_tab = tabview.tab("Protokoll")
        for tab in (account_tab, migration_tab):
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

        account_status = ctk.CTkLabel(account_tab, text="bereit", width=70, corner_radius=8, fg_color="#2c5a4a", text_color="#e6f7ef")
        account_status.grid(row=0, column=2, padx=8, pady=(8, 6), sticky="e")
        migration_status = ctk.CTkLabel(migration_tab, text="bereit", width=70, corner_radius=8, fg_color="#2c5a4a", text_color="#e6f7ef")
        migration_status.grid(row=0, column=2, padx=8, pady=(8, 6), sticky="e")
        self.system_tools_status_labels = {
            "Account": account_status,
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

        safe_account_btn = ctk.CTkButton(account_tab, text="Account + Struktur (Safe)", command=run_safe_account)
        safe_account_btn.grid(row=2, column=0, columnspan=2, padx=8, pady=(0, 8), sticky="ew")
        self._attach_tooltip(safe_account_btn, "Ändert Kontovollname und prüft/erstellt die Basis-Ordnerstruktur des gewählten Benutzers.")

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
        self._apply_ogx_style(parent)

    def _build_settings_page(self, parent) -> None:
        page = ctk.CTkFrame(parent, fg_color="transparent")
        self._pages["settings"] = page
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)

        cfg = get_config_dict(self.logger)
        frame = ctk.CTkScrollableFrame(page, fg_color=self.ogx_colors["panel_alt"], corner_radius=10)
        frame.grid(row=0, column=0, sticky="nsew")
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

            def _delete_card(software_key: str, card_widget: ctk.CTkFrame) -> None:
                removed = self._remove_software_row(software_key)
                if removed:
                    card_widget.destroy()
                    provider_vars.pop(software_key, None)

            header_row = ctk.CTkFrame(card, fg_color="transparent")
            header_row.grid(row=0, column=0, columnspan=3, sticky="ew", padx=8, pady=(8, 4))
            header_row.grid_columnconfigure(0, weight=1)
            ctk.CTkCheckBox(header_row, text=f"{software.key} aktiviert", variable=enabled_var).grid(row=0, column=0, sticky="w")
            delete_btn = ctk.CTkButton(
                header_row, text="Löschen", width=90, height=24,
                fg_color=self.ogx_colors["danger"], hover_color=self.ogx_colors["danger_hover"],
            )
            delete_btn.grid(row=0, column=1, sticky="e")
            delete_btn.configure(command=lambda k=software.key, c=card: _delete_card(k, c))
            self._attach_tooltip(delete_btn, "Entfernt diese Anwendung aus der Softwareliste (über „+ Anwendung hinzufügen“ jederzeit wieder aktivierbar).")

            ctk.CTkLabel(card, text="Anzeigename").grid(row=1, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkEntry(card, textvariable=display_var).grid(row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
            ctk.CTkLabel(card, text="Chocolatey Paketname").grid(row=2, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkEntry(card, textvariable=choco_var).grid(row=2, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
            ctk.CTkLabel(card, text="WinGet ID").grid(row=3, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkEntry(card, textvariable=winget_var).grid(row=3, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
            ctk.CTkLabel(card, text="Interner Installerpfad").grid(row=4, column=0, sticky="w", padx=8, pady=4)
            ctk.CTkEntry(card, textvariable=path_var).grid(row=4, column=1, sticky="ew", padx=8, pady=4)
            browse_btn = ctk.CTkButton(card, text="Durchsuchen…", width=110, height=24)
            browse_btn.grid(row=4, column=2, sticky="w", padx=(6, 8), pady=4)
            browse_btn.configure(command=lambda v=path_var: self._browse_installer_path(v))
            self._attach_tooltip(browse_btn, "Installer-Datei auswählen, z. B. auch von einem USB-Stick (D:\\Ordner\\Setup.exe).")
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
            save_config_dict(new_config, self.logger, backup=False)
            self.runtime = load_runtime_settings(self.logger)
            self.local_source = LocalSourceService(self.logger, self.runtime.software_providers)
            if self.runtime.local_source_last_path:
                self.local_source.scan_source(self.runtime.local_source_last_path)
                self.local_source_path_var.set(f"Aktive lokale Quelle: {self.runtime.local_source_last_path}")
            runtime_by_key = {s.key: s for s in self.runtime.visible_catalog}
            self.scanner = SoftwareScanner(
                self.choco,
                self.winget,
                self.logger,
                self.runtime.visible_catalog,
                software_providers=self.runtime.software_providers,
            )
            self.installer = InstallerService(
                self.choco,
                self.winget,
                self.logger,
                runtime_by_key,
                provider_configs=self.runtime.software_providers,
                local_source_service=self.local_source,
                prefer_local_source=self.runtime.local_source_prefer_local,
                local_source_strict=self.runtime.local_source_strict,
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
                office_tools=self.runtime.office_tools,
            )
            self._apply_title()
            self.company_label.configure(text=f"Company: {self.runtime.company_name or 'Nicht gesetzt'}")
            messagebox.showinfo(APP_NAME, "Einstellungen gespeichert. Bitte Programm neu starten, damit alle Änderungen aktiv werden.")

        ctk.CTkButton(frame, text="Speichern", command=save_settings).grid(
            row=row + 1, column=0, columnspan=2, sticky="ew", padx=8, pady=12
        )
        self._apply_ogx_style(frame)

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
                        text_color=self.ogx_colors["text"],
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
                        checkmark_color=self.ogx_colors["text"],
                        text_color=self.ogx_colors["text"],
                    )
                elif isinstance(child, ctk.CTkSegmentedButton):
                    child.configure(
                        fg_color=self.ogx_colors["panel_alt"],
                        selected_color=self.ogx_colors["accent"],
                        selected_hover_color=self.ogx_colors["accent_hover"],
                        unselected_color=self.ogx_colors["panel"],
                        unselected_hover_color=self.ogx_colors["table"],
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
                        segmented_button_unselected_hover_color=self.ogx_colors["table"],
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
            "Soll FileZilla für die Provision-Abrechnung mit installiert werden?\n\n"
            "Ja = Provision-Abrechnung (FileZilla bleibt angehakt)\n"
            "Nein = keine Provision-Abrechnung (FileZilla wird abgewählt, kein Download/Install dafür)",
            parent=self,
        )
        if buchhaltung:
            self._filezilla_buchhaltung = True
            self.logger.info("Alle Pakete: FileZilla fuer Provision-Abrechnung mit ausgewaehlt.")
            self.hint_line.configure(
                text="FileZilla: Provision-Abrechnung — bleibt in der Auswahl; Hinweis bei Installation im Log."
            )
        else:
            self._filezilla_buchhaltung = None
            fz_var = self.checkbox_vars.get("filezilla")
            if fz_var is not None:
                fz_var.set(False)
            self.logger.info("Alle Pakete: FileZilla abgewaehlt (keine Provision-Abrechnung).")
            self.hint_line.configure(
                text="FileZilla: abgewaehlt (nur bei Provision-Abrechnung mit Alle Pakete auswaehlen)."
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
                html_report_path = ""
                try:
                    html_report_path = str(write_html_report(rows, "scan_report", "Compexx-InstallTool – Prüfung"))
                    self.logger.info("HTML-Report: %s", html_report_path)
                except Exception as exc:  # pylint: disable=broad-except
                    self.logger.warning("HTML-Report: %s", exc)
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
                            "html_report": html_report_path,
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
        html_report = str(data.get("html_report") or "").strip()
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

        def open_html_report() -> None:
            hp = Path(html_report) if html_report else None
            if hp and hp.exists():
                os.startfile(str(hp))  # type: ignore[attr-defined]
            else:
                messagebox.showinfo(APP_NAME, "Kein HTML-Bericht vorhanden.", parent=dialog)

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
        html_btn = ctk.CTkButton(btn_row, text="HTML-Bericht", command=open_html_report)
        html_btn.pack(side="left", padx=4)
        if not html_report:
            html_btn.configure(state="disabled")
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

    def _reboot_pending_blocking_dialog(self) -> str:
        """Returns 'restart' or 'continue'."""
        result: dict[str, str] = {"v": "continue"}
        dlg = ctk.CTkToplevel(self)
        dlg.title(APP_NAME)
        dlg.geometry("520x200")
        dlg.transient(self)
        dlg.grab_set()
        dlg.configure(fg_color=self.ogx_colors["bg"])

        def _finish(val: str) -> None:
            result["v"] = val
            dlg.destroy()

        frame = ctk.CTkFrame(dlg, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=16, pady=16)
        ctk.CTkLabel(
            frame,
            text="System benötigt Neustart. Installationen könnten fehlschlagen.",
            anchor="w",
            justify="left",
            wraplength=480,
        ).pack(fill="x", pady=(0, 14))
        row = ctk.CTkFrame(frame, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkButton(row, text="Neustart jetzt", width=160, command=lambda: _finish("restart")).pack(side="left", padx=(0, 10))
        ctk.CTkButton(row, text="Trotzdem fortfahren", width=180, command=lambda: _finish("continue")).pack(side="left")
        dlg.protocol("WM_DELETE_WINDOW", lambda: _finish("continue"))
        self.wait_window(dlg)
        return result["v"]

    def _office_no_managed_tools_removal_dialog(self) -> str:
        """Returns ``cancel`` (skip Microsoft 365 in this run) or ``try`` (generic uninstall with acknowledgement)."""
        result: dict[str, str] = {"v": "cancel"}
        dlg = ctk.CTkToplevel(self)
        dlg.title(APP_NAME)
        dlg.geometry("560x320")
        dlg.transient(self)
        dlg.grab_set()
        dlg.configure(fg_color=self.ogx_colors["bg"])

        def _finish(val: str) -> None:
            result["v"] = val
            dlg.destroy()

        frame = ctk.CTkFrame(dlg, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=16, pady=16)
        ctk.CTkLabel(
            frame,
            text=(
                "Hinweis:\n"
                "Kein erweitertes Office-Removal-Tool gefunden. Für sauberes Entfernen bitte ODT oder Microsoft Get Help bereitstellen.\n\n"
                "ODT auf Admin-Share legen und office_tools.odt_setup_path setzen "
                r"(z.B. \\fileserver\software\OfficeODT\setup.exe)."
                "\n\n"
                "Microsoft 365 für diesen Lauf auslassen (empfohlen), oder trotzdem die generische Deinstallation versuchen?"
            ),
            anchor="w",
            justify="left",
            wraplength=520,
        ).pack(fill="both", expand=True, pady=(0, 12))
        row = ctk.CTkFrame(frame, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkButton(row, text="Abbrechen (ohne Office)", width=200, command=lambda: _finish("cancel")).pack(side="left", padx=(0, 10))
        ctk.CTkButton(row, text="Trotzdem versuchen", width=180, command=lambda: _finish("try")).pack(side="left")
        dlg.protocol("WM_DELETE_WINDOW", lambda: _finish("cancel"))
        self.wait_window(dlg)
        return result["v"]

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
                self._operation_lock.release()
                return
        elif not self.dry_run_var.get() and not messagebox.askyesno(
            APP_NAME,
            "Produktivmodus: Es werden Änderungen am System vorgenommen. Fortfahren?",
            ):
                self._operation_lock.release()
                return

        dry = self.dry_run_var.get()

        office_removal_ack_keys: set[str] = set()
        keys_to_process = list(keys)
        synthetic_office_tool_missing: ReportEntry | None = None
        if mode == "remove" and not dry and "office365business" in keys:
            if not office_removal_tools_available(self.runtime.office_tools, self.logger):
                choice_office = self._office_no_managed_tools_removal_dialog()
                if choice_office == "cancel":
                    keys_to_process = [k for k in keys if k != "office365business"]
                    prev_o = self.current_states.get("office365business", SoftwareState("Installiert"))
                    hint = (
                        "Kein erweitertes Office-Removal-Tool gefunden. "
                        "Für sauberes Entfernen bitte ODT oder Microsoft Get Help bereitstellen."
                    )
                    synthetic_office_tool_missing = ReportEntry(
                        software_key="office365business",
                        package_name=prev_o.package_name or "",
                        status_before=prev_o.status,
                        action="Entfernen",
                        status_after="Hinweis: Office-Removal-Tool fehlt",
                        result="Manuell",
                        error_message=hint,
                        manual_reason=hint,
                        uninstall_method="",
                        extended_metadata="office_removal_guidance=true",
                    )
                    if not keys_to_process:
                        self._operation_lock.release()
                        messagebox.showinfo(
                            APP_NAME,
                            "Microsoft 365 wurde nicht entfernt (kein Removal-Tool; Vorgang für Office abgebrochen).",
                            parent=self,
                        )
                        return
                else:
                    office_removal_ack_keys.add("office365business")
                    self.logger.info(
                        "[OFFICE] Hinweis: ODT auf Admin-Share legen (z.B. \\\\fileserver\\software\\OfficeODT\\setup.exe) "
                        "und office_tools.odt_setup_path in config.json setzen."
                    )

        self._set_actions_enabled(False)
        self._progress_phase = "Installation" if mode == "install" else "Deinstallation"
        self.ui_queue.put(("row_progress_reset", keys))
        total = len(keys_to_process) if mode == "remove" else len(keys)
        self.ui_queue.put(("progress", {"frac": 0.0, "done": 0, "total": max(total, 1)}))
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

        def worker() -> None:
            try:
                if mode == "install" and "filezilla" in keys:
                    if self._filezilla_buchhaltung is True:
                        self.logger.info("FileZilla: Installation im Kontext Provision-Abrechnung (per Alle Pakete: Ja).")
                    else:
                        self.logger.info("FileZilla: Installation ohne Provision-Abrechnung-Markierung (manuell oder anderer Auswahlweg).")
                if mode in ("install", "remove"):
                    self.ui_queue.put(("progress_phase", "Hintergrundprüfung"))
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
                        keys_to_process,
                        self.current_states,
                        status_callback,
                        progress_callback,
                        item_start_callback=item_start_callback,
                        dry_run=dry,
                        scanner=self.scanner,
                        software_providers=self.runtime.software_providers,
                        method_progress_callback=method_progress_callback,
                        office_removal_generic_ack_keys=office_removal_ack_keys or None,
                    )
                )
                if mode == "remove" and synthetic_office_tool_missing is not None:
                    by_key = {r.software_key: r for r in rows}
                    merged_rows: list[ReportEntry] = []
                    for rk in keys:
                        if rk == "office365business" and rk not in keys_to_process:
                            merged_rows.append(synthetic_office_tool_missing)
                        elif rk in by_key:
                            merged_rows.append(by_key[rk])
                    rows = merged_rows
                if mode == "remove" and not dry:
                    rows = apply_choco_ghost_cleanup_after_uninstall(rows, self.choco, self.logger)
                    if getattr(self.runtime, "enable_backup", True):
                        try_restore_point_or_registry_export(self.logger, reports_dir=Path(REPORT_DIR))
                    self.ui_queue.put(("progress_phase", "Bereinigung"))
                    rows = run_residue_cleanup_phase(
                        rows,
                        self.runtime.software_providers,
                        lambda k: SOFTWARE_BY_KEY[k].display_name if k in SOFTWARE_BY_KEY else k,
                        self.logger,
                        self.ui_queue,
                    )
                self.last_report_file = self.report_writer.write_report(rows, "install_report")
                html_report_path = ""
                try:
                    html_report_path = str(write_html_report(rows, "install_report", "Compexx-InstallTool – Bericht"))
                    self.logger.info("HTML-Report: %s", html_report_path)
                except Exception as exc:  # pylint: disable=broad-except
                    self.logger.warning("HTML-Report: %s", exc)
                if mode == "remove" and not dry:
                    self._queue_status("Installationsstatus wird aktualisiert...")
                    fresh_remove = self.scanner.scan()
                    self.current_states.update(fresh_remove)
                    for rk in keys:
                        if rk in fresh_remove:
                            self.ui_queue.put(("software_row", (rk, fresh_remove[rk])))
                pending_after = not dry and (
                    any((r.reboot_required or "no").lower() == "yes" for r in rows)
                    or InstallerService.is_reboot_pending()
                )
                if pending_after:
                    self.ui_queue.put(("reboot_pending_after_install", None))
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
                            "html_report": html_report_path,
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
        save_config_dict(cfg, self.logger, backup=False)
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
        for btn in self._filter_count_labels.values():
            try:
                btn.configure(state=state)
            except Exception:
                pass
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
                self._apply_status_line()
                self._refresh_progress_count_label()
            elif action == "progress_phase" and isinstance(payload, str):
                self._progress_phase = str(payload)
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
            elif action == "energy_dashboard_data" and isinstance(payload, dict):
                self._apply_energy_dashboard_data(payload)
            elif action == "energy_dashboard_action_done" and isinstance(payload, dict):
                self._apply_energy_action_done(payload)
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
                    self._update_filter_counts()
                self._set_actions_enabled(bool(payload))
            elif action == "reboot_pending_after_install":
                choice = self._reboot_pending_blocking_dialog()
                if choice == "restart":
                    import subprocess
                    try:
                        subprocess.run(["shutdown", "/r", "/t", "0"], check=False)
                    except Exception:  # pylint: disable=broad-except
                        messagebox.showerror(APP_NAME, "Neustart konnte nicht gestartet werden.", parent=self)
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
                if self.system_tools_output_box is not None and self.system_tools_output_box.winfo_exists():
                    self.system_tools_output_box.insert("end", "\n".join(str(x) for x in lines) + "\n")
                    self.system_tools_output_box.see("end")
                if tab_name:
                    self._set_system_tab_status(tab_name, "bereit" if ok else "fehler")
            elif action == "device_settings_output":
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
                if self._device_settings_output_box is not None and self._device_settings_output_box.winfo_exists():
                    self._device_settings_output_box.insert("end", "\n".join(str(x) for x in lines) + "\n")
                    self._device_settings_output_box.see("end")
                if tab_name:
                    self._set_device_tab_status(tab_name, "bereit" if ok else "fehler")

        if ui_burst >= max_ui_burst:
            self.update_idletasks()
            self.after(1, self._poll_queues)
        else:
            self.after(40, self._poll_queues)
