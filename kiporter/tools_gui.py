"""'Library Tools' and 'BOM Stock Check' tabs."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Callable

from . import bom, kicad_control, library, libtools, providers, render, sexpr

if TYPE_CHECKING:
    from .gui import App

APP_TITLE = "Kiporter"
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
    ("Browse & preview a library",
     "Look through any of your libraries: every symbol and footprint drawn, 3D models rendered, fields listed.",
     "browse"),
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
               "setup": SetupCheckDialog, "browse": LibraryBrowserDialog, "backup": None}[key]
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
                  style="Bold.TLabel").pack(anchor="w")
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

        ttk.Label(b, text="2. Merge into:", style="Bold.TLabel").pack(anchor="w", pady=(8, 0))
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

        ttk.Label(b, text="3. Options:", style="Bold.TLabel").pack(anchor="w", pady=(8, 0))
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
                  style="Bold.TLabel").pack(anchor="w")
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




# ==================================================================== library browser
class LibraryBrowserDialog(_ToolDialog):
    """Browse any library: symbols and footprints drawn, 3D models rendered."""

    def __init__(self, app: "App"):
        super().__init__(app, "Browse & preview a library", "1180x700")
        from .preview_gui import PreviewPane
        from .kicad_env import kicad_env_vars, resolve
        top = ttk.Frame(self.body)
        top.pack(fill="x")
        ttk.Label(top, text="Library:").pack(side="left")
        self.chooser = LibraryChooser(top, app, width=90, on_change=self.load)
        self.chooser.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(top, text="Filter:").pack(side="left")
        self.filter = tk.StringVar()
        self.filter.trace_add("write", lambda *a: self._fill())
        ttk.Entry(top, textvariable=self.filter, width=20).pack(side="left", padx=4)
        pw = ttk.PanedWindow(self.body, orient="horizontal")
        pw.pack(fill="both", expand=True, pady=8)
        left = ttk.Notebook(pw)
        self.sym_list = tk.Listbox(left, exportselection=False)
        self.fp_list = tk.Listbox(left, exportselection=False)
        left.add(self.sym_list, text=" Symbols ")
        left.add(self.fp_list, text=" Footprints ")
        pw.add(left, weight=1)
        self.preview = PreviewPane(pw, on_3d_requested=self._render_3d)
        pw.add(self.preview, weight=3)
        self.sym_list.bind("<<ListboxSelect>>", lambda e: self._sym_selected())
        self.fp_list.bind("<<ListboxSelect>>", lambda e: self._fp_selected())
        self.lib: list | None = None
        self.symbols: list[str] = []
        self.footprints: list[Path] = []
        self.current_fp: Path | None = None
        p = app.current_project()
        env = kicad_env_vars(app.ki, p.dir if p else None)
        self.resolver = lambda uri: resolve(uri, env)
        self.fpx = libtools.FootprintIndex(app.ki, p.dir if p else None)
        self.load()

    def load(self) -> None:
        t = self.chooser.get()
        self.lib, self.symbols, self.footprints = None, [], []
        if t and t.sym_file.exists():
            try:
                self.lib = sexpr.parse(t.sym_file.read_text(encoding="utf-8", errors="replace"))
                self.symbols = sorted(render.symbol_names(self.lib), key=str.lower)
            except ValueError as e:
                self.app.log(f"Cannot read {t.sym_file}: {e}")
        if t and t.fp_dir.is_dir():
            self.footprints = sorted(t.fp_dir.glob("*.kicad_mod"), key=lambda p: p.stem.lower())
        self._fill()
        self.preview.clear(f"{len(self.symbols)} symbols, {len(self.footprints)} footprints" if t else "")

    def _fill(self) -> None:
        f = self.filter.get().lower()
        self.sym_list.delete(0, "end")
        self._sym_shown = [s for s in self.symbols if f in s.lower()]
        for s in self._sym_shown:
            self.sym_list.insert("end", s)
        self.fp_list.delete(0, "end")
        self._fp_shown = [p for p in self.footprints if f in p.stem.lower()]
        for p in self._fp_shown:
            self.fp_list.insert("end", p.stem)

    def _sym_selected(self) -> None:
        sel = self.sym_list.curselection()
        if not sel or self.lib is None:
            return
        name = self._sym_shown[sel[0]]
        sym = next((s for s in sexpr.children(self.lib, "symbol") if sexpr.unquote(s[1]) == name), None)
        props = [(sexpr.unquote(p[1]), sexpr.unquote(p[2])) for p in sexpr.children(sym, "property")] if sym else []
        self.preview.set_details([name, ""] + [f"{k}: {v}" for k, v in props if v and not k.startswith("ki_")])
        self.preview.set_symbol(self.lib, name, "")
        fp_id = dict(props).get("Footprint", "")
        f = self.fpx.file(fp_id) if ":" in fp_id else None
        self._show_fp(f, f"Footprint '{fp_id}' not found" if fp_id else "No footprint assigned")
        if self.preview.current_tab() in ("Details", "Photo"):
            self.preview.select(self.preview.frames["Symbol"])

    def _fp_selected(self) -> None:
        sel = self.fp_list.curselection()
        if sel:
            f = self._fp_shown[sel[0]]
            self._show_fp(f, "")
            self.preview.set_details([f.stem, "", str(f)])
            if self.preview.current_tab() in ("Details", "Photo", "Symbol"):
                self.preview.select(self.preview.frames["Footprint"])

    def _show_fp(self, f: Path | None, msg: str) -> None:
        self.current_fp = f
        self.preview.set_3d(None, "Open this tab to render the 3D model")
        if not f:
            self.preview.set_footprint(None, msg)
            return
        try:
            self.preview.set_footprint(sexpr.parse(f.read_text(encoding="utf-8", errors="replace")), "")
        except ValueError as e:
            self.preview.set_footprint(None, f"Cannot read footprint: {e}")
        if self.preview.current_tab() == "3D":
            self._render_3d()

    def _render_3d(self) -> None:
        f = self.current_fp
        if not f:
            return
        import hashlib
        from .cad import preview_dir
        out = preview_dir() / f"lib_{hashlib.md5(str(f).encode()).hexdigest()}_{int(f.stat().st_mtime)}.png"
        if out.exists():
            self.preview.set_3d(out)
            return
        self.preview.set_3d(None, "Rendering 3D view with kicad-cli…")
        self.app.run_bg(lambda: render.render_3d(self.app.ki, f, out, resolver=self.resolver),
                        lambda png: f == self.current_fp and self.preview.set_3d(png),
                        lambda e: f == self.current_fp and self.preview.set_3d(None, f"3D preview: {e}"))


# ==================================================================== BOM tab
class BomTab(ttk.Frame):
    BASE_COLS = [("item", "#", 34), ("refs", "References", 150), ("qty", "Qty", 40), ("need", "Need", 50),
                 ("value", "Value", 110), ("footprint", "Footprint", 130), ("mount", "Type", 46),
                 ("mfr", "Manufacturer", 110), ("mpn", "MPN", 140), ("lcsc", "LCSC", 75),
                 ("digikey", "Digi-Key PN", 110), ("mouser", "Mouser PN", 110), ("desc", "Description", 200)]

    def __init__(self, parent, app: "App"):
        super().__init__(parent, padding=8)
        self.app = app
        self.lines: list[bom.BomLine] = []
        self.sources: list[str] = []
        self._gen = 0
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="↻ Load BOM from project", command=self.load).pack(side="left")
        ttk.Label(bar, text="   Boards to build:").pack(side="left")
        self.boards = tk.IntVar(value=app.settings.bom_boards or 1)
        sp = ttk.Spinbox(bar, from_=1, to=100000, textvariable=self.boards, width=7, command=self._redraw)
        sp.pack(side="left", padx=4)
        sp.bind("<KeyRelease>", lambda e: self._redraw())
        self.show_dnp = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Show DNP", variable=self.show_dnp, command=self._redraw).pack(side="left", padx=6)
        ttk.Button(bar, text="Check stock & lead times", command=self.check, style="Accent.TButton").pack(
            side="left", padx=8)
        ttk.Button(bar, text="⬇ Export for board house / distributor…", command=self.export_dialog,
                   style="Accent.TButton").pack(side="left")
        ttk.Button(bar, text="Stock report…", command=self.export_stock).pack(side="left", padx=6)
        self.info = tk.StringVar(value="Load the BOM of the selected project (reads the saved schematic and PCB).")
        ttk.Label(self, textvariable=self.info, style="Muted.TLabel").pack(fill="x", pady=(6, 0))
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
        self.tree.tag_configure("nompn", foreground="#888888")
        self.tree.tag_configure("dnp", foreground="#aaaaaa")
        self.tree.bind("<Double-1>", self._search_line)
        self._setup_columns()

    def settings_changed(self) -> None:
        self.boards.set(self.app.settings.bom_boards or 1)
        self._redraw()

    def _setup_columns(self) -> None:
        src_cols = [(f"s{i}", f"{s} (stock | lead)", 140) for i, s in enumerate(self.sources)]
        tail = [("best", "Best source", 140), ("status", "Status", 100)] if self.sources else []
        cols = self.BASE_COLS + src_cols + tail
        self.tree["columns"] = [c[0] for c in cols]
        for cid, head, w in cols:
            self.tree.heading(cid, text=head)
            self.tree.column(cid, width=w, stretch=cid in ("desc",),
                             anchor="e" if cid in ("qty", "need", "item") else "w")

    def load(self) -> None:
        p = self.app.current_project()
        if not p:
            messagebox.showinfo(APP_TITLE, "Select a project first.")
            return
        self.info.set(f"Reading BOM of {p.name}…")
        self.app.run_bg(lambda: bom.read_bom(p.pro_file, self.app.ki, self.app.settings), self._loaded,
                        lambda e: (self.info.set("BOM export failed"), self.app._import_failed(e)))

    def _loaded(self, lines: list[bom.BomLine]) -> None:
        self.lines = lines
        dnp = sum(1 for l in lines if l.dnp)
        no_mpn = sum(1 for l in lines if not l.dnp and not (l.mpn or l.lcsc))
        parts = sum(l.qty for l in lines if not l.dnp)
        self.info.set(f"{len(lines)} BOM lines, {parts} parts per board ({dnp} DNP lines, {no_mpn} without "
                      "MPN/LCSC number). Double-click a line to search for it.")
        self._redraw()

    def visible_lines(self) -> list[bom.BomLine]:
        return [l for l in self.lines if self.show_dnp.get() or not l.dnp]

    def _need(self, ln: bom.BomLine) -> int:
        try:
            return ln.qty * max(int(self.boards.get()), 1)
        except (tk.TclError, ValueError):
            return ln.qty

    def _best(self, ln: bom.BomLine) -> tuple[str, str]:
        if ln.dnp:
            return "", "dnp"
        if not (ln.mpn or ln.lcsc):
            return "", "no MPN"
        offers = [(s, o) for s, o in ln.offers.items() if o is not None and not isinstance(o, str)]
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
        for n, ln in enumerate(self.visible_lines(), start=1):
            cells = []
            for s in self.sources:
                o = ln.offers.get(s)
                if o is None:
                    cells.append("…" if s in ln.offers else "")
                elif isinstance(o, str):
                    cells.append(o)
                else:
                    cells.append(f"{o.stock_str or '0'} | {o.lead_str or '-'}")
            tail = []
            tag = "dnp" if ln.dnp else ""
            if self.sources:
                best, status = self._best(ln)
                tail = [best, {"ok": "✔ in stock", "low": "⚠ short", "none": "✖ none",
                               "no MPN": "no MPN", "dnp": "DNP"}.get(status, "")]
                tag = {"ok": "ok", "low": "low", "none": "none", "no MPN": "nompn", "dnp": "dnp"}.get(status, "")
            d = ln.data
            self.tree.insert("", "end", iid=str(self.lines.index(ln)), tags=(tag,) if tag else (), values=(
                n, ln.refs_str, ln.qty, self._need(ln), ln.value + ("  (DNP)" if ln.dnp else ""), ln.footprint_name,
                ln.mount, ln.manufacturer, ln.mpn, d.get("lcsc", ""), d.get("digikey", ""), d.get("mouser", ""),
                ln.description, *cells, *tail))

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
        todo = [l for l in self.lines if not l.dnp and (l.mpn or l.lcsc)]
        for ln in todo:
            ln.offers = {s: None for s in self.sources}
        self._redraw()
        self.info.set(f"Checking {len(todo)} parts at {', '.join(self.sources)}…")
        remaining = [len(self.sources)]

        def worker(name: str):
            prov = self.app.providers[name]
            delay = 2.1 if name == "Mouser" else 0.6 if name == "Farnell/Newark" else 0.3
            for ln in todo:
                if gen != self._gen:
                    return
                q = ln.mpn or ln.lcsc
                if name == "LCSC" and ln.lcsc:
                    q = ln.lcsc
                try:
                    res = prov.search(q, limit=10)
                    if q == ln.lcsc:
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
                states = [self._best(l)[1] for l in self.lines if not l.dnp]
                self.info.set(f"Done: {states.count('ok')} in stock, {states.count('low')} short, "
                              f"{states.count('none')} unavailable, {states.count('no MPN')} without MPN.")
                self.app.settings.save()
        for name in self.sources:
            threading.Thread(target=worker, args=(name,), daemon=True).start()

    def export_stock(self) -> None:
        if not self.lines or not self.sources:
            messagebox.showinfo(APP_TITLE, "Run 'Check stock & lead times' first.")
            return
        p = self.app.current_project()
        f = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv"), ("Excel", "*.xlsx")],
                                         initialfile=f"{p.name if p else 'bom'}-stock.csv",
                                         initialdir=str(p.dir) if p else None)
        if f:
            bom.write_stock_report([l for l in self.lines if not l.dnp], self.sources,
                                   max(int(self.boards.get()), 1), Path(f))
            self.app.log(f"Stock report saved to {f}")
            kicad_control.open_path(f)

    def export_dialog(self) -> None:
        if not self.lines:
            messagebox.showinfo(APP_TITLE, "Load the BOM first.")
            return
        BomExportDialog(self.app, self)

    def _search_line(self, _e=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        ln = self.lines[int(sel[0])]
        self.app.search_var.set(ln.mpn or ln.lcsc or ln.value)
        self.app.nb.select(0)
        self.app.search()


class BomExportDialog(_ToolDialog):
    """Board-house / distributor formats plus KiCad's own BOM templates (project presets, built-in
    presets and BOM plugin scripts)."""

    def __init__(self, app: "App", tab: BomTab):
        super().__init__(app, "Export BOM", "1000x660")
        self.tab = tab
        s = app.settings
        p = app.current_project()
        self.templates = bom.kicad_templates(p.pro_file, app.ki, s) if p else []
        self.kfmts = bom.kicad_formats(p.pro_file, app.ki) if p else dict(bom.KICAD_BUILTIN_FORMATS)
        self._preview_gen = 0
        b = self.body
        row = ttk.Frame(b)
        row.pack(fill="x")
        ttk.Label(row, text="Format:").pack(side="left")
        names = self._names()
        self.fmt = ttk.Combobox(row, state="readonly", width=60, values=names, height=30)
        self.fmt.set(s.bom_format if s.bom_format in names else bom.FORMAT_NAMES[0])
        self.fmt.pack(side="left", padx=6)
        self.fmt.bind("<<ComboboxSelected>>", lambda e: self._update())
        self.dnp = tk.BooleanVar(value=s.bom_include_dnp)
        self.dnp_check = ttk.Checkbutton(row, text="Include DNP", variable=self.dnp, command=self._update)
        self.dnp_check.pack(side="left", padx=6)

        self.opts_shop = ttk.Frame(b)  # options for the board-house formats
        self.ftype = tk.StringVar(value=s.bom_file_type)
        ttk.Label(self.opts_shop, text="File type:").pack(side="left")
        ttk.Radiobutton(self.opts_shop, text="CSV", variable=self.ftype, value="csv").pack(side="left", padx=4)
        ttk.Radiobutton(self.opts_shop, text="Excel (.xlsx)", variable=self.ftype, value="xlsx").pack(side="left")
        self.cpl = tk.BooleanVar(value=s.bom_export_cpl)
        self.cpl_check = ttk.Checkbutton(self.opts_shop, text="Also write pick-and-place (CPL) file",
                                         variable=self.cpl)
        self.cpl_check.pack(side="left", padx=14)
        self.opts_kicad = ttk.Frame(b)  # options for KiCad presets
        ttk.Label(self.opts_kicad, text="KiCad output format:").pack(side="left")
        self.kfmt = ttk.Combobox(self.opts_kicad, state="readonly", width=36, values=list(self.kfmts))
        self.kfmt.current(len(self.kfmts) - 1 if "Project format (as set up in KiCad)" in self.kfmts else 0)
        self.kfmt.pack(side="left", padx=6)
        self.kfmt.bind("<<ComboboxSelected>>", lambda e: self._update())

        self.notes = tk.StringVar()
        self.notes_label = ttk.Label(b, textvariable=self.notes, wraplength=960, justify="left", style="Muted.TLabel")
        self.link = ttk.Button(b, text="Open the shop's BOM / upload page", command=self._open_url)
        self.prev_label = ttk.Label(b, text="Preview:", style="Bold.TLabel")
        pf = ttk.Frame(b)
        self.prev = ttk.Treeview(pf, show="headings", height=11)
        xsb = ttk.Scrollbar(pf, orient="horizontal", command=self.prev.xview)
        self.prev.configure(xscrollcommand=xsb.set)
        self.prev.pack(fill="both", expand=True)
        xsb.pack(fill="x")
        self.pf = pf
        self.warn = tk.StringVar()
        self.warn_label = ttk.Label(b, textvariable=self.warn, foreground="#b3261e", wraplength=960, justify="left")
        out = ttk.Frame(b)
        ttk.Label(out, text="Save to folder:").pack(side="left")
        self.out_dir = tk.StringVar(value=str((p.dir / "bom-exports") if p else Path.home()))
        ttk.Entry(out, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out, text="…", width=3, command=self._pick).pack(side="left")
        self.out_row = out
        bottom = ttk.Frame(b)
        ttk.Label(bottom, textvariable=self.busy, foreground="#b3261e").pack(side="left")
        ttk.Button(bottom, text="Export", command=self.export, style="Accent.TButton").pack(side="right")
        self.bottom = bottom
        self._update()

    # ---------------------------------------------------------------- helpers
    def _names(self) -> list[str]:
        mine = [t.name for t in self.templates if bom.USER_MARK in t.name]
        rest = [t.name for t in self.templates if bom.USER_MARK not in t.name]
        return mine + bom.FORMAT_NAMES + rest + [bom.ADD_SCRIPT]

    def _reload_templates(self, select: str | None = None) -> None:
        p = self.app.current_project()
        self.templates = bom.kicad_templates(p.pro_file, self.app.ki, self.app.settings) if p else []
        self.fmt["values"] = self._names()
        if select:
            self.fmt.set(next((n for n in self._names() if select in n), self._names()[0]))

    def _choose_script(self, name: str | None) -> None:
        """Locate a missing user BOM plugin, or add a new one; remembered in the settings."""
        f = filedialog.askopenfilename(parent=self, title="Choose the BOM plugin script (.py)",
                                       filetypes=[("Python script", "*.py")])
        if not f:
            self.fmt.set(bom.FORMAT_NAMES[0])
            return
        key = name or Path(f).stem
        self.app.settings.bom_plugin_paths[key] = f
        self.app.settings.save()
        self.app.log(f"BOM plugin '{key}' → {f}")
        self._reload_templates(select=f"{key} (your BOM plugin)")

    def _template(self) -> bom.KicadTemplate | None:
        return next((t for t in self.templates if t.name == self.fmt.get()), None)

    def _format(self) -> bom.BomFormat:
        return bom.get_format(self.fmt.get())

    def _relayout(self, kicad: bool) -> None:
        for w in (self.opts_shop, self.opts_kicad, self.notes_label, self.link, self.prev_label, self.pf,
                  self.warn_label, self.out_row, self.bottom):
            w.pack_forget()
        (self.opts_kicad if kicad else self.opts_shop).pack(fill="x", pady=(8, 0))
        self.notes_label.pack(fill="x", pady=(8, 2))
        if not kicad:
            self.link.pack(anchor="w")
        self.prev_label.pack(anchor="w", pady=(8, 0))
        self.pf.pack(fill="both", expand=True)
        self.warn_label.pack(fill="x", pady=4)
        self.out_row.pack(fill="x", pady=(4, 0))
        self.bottom.pack(fill="x", pady=(10, 0))

    def _show_rows(self, rows: list[list]) -> None:
        self.prev.delete(*self.prev.get_children())
        if not rows:
            self.prev["columns"] = []
            return
        width = max(len(r) for r in rows)
        cols = [f"c{i}" for i in range(width)]
        self.prev["columns"] = cols
        head = list(rows[0]) + [""] * (width - len(rows[0]))
        for c, h in zip(cols, head):
            self.prev.heading(c, text=h)
            self.prev.column(c, width=120 if width > 1 else 900, stretch=True)
        for r in rows[1:60]:
            self.prev.insert("", "end", values=list(r))

    def _update(self) -> None:
        if self.fmt.get() == bom.ADD_SCRIPT:
            self._choose_script(None)
        t = self._template()
        if t is not None and t.source == "missing":
            name = t.name[len(bom.KICAD_PREFIX) + len(bom.USER_MARK):].split(" (BOM plugin")[0]
            self._choose_script(name)
            t = self._template()
        self._relayout(t is not None)
        if t:
            self.dnp_check.state(["disabled"])
            what = {"project": "BOM settings / a preset saved in your KiCad project", "built-in": "a KiCad built-in preset",
                    "user": f"your own BOM plugin script ({t.command[0] if t.command else ''})",
                    "kicad": "a BOM plugin script shipped with KiCad"}
            self.notes.set(f"Uses {what.get(t.source, 'KiCad')} - the same output KiCad's BOM tools produce. "
                           + ("Grouping, fields and DNP handling come from the preset." if t.kind == "preset" else
                              "The script runs on a netlist exported with kicad-cli."))
            self.warn.set("")
            self._preview_gen += 1
            gen = self._preview_gen
            self._show_rows([["Generating preview…"]])
            p = self.app.current_project()
            fmt = self.kfmts.get(self.kfmt.get())

            def work():
                import tempfile
                td = Path(tempfile.mkdtemp(prefix="kiporter_bomprev_"))
                files = bom.export_kicad_template(t, p.pro_file, self.app.ki, td, "preview", fmt)
                return bom.preview_rows(files[0])
            self.app.run_bg(work, lambda rows: gen == self._preview_gen and self._show_rows(rows),
                            lambda e: gen == self._preview_gen and self._show_rows([[f"Preview failed: {e}"]]))
            return
        self.dnp_check.state(["!disabled"])
        f = self._format()
        boards = max(int(self.tab.boards.get() or 1), 1)
        note = f.notes or ""
        if f.multiply_qty:
            note = f"Quantities are multiplied by the {boards} board(s) set on the BOM tab. " + note
        self.notes.set(note)
        self.cpl_check.state(["!disabled"] if f.cpl else ["disabled"])
        self._show_rows(bom.bom_rows(self.tab.lines, f, boards, self.dnp.get()))
        miss = bom.missing_fields(self.tab.lines, f)
        self.warn.set((f"{len(miss)} line(s) are missing data this shop needs: " + "; ".join(miss[:8]) +
                       (" …" if len(miss) > 8 else "") +
                       "  → add the fields in KiCad (or use Library Tools → Clean up to fill them).")
                      if miss else "")

    def _open_url(self) -> None:
        import webbrowser
        if self._format().url:
            webbrowser.open(self._format().url)

    def _pick(self) -> None:
        d = filedialog.askdirectory(parent=self, initialdir=self.out_dir.get())
        if d:
            self.out_dir.set(str(Path(d)))

    def export(self) -> None:
        s = self.app.settings
        p = self.app.current_project()
        base = p.name.replace(" ", "_") if p else "project"
        out_dir = Path(self.out_dir.get())
        t = self._template()
        s.bom_format = self.fmt.get()
        if t:
            s.save()
            fmt = self.kfmts.get(self.kfmt.get())
            work = lambda: bom.export_kicad_template(t, p.pro_file, self.app.ki, out_dir, base, fmt)  # noqa: E731
        else:
            f = self._format()
            s.bom_file_type, s.bom_include_dnp, s.bom_export_cpl = self.ftype.get(), self.dnp.get(), self.cpl.get()
            s.save()
            want_cpl = self.cpl.get() and f.cpl and p
            lines, boards, dnp, ftype = (self.tab.lines, max(int(self.tab.boards.get() or 1), 1), self.dnp.get(),
                                         self.ftype.get())

            def work():
                places = bom.read_positions(p.pro_file, self.app.ki) if want_cpl else None
                return bom.export(lines, f, out_dir, base, boards, dnp, ftype, places)

        def done(paths):
            self.app.log("Exported: " + ", ".join(str(x) for x in paths))
            kicad_control.open_path(out_dir)
            self.destroy()
        self.run(work, done, "Exporting")
