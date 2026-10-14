"""'Fabrication' tab: one click to a ready-to-order Build folder."""
from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from . import fab, kicad_control

if TYPE_CHECKING:
    from .gui import App

APP_TITLE = "Kiporter"


class FabTab(ttk.Frame):
    STEPS = [("refill_drc", "Refill all zones (copper pours) and run DRC + schematic parity - on a copy, "
                            "your board file is not changed"),
             ("schematic_pdf", "Schematic PDF"),
             ("bom", "BOM in the shop's format"),
             ("gerbers", "Gerbers (all copper, mask, paste, silkscreen and outline layers)"),
             ("drill", "Drill files (Excellon) + drill map"),
             ("placement", "Placement / pick-and-place (CPL) file"),
             ("make_zip", "Zip the whole Build folder (PDF, BOM, reports and Gerbers) into the project folder")]

    def __init__(self, parent, app: "App"):
        super().__init__(parent, padding=10)
        self.app = app
        s = app.settings
        ttk.Label(self, text="Fabrication outputs", style="Title.TLabel").pack(anchor="w")
        self.project_info = tk.StringVar()
        ttk.Label(self, textvariable=self.project_info, style="Muted.TLabel").pack(anchor="w", pady=(2, 8))

        row = ttk.Frame(self)
        row.pack(fill="x")
        ttk.Label(row, text="Board house:").pack(side="left")
        self.profile = ttk.Combobox(row, state="readonly", width=34)
        self._fill_profiles(s.fab_profile)
        self.profile.pack(side="left", padx=6)
        self.profile.bind("<<ComboboxSelected>>", lambda e: self._profile_changed())
        ttk.Button(row, text="Custom Gerber setup…", command=self.edit_profile).pack(side="left", padx=4)
        ttk.Button(row, text="Open order page", command=self._open_shop).pack(side="left", padx=4)
        ttk.Label(row, text="   Boards:").pack(side="left")
        self.boards = tk.IntVar(value=s.bom_boards or 1)
        ttk.Spinbox(row, from_=1, to=100000, textvariable=self.boards, width=7).pack(side="left", padx=4)
        self.ftype = tk.StringVar(value=s.bom_file_type)
        ttk.Radiobutton(row, text="CSV", variable=self.ftype, value="csv").pack(side="left", padx=(10, 0))
        ttk.Radiobutton(row, text="Excel", variable=self.ftype, value="xlsx").pack(side="left", padx=4)
        self.notes = tk.StringVar()
        ttk.Label(self, textvariable=self.notes, wraplength=1100, justify="left", style="Muted.TLabel").pack(
            fill="x", pady=(6, 4))

        box = ttk.LabelFrame(self, text="Generate")
        box.pack(fill="x", pady=4)
        self.steps: dict[str, tk.BooleanVar] = {}
        for key, label in self.STEPS:
            v = tk.BooleanVar(value=True)
            self.steps[key] = v
            ttk.Checkbutton(box, text=label, variable=v).pack(anchor="w", padx=8, pady=1)

        bf = ttk.Frame(self)
        bf.pack(fill="x", pady=8)
        self.go_btn = ttk.Button(bf, text="⚙  Generate Build folder", command=self.generate,
                                 style="Accent.TButton")
        self.go_btn.pack(side="left")
        ttk.Button(bf, text="Create production zip", command=self.zip_only).pack(side="left", padx=6)
        ttk.Button(bf, text="Open Build folder", command=self.open_folder).pack(side="left")
        self.busy = tk.StringVar()
        ttk.Label(bf, textvariable=self.busy, foreground="#b3261e").pack(side="left", padx=12)

        ttk.Label(self, text="Result", style="Bold.TLabel").pack(anchor="w")
        self.out = tk.Text(self, height=12, wrap="word", state="disabled")
        self.out.pack(fill="both", expand=True)
        self._profile_changed()

    # ---------------------------------------------------------------- helpers
    def _fill_profiles(self, select: str) -> None:
        profs = fab.all_profiles(self.app.settings)
        self.profile["values"] = [p.name + ("  (custom)" if p.custom else "") for p in profs]
        names = [p.name for p in profs]
        self.profile.current(names.index(select) if select in names else 0)

    def _profile_name(self) -> str:
        return self.profile.get().replace("  (custom)", "")

    def edit_profile(self) -> None:
        FabProfileEditor(self, self._profile_name())

    def refresh(self) -> None:
        p = self.app.current_project()
        self.project_info.set(f"Project: {p.pro_file}   →   output: {fab.build_dir_for(p.pro_file)}" if p
                              else "Select a project at the top of the window.")

    def _profile_changed(self) -> None:
        prof = fab.get_profile(self._profile_name(), self.app.settings)
        self.notes.set(prof.notes or "")
        self.app.settings.fab_profile = prof.name
        self.app.settings.save()
        self.refresh()

    def _open_shop(self) -> None:
        prof = fab.get_profile(self._profile_name(), self.app.settings)
        if prof.url:
            webbrowser.open(prof.url)

    def _write(self, lines: list[str]) -> None:
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("1.0", "\n".join(lines))
        self.out.configure(state="disabled")

    def open_folder(self) -> None:
        p = self.app.current_project()
        if p and fab.build_dir_for(p.pro_file).exists():
            kicad_control.open_path(fab.build_dir_for(p.pro_file))
        else:
            messagebox.showinfo(APP_TITLE, "No Build folder yet - click 'Generate Build folder'.")

    # ---------------------------------------------------------------- actions
    def generate(self) -> None:
        p = self.app.current_project()
        if not p:
            messagebox.showinfo(APP_TITLE, "Select a project first.")
            return
        if not self.app.ki.cli:
            messagebox.showerror(APP_TITLE, "kicad-cli was not found (Settings → KiCad).")
            return
        opts = fab.FabOptions(profile=self._profile_name(), boards=max(int(self.boards.get() or 1), 1),
                              file_type=self.ftype.get(), **{k: v.get() for k, v in self.steps.items()})
        self.go_btn.state(["disabled"])
        self.busy.set("Working…")
        self._write(["Generating…"])

        def log(msg):
            self.app.tlog(msg)
            self.app.post(self.busy.set, msg)

        def done(res: fab.FabResult):
            self.go_btn.state(["!disabled"])
            self.busy.set("")
            lines = [f"Build folder: {res.build_dir}", ""]
            if res.drc_summary:
                lines.append(f"DRC: {res.drc_summary}")
            if res.zip_file:
                lines.append(f"Production zip: {res.zip_file}")
            lines += ["", "Files:"] + [f"  {f.relative_to(res.build_dir)}" for f in res.files
                                       if f.is_relative_to(res.build_dir)]
            if res.warnings:
                lines += ["", "Check before ordering:"] + [f"  ⚠ {w}" for w in res.warnings]
            self._write(lines)
            kicad_control.open_path(res.build_dir)

        def fail(e):
            self.go_btn.state(["!disabled"])
            self.busy.set("")
            self._write([f"Failed: {e}"])
            self.app.log(f"Fabrication outputs failed: {e}")
        self.app.run_bg(lambda: fab.generate(p.pro_file, self.app.ki, self.app.settings, opts, log), done, fail)

    def zip_only(self) -> None:
        p = self.app.current_project()
        if not p:
            return
        build = fab.build_dir_for(p.pro_file)
        if not build.exists() or not any(build.rglob("*.*")):
            messagebox.showinfo(APP_TITLE, "Generate the fabrication outputs first.")
            return
        z = fab.make_zip(build, fab.zip_path_for(p.pro_file))
        self.app.log(f"Created {z}")
        kicad_control.open_path(z.parent)


class FabProfileEditor(tk.Toplevel):
    """Create / edit a custom Gerber + drill + BOM configuration."""

    GERBER_BOOLS = [("board_plot_params", "Use the plot settings saved in the board (ignores the options below)"),
                    ("protel_ext", "Protel file extensions (.gtl, .gbl, .gts, …)"),
                    ("x2", "Gerber X2 format (extended attributes)"),
                    ("netlist", "Include netlist attributes (X2)"),
                    ("subtract_soldermask", "Subtract soldermask from silkscreen"),
                    ("disable_aperture_macros", "Disable aperture macros"),
                    ("use_drill_origin", "Use the drill/place file origin"),
                    ("exclude_refdes", "Leave out reference designators"),
                    ("exclude_value", "Leave out values")]
    DRILL_CHOICES = [("drill_format", "Drill file format", ["excellon", "gerber"]),
                     ("drill_units", "Units (Excellon)", ["mm", "in"]),
                     ("drill_zeros", "Zeros (Excellon)", ["decimal", "suppressleading", "suppresstrailing", "keep"]),
                     ("drill_origin", "Drill origin", ["absolute", "plot"]),
                     ("drill_oval", "Oval holes", ["alternate", "route"]),
                     ("drill_map", "Drill map", ["none", "gerberx2", "pdf", "svg", "dxf", "ps"])]

    def __init__(self, tab: FabTab, start_from: str):
        super().__init__(tab.app)
        from . import bom
        self.tab = tab
        self.app = tab.app
        self.title("Custom Gerber setup")
        self.transient(self.app)
        self.vars: dict[str, tk.Variable] = {}
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        top = ttk.Frame(frm)
        top.pack(fill="x")
        ttk.Label(top, text="Name:").pack(side="left")
        self.name = tk.StringVar()
        ttk.Entry(top, textvariable=self.name, width=30).pack(side="left", padx=6)
        ttk.Label(top, text="Start from:").pack(side="left", padx=(12, 0))
        self.base = ttk.Combobox(top, state="readonly", width=32,
                                 values=[p.name for p in fab.all_profiles(self.app.settings)])
        self.base.pack(side="left", padx=6)
        self.base.bind("<<ComboboxSelected>>", lambda e: self.load(self.base.get()))

        cols = ttk.Frame(frm)
        cols.pack(fill="both", expand=True, pady=8)
        lay = ttk.LabelFrame(cols, text="Layers to plot", padding=8)
        lay.pack(side="left", fill="y", padx=(0, 8))
        self.layer_vars: dict[str, tk.BooleanVar] = {}
        for i, layer in enumerate(fab.ALL_LAYERS):
            v = tk.BooleanVar()
            self.layer_vars[layer] = v
            text = "All inner copper (In1.Cu …)" if layer == fab.INNER else layer
            ttk.Checkbutton(lay, text=text, variable=v).grid(row=i % 11, column=i // 11, sticky="w", padx=4)
        right = ttk.Frame(cols)
        right.pack(side="left", fill="both", expand=True)
        gb = ttk.LabelFrame(right, text="Gerber options", padding=8)
        gb.pack(fill="x")
        for key, label in self.GERBER_BOOLS:
            v = tk.BooleanVar()
            self.vars[key] = v
            ttk.Checkbutton(gb, text=label, variable=v).pack(anchor="w")
        pr = ttk.Frame(gb)
        pr.pack(anchor="w", pady=(4, 0))
        ttk.Label(pr, text="Coordinate precision:").pack(side="left")
        self.vars["precision"] = tk.StringVar()
        ttk.Combobox(pr, textvariable=self.vars["precision"], values=["5", "6"], width=4, state="readonly").pack(
            side="left", padx=6)
        db = ttk.LabelFrame(right, text="Drill files", padding=8)
        db.pack(fill="x", pady=8)
        for r, (key, label, choices) in enumerate(self.DRILL_CHOICES):
            ttk.Label(db, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=1)
            v = tk.StringVar()
            self.vars[key] = v
            ttk.Combobox(db, textvariable=v, values=choices, state="readonly", width=18).grid(
                row=r, column=1, sticky="w")
        v = tk.BooleanVar()
        self.vars["drill_separate_th"] = v
        ttk.Checkbutton(db, text="Separate files for plated (PTH) and non-plated (NPTH) holes", variable=v).grid(
            row=len(self.DRILL_CHOICES), column=0, columnspan=2, sticky="w")
        ob = ttk.LabelFrame(right, text="BOM and notes", padding=8)
        ob.pack(fill="x")
        ttk.Label(ob, text="BOM format").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.vars["bom_format"] = tk.StringVar()
        ttk.Combobox(ob, textvariable=self.vars["bom_format"], values=["(none)"] + bom.FORMAT_NAMES,
                     state="readonly", width=30).grid(row=0, column=1, sticky="w")
        for r, (key, label) in enumerate((("notes", "Notes shown in the tab"), ("url", "Order page URL")), start=1):
            ttk.Label(ob, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=1)
            self.vars[key] = tk.StringVar()
            ttk.Entry(ob, textvariable=self.vars[key], width=50).grid(row=r, column=1, sticky="ew")

        bf = ttk.Frame(frm)
        bf.pack(fill="x")
        self.del_btn = ttk.Button(bf, text="Delete", command=self.delete)
        self.del_btn.pack(side="left")
        ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bf, text="Save", command=self.save, style="Accent.TButton").pack(side="right", padx=6)
        self.load(start_from)
        self.grab_set()

    def load(self, name: str) -> None:
        prof = fab.get_profile(name, self.app.settings)
        self.base.set(prof.name)
        self.name.set(prof.name if prof.custom else f"My {prof.name}")
        layers = prof.opt("layers")
        for layer, v in self.layer_vars.items():
            v.set(layer in layers)
        for key, v in self.vars.items():
            val = prof.opt(key)
            if key == "bom_format":
                v.set(val or "(none)")
            elif isinstance(v, tk.BooleanVar):
                v.set(bool(val))
            else:
                v.set(str(val))
        self.del_btn.state(["!disabled"] if prof.custom else ["disabled"])

    def save(self) -> None:
        name = self.name.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, "Give the setup a name.", parent=self)
            return
        if name in fab.PROFILE_NAMES:
            messagebox.showwarning(APP_TITLE, f"'{name}' is a built-in setup - pick another name.", parent=self)
            return
        layers = [layer for layer in fab.ALL_LAYERS if self.layer_vars[layer].get()]
        if not layers and not self.vars["board_plot_params"].get():
            messagebox.showwarning(APP_TITLE, "Select at least one layer.", parent=self)
            return
        opts: dict = {"layers": layers}
        for key, v in self.vars.items():
            val = v.get()
            if key == "bom_format":
                val = None if val == "(none)" else val
            elif key == "precision":
                val = int(val)
            opts[key] = val
        fab.save_custom_profile(self.app.settings, name, opts)
        self.app.log(f"Saved custom Gerber setup '{name}'")
        self.tab._fill_profiles(name)
        self.tab._profile_changed()
        self.destroy()

    def delete(self) -> None:
        name = self.base.get()
        if messagebox.askyesno(APP_TITLE, f"Delete the custom setup '{name}'?", parent=self):
            fab.delete_custom_profile(self.app.settings, name)
            self.tab._fill_profiles(self.app.settings.fab_profile)
            self.tab._profile_changed()
            self.destroy()
