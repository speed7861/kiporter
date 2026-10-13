"""Previews: symbols and footprints drawn on a Tk canvas, 3D models rendered by kicad-cli.

The canvas renderers read KiCad's own file formats (current and legacy), so they
preview anything: EasyEDA conversions, vendor zips and your existing libraries.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import sexpr
from .kicad_env import NO_WINDOW, KiCadInstall
from .library import pick_model

Pt = tuple[float, float]

SYM_COLORS = {"bg": "#f5f4ef", "body": "#840000", "fill": "#ffffc2", "pin": "#840000",
              "pin_name": "#006464", "pin_num": "#a90000", "text": "#000000"}
FP_COLORS = {"bg": "#001023", "F.Cu": "#c83434", "B.Cu": "#4d7fc4", "F.SilkS": "#f2eda1",
             "F.Silkscreen": "#f2eda1", "F.Fab": "#afafaf", "F.CrtYd": "#ff26e2", "F.Courtyard": "#ff26e2",
             "Edge.Cuts": "#d0d2cd", "hole": "#e3b72e", "pad_num": "#ffffff"}
FP_LAYER_ORDER = ["F.CrtYd", "F.Courtyard", "F.Fab", "Edge.Cuts", "B.Cu", "F.Cu", "pads", "F.SilkS", "F.Silkscreen"]


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _xy(node: list | None, default: Pt = (0.0, 0.0)) -> Pt:
    if not node or len(node) < 3:
        return default
    return _num(node[1]), _num(node[2])


def _pts(node: list | None) -> list[Pt]:
    pts = sexpr.child(node, "pts") if node else None
    return [_xy(c) for c in sexpr.children(pts, "xy")] if pts else []


def arc_points(start: Pt, mid: Pt, end: Pt, n: int = 24) -> list[Pt]:
    """Points along the circular arc through start, mid and end."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [start, end]
    ux = ((ax ** 2 + ay ** 2) * (by - cy) + (bx ** 2 + by ** 2) * (cy - ay) + (cx ** 2 + cy ** 2) * (ay - by)) / d
    uy = ((ax ** 2 + ay ** 2) * (cx - bx) + (bx ** 2 + by ** 2) * (ax - cx) + (cx ** 2 + cy ** 2) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, am, a1 = (math.atan2(p[1] - uy, p[0] - ux) for p in (start, mid, end))

    def ccw_span(f, t):
        return (t - f) % (2 * math.pi)
    sweep = ccw_span(a0, a1)
    if ccw_span(a0, am) > sweep:  # mid not on the CCW path: go the other way
        sweep -= 2 * math.pi
    return [(ux + r * math.cos(a0 + sweep * i / n), uy + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]


def _rot(p: Pt, deg: float) -> Pt:
    """Rotate as KiCad does on screen (Y down, positive = counter-clockwise)."""
    a = math.radians(deg)
    return p[0] * math.cos(a) + p[1] * math.sin(a), -p[0] * math.sin(a) + p[1] * math.cos(a)


# ==================================================================== drawing list
class Drawing:
    """Primitives in model space; fitted to a canvas at draw time."""

    def __init__(self, y_up: bool):
        self.y_up = y_up
        self.items: list[tuple] = []  # (kind, points, opts)

    def line(self, pts: list[Pt], color: str, width: float = 0.15) -> None:
        if len(pts) >= 2:
            self.items.append(("line", pts, {"color": color, "width": width}))

    def poly(self, pts: list[Pt], outline: str, fill: str = "", width: float = 0.1) -> None:
        if len(pts) >= 3:
            self.items.append(("poly", pts, {"outline": outline, "fill": fill, "width": width}))

    def circle(self, c: Pt, r: float, outline: str, fill: str = "", width: float = 0.1) -> None:
        pts = [(c[0] + r * math.cos(t * math.pi / 18), c[1] + r * math.sin(t * math.pi / 18)) for t in range(37)]
        self.poly(pts, outline, fill, width)

    def text(self, p: Pt, s: str, color: str, size: float = 1.27, anchor: str = "center", angle: float = 0) -> None:
        if s and s != "~":
            self.items.append(("text", [p], {"text": s, "color": color, "size": size, "anchor": anchor,
                                              "angle": angle}))

    def bbox(self) -> tuple[float, float, float, float] | None:
        xs, ys = [], []
        for kind, pts, o in self.items:
            for x, y in pts:
                xs.append(x)
                ys.append(y)
        if not xs:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    def draw(self, canvas, bg: str, margin: int = 16) -> None:
        canvas.delete("all")
        w, h = max(canvas.winfo_width(), 50), max(canvas.winfo_height(), 50)
        canvas.configure(background=bg)
        bb = self.bbox()
        if not bb:
            canvas.create_text(w / 2, h / 2, text="Nothing to show", fill="#888")
            return
        x0, y0, x1, y1 = bb
        bw, bh = max(x1 - x0, 1e-3), max(y1 - y0, 1e-3)
        sc = min((w - 2 * margin) / bw, (h - 2 * margin) / bh)
        sc = min(sc, 60)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

        def tr(p):
            dy = (p[1] - cy) * sc
            return w / 2 + (p[0] - cx) * sc, h / 2 + (-dy if self.y_up else dy)
        for kind, pts, o in self.items:
            flat = [c for p in pts for c in tr(p)]
            if kind == "line":
                canvas.create_line(*flat, fill=o["color"], width=max(1, o["width"] * sc), capstyle="round")
            elif kind == "poly":
                canvas.create_polygon(*flat, outline=o["outline"], fill=o["fill"], width=max(1, o["width"] * sc))
            elif kind == "text":
                px = max(6, int(o["size"] * sc * 0.75))
                if px < 6.5 and sc < 4:
                    continue  # too small to read
                kw = {"angle": o["angle"]} if o["angle"] else {}
                canvas.create_text(*flat, text=o["text"], fill=o["color"], font=("TkDefaultFont", -px),
                                   anchor=o["anchor"], **kw)


# ==================================================================== symbols
def _unit_parts(sym: list) -> list[list]:
    """Sub-symbols of unit 1 (plus common unit 0), first body style."""
    out = []
    for sub in sexpr.children(sym, "symbol"):
        m = re.search(r"_(\d+)_(\d+)$", sexpr.unquote(sub[1]))
        if not m or (m.group(1) in ("0", "1") and m.group(2) in ("0", "1")):
            out.append(sub)
    return out


def symbol_drawing(lib: list, name: str) -> Drawing:
    d = Drawing(y_up=True)
    syms = {sexpr.unquote(s[1]): s for s in sexpr.children(lib, "symbol")}
    sym = syms.get(name)
    if sym is None:
        return d
    ext = sexpr.child(sym, "extends")
    base = syms.get(sexpr.unquote(ext[1])) if ext else None
    graphic_src = base if base is not None else sym
    pn = sexpr.child(graphic_src, "pin_names")
    show_names = not (pn and ("hide" in pn or sexpr.child(pn, "hide")))
    pnum = sexpr.child(graphic_src, "pin_numbers")
    show_nums = not (pnum and ("hide" in pnum or sexpr.child(pnum, "hide")))
    name_offset = _num(sexpr.child(pn, "offset")[1]) if pn and sexpr.child(pn, "offset") else 0.508
    c = SYM_COLORS
    for part in _unit_parts(graphic_src):
        for it in part[2:]:
            h = sexpr.head(it)
            fill_node = sexpr.child(it, "fill") if isinstance(it, list) else None
            ftype = sexpr.child(fill_node, "type")[1] if fill_node and sexpr.child(fill_node, "type") else "none"
            fill = {"background": c["fill"], "outline": c["body"]}.get(ftype, "")
            if h == "rectangle":
                (x0, y0), (x1, y1) = _xy(sexpr.child(it, "start")), _xy(sexpr.child(it, "end"))
                d.poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], c["body"], fill, 0.254)
            elif h in ("polyline", "bezier"):
                pts = _pts(it)
                if fill and len(pts) >= 3:
                    d.poly(pts, c["body"], fill, 0.254)
                else:
                    d.line(pts, c["body"], 0.254)
            elif h == "circle":
                d.circle(_xy(sexpr.child(it, "center")), _num(sexpr.child(it, "radius")[1]), c["body"], fill, 0.254)
            elif h == "arc":
                if sexpr.child(it, "mid"):
                    d.line(arc_points(_xy(sexpr.child(it, "start")), _xy(sexpr.child(it, "mid")),
                                      _xy(sexpr.child(it, "end"))), c["body"], 0.254)
                elif sexpr.child(it, "radius"):
                    rad = sexpr.child(it, "radius")
                    ctr = _xy(sexpr.child(rad, "at"))
                    r = _num(sexpr.child(rad, "length")[1])
                    a = sexpr.child(rad, "angles")
                    a0, a1 = math.radians(_num(a[1])), math.radians(_num(a[2]))
                    if a1 < a0:
                        a1 += 2 * math.pi
                    d.line([(ctr[0] + r * math.cos(a0 + (a1 - a0) * i / 24), ctr[1] + r * math.sin(a0 + (a1 - a0) * i / 24))
                            for i in range(25)], c["body"], 0.254)
            elif h == "text":
                at = sexpr.child(it, "at")
                d.text(_xy(at), sexpr.unquote(it[1]), c["text"])
            elif h == "pin":
                if "hide" in it or (sexpr.child(it, "hide") and sexpr.child(it, "hide")[1:] != ["no"]):
                    continue
                at = sexpr.child(it, "at")
                (x, y), ang = _xy(at), _num(at[3]) if len(at) > 3 else 0.0
                ln = _num(sexpr.child(it, "length")[1]) if sexpr.child(it, "length") else 2.54
                dx, dy = math.cos(math.radians(ang)), math.sin(math.radians(ang))
                ex, ey = x + dx * ln, y + dy * ln
                d.line([(x, y), (ex, ey)], c["pin"], 0.15)
                nm = sexpr.child(it, "name")
                nu = sexpr.child(it, "number")
                horiz = abs(dx) > 0.5
                if show_names and nm:
                    # vertical text is rotated 90 deg about its anchor: 'w' grows upwards, 'e' downwards
                    anchor = ("w" if dx > 0 else "e") if horiz else ("w" if dy > 0 else "e")
                    off = name_offset + 0.3
                    d.text((ex + dx * off, ey + dy * off), sexpr.unquote(nm[1]), c["pin_name"], 1.1, anchor,
                           0 if horiz else 90)
                if show_nums and nu:
                    mx, my = (x + ex) / 2, (y + ey) / 2
                    d.text((mx, my + 0.5) if horiz else (mx - 0.5, my), sexpr.unquote(nu[1]), c["pin_num"], 1.0,
                           "s", 0 if horiz else 90)
    return d


def symbol_names(lib: list) -> list[str]:
    return [sexpr.unquote(s[1]) for s in sexpr.children(lib, "symbol")]


# ==================================================================== footprints
def _pad_outline(shape: str, w: float, h: float, rratio: float) -> list[Pt]:
    hw, hh = w / 2, h / 2
    if shape in ("circle",) or (shape == "oval" and abs(w - h) < 1e-6):
        r = hw
        return [(r * math.cos(t * math.pi / 16), r * math.sin(t * math.pi / 16)) for t in range(32)]
    if shape == "oval":
        r = min(hw, hh)
        pts = []
        if w >= h:
            for t in range(17):
                a = -math.pi / 2 + math.pi * t / 16
                pts.append((hw - r + r * math.cos(a), r * math.sin(a)))
            for t in range(17):
                a = math.pi / 2 + math.pi * t / 16
                pts.append((-hw + r + r * math.cos(a), r * math.sin(a)))
        else:
            for t in range(17):
                a = math.pi * t / 16
                pts.append((r * math.cos(a), hh - r + r * math.sin(a)))
            for t in range(17):
                a = math.pi + math.pi * t / 16
                pts.append((r * math.cos(a), -hh + r + r * math.sin(a)))
        return pts
    if shape == "roundrect" and rratio > 0:
        r = min(hw, hh) * 2 * min(rratio, 0.5)
        pts = []
        for (cx, cy, a0) in ((hw - r, -hh + r, -90), (hw - r, hh - r, 0), (-hw + r, hh - r, 90), (-hw + r, -hh + r, 180)):
            for t in range(7):
                a = math.radians(a0 + 15 * t)
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts
    return [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]


def _layers(node: list) -> list[str]:
    ln = sexpr.child(node, "layers")
    if ln:
        return [sexpr.unquote(x) for x in ln[1:] if isinstance(x, str)]
    ly = sexpr.child(node, "layer")
    return [sexpr.unquote(ly[1])] if ly else []


def _stroke_width(node: list) -> float:
    st = sexpr.child(node, "stroke")
    w = sexpr.child(st, "width") if st else sexpr.child(node, "width")
    return _num(w[1]) if w else 0.12


def footprint_drawing(fp: list) -> Drawing:
    d = Drawing(y_up=False)
    c = FP_COLORS
    graphics: dict[str, list] = {}
    for it in fp[2:]:
        h = sexpr.head(it)
        if h in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
            lay = (_layers(it) or [""])[0]
            graphics.setdefault(lay, []).append(it)

    def draw_graphic(it, color):
        h = sexpr.head(it)
        w = _stroke_width(it)
        fill_node = sexpr.child(it, "fill")
        filled = fill_node is not None and ("solid" in fill_node or "yes" in fill_node)
        if h == "fp_line":
            d.line([_xy(sexpr.child(it, "start")), _xy(sexpr.child(it, "end"))], color, w)
        elif h == "fp_rect":
            (x0, y0), (x1, y1) = _xy(sexpr.child(it, "start")), _xy(sexpr.child(it, "end"))
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            d.poly(pts, color, color if filled else "", w) if filled else d.line(pts + [pts[0]], color, w)
        elif h == "fp_circle":
            ctr, e = _xy(sexpr.child(it, "center")), _xy(sexpr.child(it, "end"))
            r = math.hypot(e[0] - ctr[0], e[1] - ctr[1])
            pts = [(ctr[0] + r * math.cos(t * math.pi / 18), ctr[1] + r * math.sin(t * math.pi / 18)) for t in range(37)]
            d.poly(pts, color, color, w) if filled else d.line(pts, color, w)
        elif h == "fp_arc":
            if sexpr.child(it, "mid"):
                d.line(arc_points(_xy(sexpr.child(it, "start")), _xy(sexpr.child(it, "mid")),
                                  _xy(sexpr.child(it, "end"))), color, w)
            else:  # legacy: start = centre, end = arc start, angle = sweep
                ctr, s = _xy(sexpr.child(it, "start")), _xy(sexpr.child(it, "end"))
                ang = math.radians(_num(sexpr.child(it, "angle")[1])) if sexpr.child(it, "angle") else 0
                r, a0 = math.hypot(s[0] - ctr[0], s[1] - ctr[1]), math.atan2(s[1] - ctr[1], s[0] - ctr[0])
                d.line([(ctr[0] + r * math.cos(a0 + ang * i / 24), ctr[1] + r * math.sin(a0 + ang * i / 24))
                        for i in range(25)], color, w)
        elif h == "fp_poly":
            pts = _pts(it)
            d.poly(pts, color, color if (filled or fill_node is None) else "", w)

    for layer in FP_LAYER_ORDER:
        if layer == "pads":
            for pad in sexpr.children(fp, "pad"):
                at = sexpr.child(pad, "at")
                (px, py), prot = _xy(at), (_num(at[3]) if at and len(at) > 3 else 0.0)
                w, h = _xy(sexpr.child(pad, "size"), (1, 1))
                ptype = pad[2] if len(pad) > 2 else "smd"
                shape = pad[3] if len(pad) > 3 else "rect"
                rr = sexpr.child(pad, "roundrect_rratio")
                pts = [_rot(p, prot) for p in _pad_outline(shape, w, h, _num(rr[1]) if rr else 0)]
                pts = [(px + x, py + y) for x, y in pts]
                lays = _layers(pad)
                color = c["F.Cu"] if any(l in ("F.Cu", "*.Cu") for l in lays) else c["B.Cu"]
                if ptype != "np_thru_hole":
                    d.poly(pts, color, color, 0.02)
                drill = sexpr.child(pad, "drill")
                if drill and len(drill) > 1:
                    vals = [_num(v) for v in drill[1:] if isinstance(v, str) and re.match(r"^-?[\d.]+$", v)]
                    if vals:
                        dw = vals[0]
                        dh = vals[1] if len(vals) > 1 else dw
                        hole = [_rot(p, prot) for p in _pad_outline("oval", dw, dh, 0)]
                        d.poly([(px + x, py + y) for x, y in hole], c["hole"], c["bg"], 0.05)
                num = sexpr.unquote(pad[1]) if len(pad) > 1 else ""
                if num and min(w, h) > 0.3:
                    d.text((px, py), num, c["pad_num"], min(w, h) * 0.6)
        else:
            for it in graphics.get(layer, []):
                draw_graphic(it, c.get(layer, "#888"))
    return d


# ==================================================================== 3D via kicad-cli
BOARD_HEAD = """(kicad_pcb (version 20240108) (generator "kiporter")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal) (31 "B.Cu" signal)
    (34 "B.Paste" user) (35 "F.Paste" user) (36 "B.SilkS" user "B.Silkscreen") (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user) (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard") (48 "B.Fab" user) (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
"""


def render_3d(ki: KiCadInstall, fp_file: Path, out_png: Path, models: list[Path] | None = None,
              resolver: Callable[[str], Path | None] | None = None, width: int = 640, height: int = 480,
              rotate: str = "-55,0,35") -> Path:
    """Render a footprint (with its 3D model) to PNG using `kicad-cli pcb render` (KiCad 9+)."""
    if not ki.cli:
        raise RuntimeError("kicad-cli not found")
    td = Path(tempfile.mkdtemp(prefix="kiporter3d_"))
    try:
        src = td / "in.pretty"
        src.mkdir()
        shutil.copy2(fp_file, src / fp_file.name)
        up = td / "up.pretty"
        subprocess.run(ki.cli_cmd("fp", "upgrade", "--force", src, "-o", up), capture_output=True,
                       timeout=120, creationflags=NO_WINDOW)
        files = list(up.glob("*.kicad_mod")) or [src / fp_file.name]
        node = sexpr.parse(files[0].read_text(encoding="utf-8", errors="replace"))
        node[0] = "footprint"
        node[1] = sexpr.quote("P:" + sexpr.unquote(node[1]))
        has_model = False
        for mn in sexpr.children(node, "model"):
            ref = sexpr.unquote(mn[1])
            p = pick_model(fp_file.stem, ref, models or []) if models else None
            if not p and resolver:
                p = resolver(ref)
            if p and p.exists():
                mn[1] = sexpr.quote(p.resolve().as_posix())
                has_model = True
        if not has_model:
            raise RuntimeError("This footprint has no 3D model to render")
        if not sexpr.child(node, "at"):
            idx = next((i for i, x in enumerate(node) if sexpr.head(x) == "layer"), 2)
            node.insert(idx + 1, ["at", "0", "0"])
        bb = footprint_drawing(node).bbox() or (-3, -3, 3, 3)
        m = 1.0
        outline = (f'  (gr_rect (start {bb[0] - m:.3f} {bb[1] - m:.3f}) (end {bb[2] + m:.3f} {bb[3] + m:.3f}) '
                   '(stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))\n')
        board = td / "preview.kicad_pcb"
        board.write_text(BOARD_HEAD + outline + sexpr.dump(node) + "\n)\n", encoding="utf-8")
        r = subprocess.run(ki.cli_cmd("pcb", "render", board, "-o", out_png, "--width", width, "--height", height,
                                      "--rotate", rotate, "--quality", "basic", "--zoom", "0.9"),
                           capture_output=True, text=True, timeout=180, creationflags=NO_WINDOW)
        if r.returncode != 0 or not out_png.exists():
            msg = (r.stderr or r.stdout or "").strip()
            if "render" in msg.lower() and ("unknown" in msg.lower() or "invalid" in msg.lower()):
                msg = "3D preview needs KiCad 9 or newer (kicad-cli pcb render)"
            raise RuntimeError(msg or "kicad-cli could not render the board")
        return out_png
    finally:
        shutil.rmtree(td, ignore_errors=True)
