"""Preview pane: Details | Photo | Symbol | Footprint | 3D."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable

from . import render, ui


class PreviewPane(ttk.Notebook):
    TABS = ("Details", "Photo", "Symbol", "Footprint", "3D")

    def __init__(self, parent, on_3d_requested: Callable[[], None] | None = None):
        super().__init__(parent)
        self.on_3d_requested = on_3d_requested
        self.frames: dict[str, ttk.Frame] = {}
        for t in self.TABS:
            f = ttk.Frame(self)
            self.add(f, text=f" {t} ")
            self.frames[t] = f

        self.details = ui.scrolled_text(self.frames["Details"], height=12, width=40, wrap="word",
                                        font=ui.FONTS.get("normal"), relief="flat", borderwidth=0)
        self.details.frame.pack(fill="both", expand=True, padx=2, pady=2)
        self.details.configure(state="disabled")

        self.photo_label = ttk.Label(self.frames["Photo"], anchor="center", justify="center")
        self.photo_label.pack(fill="both", expand=True)
        self._photo = None

        self.sym_canvas = tk.Canvas(self.frames["Symbol"], highlightthickness=0, background=render.SYM_COLORS["bg"])
        self.sym_canvas.pack(fill="both", expand=True)
        self.fp_canvas = tk.Canvas(self.frames["Footprint"], highlightthickness=0,
                                   background=render.FP_COLORS["bg"])
        self.fp_canvas.pack(fill="both", expand=True)
        self._sym_drawing: render.Drawing | None = None
        self._fp_drawing: render.Drawing | None = None
        self._sym_msg = self._fp_msg = ""
        self.sym_canvas.bind("<Configure>", lambda e: self._redraw("sym"))
        self.fp_canvas.bind("<Configure>", lambda e: self._redraw("fp"))

        self.model_label = ttk.Label(self.frames["3D"], anchor="center", justify="center")
        self.model_label.pack(fill="both", expand=True)
        self._model_img = None
        self.bind("<<NotebookTabChanged>>", self._tab_changed)
        self.clear()

    # ---------------------------------------------------------------- content
    def clear(self, msg: str = "Select a part to preview it.") -> None:
        self.set_details([msg])
        self.set_photo(None, "No photo")
        self.set_symbol(None, None, msg)
        self.set_footprint(None, msg)
        self.set_3d(None, msg)

    def set_details(self, lines: list[str], title: bool = True) -> None:
        d = self.details
        d.configure(state="normal")
        d.delete("1.0", "end")
        d.insert("1.0", "\n".join(str(x) for x in lines))
        if title and lines:
            d.tag_add("title", "1.0", "1.end")
            d.tag_configure("title", font=ui.FONTS.get("title"))
        d.configure(state="disabled")

    def set_photo(self, data: bytes | None, msg: str = "") -> None:
        img = ui.load_image_bytes(data, 360, 300) if data else None
        self._photo = img
        self.photo_label.configure(image=img or "", text="" if img else (msg or "No photo available"))

    def set_symbol(self, lib: list | None, name: str | None, msg: str = "") -> None:
        self._sym_drawing = render.symbol_drawing(lib, name) if lib is not None and name else None
        self._sym_msg = msg
        self._redraw("sym")

    def set_footprint(self, node: list | None, msg: str = "") -> None:
        self._fp_drawing = render.footprint_drawing(node) if node is not None else None
        self._fp_msg = msg
        self._redraw("fp")

    def set_3d(self, png: Path | None, msg: str = "") -> None:
        img = ui.load_image_file(png, 520, 400) if png and png.exists() else None
        self._model_img = img
        self.model_label.configure(image=img or "", text="" if img else msg)

    def current_tab(self) -> str:
        try:
            return self.TABS[self.index(self.select())]
        except tk.TclError:
            return ""

    # ---------------------------------------------------------------- internals
    def _redraw(self, which: str) -> None:
        canvas = self.sym_canvas if which == "sym" else self.fp_canvas
        drawing = self._sym_drawing if which == "sym" else self._fp_drawing
        bg = render.SYM_COLORS["bg"] if which == "sym" else render.FP_COLORS["bg"]
        if drawing is not None and drawing.items:
            drawing.draw(canvas, bg)
        else:
            canvas.delete("all")
            msg = (self._sym_msg if which == "sym" else self._fp_msg) or "Nothing to show"
            canvas.create_text(max(canvas.winfo_width(), 100) / 2, max(canvas.winfo_height(), 60) / 2, text=msg,
                               fill="#777" if which == "sym" else "#aaa", width=max(canvas.winfo_width() - 30, 80),
                               justify="center")

    def _tab_changed(self, _e=None) -> None:
        if self.current_tab() == "3D" and self.on_3d_requested:
            self.on_3d_requested()
