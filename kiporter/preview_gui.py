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
        self._photo_msg = self._model_msg = ""
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

    def set_rich(self, title: str, subtitle: str, description: str,
                 sections: list[tuple[str, list[tuple]]]) -> None:
        """Readable part sheet: title, description, then labelled rows grouped under headings.

        A row is (label, value) or (label, value, style) with style 'good', 'bad', 'muted' or
        ('link', url) - links are clickable.
        """
        import webbrowser
        d = self.details
        d.configure(state="normal")
        d.delete("1.0", "end")
        for t in d.tag_names():
            if t.startswith("link"):
                d.tag_delete(t)
        f = ui.FONTS
        d.configure(tabs=("150",), padx=8, pady=6, spacing1=1, spacing3=1)
        d.tag_configure("title", font=f.get("title"))
        d.tag_configure("subtitle", font=f.get("bold"), foreground="#555555", spacing3=6)
        d.tag_configure("desc", lmargin1=0, lmargin2=0, spacing3=8)
        d.tag_configure("h", font=f.get("bold"), foreground="#1f5fa8", spacing1=10, spacing3=3)
        d.tag_configure("row", lmargin1=6, lmargin2=156)
        d.tag_configure("label", foreground="#6b6b6b")
        d.tag_configure("good", foreground="#0b6e4f", font=f.get("bold"))
        d.tag_configure("bad", foreground="#b3261e", font=f.get("bold"))
        d.tag_configure("muted", foreground="#888888")
        d.insert("end", title + "\n", "title")
        if subtitle:
            d.insert("end", subtitle + "\n", "subtitle")
        if description:
            d.insert("end", description + "\n", "desc")
        n = 0
        for heading, rows in sections:
            rows = [r for r in rows if r[1] not in ("", None)]
            if not rows:
                continue
            d.insert("end", heading + "\n", "h")
            for row in rows:
                label, value = row[0], str(row[1])
                style = row[2] if len(row) > 2 else None
                d.insert("end", f"{label}\t", ("row", "label"))
                if isinstance(style, tuple) and style[0] == "link":
                    n += 1
                    tag = f"link{n}"
                    d.tag_configure(tag, foreground="#1f5fa8", underline=True)
                    d.tag_bind(tag, "<Button-1>", lambda e, u=style[1]: webbrowser.open(u))
                    d.tag_bind(tag, "<Enter>", lambda e: d.configure(cursor="hand2"))
                    d.tag_bind(tag, "<Leave>", lambda e: d.configure(cursor=""))
                    d.insert("end", value, ("row", tag))
                else:
                    d.insert("end", value, ("row", style) if style else ("row",))
                d.insert("end", "\n", "row")
        d.configure(state="disabled")
        d.yview_moveto(0)

    def set_photo(self, data: bytes | None, msg: str = "") -> None:
        img = ui.load_image_bytes(data, 360, 300) if data else None
        self._photo = img
        self._photo_msg = "" if img else (msg or "No photo available")
        self.photo_label.configure(image=img or "", text=self._photo_msg)

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
        self._model_msg = "" if img else msg
        self.model_label.configure(image=img or "", text=self._model_msg)

    def animate(self, frame: str) -> None:
        """Put the spinner in front of every message that says something is still loading (ends in …)."""
        for canvas, drawing, msg in ((self.sym_canvas, self._sym_drawing, self._sym_msg),
                                     (self.fp_canvas, self._fp_drawing, self._fp_msg)):
            if drawing is None and msg.endswith("…"):
                canvas.itemconfigure("msg", text=f"{frame}  {msg}")
        for label, msg in ((self.photo_label, self._photo_msg), (self.model_label, self._model_msg)):
            if msg.endswith("…"):
                label.configure(text=f"{frame}  {msg}")

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
                               justify="center", tags="msg")

    def _tab_changed(self, _e=None) -> None:
        if self.current_tab() == "3D" and self.on_3d_requested:
            self.on_3d_requested()
