"""Look & feel shared by all windows: native theme per OS, fonts, logo, images."""
from __future__ import annotations

import io
import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

ASSETS = Path(__file__).resolve().parent / "assets"
FONTS: dict[str, tuple] = {}
_images: list = []  # keep recent PhotoImage references alive


def _keep(ph):
    _images.append(ph)
    if len(_images) > 80:
        del _images[:30]
    return ph


APP_ID = "speed7861.Kiporter"


def set_app_id() -> None:
    """Windows: give Kiporter its own taskbar identity so it shows its own icon, not Python's.
    Must run before the first window is created."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except (AttributeError, OSError):
            pass


def logo_path() -> Path | None:
    for name in ("logo.png", "logo.gif"):
        p = ASSETS / name
        if p.exists():
            return p
    return None


def setup(root: tk.Tk, settings) -> None:
    style = ttk.Style(root)
    preferred = {"win32": "vista", "darwin": "aqua"}.get(sys.platform, "clam")
    if preferred in style.theme_names():
        style.theme_use(preferred)
    if settings.ui_scale and settings.ui_scale > 0:
        root.tk.call("tk", "scaling", float(settings.ui_scale))
    if settings.font_size and settings.font_size > 0:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
            try:
                tkfont.nametofont(name).configure(size=int(settings.font_size))
            except tk.TclError:
                pass
    base = tkfont.nametofont("TkDefaultFont").actual()
    fam, size = base["family"], abs(int(base["size"])) or 10
    FONTS.update({
        "normal": (fam, size), "small": (fam, max(size - 1, 7)), "bold": (fam, size, "bold"),
        "big": (fam, size + 2), "title": (fam, size + 4, "bold"),
        "mono": tuple(tkfont.nametofont("TkFixedFont").actual()[k] for k in ("family", "size")),
    })
    style.configure("Accent.TButton", font=FONTS["bold"])
    style.configure("Title.TLabel", font=FONTS["title"])
    style.configure("Bold.TLabel", font=FONTS["bold"])
    style.configure("Muted.TLabel", foreground="#666666")
    style.configure("Good.TLabel", foreground="#0b6e4f", font=FONTS["bold"])
    style.configure("Bad.TLabel", foreground="#b3261e", font=FONTS["bold"])
    style.configure("Busy.TLabel", foreground="#8a6d00")
    style.configure("Disconnected.TCheckbutton", foreground="#b3261e")
    style.configure("Treeview", rowheight=int(size * 2.2))
    ico = ASSETS / "logo.ico"
    if sys.platform == "win32" and ico.exists():
        try:
            root.iconbitmap(default=str(ico))  # crisp multi-size icon for title bar, taskbar and Alt+Tab
        except tk.TclError:
            pass
    icon = load_logo(256)
    if icon is not None:
        try:
            root.iconphoto(True, icon)
        except tk.TclError:
            pass


def load_logo(max_px: int):
    p = logo_path()
    if not p:
        return None
    return load_image_file(p, max_px, max_px)


def _pil_photo(img, max_w: int, max_h: int):
    # Re-encode as PNG and let Tk (8.6+) decode it: avoids PIL.ImageTk's native helper,
    # which is missing from some Linux/macOS Python builds.
    import base64
    img = img.copy()
    img.thumbnail((max_w, max_h))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return _keep(tk.PhotoImage(data=base64.b64encode(buf.getvalue())))


def load_image_file(path: Path, max_w: int, max_h: int):
    try:
        from PIL import Image
        return _pil_photo(Image.open(path), max_w, max_h)
    except Exception:
        try:
            ph = tk.PhotoImage(file=str(path))
            f = max(1, (ph.width() + max_w - 1) // max_w, (ph.height() + max_h - 1) // max_h)
            return _keep(ph.subsample(f) if f > 1 else ph)
        except tk.TclError:
            return None


def load_image_bytes(data: bytes, max_w: int, max_h: int):
    """JPEG/PNG/GIF/WebP bytes -> PhotoImage (needs Pillow for anything but PNG/GIF)."""
    try:
        from PIL import Image
        return _pil_photo(Image.open(io.BytesIO(data)), max_w, max_h)
    except Exception:
        try:
            import base64
            return _keep(tk.PhotoImage(data=base64.b64encode(data)))
        except tk.TclError:
            return None


def scrolled_text(parent, **kw) -> tk.Text:
    frame = ttk.Frame(parent)
    t = tk.Text(frame, **kw)
    sb = ttk.Scrollbar(frame, orient="vertical", command=t.yview)
    t.configure(yscrollcommand=sb.set)
    t.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    t.frame = frame  # type: ignore[attr-defined]
    return t
