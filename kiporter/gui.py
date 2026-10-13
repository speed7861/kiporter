"""Kiporter main window."""
from __future__ import annotations

import concurrent.futures
import queue
import threading
import time
import traceback
import webbrowser
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
import tkinter as tk
from tkinter import ttk

from . import __version__, cad, fields, importers, kicad_control, kicad_env, library, providers, render, sexpr, ui
from .config import Settings, app_data_dir
from .providers import PartResult

APP_TITLE = "Kiporter"
NEW_LIB = "➕  New library…"

# results table: column id -> (heading, width, sort key)
COLUMNS = {
    "source": ("Source", 66, lambda r: r.source),
    "mpn": ("MPN", 130, lambda r: r.mpn.lower()),
    "mfr": ("Manufacturer", 115, lambda r: r.manufacturer.lower()),
    "package": ("Package", 80, lambda r: r.package.lower()),
    "cad": ("CAD", 82, lambda r: cad.score(r)),
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
        super().__init__()
        self.settings = Settings.load()
        ui.setup(self, self.settings)
        self.ki = kicad_env.detect_kicad(self.settings.kicad_version, self.settings.kicad_cli_path)
        self.providers = providers.make_providers(self.settings)
        self.cad_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)

        self.projects: list[kicad_env.ProjectInfo] = []
        self.results: dict[str, PartResult] = {}
        self.project_libs: list[library.LibraryTarget] = []
        self.global_libs: list[library.LibraryTarget] = []
        self._ui_q: queue.Queue = queue.Queue()
        self._search_gen = 0
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

        self.title(f"{APP_TITLE} — KiCad part importer")
        self.geometry("1440x900")
        self.minsize(1050, 680)
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._pump_ui_queue)
        self.after(200, self._startup)

    # ================================================================ plumbing
    def run_bg(self, fn, on_done=None, on_error=None) -> None:
        """Run fn() in a worker thread; deliver result/exception on the Tk thread."""
        def worker():
            try:
                res = fn()
                self._ui_q.put((on_done, res, None))
            except Exception as e:  # noqa: BLE001
                self._ui_q.put((on_error, e, traceback.format_exc()))
        threading.Thread(target=worker, daemon=True).start()

    def _pump_ui_queue(self) -> None:
        try:
            for _ in range(200):
                cb, payload, tb = self._ui_q.get_nowait()
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

    def post(self, fn, *args) -> None:
        """Call fn(*args) on the Tk thread (safe from worker threads)."""
        self._ui_q.put((lambda _: fn(*args), None, None))

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
        ttk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken").pack(fill="x", side="bottom")
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
            self.tree.column(c, width=w, stretch=(c == "desc"), anchor=anchor)
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

        self.run_bg(work, self._projects_loaded)

    def _auto_refresh(self) -> None:
        self.run_bg(lambda: kicad_env.detect_projects(self.ki), self._projects_loaded)
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
            cb.configure(text=name if prov.configured or not prov.key_fields else f"{name} (connect)")

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
    def search(self) -> None:
        kw = self.search_var.get().strip()
        if not kw:
            return
        self._sources_changed()
        wanted = [n for n, v in self.source_vars.items() if v.get()]
        names = self.ensure_connected(wanted)
        if not names:
            self.set_status("No search source enabled/connected")
            return
        self._search_gen += 1
        gen = self._search_gen
        self.tree.delete(*self.tree.get_children())
        self.results.clear()
        self.preview.clear()
        self._pending_jobs = len(names)
        self.set_status(f"Searching {', '.join(names)} for '{kw}'…")
        limit = max(1, min(int(self.settings.search_limit or 50), 50))
        for name in names:
            prov = self.providers[name]
            self.run_bg(lambda p=prov: p.search(kw, limit), lambda res, n=name: self._add_results(gen, n, res),
                        lambda e, n=name: self._search_failed(gen, n, e))

    def _search_done_one(self, gen: int) -> None:
        if gen != self._search_gen:
            return
        self._pending_jobs -= 1
        if self._pending_jobs <= 0:
            self._update_count_status()

    def _update_count_status(self) -> None:
        shown = len(self.tree.get_children())
        in_stock = sum(1 for r in self.results.values() if r.stock)
        pending = sum(1 for r in self.results.values() if r.cad is None) if self.settings.check_cad else 0
        msg = f"{shown} of {len(self.results)} results shown ({in_stock} in stock)"
        if pending:
            msg += f" • checking CAD models for {pending}…"
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
            self.tree.insert("", "end", iid=iid, tags=tags, values=self._row_values(r))
            if self.settings.check_cad:
                self.cad_pool.submit(self._check_cad, gen, iid, r)
        self._apply_sort()
        self.log(f"{name}: {len(res)} results")
        self._search_done_one(gen)

    @staticmethod
    def _row_values(r: PartResult) -> tuple:
        return (r.source, r.mpn, r.manufacturer, r.package, cad.label(r), r.stock_str, r.lead_str, r.price_str,
                r.moq if r.moq is not None else "", r.supplier_pn, r.description)

    def _check_cad(self, gen: int, iid: str, r: PartResult) -> None:
        if gen != self._search_gen:
            return
        try:
            cad.check(r)
        except Exception as e:  # noqa: BLE001
            r.cad = cad.NONE
            self.tlog(f"CAD check {r.mpn}: {e}")
        self.post(self._cad_checked, gen, iid)

    def _cad_checked(self, gen: int, iid: str) -> None:
        if gen != self._search_gen or iid not in self.results:
            return
        if self.tree.exists(iid):
            self.tree.set(iid, "cad", cad.label(self.results[iid]))
        if not self._resort_pending:  # batch re-sorting/filtering while results stream in
            self._resort_pending = True
            self.after(400, self._deferred_resort)

    def _deferred_resort(self) -> None:
        self._resort_pending = False
        self._apply_sort()
        self._update_count_status()

    def _cad_filter_changed(self) -> None:
        self.settings.cad_filter = CAD_FILTERS[max(self.cad_filter_combo.current(), 0)][0]
        self.settings.save()
        self._apply_sort()
        self._update_count_status()

    def _passes(self, r: PartResult) -> bool:
        need = {"symfp": 2, "symfp3d": 3}.get(self.settings.cad_filter, 0)
        if not need:
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

    # ================================================================ preview
    def _selection_changed(self) -> None:
        r = self.selected_part()
        self._preview_token += 1
        token = self._preview_token
        self._preview_part = None
        if not r:
            return
        self._show_details(r)
        self.preview.set_photo(None, "Loading photo…" if r.image_url and self.settings.show_images
                               else "No photo available")
        msg = "Loading preview…" if self.settings.auto_preview else "Preview off (Settings → Search)"
        self.preview.set_symbol(None, None, msg)
        self.preview.set_footprint(None, msg)
        self.preview.set_3d(None, "Open this tab to render the 3D model" if self.settings.preview_3d
                            else "3D preview off (Settings → Search)")
        # debounce: only load if the user stays on this row
        self.after(250, lambda: self._load_preview(token, r))

    def _load_preview(self, token: int, r: PartResult) -> None:
        if token != self._preview_token:
            return
        if self.settings.show_images and r.image_url:
            self.run_bg(lambda: cad.image_bytes(r.image_url),
                        lambda data: token == self._preview_token and self.preview.set_photo(
                            data, "No photo available"))
        if not self.settings.auto_preview:
            return

        def work():
            lcsc = cad.lcsc_for(r)
            if not lcsc:
                return None
            return lcsc, cad.staged(lcsc)

        def done(res):
            if token != self._preview_token:
                return
            if not res:
                msg = (f"No free EasyEDA model found for {r.mpn}.\nUse 'Find CAD elsewhere…' "
                       "for SnapEDA / Ultra Librarian / SamacSys.")
                self.preview.set_symbol(None, None, msg)
                self.preview.set_footprint(None, msg)
                self.preview.set_3d(None, msg)
                return
            lcsc, part = res
            self._preview_part = (token, lcsc, part)
            try:
                lib = sexpr.parse(part.symbol_files[0].read_text(encoding="utf-8")) if part.symbol_files else None
                names = render.symbol_names(lib) if lib else []
                self.preview.set_symbol(lib, names[0] if names else None, "No symbol")
                fpn = sexpr.parse(part.footprint_files[0].read_text(encoding="utf-8")) if part.footprint_files else None
                self.preview.set_footprint(fpn, "No footprint")
            except (ValueError, OSError) as e:
                self.preview.set_symbol(None, None, f"Preview failed: {e}")
            if self.preview.current_tab() == "3D":
                self._render_3d()

        def fail(e):
            if token == self._preview_token:
                self.preview.set_symbol(None, None, f"Preview failed: {e}")
                self.preview.set_footprint(None, f"Preview failed: {e}")
        self.run_bg(work, done, fail)

    def _render_3d(self) -> None:
        if not self.settings.preview_3d or not self._preview_part:
            return
        token, lcsc, part = self._preview_part
        if token != self._preview_token:
            return
        if not part.footprint_files or not part.model_files:
            self.preview.set_3d(None, "No 3D model available for this part")
            return
        out = cad.preview_dir() / f"{lcsc}.png"
        if out.exists():
            self.preview.set_3d(out)
            return
        self.preview.set_3d(None, "Rendering 3D view with kicad-cli…")
        self.run_bg(lambda: render.render_3d(self.ki, part.footprint_files[0], out, part.model_files),
                    lambda png: token == self._preview_token and self.preview.set_3d(png),
                    lambda e: token == self._preview_token and self.preview.set_3d(None, f"3D preview: {e}"))

    def _show_details(self, r: PartResult) -> None:
        names = fields.field_names(self.settings.field_names)
        data = r.part_data()
        lines = [r.mpn, r.manufacturer, "", r.description, ""]
        for k, v in (("Source", r.source), ("Supplier PN", r.supplier_pn), ("In stock", r.stock_str),
                     ("Lead time", r.lead_str), ("Price (1 pc)", r.price_str), ("MOQ", r.moq),
                     ("Package", r.package), ("CAD models", cad.label(r) + (f" (EasyEDA {r.cad_lcsc})"
                                                                         if r.cad_lcsc else "")),
                     ("Category", r.category), ("Lifecycle", r.lifecycle), ("Datasheet", r.datasheet)):
            if v not in ("", None):
                lines.append(f"{k}: {v}")
        for k, v in r.extra.items():
            if v:
                lines.append(f"{k}: {v}")
        lines += ["", "Symbol fields written on import:"]
        lines += [f"   {names[k]} = {data[k][:60]}" for k in fields.FIELD_ORDER if k in data]
        self.preview.set_details(lines)

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
        if r.source == "LCSC":
            self._import_lcsc(r, r.supplier_pn, r.part_data(), target)
            return
        self.set_status(f"Looking for {r.mpn} at LCSC/EasyEDA…")

        def found(lcsc: str):
            if lcsc and messagebox.askyesno(
                    APP_TITLE, f"Free CAD model found at LCSC/EasyEDA for {r.mpn}:  {lcsc}  ({cad.label(r)})\n\n"
                               f"Import it with your {r.source} part data?\n\nChoose 'No' to download CAD from "
                               "SnapEDA / Ultra Librarian / SamacSys instead."):
                data = {**r.part_data(), "lcsc": lcsc}
                self._import_lcsc(r, lcsc, data, target)
            else:
                if not lcsc:
                    self.log(f"No LCSC/EasyEDA model for {r.mpn}.")
                self.find_cad_elsewhere(r)

        self.run_bg(lambda: cad.lcsc_for(r), found)

    def _import_lcsc(self, r: PartResult, lcsc_id: str, data: dict, target: library.LibraryTarget) -> None:
        self.set_status(f"Importing {lcsc_id} ({r.mpn}) into {target.name}…")
        self.log(f"Importing EasyEDA CAD data for {lcsc_id} ({r.mpn})…")
        opts = self._install_opts()
        self.run_bg(lambda: library.install(target, cad.staged(lcsc_id, data), **opts),
                    lambda res: self._import_done(res, target, r.mpn),
                    lambda e: self._import_failed(e))

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
        if res.symbols:
            sym = res.symbols[0]
            self.clipboard_clear()
            self.clipboard_append(sym.split(":", 1)[1])
            self.set_status(f"Imported {sym} — name copied to clipboard. In the Schematic Editor press A and paste.")
        elif res.footprints:
            self.set_status(f"Imported footprint {res.footprints[0]}")
        else:
            self.set_status(f"Nothing new imported for {label}")
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

        self.run_bg(lambda: library.install(target, importers.stage_zip(path, self.ki, data), **opts),
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
        self.run_bg(lambda: kicad_control.update_pcb_from_schematic(self.ki, p, s.update_pcb_method,
                                                                    s.update_pcb_hotkey), done, fail)

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
