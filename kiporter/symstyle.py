"""Symbol drawing style: ANSI (zigzag resistor) or IEC (box resistor).

Supplier symbols come in whatever style the vendor drew. The one everyday part where ANSI and IEC
really differ is the plain two-pin resistor, so that is what gets redrawn here, using the same shapes
as KiCad's own Device:R_US and Device:R. Everything else on the symbol (fields, pin numbers) is kept.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import replace
from pathlib import Path

from . import fields, sexpr
from .library import StagedPart

STYLES = [("ansi", "ANSI / US (zigzag resistors)"), ("iec", "IEC / European (box resistors)"),
          ("keep", "Leave symbols as the supplier drew them")]

_STROKE = "(stroke (width {w}) (type default)) (fill (type none))"
_ZIGZAG = [[(0, 2.286), (0, 2.54)],
           [(0, 2.286), (1.016, 1.905), (0, 1.524), (-1.016, 1.143), (0, 0.762)],
           [(0, 0.762), (1.016, 0.381), (0, 0), (-1.016, -0.381), (0, -0.762)],
           [(0, -0.762), (1.016, -1.143), (0, -1.524), (-1.016, -1.905), (0, -2.286)],
           [(0, -2.286), (0, -2.54)]]
# style -> (body graphics, Reference position, Value position)
_RESISTOR = {
    "ansi": (" ".join("(polyline (pts " + " ".join(f"(xy {x} {y})" for x, y in line) + ") "
                      + _STROKE.format(w=0) + ")" for line in _ZIGZAG), "2.54 0 90", "-2.54 0 90"),
    "iec": ("(rectangle (start -1.016 -2.54) (end 1.016 2.54) " + _STROKE.format(w=0.254) + ")",
            "2.032 0 90", "0 0 90"),
}
# two-pin "R" parts that are not plain resistors keep their own drawing
_NOT_PLAIN = re.compile(r"thermistor|\bntc\b|\bptc\b|varistor|\bmov\b|potentiometer|trimmer|trimpot|rheostat|"
                        r"photo|\bldr\b|fuse|network|array|shunt|sensor|heater", re.I)


def _pins(sym: list) -> list[list]:
    found = []
    for node in [sym] + sexpr.children(sym, "symbol"):
        found += sexpr.children(node, "pin")
    return found


def _pin_number(pin: list) -> str:
    num = sexpr.child(pin, "number")
    return sexpr.unquote(num[1]) if num and len(num) > 1 else ""


def is_plain_resistor(sym: list, hint: str = "") -> bool:
    if sexpr.child(sym, "extends") or (fields.get_property(sym, "Reference") or "").strip().upper() != "R":
        return False
    if len(_pins(sym)) != 2:
        return False
    text = " ".join([hint] + [sexpr.unquote(p[2]) for p in sexpr.children(sym, "property") if len(p) > 2
                              and sexpr.unquote(p[1]).lower() not in ("footprint", "datasheet")])
    return not _NOT_PLAIN.search(text)


def restyle_symbol(sym: list, style: str, hint: str = "") -> bool:
    """Redraw a plain resistor symbol in the given style. Returns True if the symbol was changed."""
    if style not in _RESISTOR or not is_plain_resistor(sym, hint):
        return False
    body, ref_at, val_at = _RESISTOR[style]
    name = sexpr.unquote(sym[1]).split(":")[-1]
    numbers = sorted((_pin_number(p) for p in _pins(sym)), key=lambda n: (len(n), n))
    pin = ('(pin passive line (at 0 {y} {rot}) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) '
           '(number {n} (effects (font (size 1.27 1.27)))))')
    pins = " ".join(pin.format(y=y, rot=rot, n=sexpr.quote(n))
                    for (y, rot), n in zip(((3.81, 270), (-3.81, 90)), numbers))
    # drop the old drawing: units, loose graphics and pin display options
    drawing = ("symbol", "pin", "polyline", "rectangle", "circle", "arc", "bezier", "text", "pin_numbers", "pin_names")
    sym[:] = [c for c in sym if sexpr.head(c) not in drawing]
    at = max((i for i, c in enumerate(sym) if sexpr.head(c) in ("in_bom", "on_board", "exclude_from_sim")), default=1)
    sym[at + 1:at + 1] = [sexpr.parse("(pin_numbers hide)"), sexpr.parse("(pin_names (offset 0))")]
    sym.append(sexpr.parse(f"(symbol {sexpr.quote(name + '_0_1')} {body})"))
    sym.append(sexpr.parse(f"(symbol {sexpr.quote(name + '_1_1')} {pins})"))
    for prop, pos in (("Reference", ref_at), ("Value", val_at)):
        for p in sexpr.children(sym, "property"):
            if len(p) > 2 and sexpr.unquote(p[1]) == prop:
                old = sexpr.child(p, "at")
                if old is not None:
                    p[p.index(old)] = sexpr.parse(f"(at {pos})")
    return True


def restyle_part(part: StagedPart, style: str) -> StagedPart:
    """Copy of the part whose symbol files are redrawn in the wanted style (the originals stay untouched)."""
    if style not in _RESISTOR or not part.symbol_files:
        return part
    hint = " ".join(str(v) for k, v in part.data.items() if k in ("description", "category"))
    out: list[Path] = []
    changed = False
    for f in part.symbol_files:
        try:
            lib = sexpr.parse(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            out.append(f)
            continue
        hits = [restyle_symbol(s, style, hint) for s in sexpr.children(lib, "symbol")]
        if any(hits):
            d = Path(tempfile.mkdtemp(prefix="kiporter_style_"))
            new = d / f.name
            new.write_text(sexpr.dump(lib) + "\n", encoding="utf-8")
            out.append(new)
            changed = True
        else:
            out.append(f)
    return replace(part, symbol_files=out) if changed else part
