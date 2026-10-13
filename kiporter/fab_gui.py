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
             ("make_zip", "Zip the Gerbers + drill files for upload")]

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
        self.profile = ttk.Combobox(row, state="readonly", width=28, values=fab.PROFILE_NAMES)
        self.profile.set(s.fab_profile if s.fab_profile in fab.PROFILE_NAMES else fab.PROFILE_NAMES[0])
        self.profile.pack(side="left", padx=6)
        self.profile.bind("<<ComboboxSelected>>", lambda e: self._profile_changed())
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
        ttk.Button(bf, text="Create upload zip", command=self.zip_only).pack(side="left", padx=6)
        ttk.Button(bf, text="Open Build folder", command=self.open_folder).pack(side="left")
        self.busy = tk.StringVar()
        ttk.Label(bf, textvariable=self.busy, foreground="#b3261e").pack(side="left", padx=12)

        ttk.Label(self, text="Result", style="Bold.TLabel").pack(anchor="w")
        self.out = tk.Text(self, height=12, wrap="word", state="disabled")
        self.out.pack(fill="both", expand=True)
        self._profile_changed()

    # ---------------------------------------------------------------- helpers
    def refresh(self) -> None:
        p = self.app.current_project()
        self.project_info.set(f"Project: {p.pro_file}   →   output: {fab.build_dir_for(p.pro_file)}" if p
                              else "Select a project at the top of the window.")

    def _profile_changed(self) -> None:
        prof = fab.get_profile(self.profile.get())
        self.notes.set(prof.notes or "")
        self.app.settings.fab_profile = prof.name
        self.app.settings.save()
        self.refresh()

    def _open_shop(self) -> None:
        prof = fab.get_profile(self.profile.get())
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
        opts = fab.FabOptions(profile=self.profile.get(), boards=max(int(self.boards.get() or 1), 1),
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
                lines.append(f"Upload zip: {res.zip_file.name}")
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
        gdir = fab.build_dir_for(p.pro_file) / "Gerbers"
        if not gdir.exists() or not any(gdir.iterdir()):
            messagebox.showinfo(APP_TITLE, "Generate the Gerbers first.")
            return
        prof = fab.get_profile(self.profile.get())
        z = fab.make_zip(gdir, fab.build_dir_for(p.pro_file) / f"{p.pro_file.stem}-Gerbers-"
                         f"{prof.name.split()[0]}.zip")
        self.app.log(f"Created {z}")
        kicad_control.open_path(z.parent)
