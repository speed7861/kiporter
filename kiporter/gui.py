"""Kiporter main window."""
from __future__ import annotations

import concurrent.futures
import queue
import re
import threading
import time
import traceback
import webbrowser
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
import tkinter as tk
from tkinter import ttk

from . import (__version__, cad, cad_sources, fields, importers, kicad_control, kicad_env, library, providers,
               render, sexpr, symstyle, ui)
from .config import Settings, app_data_dir
from .providers import PartResult

APP_TITLE = "Kiporter"
SEARCH_CACHE_S = 300  # a repeated search within 5 minutes is answered from memory
NEW_LIB = "➕  New library…"

# results table: column id -> (heading, width, sort key)
COLUMNS = {
    "source": ("Source", 66, lambda r: r.source),
    "mpn": ("MPN", 130, lambda r: r.mpn.lower()),
    "mfr": ("Manufacturer", 115, lambda r: r.manufacturer.lower()),
    "package": ("Package", 80, lambda r: r.package.lower()),
    "cad": ("CAD", 100, lambda r: cad.score(r)),
    "stock": ("In stock", 78, lambda r: r.stock if r.stock is not None else -1),
    "lead": ("Lead time", 66, lambda r: r.lead_days if r.lead_days is not None else 10 ** 6),
    "price": ("Price", 60, lambda r: r.price if r.price is not None else 10 ** 9),
    "moq": ("MOQ", 42, lambda r: r.moq if r.moq is not None else 10 ** 9),
    "spn": ("Supplier PN", 100, lambda r: r.supplier_pn.lower()),
    "desc": ("Description", 200, lambda r: r.description.lower()),
}
SOURCE_COLORS = {"LCSC": "#0b6e4f", "Digi-Key": "#b3261e", "Mouser": "#1f5fa8", "Newark": "#8a4b00",
                 "Farnell": "#8a4b00", "element14": "#8a4b00", "TME": "#5b2a86", "Octopart": "#00636e"}
CAD_FILTERS = [("any", "All parts"), ("symfp", "Symbol + footprint"), ("symfp3d", "Symbol + footprint + 3D")]


class App(tk.Tk):
    def __init__(self) -> None:
        ui.set_app_id()
        super().__init__()
        self.settings = Settings.load()
        ui.setup(self, self.settings)
        self.ki = kicad_env.detect_kicad(self.settings.kicad_version, self.settings.kicad_cli_path)
        self.providers = providers.make_providers(self.settings)
        self.cad_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)  # EasyEDA: one request at a time
        # parts from other suppliers may need SamacSys, whose answers take 10-15 s each: ask several at once
        self.cad_pool_other = concurrent.futures.ThreadPoolExecutor(max_workers=6)
        self._learn_job: concurrent.futures.Future | None = None

        self.projects: list[kicad_env.ProjectInfo] = []
        self.results: dict[str, PartResult] = {}
        self.project_libs: list[library.LibraryTarget] = []
        self.global_libs: list[library.LibraryTarget] = []
        self._ui_q: queue.Queue = queue.Queue()
        self._show_all = False  # current search ignores the 'Show' filter
        self._busy = 0  # background jobs in flight
        self._spin_iid: str | None = None
        self._pending_sources: set = set()
        self._search_gen = 0
        self._search_cache: dict[tuple, tuple] = {}
        self._cad_queued: set[str] = set()
        self._pending_jobs = 0
        self._ipc_version = ""
        self._sort_col, self._sort_desc = "stock", True
        self._resort_pending = False
        self._user_picked_project = False
        self._startup_prompted = False
        self._preview_token = 0
        self._preview_part: tuple | None = None  # (token, lcsc_id, staged part)
        self._pending_cad_part: tuple[PartResult, float] | None = None
        self._zip_seen: dict[str, float] = {}
        self._zip_sizes: dict[str, int] = {}
        self._watch_start = time.time()

        self.title(f"{APP_TITLE} — A KiCad Helper")
        self.geometry("1440x900")
        self.minsize(1050, 680)
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._pump_ui_queue)
        self.after(300, self._spin)
        self.after(200, self._startup)

    # ================================================================ plumbing
    def run_bg(self, fn, on_done=None, on_error=None, quiet: bool = False) -> None:
        """Run fn() in a worker thread; deliver result/exception on the Tk thread.
        quiet: do not show the busy spinner (for routine background polling)."""
        def worker():
            try:
                res = fn()
                self._ui_q.put((on_done, res, None, not quiet))
            except Exception as e:  # noqa: BLE001
                self._ui_q.put((on_error, e, traceback.format_exc(), not quiet))
        if not quiet:
            self._busy += 1
        threading.Thread(target=worker, daemon=True).start()

    def _pump_ui_queue(self) -> None:
        try:
            for _ in range(200):
                cb, payload, tb, counted = self._ui_q.get_nowait()
                if counted:
                    self._busy = max(0, self._busy - 1)
                if tb is not None:
                    self.log(tb, quiet=True)
                    if cb:
                        cb(payload)
                    else:
                        self.log(f"ERROR: {payload}")
                elif cb:
                    cb(payload)
        except queue.Empty:
            pass
        self.after(40, self._pump_ui_queue)

    def _spin(self) -> None:
        """Animate the little circle wherever something is still loading."""
        frame = ui.spinner_frame()
        try:
            busy = self._busy > 0 or bool(self._cad_queued) or self.bom_tab.checking
            self.busy_var.set(frame if busy else "")
            for iid in self._cad_queued:  # CAD column of rows still being checked
                if self.tree.exists(iid):
                    self.tree.set(iid, "cad", frame)
            sel = self.tree.selection()
            iid = sel[0] if sel else None
            r = self.results.get(iid) if iid else None
            if r is not None and r.cad is None and self._pending_sources:
                self.tree.set(iid, "cad", frame)
                self._spin_iid = iid
            elif self._spin_iid:
                old = self._spin_iid
                self._spin_iid = None
                if old in self.results and self.tree.exists(old) and old not in self._cad_queued:
                    self.tree.set(old, "cad", cad.label(self.results[old]))
            self.preview.animate(frame)
            self.bom_tab.animate(frame)
        except tk.TclError:
            pass
        self.after(160, self._spin)

    def post(self, fn, *args) -> None:
        """Call fn(*args) on the Tk thread (safe from worker threads)."""
        self._ui_q.put((lambda _: fn(*args), None, None, False))

    def log(self, msg: str, quiet: bool = False) -> None:
        try:
            with open(app_data_dir() / "log.txt", "a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
        except OSError:
            pass
        if quiet:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def tlog(self, msg: str) -> None:
        """Thread-safe log."""
        self.post(self.log, msg)

    def set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    def _on_close(self) -> None:
        cad.save_cache()
        self.cad_pool.shutdown(wait=False, cancel_futures=True)
        self.cad_pool_other.shutdown(wait=False, cancel_futures=True)
        self.destroy()

    # ================================================================ layout
    def _build_ui(self) -> None:
        pad = {"padx": 5, "pady": 3}

        # ---- header: logo + project bar
        head = ttk.Frame(self)
        head.pack(fill="x", padx=8, pady=(8, 4))
        logo = ui.load_logo(56)
        if logo:
            ttk.Label(head, image=logo).pack(side="left", padx=(0, 10))
        else:
            ttk.Label(head, text="Kiporter", style="Title.TLabel").pack(side="left", padx=(0, 12))
        top = ttk.LabelFrame(head, text="KiCad project")
        top.pack(side="left", fill="x", expand=True)
        self.project_var = tk.StringVar()
        self.project_combo = ttk.Combobox(top, textvariable=self.project_var, state="readonly")
        self.project_combo.grid(row=0, column=0, sticky="ew", **pad)
        self.project_combo.bind("<<ComboboxSelected>>", self._on_project_picked)
        ttk.Button(top, text="↻ Detect", command=self.refresh_projects).grid(row=0, column=1, **pad)
        ttk.Button(top, text="Browse…", command=self.browse_project).grid(row=0, column=2, **pad)
        ttk.Button(top, text="Open in KiCad", command=self.open_project_in_kicad).grid(row=0, column=3, **pad)
        self.update_btn = ttk.Button(top, text="↻  Update PCB from Schematic",
                                     command=self.update_pcb, style="Accent.TButton")
        self.update_btn.grid(row=0, column=4, **pad)
        self.kicad_status_var = tk.StringVar(value="Detecting KiCad…")
        ttk.Label(top, textvariable=self.kicad_status_var, style="Muted.TLabel").grid(
            row=1, column=0, columnspan=5, sticky="w", padx=8, pady=(0, 4))
        top.columnconfigure(0, weight=1)

        # ---- log + status bar (packed before the notebook so they keep their height)
        self.status_var = tk.StringVar(value="Ready")
        bar = ttk.Frame(self, relief="sunken")
        bar.pack(fill="x", side="bottom")
        self.busy_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.busy_var, width=3, anchor="center").pack(side="left")
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)
        lf = ttk.LabelFrame(self, text="Log")
        lf.pack(fill="x", side="bottom", padx=8, pady=(2, 2))
        self.log_text = tk.Text(lf, height=6, wrap="word", font=ui.FONTS.get("mono"), state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=4)
        search_tab = ttk.Frame(self.nb)
        self.nb.add(search_tab, text="  Search & Import  ")
        self._build_search_tab(search_tab)

        from .settings_gui import SettingsTab
        from .tools_gui import BomTab, ToolsTab
        from .fab_gui import FabTab
        self.bom_tab = BomTab(self.nb, self)
        self.nb.add(self.bom_tab, text="  BOM  ")
        self.fab_tab = FabTab(self.nb, self)
        self.nb.add(self.fab_tab, text="  Fabrication  ")
        self.tools_tab = ToolsTab(self.nb, self)
        self.nb.add(self.tools_tab, text="  Library Tools  ")
        self.settings_tab = SettingsTab(self.nb, self)
        self.nb.add(self.settings_tab, text="  Settings  ")

    def _build_search_tab(self, parent: ttk.Frame) -> None:
        sb = ttk.Frame(parent)
        sb.pack(fill="x", pady=(6, 2))
        ttk.Label(sb, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        ent = ttk.Entry(sb, textvariable=self.search_var, font=ui.FONTS.get("big"))
        ent.pack(side="left", fill="x", expand=True, padx=6)
        ent.bind("<Return>", lambda e: self.search())
        ent.focus_set()
        ttk.Button(sb, text="Search", command=self.search, style="Accent.TButton").pack(side="left", padx=4)

        srcs = ttk.Frame(parent)
        srcs.pack(fill="x", pady=(0, 4))
        ttk.Label(srcs, text="Sources:").pack(side="left")
        self.source_vars: dict[str, tk.BooleanVar] = {}
        self.source_checks: dict[str, ttk.Checkbutton] = {}
        for name, flag in providers.ENABLE_FLAGS.items():
            v = tk.BooleanVar(value=getattr(self.settings, flag))
            self.source_vars[name] = v
            cb = ttk.Checkbutton(srcs, variable=v, command=self._sources_changed)
            cb.pack(side="left", padx=(6, 0))
            self.source_checks[name] = cb
        ttk.Label(srcs, text="   Show:").pack(side="left")
        self.cad_filter_combo = ttk.Combobox(srcs, state="readonly", width=24, values=[l for _, l in CAD_FILTERS])
        self.cad_filter_combo.current(next((i for i, (k, _) in enumerate(CAD_FILTERS)
                                            if k == self.settings.cad_filter), 0))
        self.cad_filter_combo.pack(side="left", padx=4)
        self.cad_filter_combo.bind("<<ComboboxSelected>>", lambda e: self._cad_filter_changed())
        ttk.Label(srcs, text="   CAD for other suppliers:").pack(side="left")
        self.cad_src_combo = ttk.Combobox(srcs, state="readonly", width=16,
                                          values=["Auto (your order)"] + cad_sources.SOURCES)
        self.cad_src_combo.set(self.settings.cad_source if self.settings.cad_source in cad_sources.SOURCES
                               else "Auto (your order)")
        self.cad_src_combo.pack(side="left", padx=4)
        self.cad_src_combo.bind("<<ComboboxSelected>>", lambda e: self._cad_source_changed())
        self.in_stock_var = tk.BooleanVar(value=self.settings.in_stock_only)
        ttk.Checkbutton(srcs, text="In stock only", variable=self.in_stock_var,
                        command=self._sources_changed).pack(side="left", padx=(8, 0))
        self._refresh_source_labels()

        pw = ttk.PanedWindow(parent, orient="horizontal")
        pw.pack(fill="both", expand=True, pady=2)

        res_frame = ttk.Frame(pw)
        self.tree = ttk.Treeview(res_frame, columns=list(COLUMNS), show="headings", selectmode="browse")
        for c, (h, w, _) in COLUMNS.items():
            self.tree.heading(c, text=h, command=lambda c=c: self._sort_clicked(c))
            anchor = "e" if c in ("stock", "lead", "price", "moq") else "center" if c == "cad" else "w"
            self.tree.column(c, width=w, stretch=False, anchor=anchor)
        ysb = ttk.Scrollbar(res_frame, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(res_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        res_frame.rowconfigure(0, weight=1)
        res_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._selection_changed())
        self.tree.bind("<Double-1>", lambda e: self.import_selected())
        for src, col in SOURCE_COLORS.items():
            self.tree.tag_configure(src, foreground=col)
        self.tree.tag_configure("nostock", foreground="#999999")
        pw.add(res_frame, weight=3)

        right = ttk.Frame(pw)
        from .preview_gui import PreviewPane
        mrow = ttk.Frame(right)
        mrow.pack(fill="x", padx=2, pady=(0, 3))
        ttk.Label(mrow, text="Model from:").pack(side="left")
        self.model_combo = ttk.Combobox(mrow, state="readonly", width=52)
        self.model_combo.pack(side="left", padx=4, fill="x", expand=True)
        self.model_combo.bind("<<ComboboxSelected>>", lambda e: self._model_picked())
        self.model_status = tk.StringVar()
        ttk.Label(mrow, textvariable=self.model_status, style="Muted.TLabel").pack(side="left", padx=4)
        self._hits: list = []
        self._hit = None
        self.preview = PreviewPane(right, on_3d_requested=self._render_3d)
        self.preview.pack(fill="both", expand=True, padx=2)
        links = ttk.Frame(right)
        links.pack(fill="x", pady=2)
        ttk.Button(links, text="Datasheet", command=lambda: self._open_selected("datasheet")).pack(side="left", padx=2)
        ttk.Button(links, text="Product page", command=lambda: self._open_selected("url")).pack(side="left", padx=2)
        ttk.Button(links, text="Find CAD elsewhere…", command=self.find_cad_elsewhere).pack(side="left", padx=2)

        imp = ttk.LabelFrame(right, text="Import into")
        imp.pack(fill="x", pady=4, padx=2)
        self.target_var = tk.StringVar(value=self.settings.default_target)
        self.rb_project = ttk.Radiobutton(imp, text="Project library", variable=self.target_var,
                                          value="project", command=self._target_changed)
        self.rb_project.grid(row=0, column=0, sticky="w", padx=6, pady=2)
        self.project_lib_combo = ttk.Combobox(imp, state="readonly", width=46)
        self.project_lib_combo.grid(row=0, column=1, sticky="ew", padx=6, pady=2)
        self.project_lib_combo.bind("<<ComboboxSelected>>", lambda e: self._lib_picked("project"))
        self.rb_global = ttk.Radiobutton(imp, text="Local library", variable=self.target_var,
                                         value="global", command=self._target_changed)
        self.rb_global.grid(row=1, column=0, sticky="w", padx=6, pady=2)
        self.global_lib_combo = ttk.Combobox(imp, state="readonly", width=46)
        self.global_lib_combo.grid(row=1, column=1, sticky="ew", padx=6, pady=2)
        self.global_lib_combo.bind("<<ComboboxSelected>>", lambda e: self._lib_picked("global"))
        self.target_path_var = tk.StringVar()
        ttk.Label(imp, textvariable=self.target_path_var, style="Muted.TLabel", wraplength=470).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=8)
        imp.columnconfigure(1, weight=1)
        opts = ttk.Frame(imp)
        opts.grid(row=3, column=0, columnspan=2, sticky="ew", padx=6, pady=3)
        s = self.settings
        self.want_sym = tk.BooleanVar(value=s.import_symbol)
        self.want_fp = tk.BooleanVar(value=s.import_footprint)
        self.want_3d = tk.BooleanVar(value=s.import_3d)
        self.overwrite = tk.BooleanVar(value=s.overwrite_existing)
        for text, var in (("Symbol", self.want_sym), ("Footprint", self.want_fp),
                          ("3D model", self.want_3d), ("Overwrite existing", self.overwrite)):
            ttk.Checkbutton(opts, text=text, variable=var).pack(side="left", padx=(0, 8))
        btns = ttk.Frame(imp)
        btns.grid(row=4, column=0, columnspan=2, sticky="ew", padx=6, pady=(2, 6))
        ttk.Button(btns, text="⬇  Import selected part", command=self.import_selected,
                   style="Accent.TButton").pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(btns, text="Import ZIP…", command=self.import_zip_dialog).pack(side="left")
        self.watch_var = tk.BooleanVar(value=self.settings.watch_downloads)
        self.watch_check = ttk.Checkbutton(imp, variable=self.watch_var, command=self._watch_toggled)
        self.watch_check.grid(row=5, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 6))
        pw.add(right, weight=2)

        def place_sash(e):  # once the tab has its real size, give the results table ~62%
            if e.width > 200:
                pw.sashpos(0, int(e.width * 0.62))
                pw.unbind("<Configure>")
        pw.bind("<Configure>", place_sash)

    # ================================================================ startup / setup
    def _startup(self) -> None:
        ki = self.ki
        self.log(f"Kiporter {__version__} • {ki.label}")
        self.log(f"KiCad config: {ki.config_dir} • kicad-cli: {ki.kicad_cli or 'not found'}")
        if not ki.config_dir.exists():
            messagebox.showwarning(APP_TITLE, "Could not find a KiCad configuration folder.\n"
                                   "Start KiCad once, then reopen Kiporter.")
        if not self.settings.first_run_done:
            self.auto_setup(first_run=True)
        self.refresh_projects()
        self.after(max(5, self.settings.auto_refresh_s) * 1000, self._auto_refresh)
        self.after(2000, self._watch_tick)

    def auto_setup(self, first_run: bool = False) -> None:
        """Create/register the default local library and enable KiCad's IPC API."""
        notes = []
        running = bool(kicad_env.running_kicad_processes())
        try:
            msgs = library.global_target(self.settings, self.ki).ensure()
            for m in msgs:
                self.log(m)
            notes.append(f"Local library '{self.settings.global_lib_name}' is ready at\n  {self.settings.global_lib_dir}")
            if msgs and running:
                notes.append("KiCad is running: restart it once so it picks up the new library.")
        except OSError as e:
            notes.append(f"Could not create the local library: {e}")
        if not kicad_env.ipc_api_enabled(self.ki):
            if running:
                notes.append("Enable KiCad's API for the best 'Update PCB' integration:\n"
                             "  KiCad → Preferences → Plugins → 'Enable KiCad API'.")
            elif kicad_env.enable_ipc_api(self.ki):
                notes.append("Enabled KiCad's IPC API server (used for 'Update PCB').")
        else:
            notes.append("KiCad IPC API is enabled.")
        self.settings.first_run_done = True
        self.settings.save()
        self.log("Automatic setup complete.")
        if first_run:
            msg = ("Welcome to Kiporter! Automatic setup finished:\n\n" + "\n\n".join(notes) +
                   "\n\nLCSC search works right away. Connect Digi-Key, Mouser and other suppliers "
                   "(free API keys - step-by-step directions included) for their stock and lead times.\n\n"
                   "Connect suppliers now?")
            if messagebox.askyesno(APP_TITLE, msg):
                self.open_accounts()

    def apply_settings(self) -> None:
        """Push saved settings into the live UI."""
        s = self.settings
        new_ki = kicad_env.detect_kicad(s.kicad_version, s.kicad_cli_path)
        if (new_ki.version, new_ki.cli) != (self.ki.version, self.ki.cli):
            self.ki = new_ki
            self.log(f"Using {new_ki.label}")
            self.refresh_projects()
        for name, flag in providers.ENABLE_FLAGS.items():
            self.source_vars[name].set(getattr(s, flag))
        self.in_stock_var.set(s.in_stock_only)
        self.cad_filter_combo.current(next((i for i, (k, _) in enumerate(CAD_FILTERS) if k == s.cad_filter), 0))
        self.want_sym.set(s.import_symbol)
        self.want_fp.set(s.import_footprint)
        self.want_3d.set(s.import_3d)
        self.overwrite.set(s.overwrite_existing)
        self.watch_var.set(s.watch_downloads)
        self.target_var.set(s.default_target if self.current_project() or s.default_target == "global" else "global")
        self._refresh_source_labels()
        self.refresh_libraries()
        self._apply_sort()
        if hasattr(self, "bom_tab"):
            self.bom_tab.settings_changed()

    def _prompt_destination(self) -> None:
        if self._startup_prompted or not self.settings.ask_target_on_startup:
            return
        self._startup_prompted = True
        DestinationDialog(self)

    # ================================================================ projects
    def refresh_projects(self) -> None:
        self.kicad_status_var.set("Detecting KiCad projects…")

        def work():
            projects, status = kicad_env.detect_projects(self.ki)
            status["ipc_version"] = kicad_control.ipc_version() if status.get("running") else ""
            return projects, status

        self.run_bg(work, self._projects_loaded, quiet=True)

    def _auto_refresh(self) -> None:
        self.run_bg(lambda: kicad_env.detect_projects(self.ki), self._projects_loaded, quiet=True)
        self.after(max(5, self.settings.auto_refresh_s) * 1000, self._auto_refresh)

    def _projects_loaded(self, res) -> None:
        projects, status = res
        prev = self.current_project()
        extra = Path(self.settings.last_project) if self.settings.last_project else None
        if extra and extra.exists() and all(p.pro_file != extra for p in projects):
            projects.append(kicad_env.ProjectInfo(extra))
        changed = [p.pro_file for p in projects] != [p.pro_file for p in self.projects]
        self.projects = projects
        self.project_combo["values"] = [p.label() for p in projects]
        pick = None
        if prev and self._user_picked_project:
            pick = next((i for i, p in enumerate(projects) if p.pro_file == prev.pro_file), None)
        if pick is None:
            pick = next((i for i, p in enumerate(projects) if p.is_open), None)
        if pick is None and extra:
            pick = next((i for i, p in enumerate(projects) if p.pro_file == extra), None)
        if pick is None and projects:
            pick = 0
        if pick is not None:
            self.project_combo.current(pick)
        if status.get("running"):
            if status.get("ipc_version"):
                self._ipc_version = status["ipc_version"]
            ipc = f" • IPC API v{self._ipc_version}" if self._ipc_version else (
                " • IPC API connected" if status.get("ipc") else "")
            opened = [p.name for p in projects if p.is_open]
            st = f"KiCad {self.ki.version} running{ipc}"
            st += f" • open project: {', '.join(opened)}" if opened else " • no open project detected"
        else:
            st = f"KiCad {self.ki.version} is not running • showing recent projects"
        self.kicad_status_var.set(st)
        cur = self.current_project()
        if changed or not prev or not cur or prev.pro_file != cur.pro_file:
            self.refresh_libraries()
        self._prompt_destination()

    def _on_project_picked(self, _e=None) -> None:
        self._user_picked_project = True
        p = self.current_project()
        if p:
            self.settings.last_project = str(p.pro_file)
            self.settings.save()
        self.refresh_libraries()

    def current_project(self) -> kicad_env.ProjectInfo | None:
        i = self.project_combo.current() if self.projects else -1
        return self.projects[i] if 0 <= i < len(self.projects) else None

    def browse_project(self) -> None:
        f = filedialog.askopenfilename(title="Choose KiCad project",
                                       filetypes=[("KiCad project", "*.kicad_pro"), ("All files", "*")])
        if not f:
            return
        p = Path(f)
        existing = next((i for i, x in enumerate(self.projects) if x.pro_file == p), None)
        if existing is None:
            self.projects.insert(0, kicad_env.ProjectInfo(p))
            self.project_combo["values"] = [x.label() for x in self.projects]
            existing = 0
        self.project_combo.current(existing)
        self._on_project_picked()

    def open_project_in_kicad(self) -> None:
        p = self.current_project()
        if not p:
            messagebox.showinfo(APP_TITLE, "Pick a project first.")
            return
        ok, msg = kicad_control.open_in_kicad(self.ki, p)
        self.log(msg)
        self.after(8000, self.refresh_projects)

    # ================================================================ import destinations
    def refresh_libraries(self) -> None:
        p = self.current_project()
        try:
            self.project_libs = library.list_targets(self.settings, self.ki, p.dir if p else None, "project")
            self.global_libs = library.list_targets(self.settings, self.ki, p.dir if p else None, "global")
        except Exception as e:  # noqa: BLE001
            self.log(f"Could not read library tables: {e}")
        self._fill_lib_combo(self.project_lib_combo, self.project_libs, self.settings.project_lib_choice)
        self._fill_lib_combo(self.global_lib_combo, self.global_libs, self.settings.global_lib_choice)
        if not p:
            self.rb_project.configure(state="disabled")
            self.project_lib_combo.configure(state="disabled")
            if self.target_var.get() == "project":
                self.target_var.set("global")
        else:
            self.rb_project.configure(state="normal")
            self.project_lib_combo.configure(state="readonly")
        self._target_changed()
        if hasattr(self, "tools_tab"):
            self.tools_tab.libraries_changed()
        if hasattr(self, "fab_tab"):
            self.fab_tab.refresh()

    @staticmethod
    def _fill_lib_combo(combo: ttk.Combobox, libs: list[library.LibraryTarget], choice: str) -> None:
        combo["values"] = [t.label() for t in libs] + [NEW_LIB]
        idx = next((i for i, t in enumerate(libs) if t.name == choice), 0)
        if libs:
            combo.current(idx)
        else:
            combo.set("")

    def _lib_picked(self, kind: str) -> None:
        combo = self.project_lib_combo if kind == "project" else self.global_lib_combo
        libs = self.project_libs if kind == "project" else self.global_libs
        if combo.get() == NEW_LIB:
            t = self.create_library(kind)
            self.refresh_libraries()
            if t:
                self.select_library(kind, t.name)
            return
        i = combo.current()
        if 0 <= i < len(libs):
            if kind == "project":
                self.settings.project_lib_choice = libs[i].name
            else:
                self.settings.global_lib_choice = libs[i].name
            self.settings.save()
        self.target_var.set(kind)
        self._target_changed()

    def select_library(self, kind: str, name: str) -> None:
        libs = self.project_libs if kind == "project" else self.global_libs
        combo = self.project_lib_combo if kind == "project" else self.global_lib_combo
        i = next((i for i, t in enumerate(libs) if t.name == name), None)
        if i is not None:
            combo.current(i)
            self._lib_picked(kind)

    def create_library(self, kind: str, parent=None) -> library.LibraryTarget | None:
        p = self.current_project()
        if kind == "project" and not p:
            messagebox.showwarning(APP_TITLE, "Pick a project first.", parent=parent or self)
            return None
        name = simpledialog.askstring(APP_TITLE, "Name (nickname) of the new library:", parent=parent or self)
        if not name:
            return None
        name = library.safe_name(name).replace(" ", "_")
        if kind == "project":
            t = library.project_target(self.settings, p.dir, name)
        else:
            folder = filedialog.askdirectory(title=f"Folder for library '{name}'",
                                             initialdir=self.settings.global_lib_dir, parent=parent or self)
            if not folder:
                return None
            t = library.global_target(self.settings, self.ki, name, Path(folder))
        for m in t.ensure():
            self.log(m)
        if kind == "project":
            self.settings.project_lib_choice = name
        else:
            self.settings.global_lib_choice = name
        self.settings.save()
        return t

    def _target_changed(self) -> None:
        t = self._selected_target()
        self.target_path_var.set(f"→ {t.sym_file}" if t else "")
        if self.target_var.get() != self.settings.default_target:
            self.settings.default_target = self.target_var.get()
            self.settings.save()

    def _selected_target(self) -> library.LibraryTarget | None:
        kind = self.target_var.get()
        combo = self.project_lib_combo if kind == "project" else self.global_lib_combo
        libs = self.project_libs if kind == "project" else self.global_libs
        i = combo.current()
        return libs[i] if 0 <= i < len(libs) else None

    def current_target(self) -> library.LibraryTarget | None:
        t = self._selected_target()
        if not t:
            messagebox.showwarning(APP_TITLE, "Choose a library to import into (Project library needs a "
                                   "project: open it in KiCad, click Detect, or Browse to it).")
        return t

    # ================================================================ suppliers
    def _refresh_source_labels(self) -> None:
        for name, cb in self.source_checks.items():
            prov = self.providers[name]
            if prov.configured or not prov.key_fields:
                cb.configure(text=name, style="TCheckbutton")
            else:  # red until an API key is entered (Settings -> Suppliers)
                cb.configure(text=f"{name} (not connected)", style="Disconnected.TCheckbutton")

    def _sources_changed(self) -> None:
        for name, flag in providers.ENABLE_FLAGS.items():
            setattr(self.settings, flag, self.source_vars[name].get())
        self.settings.in_stock_only = self.in_stock_var.get()
        self.settings.save()

    def ensure_connected(self, names: list[str]) -> list[str]:
        """Prompt to connect enabled-but-unconfigured suppliers. Returns the usable ones."""
        from .settings_gui import AccountsDialog
        usable = []
        for name in names:
            prov = self.providers[name]
            if prov.configured:
                usable.append(name)
                continue
            if name in self.settings.declined_connect:
                self.log(f"{name} skipped: not connected (Settings → Suppliers to connect).")
                continue
            if messagebox.askyesno(APP_TITLE, f"{name} is not connected yet.\n\nIt needs a free API key: you "
                                              f"sign in with your normal {name} account and create one. "
                                              f"Step-by-step directions are on the next screen.\n\n"
                                              f"Connect {name} now? (No = skip it from now on.)"):
                AccountsDialog(self, focus=name, wait=True)
                if prov.configured:
                    usable.append(name)
            else:
                self.settings.declined_connect.append(name)
                self.source_vars[name].set(False)
                self._sources_changed()
        self._refresh_source_labels()
        return usable

    # ================================================================ search
    def search(self, show_all: bool = False) -> None:
        """show_all: do not apply the 'Show' CAD filter to this search (used when looking up one known part)."""
        kw = self.search_var.get().strip()
        if not kw:
            return
        self._show_all = show_all
        self._sources_changed()
        wanted = [n for n, v in self.source_vars.items() if v.get()]
        names = self.ensure_connected(wanted)
        if not names:
            self.set_status("No search source enabled/connected")
            return
        self._search_gen += 1
        gen = self._search_gen
        self._cad_queued.clear()
        # rows hidden by a filter are detached, not listed as children: delete those too
        self.tree.delete(*[i for i in set(self.tree.get_children()) | set(self.results) if self.tree.exists(i)])
        self.results.clear()
        self.preview.clear()
        self._pending_jobs = len(names)
        self.set_status(f"Searching {', '.join(names)} for '{kw}'…")
        limit = max(1, min(int(self.settings.search_limit or 50), 50))
        if self.settings.auto_check_cad or self.settings.cad_filter != "any":
            # one catalogue request tells which of the coming results have CAD models; it runs first in
            # the CAD queue, so the per-part checks that follow mostly find their answer already there
            self._learn_job = self.cad_pool.submit(self._learn_cad, gen, kw)
        for name in names:
            prov = self.providers[name]
            key = (name, kw.lower(), limit)
            hit = self._search_cache.get(key)
            if hit and time.time() - hit[0] < SEARCH_CACHE_S:  # same search again: no need to ask twice
                self._add_results(gen, name, hit[1])
                continue

            def got(res, n=name, key=key):
                self._search_cache[key] = (time.time(), res)
                self._add_results(gen, n, res)
            self.run_bg(lambda p=prov: p.search(kw, limit), got, lambda e, n=name: self._search_failed(gen, n, e))

    def _search_done_one(self, gen: int) -> None:
        if gen != self._search_gen:
            return
        self._pending_jobs -= 1
        if self._pending_jobs <= 0:
            self._update_count_status()

    def _update_count_status(self) -> None:
        shown = len(self.tree.get_children())
        in_stock = sum(1 for r in self.results.values() if r.stock)
        pending = len(self._cad_queued)
        msg = f"{shown} of {len(self.results)} results shown ({in_stock} in stock)"
        hidden = len(self.results) - shown
        label = dict(CAD_FILTERS).get(self.settings.cad_filter, "")
        if hidden and self.settings.cad_filter != "any":
            msg += f" • {hidden} hidden because 'Show' is set to '{label}'"
        elif self._show_all and self.settings.cad_filter != "any":
            msg += " • showing every match for this part ('Show' filter not applied)"
        if cad.blocked_for():
            msg += " • EasyEDA is blocking CAD lookups for a few minutes (cached parts still work)"
        elif pending:
            msg += f" • checking CAD models ({pending} left)…"
        self.set_status(msg + " — double-click a part to import it")

    def _add_results(self, gen: int, name: str, res: list[PartResult]) -> None:
        if gen != self._search_gen:
            return
        if self.settings.in_stock_only:
            res = [r for r in res if r.stock]
        for r in res:
            iid = f"r{len(self.results)}"
            self.results[iid] = r
            tags = (r.source,) if r.stock else (r.source, "nostock")
            if r.source == "LCSC" or self.settings.cad_source in ("auto", "EasyEDA"):
                cad.fill_known(r)  # from the cache - no network
            self.tree.insert("", "end", iid=iid, tags=tags, values=self._row_values(r))
        if self.settings.auto_check_cad or self.settings.cad_filter != "any":
            self._queue_cad_checks()
        self._apply_sort()
        self._autosize_columns()
        self.log(f"{name}: {len(res)} results")
        self._search_done_one(gen)

    @staticmethod
    def _row_values(r: PartResult) -> tuple:
        return (r.source, r.mpn, r.manufacturer, r.package, cad.label(r), r.stock_str, r.lead_str, r.price_str,
                r.moq if r.moq is not None else "", r.supplier_pn, r.description)

    def _learn_cad(self, gen: int, kw: str) -> None:
        if gen != self._search_gen or cad.blocked_for():
            return
        try:
            cad.learn(kw)
        except Exception as e:  # noqa: BLE001
            self.tlog(f"CAD catalogue: {e}")

    def _queue_cad_checks(self) -> None:
        """Find out which rows have CAD models (one at a time; LCSC rows first, they are quickest)."""
        gen = self._search_gen
        for iid, r in sorted(self.results.items(), key=lambda kv: kv[1].source != "LCSC"):
            if r.cad is None and iid not in self._cad_queued:
                self._cad_queued.add(iid)
                pool = self.cad_pool if r.source == "LCSC" else self.cad_pool_other
                pool.submit(self._check_cad, gen, iid, r)
        self._update_count_status()

    def _check_cad(self, gen: int, iid: str, r: PartResult) -> None:
        if gen != self._search_gen:
            return
        job = self._learn_job
        if job is not None and r.source != "LCSC":  # the catalogue answer may already cover this part
            try:
                job.result(timeout=30)
            except Exception:  # noqa: BLE001
                pass
        if not cad.blocked_for():
            try:
                cad_sources.check_availability(r, self.settings)
            except cad.EasyedaBlocked:
                self.tlog("EasyEDA is blocking requests for a few minutes - CAD checks paused.")
            except Exception as e:  # noqa: BLE001
                self.tlog(f"CAD check {r.mpn}: {e}")
        self.post(self._cad_checked, gen, iid)

    def _cad_checked(self, gen: int, iid: str) -> None:
        self._cad_queued.discard(iid)
        if gen != self._search_gen or iid not in self.results:
            return
        if self.tree.exists(iid):
            self.tree.set(iid, "cad", cad.label(self.results[iid]))
            self.tree.set(iid, "package", self.results[iid].package)
            if iid in self.tree.selection():  # update the "checking..." line in the details
                self._show_details(self.results[iid])
        if not self._resort_pending:  # batch re-sorting/filtering while results stream in
            self._resort_pending = True
            self.after(400, self._deferred_resort)

    def _deferred_resort(self) -> None:
        self._resort_pending = False
        self._apply_sort()
        self._autosize_columns()
        self._update_count_status()

    def _autosize_columns(self) -> None:
        """Fit columns to their text so long descriptions can be read by scrolling sideways."""
        from tkinter import font as tkfont
        f = tkfont.nametofont("TkDefaultFont")
        caps = {"mpn": 320, "mfr": 260, "package": 220, "spn": 260, "cad": 140, "desc": 3000}
        idx = list(COLUMNS)
        rows = [self.tree.item(i, "values") for i in self.tree.get_children()]
        for col, cap in caps.items():
            j = idx.index(col)
            longest = max([f.measure(str(r[j])) for r in rows if len(r) > j] + [f.measure(COLUMNS[col][0]) + 18])
            self.tree.column(col, width=min(longest + 18, cap), stretch=False)

    def _cad_source_changed(self) -> None:
        v = self.cad_src_combo.get()
        self.settings.cad_source = v if v in cad_sources.SOURCES else "auto"
        self.settings.save()
        for r in self.results.values():
            if r.source == "LCSC":
                continue  # LCSC parts always use their own EasyEDA model
            r.cad, r.cad_source = None, ""
            if self.settings.cad_source in ("auto", "EasyEDA"):
                cad.fill_known(r)
            self._refresh_row(r)
        if self.settings.cad_filter != "any" or self.settings.auto_check_cad:
            self._cad_queued.clear()
            self._queue_cad_checks()
        self._apply_sort()
        self._selection_changed()

    def _cad_filter_changed(self) -> None:
        self.settings.cad_filter = CAD_FILTERS[max(self.cad_filter_combo.current(), 0)][0]
        self._show_all = False  # the user picked a filter: apply it
        self.settings.save()
        if self.settings.cad_filter != "any":
            self._queue_cad_checks()
        self._apply_sort()
        self._update_count_status()

    def _passes(self, r: PartResult) -> bool:
        need = {"symfp": 2, "symfp3d": 3}.get(self.settings.cad_filter, 0)
        if not need or self._show_all:
            return True
        sc = cad.score(r)
        return sc == -1 or sc >= need  # keep unknowns visible until their check finishes

    def _search_failed(self, gen: int, name: str, e: Exception) -> None:
        self.log(f"{name}: {e}")
        self._search_done_one(gen)

    def _sort_clicked(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, col in ("stock", "cad")
        self._apply_sort()

    def _apply_sort(self) -> None:
        if not hasattr(self, "tree"):
            return
        key = COLUMNS[self._sort_col][2]
        visible = [i for i, r in self.results.items() if self._passes(r)]
        for i in set(self.results) - set(visible):
            if self.tree.exists(i):
                self.tree.detach(i)
        # secondary key: stock, so equal CAD scores still show the most available part first
        rows = sorted(visible, key=lambda i: (key(self.results[i]), COLUMNS["stock"][2](self.results[i])),
                      reverse=self._sort_desc)
        for idx, i in enumerate(rows):
            self.tree.move(i, "", idx)
        for c, (h, _, _) in COLUMNS.items():
            arrow = (" ▼" if self._sort_desc else " ▲") if c == self._sort_col else ""
            self.tree.heading(c, text=h + arrow)

    def selected_part(self) -> PartResult | None:
        sel = self.tree.selection()
        return self.results.get(sel[0]) if sel else None

    # ================================================================ preview / CAD sources
    def _selection_changed(self) -> None:
        r = self.selected_part()
        self._preview_token += 1
        token = self._preview_token
        self._preview_part = None
        self._hits, self._hit = [], None
        self.model_combo.configure(values=[])
        self.model_combo.set("")
        if not r:
            return
        self._show_details(r)
        self.preview.set_photo(None, "Loading photo…" if r.image_url and self.settings.show_images
                               else "No photo available")
        msg = "Looking for CAD models…" if self.settings.auto_preview else "Preview off (Settings → Search)"
        self.model_status.set(msg)
        self.preview.set_symbol(None, None, msg)
        self.preview.set_footprint(None, msg)
        self.preview.set_3d(None, "The 3D view appears here once the model has loaded" if
                            self.settings.preview_3d else "3D preview off (Settings → Search)")
        # debounce: only load if the user stays on this row
        self.after(120, lambda: self._load_preview(token, r))

    def _load_preview(self, token: int, r: PartResult) -> None:
        if token != self._preview_token:
            return
        if self.settings.show_images and r.image_url:
            self.run_bg(lambda: cad.image_bytes(r.image_url),
                        lambda data: token == self._preview_token and self.preview.set_photo(
                            data, "No photo available"))
        if not self.settings.auto_preview:
            return
        # ask every usable source in parallel; show results as they arrive
        self._user_picked = False
        srcs = [x for x in cad_sources.order(self.settings, r) if cad_sources.usable(x, self.settings)]
        self._pending_sources = set(srcs)
        for src in srcs:
            self.run_bg(lambda src=src: cad_sources.find(src, r, self.settings),
                        lambda hits, src=src: self._source_found(token, r, src, hits),
                        lambda e, src=src: self._source_found(token, r, src, [], e))

    def _rank(self, h) -> tuple:
        r = self.selected_part()
        order = cad_sources.order(self.settings, r)
        forced = self.settings.cad_source if not (r and r.source == "LCSC") else "auto"
        return (forced != "auto" and h.source != forced, h.browser_only or not (h.sym and h.fp),
                order.index(h.source) if h.source in order else 99)

    def _source_found(self, token: int, r: PartResult, src: str, hits: list, err=None) -> None:
        if token != self._preview_token:
            return
        self._pending_sources.discard(src)
        if err is not None:
            self.log(f"{src}: {err}")
        self._hits = sorted(self._hits + list(hits), key=self._rank)
        self.model_combo["values"] = [h.label for h in self._hits]
        waiting = ", ".join(x for x in cad_sources.order(self.settings, r) if x in self._pending_sources)
        self.model_status.set(f"still asking {waiting}…" if waiting else f"{len(self._hits)} source(s)")
        if self._hit is not None:
            self.model_combo.current(self._hits.index(self._hit))
        if not self._hits:
            if not self._pending_sources:
                tried = ", ".join(x for x in cad_sources.order(self.settings, r) if cad_sources.usable(x, self.settings))
                msg = (f"No CAD model found for {r.mpn} ({tried}).\n"
                       + (cad.blocked_message() if cad.blocked_for() else "Try 'Find CAD elsewhere…'."))
                self.model_status.set("No CAD model found")
                self.preview.set_symbol(None, None, msg)
                self.preview.set_footprint(None, msg)
                self.preview.set_3d(None, msg)
            return
        best = self._hits[0]
        if self._hit is None or (not self._user_picked and self._rank(best) < self._rank(self._hit)):
            self.model_combo.current(0)
            if not best.browser_only:
                r.cad, r.cad_source = best.avail, best.source
                if best.source == "EasyEDA":
                    r.cad_lcsc = best.ref
                self._refresh_row(r)
            self._show_hit(token, r, best)

    def _model_picked(self) -> None:
        r = self.selected_part()
        i = self.model_combo.current()
        self._user_picked = True
        if r and 0 <= i < len(self._hits):
            self._show_hit(self._preview_token, r, self._hits[i])

    def _refresh_row(self, r: PartResult) -> None:
        iid = next((i for i, x in self.results.items() if x is r), None)
        if iid and self.tree.exists(iid):
            self.tree.set(iid, "cad", cad.label(r))
            self.tree.set(iid, "package", r.package)

    def _show_hit(self, token: int, r: PartResult, h) -> None:
        self._hit = h
        self._preview_part = None
        self._show_details(r)
        if h.browser_only:
            msg = (f"{h.source} models download in your browser ({h.detail}).\nClick 'Import selected part' to open "
                   f"it - the zip you save is imported automatically.")
            self.preview.set_symbol(None, None, msg)
            self.preview.set_footprint(None, msg)
            self.preview.set_3d(None, msg)
            return
        loading = f"Loading the {h.source} model…"
        self.preview.set_symbol(None, None, loading)
        self.preview.set_footprint(None, loading)

        def draw(part):
            try:
                lib = sexpr.parse(part.symbol_files[0].read_text(encoding="utf-8", errors="replace")) \
                    if part.symbol_files else None
                names = render.symbol_names(lib) if lib else []
                self.preview.set_symbol(lib, names[0] if names else None, "This model has no symbol")
                fpn = sexpr.parse(part.footprint_files[0].read_text(encoding="utf-8", errors="replace")) \
                    if part.footprint_files else None
                self.preview.set_footprint(fpn, "This model has no footprint")
            except (ValueError, OSError) as e:
                self.preview.set_symbol(None, None, f"Preview failed: {e}")

        def early(part):  # symbol and footprint first; the 3D model is still on its way
            if part and token == self._preview_token and self._hit is h and self._preview_part is None:
                draw(symstyle.restyle_part(part, self.settings.symbol_style))

        def done(part):
            if token != self._preview_token or self._hit is not h:
                return
            key = f"{h.source}_{re.sub(r'[^A-Za-z0-9]+', '_', h.ref)[-60:]}"
            self._preview_part = (token, key, part)
            draw(part)
            if self.preview.current_tab() == "3D":
                self._render_3d()

        def fail(e):
            if token != self._preview_token or self._hit is not h:
                return
            msg = str(e)
            if isinstance(e, cad_sources.NeedsBrowser):
                msg += "\nClick 'Import selected part' to open it in your browser - the zip is imported automatically."
            self.preview.set_symbol(None, None, msg)
            self.preview.set_footprint(None, msg)
            self.preview.set_3d(None, msg)
            self._update_count_status()
        if h.source == "EasyEDA":
            self.run_bg(lambda: cad.quick(h.ref), early, lambda e: None)
        self.run_bg(lambda: cad_sources.fetch(h, self.settings, self.ki), done, fail)

    def _render_3d(self) -> None:
        if not self.settings.preview_3d or not self._preview_part:
            return
        token, key, part = self._preview_part
        if token != self._preview_token:
            return
        if not part.footprint_files or not part.model_files:
            self.preview.set_3d(None, "This model has no 3D model")
            return
        out = cad.preview_dir() / f"{key}.png"
        if out.exists():
            self.preview.set_3d(out)
            return
        self.preview.set_3d(None, "Rendering 3D view with kicad-cli…")
        self.run_bg(lambda: render.render_3d(self.ki, part.footprint_files[0], out, part.model_files),
                    lambda png: token == self._preview_token and self.preview.set_3d(png),
                    lambda e: token == self._preview_token and self.preview.set_3d(None, f"3D preview: {e}"))

    def _cad_text(self, r: PartResult) -> tuple[str, str | None, str]:
        h = self._hit if self._hit is not None and r is self.selected_part() else None
        avail = h.avail if h and not h.browser_only else r.cad
        if avail is None:
            return ("browser download" if h else "checking…"), "muted", (h.detail if h else "")
        s, f, m = avail
        marks = "   ".join(("✔ " if ok else "✖ ") + n for n, ok in
                           (("Symbol", s), ("Footprint", f), ("3D model", m)))
        src_name = h.source if h else r.cad_source
        if src_name == "EasyEDA":
            lcsc = h.ref if h else r.cad_lcsc
            src = (f"EasyEDA (LCSC part {lcsc})" if r.source == "LCSC" else
                   f"EasyEDA, via LCSC part {lcsc} with the same MPN ({r.source} does not supply CAD files)")
        elif src_name == "SamacSys":
            src = f"SamacSys / Component Search Engine - the models Mouser links to ({h.detail if h else ''})"
        elif src_name:
            src = f"{src_name} ({h.detail})" if h else src_name
        else:
            src = "none found - try another source in 'Model from' or 'Find CAD elsewhere'"
        return marks, "good" if s and f else "bad", src

    @staticmethod
    def _spec_rows(text: str) -> list[tuple]:
        rows = []
        for part in (text or "").split(";"):
            k, sep, v = part.partition(":")
            k, v = k.strip(), v.strip()
            if sep and k and v and v not in ("-", "~", "N/A"):
                rows.append((k, v))
        return rows

    def _show_details(self, r: PartResult) -> None:
        names = fields.field_names(self.settings.field_names)
        data = r.part_data()
        stock_style = "good" if r.stock else "bad"
        price = f"{r.price_str} each" + (f"  (min. order {r.moq})" if r.moq and r.moq > 1 else "") if r.price_str else ""
        host = r.url.split("/")[2] if r.url.count("/") >= 2 else r.url
        cad_txt, cad_style, cad_src = self._cad_text(r)
        buy = [("Supplier", r.source), ("Supplier part #", r.supplier_pn),
               ("In stock", (r.stock_str or "0") if r.stock is not None else "", stock_style),
               ("Lead time", r.lead_str), ("Unit price", price), ("Lifecycle", r.lifecycle),
               ("JLCPCB library", r.extra.get("JLCPCB library", ""))]
        part = [("Package", r.package), ("Category", r.category), ("CAD models", cad_txt, cad_style),
                ("CAD source", cad_src), ("Datasheet", "Open datasheet" if r.datasheet else "", ("link", r.datasheet)),
                ("Product page", f"Open on {host}" if r.url else "", ("link", r.url))]
        specs = self._spec_rows(r.extra.get("Attributes", ""))
        notes = [(k, v) for k, v in r.extra.items() if k not in ("Attributes", "JLCPCB library", "Detailed") and v]
        imported = [(names[k], data[k]) for k in fields.FIELD_ORDER if k in data]
        desc = r.extra.get("Detailed") or r.description
        self.preview.set_rich(r.mpn, r.manufacturer, desc,
                              [("Buying", buy), ("Part", part), ("Specifications", specs), ("Notes", notes),
                               ("Fields added to the symbol on import", imported)])

    def _open_selected(self, attr: str) -> None:
        r = self.selected_part()
        url = getattr(r, attr, "") if r else ""
        if url:
            webbrowser.open(url)
        else:
            self.set_status("Nothing to open for this part")

    # ================================================================ import
    def import_selected(self) -> None:
        r = self.selected_part()
        if not r:
            messagebox.showinfo(APP_TITLE, "Select a part in the results list first.")
            return
        target = self.current_target()
        if not target:
            return
        if self._hit is not None:
            self._import_hit(r, self._hit, target)
            return
        self.set_status(f"Looking for CAD models for {r.mpn}…")

        def found(hits):
            h = cad_sources.best(hits, self.settings.cad_source)
            if h:
                self._import_hit(r, h, target)
            else:
                self.log(f"No CAD model found for {r.mpn}.")
                self.find_cad_elsewhere(r)
        self.run_bg(lambda: cad_sources.find_all(r, self.settings, self.tlog), found, self._import_failed)

    def _import_hit(self, r: PartResult, h, target: library.LibraryTarget) -> None:
        data = r.part_data()
        if h.source == "EasyEDA":
            data["lcsc"] = h.ref
        if h.browser_only:
            self._open_for_download(r, h.page or h.ref, h.source)
            return
        self.set_status(f"Importing the {h.source} model of {r.mpn} into {target.name}…")
        self.log(f"Importing {r.mpn} from {h.source} ({h.detail})…")
        opts = self._install_opts()

        def fail(e):
            if isinstance(e, cad_sources.NeedsBrowser):
                self._open_for_download(r, e.url, h.source)
            elif isinstance(e, cad_sources.NeedsLogin):
                if messagebox.askyesno(APP_TITLE, f"{e}\n\nOpen Settings → CAD sources now?"):
                    self.open_settings("CAD sources")
            else:
                self._import_failed(e)
        self.run_bg(lambda: library.install(target, cad_sources.fetch(h, self.settings, self.ki, data), **opts),
                    lambda res: self._import_done(res, target, r.mpn), fail)

    def _open_for_download(self, r: PartResult, url: str, source: str) -> None:
        webbrowser.open(url)
        self._pending_cad_part = (r, time.time())
        if not self.watch_var.get():
            self.watch_var.set(True)
            self._watch_toggled()
        self.log(f"Opened {source} in your browser - the KiCad zip you download to {self.settings.watch_dir} "
                 f"is imported automatically with {r.mpn}'s part data.")
        self.set_status(f"Download the KiCad model of {r.mpn} from {source}; Kiporter imports it automatically.")

    def _install_opts(self) -> dict:
        return dict(overwrite=self.overwrite.get(), want_symbol=self.want_sym.get(),
                    want_footprint=self.want_fp.get(), want_3d=self.want_3d.get(), settings=self.settings)

    def _import_failed(self, e: Exception) -> None:
        self.log(f"Failed: {e}")
        self.set_status("Failed")
        messagebox.showerror(APP_TITLE, f"Failed:\n{e}")

    def _import_done(self, res: library.InstallResult, target: library.LibraryTarget, label: str) -> None:
        new_lib = any("Registered" in m for m in res.messages)
        for m in res.messages:
            self.log(m)
        for s in res.symbols:
            self.log(f"  + symbol     {s}")
        for f in res.footprints:
            self.log(f"  + footprint  {f}")
        for m in res.models:
            self.log(f"  + 3D model   {m}")
        for s in res.skipped:
            self.log(f"  = skipped    {s}  (tick 'Overwrite existing' to replace)")
        if res.symbols or res.footprints or res.models:
            self.log(f"✔ Successfully imported {label} into {target.name}.")
        else:
            self.log(f"Nothing new imported for {label} - it is already in {target.name}.")
        if res.symbols:
            sym = res.symbols[0]
            self.clipboard_clear()
            self.clipboard_append(sym.split(":", 1)[1])
            self.set_status(f"✔ Successfully imported {sym} — name copied to clipboard. "
                            "In the Schematic Editor press A and paste.")
        elif res.footprints:
            self.set_status(f"✔ Successfully imported footprint {res.footprints[0]}")
        elif res.models:
            self.set_status(f"✔ Successfully imported the 3D model of {label}")
        else:
            self.set_status(f"Nothing new imported for {label} - it is already in {target.name}")
        if new_lib:
            self.refresh_libraries()
            if kicad_env.running_kicad_processes():
                messagebox.showinfo(
                    APP_TITLE, f"Library '{target.name}' was just added to "
                               f"{'the project' if target.kind == 'project' else 'KiCad'}'s library table.\n\n"
                               f"KiCad is running: {'close and reopen the project' if target.kind == 'project' else 'restart KiCad'} "
                               "once so it loads the new library. Later imports appear without restarting.")

    # ---------------------------------------------------------------- zip / other CAD sources
    def find_cad_elsewhere(self, r: PartResult | None = None) -> None:
        r = r or self.selected_part()
        if not r:
            messagebox.showinfo(APP_TITLE, "Select a part first.")
            return
        links = providers.cad_download_links(r.mpn, r.manufacturer)
        win = tk.Toplevel(self)
        win.title(f"Get CAD model for {r.mpn}")
        win.transient(self)
        ttk.Label(win, text=f"Download the KiCad model for  {r.mpn}  from one of these sites.\n"
                            f"Pick the KiCad format. The zip saved to\n  {self.settings.watch_dir}\n"
                            "is imported automatically with this part's data "
                            "(or use 'Import ZIP…').", justify="left").pack(padx=14, pady=10, anchor="w")
        for name, url in links.items():
            ttk.Button(win, text=f"Open {name}", command=lambda u=url: webbrowser.open(u)).pack(
                fill="x", padx=14, pady=2)
        if r.url:
            ttk.Button(win, text=f"Open {r.source} product page (ECAD model link)",
                       command=lambda: webbrowser.open(r.url)).pack(fill="x", padx=14, pady=2)
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=10)
        self._pending_cad_part = (r, time.time())
        if not self.watch_var.get():
            self.watch_var.set(True)
            self._watch_toggled()
        self.log(f"Waiting for a CAD zip for {r.mpn} in {self.settings.watch_dir} …")

    def import_zip_dialog(self) -> None:
        f = filedialog.askopenfilename(title="Choose CAD model zip (SnapEDA, Ultra Librarian, SamacSys/Mouser)",
                                       initialdir=self.settings.watch_dir,
                                       filetypes=[("Zip archives", "*.zip"), ("All files", "*")])
        if f:
            self.import_zip(Path(f), ask=False)

    def import_zip(self, path: Path, ask: bool) -> None:
        target = self.current_target()
        if not target:
            return
        data: dict = {}
        if self._pending_cad_part and time.time() - self._pending_cad_part[1] < 1800:
            data = self._pending_cad_part[0].part_data()
        if ask and not messagebox.askyesno(
                APP_TITLE, f"New CAD download found:\n  {path.name}\n\nImport it into '{target.name}'?"):
            return
        self.log(f"Importing {path.name} into {target.name}…")
        opts = self._install_opts()

        def done(res):
            self._pending_cad_part = None
            self._import_done(res, target, path.name)

        self.run_bg(lambda: library.install(target, symstyle.restyle_part(
            importers.stage_zip(path, self.ki, data), self.settings.symbol_style), **opts),
                    done, self._import_failed)

    def _watch_toggled(self) -> None:
        self.settings.watch_downloads = self.watch_var.get()
        self.settings.save()
        if self.watch_var.get():
            self._watch_start = min(self._watch_start, time.time())

    def _watch_tick(self) -> None:
        self.watch_check.configure(text="Auto-import CAD zips downloaded to " + self.settings.watch_dir)
        try:
            if self.watch_var.get():
                d = Path(self.settings.watch_dir)
                for z in d.glob("*.zip") if d.is_dir() else []:
                    key = str(z)
                    try:
                        st = z.stat()
                    except OSError:
                        continue
                    if st.st_mtime < self._watch_start or key in self._zip_seen:
                        continue
                    # wait until the browser has finished writing the file
                    if self._zip_sizes.get(key) != st.st_size:
                        self._zip_sizes[key] = st.st_size
                        continue
                    self._zip_seen[key] = st.st_mtime
                    if self._looks_like_cad_zip(z):
                        self.import_zip(z, ask=True)
        finally:
            self.after(2000, self._watch_tick)

    @staticmethod
    def _looks_like_cad_zip(z: Path) -> bool:
        try:
            with zipfile.ZipFile(z) as zf:
                names = [n.lower() for n in zf.namelist()]
        except (zipfile.BadZipFile, OSError):
            return False
        return any(n.endswith((".kicad_sym", ".kicad_mod")) or ("kicad" in n and n.endswith((".lib", ".mod")))
                   for n in names)

    # ================================================================ KiCad control
    def update_pcb(self) -> None:
        p = self.current_project()
        self.set_status("Updating PCB from schematic…")
        self.update_btn.state(["disabled"])

        def done(res):
            ok, msg = res
            self.update_btn.state(["!disabled"])
            self.log(("Update PCB: " if ok else "Update PCB failed: ") + msg)
            self.set_status(msg)
            if not ok:
                messagebox.showwarning(APP_TITLE, f"Could not run Update PCB from Schematic:\n\n{msg}")

        def fail(e):
            self.update_btn.state(["!disabled"])
            self._import_failed(e)

        s = self.settings
        options = {key: getattr(s, f"update_pcb_{key}", "") for key, _ in kicad_control.UPDATE_OPTIONS}
        self.run_bg(lambda: kicad_control.update_pcb_from_schematic(
            self.ki, p, s.update_pcb_method, s.update_pcb_hotkey, options, s.update_pcb_press), done, fail)

    # ================================================================ dialogs
    def open_settings(self, page: str | None = None) -> None:
        self.nb.select(self.settings_tab)
        if page and page in self.settings_tab.pages:
            self.settings_tab.nb.select(self.settings_tab.pages[page])

    def open_accounts(self, focus: str | None = None) -> None:
        from .settings_gui import AccountsDialog
        AccountsDialog(self, focus=focus)


class DestinationDialog(tk.Toplevel):
    """Start-up question: which library do imported parts go to this session?"""

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("Where should imported parts go?")
        self.transient(app)
        self.resizable(False, False)
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        p = app.current_project()
        ttk.Label(frm, text="Choose the library that new parts are imported into.", style="Bold.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.kind = tk.StringVar(value=app.target_var.get() if p else "global")
        rb = ttk.Radiobutton(frm, text=f"Project library  ({p.name if p else 'no project detected'})",
                             variable=self.kind, value="project")
        rb.grid(row=1, column=0, sticky="w")
        if not p:
            rb.state(["disabled"])
        self.pcombo = ttk.Combobox(frm, state="readonly" if p else "disabled", width=70,
                                   values=list(app.project_lib_combo["values"]))
        self.pcombo.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(22, 0), pady=(0, 8))
        if app.project_lib_combo.current() >= 0:
            self.pcombo.current(app.project_lib_combo.current())
        ttk.Radiobutton(frm, text="Local library (shared by all projects)", variable=self.kind,
                        value="global").grid(row=3, column=0, sticky="w")
        self.gcombo = ttk.Combobox(frm, state="readonly", width=70, values=list(app.global_lib_combo["values"]))
        self.gcombo.grid(row=4, column=0, columnspan=2, sticky="ew", padx=(22, 0))
        if app.global_lib_combo.current() >= 0:
            self.gcombo.current(app.global_lib_combo.current())
        self.pcombo.bind("<<ComboboxSelected>>", lambda e: self._picked("project"))
        self.gcombo.bind("<<ComboboxSelected>>", lambda e: self._picked("global"))
        self.ask = tk.BooleanVar(value=app.settings.ask_target_on_startup)
        ttk.Checkbutton(frm, text="Ask me every time Kiporter starts", variable=self.ask).grid(
            row=5, column=0, sticky="w", pady=(12, 0))
        ttk.Button(frm, text="OK", command=self._ok, style="Accent.TButton").grid(row=5, column=1, sticky="e",
                                                                                pady=(12, 0))
        self.bind("<Return>", lambda e: self._ok())
        self.grab_set()
        self.focus_set()

    def _picked(self, kind: str) -> None:
        self.kind.set(kind)
        combo = self.pcombo if kind == "project" else self.gcombo
        if combo.get() == NEW_LIB:
            t = self.app.create_library(kind, parent=self)
            self.app.refresh_libraries()
            src = self.app.project_lib_combo if kind == "project" else self.app.global_lib_combo
            combo["values"] = list(src["values"])
            if t:
                self.app.select_library(kind, t.name)
            combo.current(max(src.current(), 0))

    def _ok(self) -> None:
        kind = self.kind.get()
        combo = self.pcombo if kind == "project" else self.gcombo
        target_combo = self.app.project_lib_combo if kind == "project" else self.app.global_lib_combo
        if combo.current() >= 0 and combo.get() != NEW_LIB:
            target_combo.current(combo.current())
            self.app._lib_picked(kind)
        self.app.target_var.set(kind)
        self.app._target_changed()
        self.app.settings.ask_target_on_startup = self.ask.get()
        self.app.settings.save()
        t = self.app._selected_target()
        if t:
            self.app.log(f"Importing into {'project' if kind == 'project' else 'local'} library '{t.name}' ({t.sym_file})")
        self.destroy()


def main() -> None:
    App().mainloop()
