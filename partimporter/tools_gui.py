"""'Library Tools' and 'BOM Stock Check' tabs."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Callable

from . import kicad_control, library, libtools, providers

if TYPE_CHECKING:
    from .gui import App

APP_TITLE = "KiCad Part Importer"
BROWSE = "\U0001f4c2  Browse for a .kicad_sym file…"


# ==================================================================== shared widgets
class LibraryChooser(ttk.Frame):
    """Combobox over every writable library (project + local), plus 'browse for file'."""

    def __init__(self, parent, app: "App", width: int = 80, on_change: Callable | None = None):
        super().__init__(parent)
        self.app = app
        self.extra: list[library.LibraryTarget] = []
        self.combo = ttk.Combobox(self, state="readonly", width=width)
        self.combo.pack(fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", self._picked)
        self.on_change = on_change
        self.refresh()

    def libs(self) -> list[library.LibraryTarget]:
        return list(self.app.project_libs) + list(self.app.global_libs) + self.extra

    @staticmethod
    def label(t: library.LibraryTarget) -> str:
        scope = "Project" if t.kind == "project" else "Local"
        return f"[{scope}]  {t.label()}"

    def refresh(self) -> None:
        cur = self.combo.get()
        self.combo["values"] = [self.label(t) for t in self.libs()] + [BROWSE]
        vals = list(self.combo["values"])
        self.combo.current(vals.index(cur) if cur in vals else (0 if len(vals) > 1 else len(vals) - 1))

    def _picked(self, _e=None) -> None:
        if self.combo.get() == BROWSE:
            f = filedialog.askopenfilename(title="Choose symbol library", parent=self,
                                           filetypes=[("KiCad symbol library", "*.kicad_sym")])
            if f:
                t = library.target_from_files(Path(f), self.app.ki)
                self.extra.append(t)
                self.refresh()
                self.combo.set(self.label(t))
            else:
                self.combo.current(0)
        if self.on_change:
            self.on_change()

    def get(self) -> library.LibraryTarget | None:
        i = self.combo.current()
        libs = self.libs()
        return libs[i] if 0 <= i < len(libs) else None


class IssuesView(ttk.Frame):
    """Checkable list of libtools.Issue rows."""

    def __init__(self, parent):
        super().__init__(parent)
        self.issues: list[libtools.Issue] = []
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="Show:").pack(side="left")
        self.filter = ttk.Combobox(bar, state="readonly", width=22, values=["All"])
        self.filter.current(0)
        self.filter.pack(side="left", padx=4)
        self.filter.bind("<<ComboboxSelected>>", lambda e: self._fill())
        ttk.Button(bar, text="Select all fixes", command=lambda: self._select(True)).pack(side="left", padx=4)
        ttk.Button(bar, text="Select none", command=lambda: self._select(False)).pack(side="left")
        self.summary = tk.StringVar()
        ttk.Label(bar, textvariable=self.summary, foreground="#555").pack(side="right")
        cols = {"sel": ("Apply", 50), "cat": ("Category", 110), "item": ("Item", 200),
                "detail": ("Problem", 420), "fix": ("Automatic fix", 330)}
        self.tree = ttk.Treeview(self, columns=list(cols), show="headings", height=16)
        for c, (h, w) in cols.items():
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, stretch=c in ("detail", "fix"), anchor="center" if c == "sel" else "w")
        ysb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.selection()))
        self.tree.tag_configure("nofix", foreground="#777")

    def set(self, issues: list[libtools.Issue]) -> None:
        self.issues = issues
        cats = sorted({i.category for i in issues})
        self.filter["values"] = ["All"] + cats
        if self.filter.get() not in self.filter["values"]:
            self.filter.current(0)
        self._fill()

    def _fill(self) -> None:
        self.tree.delete(*self.tree.get_children())
        flt = self.filter.get()
        for n, i in enumerate(self.issues):
            if flt not in ("All", i.category):
                continue
            mark = ("☑" if i.selected else "☐") if i.action else ""
            self.tree.insert("", "end", iid=str(n), values=(mark, i.category, i.item, i.detail, i.fix),
                             tags=() if i.action else ("nofix",))
        fixable = sum(1 for i in self.issues if i.action)
        chosen = sum(1 for i in self.issues if i.action and i.selected)
        self.summary.set(f"{len(self.issues)} findings • {fixable} fixable • {chosen} selected")

    def _click(self, e) -> None:
        if self.tree.identify_column(e.x) == "#1":
            row = self.tree.identify_row(e.y)
            if row:
                self._toggle([row])

    def _toggle(self, rows) -> None:
        for r in rows:
            i = self.issues[int(r)]
            if i.action:
                i.selected = not i.selected
        self._fill()

    def _select(self, on: bool) -> None:
        flt = self.filter.get()
        for i in self.issues:
            if i.action and flt in ("All", i.category):
                i.selected = on
        self._fill()


# ==================================================================== tools tab
TOOLS = [
    ("Clean up & auto-link library",
     "Links symbols to their footprints and footprints to 3D models, repairs broken / legacy (${KISYS3DMOD}) "
     "3D paths, folds field names (MFGR, Manufacturer_Name, …) into one consistent set, can fill missing "
     "manufacturer/datasheet/LCSC data online, and lists unused footprints and 3D models.", "cleanup"),
    ("Merge libraries",
     "Combines several symbol/footprint libraries (with their 3D models) into one library, renaming clashes, "
     "re-linking footprints and 3D paths, and optionally re-pointing your project at the merged library.", "merge"),
    ("Make project self-contained",
     "Copies every symbol, footprint and 3D model the selected project uses into its project library and "
     "re-points the schematic and PCB at it - ideal before sharing or archiving a design.", "localize"),
    ("KiCad setup check",
     "Finds path variables that hide KiCad's stock libraries/3D models, library-table entries pointing at "
     "missing files, and a disabled IPC API - and fixes them.", "setup"),
    ("Back up a library",
     "Zips a library's symbols, footprints and 3D models into one file.", "backup"),
]


class ToolsTab(ttk.Frame):
    def __init__(self, parent, app: "App"):
        super().__init__(parent, padding=10)
        self.app = app
        self.dialogs: list = []
        for r, (title, text, key) in enumerate(TOOLS):
            ttk.Button(self, text=title, width=32, command=lambda k=key: self.open(k),
                       style="Accent.TButton").grid(row=r, column=0, sticky="nw", pady=6, padx=(0, 14))
            ttk.Label(self, text=text, wraplength=820, justify="left").grid(row=r, column=1, sticky="w", pady=6)
        self.columnconfigure(1, weight=1)

    def libraries_changed(self) -> None:
        for d in list(self.dialogs):
            try:
                if d.winfo_exists() and hasattr(d, "chooser"):
                    d.chooser.refresh()
            except tk.TclError:
                self.dialogs.remove(d)

    def open(self, key: str) -> None:
        cls = {"cleanup": CleanupDialog, "merge": MergeDialog, "localize": LocalizeDialog,
               "setup": SetupCheckDialog, "backup": None}[key]
        if key == "backup":
            self._backup()
            return
        self.dialogs.append(cls(self.app))

    def _backup(self) -> None:
        win = tk.Toplevel(self.app)
        win.title("Back up a library")
        win.transient(self.app)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Library:").pack(anchor="w")
        chooser = LibraryChooser(frm, self.app)
        chooser.pack(fill="x", pady=4)

        def go():
            t = chooser.get()
            if not t:
                return
            dest = filedialog.asksaveasfilename(parent=win, defaultextension=".zip",
                                                initialfile=f"{t.name}-{time.strftime('%Y%m%d')}.zip",
                                                filetypes=[("Zip", "*.zip")])
            if dest:
                n = libtools.backup_library_zip(t, Path(dest), self.app.log)
                messagebox.showinfo(APP_TITLE, f"Saved {n} files to\n{dest}", parent=win)
                win.destroy()
        ttk.Button(frm, text="Save zip…", command=go, style="Accent.TButton").pack(anchor="e", pady=(8, 0))


class _ToolDialog(tk.Toplevel):
    def __init__(self, app: "App", title: str, size: str = "1180x640"):
        super().__init__(app)
        self.app = app
        self.title(title)
        self.geometry(size)
        self.transient(app)
        self.body = ttk.Frame(self, padding=10)
        self.body.pack(fill="both", expand=True)
        self.busy = tk.StringVar()

    def run(self, fn, done, what: str) -> None:
        self.busy.set(what + "…")
        self.config(cursor="watch")

        def alive() -> bool:
            try:
                return bool(self.winfo_exists())
            except tk.TclError:
                return False

        def ok(res):
            if not alive():  # dialog was closed while working
                return
            self.config(cursor="")
            self.busy.set("")
            done(res)

        def fail(e):
            self.app.log(f"{what} failed: {e}")
            if not alive():
                return
            self.config(cursor="")
            self.busy.set("")
            messagebox.showerror(APP_TITLE, f"{what} failed:\n\n{e}", parent=self)
        self.app.run_bg(fn, ok, fail)


class CleanupDialog(_ToolDialog):
    def __init__(self, app: "App"):
        super().__init__(app, "Clean up & auto-link library")
        top = ttk.Frame(self.body)
        top.pack(fill="x")
        ttk.Label(top, text="Library:").pack(side="left")
        self.chooser = LibraryChooser(top, app, width=95)
        self.chooser.pack(side="left", fill="x", expand=True, padx=6)
        self.online = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Fill missing data from LCSC (slower)", variable=self.online).pack(side="left")
        ttk.Button(top, text="Scan", command=self.scan, style="Accent.TButton").pack(side="left", padx=6)
        self.view = IssuesView(self.body)
        self.view.pack(fill="both", expand=True, pady=8)
        bottom = ttk.Frame(self.body)
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.busy, foreground="#b3261e").pack(side="left")
        ttk.Label(bottom, text="A backup is made before any change.  ", foreground="#555").pack(side="left")
        ttk.Button(bottom, text="Apply selected fixes", command=self.apply, style="Accent.TButton").pack(side="right")
        self.target: library.LibraryTarget | None = None

    def scan(self) -> None:
        t = self.chooser.get()
        if not t:
            return
        if not t.sym_file.exists() and not t.fp_dir.exists():
            messagebox.showinfo(APP_TITLE, f"'{t.name}' has no files yet.", parent=self)
            return
        self.target = t
        p = self.app.current_project()
        pdir = p.dir if p and t.kind == "project" else None
        self.run(lambda: libtools.scan_library(t, self.app.ki, pdir, self.app.settings, self.online.get(),
                                               self.app.tlog), self.view.set, f"Scanning {t.name}")

    def apply(self) -> None:
        if not self.target or not self.view.issues:
            return
        n = sum(1 for i in self.view.issues if i.action and i.selected)
        if not n or not messagebox.askyesno(APP_TITLE, f"Apply {n} fixes to '{self.target.name}'?", parent=self):
            return
        t = self.target

        def done(msg):
            messagebox.showinfo(APP_TITLE, msg, parent=self)
            self.scan()
        self.run(lambda: libtools.apply_fixes(t, self.view.issues, self.app.ki, self.app.settings, self.app.tlog),
                 done, "Applying fixes")


class SetupCheckDialog(_ToolDialog):
    def __init__(self, app: "App"):
        super().__init__(app, "KiCad setup check", "1100x480")
        top = ttk.Frame(self.body)
        top.pack(fill="x")
        ttk.Label(top, text="Checks KiCad's path variables, library tables and API settings.").pack(side="left")
        ttk.Button(top, text="Check again", command=self.scan).pack(side="right")
        self.view = IssuesView(self.body)
        self.view.pack(fill="both", expand=True, pady=8)
        bottom = ttk.Frame(self.body)
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.busy, foreground="#b3261e").pack(side="left")
        ttk.Label(bottom, text="KiCad must be closed to apply fixes. Settings are backed up first.",
                  foreground="#555").pack(side="left")
        ttk.Button(bottom, text="Apply selected fixes", command=self.apply, style="Accent.TButton").pack(side="right")
        self.scan()

    def scan(self) -> None:
        p = self.app.current_project()
        self.run(lambda: libtools.check_setup(self.app.ki, p.dir if p else None, self.app.tlog),
                 lambda iss: (self.view.set(iss), iss or messagebox.showinfo(APP_TITLE, "No problems found.",
                                                                             parent=self)),
                 "Checking")

    def apply(self) -> None:
        msg = libtools.apply_setup_fixes(self.app.ki, self.view.issues, self.app.log)
        messagebox.showinfo(APP_TITLE, msg, parent=self)
        self.app.refresh_libraries()
        self.scan()


class MergeDialog(_ToolDialog):
    def __init__(self, app: "App"):
        super().__init__(app, "Merge libraries", "980x640")
        b = self.body
        ttk.Label(b, text="1. Libraries to merge (Ctrl/Shift-click to select several):",
                  font=("Segoe UI", 9, "bold")).pack(anchor="w")
        lf = ttk.Frame(b)
        lf.pack(fill="both", expand=True, pady=4)
        self.listbox = tk.Listbox(lf, selectmode="extended", height=10, exportselection=False)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.listbox.yview)
        sb.pack(side="left", fill="y")
        self.listbox.configure(yscrollcommand=sb.set)
        ttk.Button(lf, text="Add library file…", command=self._add_file).pack(side="left", anchor="n", padx=6)
        self.sources: list[library.LibraryTarget] = []
        self._fill_sources()

        ttk.Label(b, text="2. Merge into:", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 0))
        dest = ttk.Frame(b)
        dest.pack(fill="x", pady=4)
        self.dest_mode = tk.StringVar(value="new")
        ttk.Radiobutton(dest, text="New local library named", variable=self.dest_mode, value="new").grid(
            row=0, column=0, sticky="w")
        self.new_name = tk.StringVar(value="Merged")
        ttk.Entry(dest, textvariable=self.new_name, width=24).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(dest, text="in folder").grid(row=0, column=2)
        self.new_dir = tk.StringVar(value=app.settings.global_lib_dir)
        ttk.Entry(dest, textvariable=self.new_dir, width=50).grid(row=0, column=3, sticky="ew", padx=4)
        ttk.Button(dest, text="…", width=3, command=self._pick_dir).grid(row=0, column=4)
        ttk.Radiobutton(dest, text="Existing library", variable=self.dest_mode, value="existing").grid(
            row=1, column=0, sticky="w", pady=(4, 0))
        self.chooser = LibraryChooser(dest, app, width=80)
        self.chooser.grid(row=1, column=1, columnspan=4, sticky="ew", padx=4, pady=(4, 0))
        dest.columnconfigure(3, weight=1)

        ttk.Label(b, text="3. Options:", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 0))
        opt = ttk.Frame(b)
        opt.pack(fill="x")
        ttk.Label(opt, text="When names clash:").grid(row=0, column=0, sticky="w")
        self.conflict = tk.StringVar(value="rename")
        for c, (val, txt) in enumerate((("rename", "keep both (rename)"), ("skip", "keep existing"),
                                        ("overwrite", "overwrite"))):
            ttk.Radiobutton(opt, text=txt, variable=self.conflict, value=val).grid(row=0, column=c + 1, sticky="w",
                                                                                  padx=6)
        self.normalize = tk.BooleanVar(value=True)
        self.unregister = tk.BooleanVar(value=False)
        self.remap = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Normalise symbol fields while merging", variable=self.normalize).grid(
            row=1, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(opt, text="Remove the merged source libraries from KiCad's library tables "
                                  "(files are kept)", variable=self.unregister).grid(row=2, column=0, columnspan=4,
                                                                                    sticky="w")
        p = app.current_project()
        ttk.Checkbutton(opt, text=f"Re-point project '{p.name if p else '-'}' at the merged library "
                                  "(project must be closed in KiCad)", variable=self.remap,
                        state="normal" if p else "disabled").grid(row=3, column=0, columnspan=4, sticky="w")
        bottom = ttk.Frame(b)
        bottom.pack(fill="x", pady=(10, 0))
        ttk.Label(bottom, textvariable=self.busy, foreground="#b3261e").pack(side="left")
        ttk.Button(bottom, text="Merge", command=self.merge, style="Accent.TButton").pack(side="right")

    def _fill_sources(self) -> None:
        self.sources = [t for t in list(self.app.project_libs) + list(self.app.global_libs)
                        if t.sym_file.exists() or t.fp_dir.exists()] + \
                       [t for t in self.sources if t.existing and t not in self.app.project_libs + self.app.global_libs]
        self.listbox.delete(0, "end")
        for t in self.sources:
            self.listbox.insert("end", LibraryChooser.label(t))

    def _add_file(self) -> None:
        f = filedialog.askopenfilename(parent=self, title="Choose symbol library",
                                       filetypes=[("KiCad symbol library", "*.kicad_sym")])
        if f:
            self.sources.append(library.target_from_files(Path(f), self.app.ki))
            self.listbox.insert("end", LibraryChooser.label(self.sources[-1]))

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(parent=self, initialdir=self.new_dir.get())
        if d:
            self.new_dir.set(str(Path(d)))

    def merge(self) -> None:
        srcs = [self.sources[i] for i in self.listbox.curselection()]
        if not srcs:
            messagebox.showinfo(APP_TITLE, "Select at least one library to merge.", parent=self)
            return
        if self.dest_mode.get() == "new":
            name = library.safe_name(self.new_name.get()).replace(" ", "_")
            dest = library.global_target(self.app.settings, self.app.ki, name, Path(self.new_dir.get()))
        else:
            dest = self.chooser.get()
        if not dest:
            return
        srcs = [s for s in srcs if s.sym_file != dest.sym_file]
        p = self.app.current_project()
        remap = self.remap.get() and p
        if remap:
            try:
                libtools.check_project_closed(p.dir)
            except libtools.ProjectLocked as e:
                messagebox.showwarning(APP_TITLE, str(e), parent=self)
                return
        if not messagebox.askyesno(APP_TITLE, f"Merge {len(srcs)} libraries into '{dest.name}'?\n\n" +
                                   "\n".join(f"  • {s.name}" for s in srcs), parent=self):
            return
        app, conflict, norm, unreg = self.app, self.conflict.get(), self.normalize.get(), self.unregister.get()

        def work():
            res = libtools.merge_libraries(srcs, dest, app.ki, p.dir if p else None, app.settings, conflict,
                                           norm, app.tlog)
            refs = 0
            if remap:
                refs = libtools.remap_project(p.dir, res.sym_map, res.fp_map, res.model_map, app.tlog)
            if unreg:
                libtools.unregister_libraries(srcs, app.tlog)
            return res, refs

        def done(out):
            res, refs = out
            for w in res.warnings[:50]:
                app.log("  ! " + w)
            app.refresh_libraries()
            messagebox.showinfo(APP_TITLE, f"Merged into '{dest.name}':\n\n{res.symbols} symbols\n{res.footprints} "
                                           f"footprints\n{res.models} 3D models\n{len(res.skipped)} skipped\n"
                                           f"{len(res.warnings)} warnings (see log)" +
                                (f"\n{refs} project references updated" if remap else ""), parent=self)
        self.run(work, done, "Merging")


class LocalizeDialog(_ToolDialog):
    def __init__(self, app: "App"):
        super().__init__(app, "Make project self-contained", "760x330")
        p = app.current_project()
        b = self.body
        ttk.Label(b, text=f"Project:  {p.pro_file if p else '(none selected)'}",
                  font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(b, wraplength=720, justify="left", text=(
            "Copies every symbol, footprint and 3D model used by this project into the project library below, "
            "then re-points the schematic and PCB at it. The project then opens correctly on any PC. "
            "A backup of the schematic/PCB files is made first. Close the project in KiCad before running.")
                  ).pack(anchor="w", pady=8)
        ttk.Label(b, text="Project library:").pack(anchor="w")
        self.combo = ttk.Combobox(b, state="readonly", width=90,
                                  values=[t.label() for t in app.project_libs])
        if app.project_libs:
            self.combo.current(max(app.project_lib_combo.current(), 0) if app.project_lib_combo.current()
                               < len(app.project_libs) else 0)
        self.combo.pack(fill="x", pady=4)
        self.stock = tk.BooleanVar(value=False)
        ttk.Checkbutton(b, text="Also copy parts from KiCad's built-in libraries (power symbols, Device:R, …)",
                        variable=self.stock).pack(anchor="w", pady=4)
        bottom = ttk.Frame(b)
        bottom.pack(fill="x", pady=(10, 0))
        ttk.Label(bottom, textvariable=self.busy, foreground="#b3261e").pack(side="left")
        ttk.Button(bottom, text="Run", command=self.go, style="Accent.TButton",
                   state="normal" if p else "disabled").pack(side="right")

    def go(self) -> None:
        p = self.app.current_project()
        i = self.combo.current()
        if not p or i < 0:
            return
        t = self.app.project_libs[i]
        try:
            libtools.check_project_closed(p.dir)
        except libtools.ProjectLocked as e:
            messagebox.showwarning(APP_TITLE, str(e), parent=self)
            return
        app, stock = self.app, self.stock.get()

        def done(res: libtools.LocalizeResult):
            for w in res.warnings[:60]:
                app.log("  ! " + w)
            app.refresh_libraries()
            messagebox.showinfo(APP_TITLE, f"Copied into '{t.name}':\n\n{res.symbols} symbols\n{res.footprints} "
                                           f"footprints\n{res.models} 3D models\n{res.references} references "
                                           f"updated\n{len(res.warnings)} warnings (see log)", parent=self)
        self.run(lambda: libtools.localize_project(p.dir, t, app.ki, app.settings, stock, app.tlog), done,
                 "Localizing project")


# ==================================================================== BOM tab
class BomTab(ttk.Frame):
    def __init__(self, parent, app: "App"):
        super().__init__(parent, padding=8)
        self.app = app
        self.lines: list[libtools.BomLine] = []
        self.sources: list[str] = []
        self._gen = 0
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Load BOM from project", command=self.load).pack(side="left")
        ttk.Label(bar, text="   Boards to build:").pack(side="left")
        self.boards = tk.IntVar(value=app.settings.bom_boards or 1)
        sp = ttk.Spinbox(bar, from_=1, to=100000, textvariable=self.boards, width=7, command=self._redraw)
        sp.pack(side="left", padx=4)
        sp.bind("<KeyRelease>", lambda e: self._redraw())
        ttk.Button(bar, text="Check stock & lead times", command=self.check, style="Accent.TButton").pack(
            side="left", padx=8)
        ttk.Button(bar, text="Export CSV…", command=self.export).pack(side="left")
        self.info = tk.StringVar(value="Uses the saved schematic of the selected project. Sources = the suppliers "
                                       "ticked on the Search tab.")
        ttk.Label(bar, textvariable=self.info, foreground="#555").pack(side="left", padx=10)
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, pady=6)
        self.tree = ttk.Treeview(frame, show="headings")
        ysb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree.tag_configure("ok", background="#e3f4e8")
        self.tree.tag_configure("low", background="#fff1d6")
        self.tree.tag_configure("none", background="#fbe0e0")
        self.tree.tag_configure("nompn", foreground="#888")
        self.tree.bind("<Double-1>", self._search_line)
        self._setup_columns()

    def _setup_columns(self) -> None:
        base = [("refs", "References", 160), ("qty", "Qty", 45), ("need", "Need", 55), ("value", "Value", 120),
                ("mpn", "MPN", 150), ("mfr", "Manufacturer", 120)]
        src_cols = [(f"s{i}", f"{s}  (stock | lead)", 150) for i, s in enumerate(self.sources)]
        tail = [("best", "Best source", 150), ("status", "Status", 110)]
        cols = base + src_cols + tail
        self.tree["columns"] = [c[0] for c in cols]
        for cid, head, w in cols:
            self.tree.heading(cid, text=head)
            self.tree.column(cid, width=w, stretch=cid in ("refs",), anchor="e" if cid in ("qty", "need") else "w")

    def load(self) -> None:
        p = self.app.current_project()
        if not p:
            messagebox.showinfo(APP_TITLE, "Select a project first.")
            return
        self.info.set(f"Reading BOM of {p.name}…")
        self.app.run_bg(lambda: libtools.read_bom(p.pro_file, self.app.ki, self.app.settings), self._loaded,
                        lambda e: (self.info.set("BOM export failed"), self.app._import_failed(e)))

    def _loaded(self, lines: list[libtools.BomLine]) -> None:
        self.lines = [l for l in lines if not l.dnp]
        dnp = len(lines) - len(self.lines)
        no_mpn = sum(1 for l in self.lines if not (l.mpn or l.lcsc))
        self.info.set(f"{len(self.lines)} BOM lines ({dnp} DNP hidden, {no_mpn} without MPN/LCSC number). "
                      "Double-click a line to search it.")
        self._redraw()

    def _need(self, ln: libtools.BomLine) -> int:
        try:
            return ln.qty * max(int(self.boards.get()), 1)
        except (tk.TclError, ValueError):
            return ln.qty

    def _best(self, ln: libtools.BomLine) -> tuple[str, str]:
        offers = [(s, o) for s, o in ln.offers.items() if o is not None and not isinstance(o, str)]
        if not (ln.mpn or ln.lcsc):
            return "", "no MPN"
        if not ln.offers:
            return "", ""
        need = self._need(ln)
        enough = [(s, o) for s, o in offers if (o.stock or 0) >= need]
        if enough:
            s, o = min(enough, key=lambda so: (so[1].price if so[1].price is not None else 1e9))
            return f"{s} ({o.price_str})", "ok"
        if offers:
            s, o = max(offers, key=lambda so: so[1].stock or 0)
            if o.stock:
                return f"{s} ({o.stock_str})", "low"
            lead = min((o.lead_days for _, o in offers if o.lead_days), default=None)
            return (f"{s} lead {o.lead_str}" if lead else ""), "none"
        return "", "none"

    def _redraw(self) -> None:
        try:
            self.app.settings.bom_boards = max(int(self.boards.get()), 1)
        except (tk.TclError, ValueError):
            pass
        self.tree.delete(*self.tree.get_children())
        for n, ln in enumerate(self.lines):
            cells = []
            for s in self.sources:
                o = ln.offers.get(s)
                if o is None:
                    cells.append("…" if s in ln.offers else "")
                elif isinstance(o, str):
                    cells.append(o)
                else:
                    cells.append(f"{o.stock_str or '0'} | {o.lead_str or '-'}")
            best, status = self._best(ln)
            label = {"ok": "✔ in stock", "low": "⚠ short", "none": "✖ none", "no MPN": "no MPN"}.get(
                status, "")
            tag = {"ok": "ok", "low": "low", "none": "none", "no MPN": "nompn"}.get(status, "")
            self.tree.insert("", "end", iid=str(n), tags=(tag,) if tag else (), values=(
                ln.refs, ln.qty, self._need(ln), ln.value, ln.mpn or (f"LCSC {ln.lcsc}" if ln.lcsc else ""),
                ln.manufacturer, *cells, best, label))

    def check(self) -> None:
        if not self.lines:
            self.load()
            return
        wanted = [n for n, v in self.app.source_vars.items() if v.get()]
        self.sources = self.app.ensure_connected(wanted)
        if not self.sources:
            return
        self._setup_columns()
        self._gen += 1
        gen = self._gen
        todo = [l for l in self.lines if l.mpn or l.lcsc]
        for ln in todo:
            ln.offers = {s: None for s in self.sources}
        self._redraw()
        self.info.set(f"Checking {len(todo)} parts at {', '.join(self.sources)}…")
        remaining = [len(self.sources)]

        def worker(name: str):
            prov = self.app.providers[name]
            delay = 2.1 if name == "Mouser" else 0.3  # Mouser allows ~30 requests/minute
            for ln in todo:
                if gen != self._gen:
                    return
                q = ln.mpn or ln.lcsc
                try:
                    res = prov.search(q, limit=10)
                    if not ln.mpn and name == "LCSC":
                        best = next((r for r in res if r.supplier_pn == ln.lcsc), None)
                    else:
                        best = providers.best_match(res, q)
                    ln.offers[name] = best or "not found"
                except Exception as e:  # noqa: BLE001
                    ln.offers[name] = "error"
                    self.app.tlog(f"BOM check {name} {q}: {e}")
                self.app.post(self._redraw)
                time.sleep(delay)
            self.app.post(finish)

        def finish():
            remaining[0] -= 1
            if remaining[0] == 0 and gen == self._gen:
                self._redraw()
                states = [self._best(l)[1] for l in self.lines]
                self.info.set(f"Done: {states.count('ok')} in stock, {states.count('low')} short, "
                              f"{states.count('none')} unavailable, {states.count('no MPN')} without MPN.")
                self.app.settings.save()
        for name in self.sources:
            threading.Thread(target=worker, args=(name,), daemon=True).start()

    def export(self) -> None:
        if not self.lines:
            return
        p = self.app.current_project()
        f = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                         initialfile=f"{p.name if p else 'bom'}-stock.csv",
                                         initialdir=str(p.dir) if p else None)
        if f:
            libtools.write_bom_report(self.lines, self.sources, max(int(self.boards.get()), 1), Path(f))
            self.app.log(f"BOM stock report saved to {f}")
            kicad_control.open_path(f)

    def _search_line(self, _e=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        ln = self.lines[int(sel[0])]
        q = ln.mpn or ln.lcsc or ln.value
        self.app.search_var.set(q)
        self.app.nb.select(0)
        self.app.search()
