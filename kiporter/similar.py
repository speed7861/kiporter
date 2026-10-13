"""Closest alternatives for a part, judged by its specifications rather than its part number.

Given LCSC's listing of a part (the reference), look through the same LCSC category for parts that are in
stock and compare their attributes one by one: pin layout, pitch, mounting, value, ratings, and so on.
Ratings may be met or exceeded (a 50 V capacitor can stand in for a 16 V one); everything else must be equal
to count as a match. Also checks whether a candidate's footprint has its pads where the board already has them.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr
from .providers import PartResult, _norm_mpn

# attribute names worth putting into the search words, most telling first
KEY_ATTRS = ["Pins Structure", "Number of Pins", "Pitch", "Row Spacing", "Capacitance", "Resistance", "Inductance",
             "Frequency", "Output Voltage", "Type", "Mounting Type", "Number of Rows", "Number of Positions",
             "Voltage Rating", "Temperature Coefficient"]
_MEETS_OR_EXCEEDS = re.compile(r"voltage rating|rated voltage|current rating|rated current|power|withstand|"
                               r"breakdown|voltage - |current - ", re.I)
_AT_MOST = re.compile(r"tolerance|resistance \(dcr\)|esr|leakage|on-state resistance|rds", re.I)
_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6,
       "G": 1e9}
_EMPTY = ("", "-", "~", "n/a", "na", "none")
# a part that differs in one of these is a different part, however much else agrees
MUST_MATCH = {"Pins Structure", "Number of Pins", "Number of Positions", "Number of Rows", "Pitch", "Row Spacing",
              "Capacitance", "Resistance", "Inductance", "Frequency", "Output Voltage", "Mounting Type"}


@dataclass
class Candidate:
    part: PartResult
    score: float = 0.0  # 0..1: share of the reference's specs this part meets
    same: list[str] = field(default_factory=list)
    differs: list[str] = field(default_factory=list)  # "Contact Plating: Gold → Tin"
    unknown: list[str] = field(default_factory=list)
    wrong: bool = False  # differs in something that must match (pitch, pin count, value ...)

    @property
    def percent(self) -> str:
        return f"{round(self.score * 100)}%"

    def summary(self) -> str:
        bits = []
        if self.differs:
            bits.append("differs: " + "; ".join(self.differs))
        if self.unknown:
            bits.append("not stated: " + ", ".join(self.unknown))
        return " | ".join(bits) or "all listed specs match"


def attributes(r: PartResult | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in str((r.extra if r is not None else {}).get("Attributes", "")).split("; "):
        k, sep, v = item.partition(": ")
        if sep and k.strip() and v.strip().lower() not in _EMPTY:
            out[k.strip()] = v.strip()
    return out


def _number(text: str) -> float | None:
    """First quantity in a spec value: '16V' -> 16, '100nF' -> 1e-7, '±10%' -> 10, '4.2mm' -> 0.0042."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*([pnuµμmkKMG]?)(?=[A-Za-zΩ%℃°]|$|\s)", text or "")
    if not m:
        return None
    return float(m.group(1)) * _SI.get(m.group(2), 1.0)


def _norm(v: str) -> str:
    return re.sub(r"[\s\-_]", "", (v or "").lower())


def pin_count(r: PartResult) -> int | None:
    a = attributes(r)
    m = re.match(r"\s*(\d+)\s*[xX*]\s*(\d+)", a.get("Pins Structure", ""))
    if m:
        return int(m.group(1)) * int(m.group(2))
    for key in ("Number of Pins", "Number of Positions", "Number of Contacts"):
        m = re.match(r"\s*(\d+)", a.get(key, ""))
        if m:
            return int(m.group(1))
    return None


def pin_layout(ref: PartResult, pins: int | None) -> str:
    """'2x2P' for a connector whose listing gives the rows but not the pin layout (pins: from the board)."""
    a = attributes(ref)
    if a.get("Pins Structure"):
        return ""  # already one of the search words
    m = re.match(r"\s*(\d+)", a.get("Number of Rows", ""))
    rows = int(m.group(1)) if m else 0
    return f"{rows}x{pins // rows}P" if pins and rows and pins % rows == 0 else ""


def compare(ref: PartResult, cand: PartResult) -> Candidate:
    """How well cand meets ref's specifications."""
    want, have = attributes(ref), attributes(cand)
    if ref.package:
        want = {"Package": ref.package, **want}
        have = {"Package": cand.package, **have}
    c = Candidate(cand)
    credit = 0.0
    for name, w in want.items():
        h = have.get(name, "")
        if not h or h.lower() in _EMPTY:
            c.unknown.append(name)
            credit += 0.5
            continue
        ok = _norm(w) == _norm(h)
        if not ok and name == "Package":
            ok = _norm(w) in _norm(h) or _norm(h) in _norm(w)
        if not ok:
            a, b = _number(w), _number(h)
            if a is not None and b is not None:
                if _MEETS_OR_EXCEEDS.search(name):
                    ok = b >= a
                elif _AT_MOST.search(name):
                    ok = b <= a
                else:
                    ok = math.isclose(a, b, rel_tol=0.005)
        if ok:
            c.same.append(name if _norm(w) == _norm(h) else f"{name} ({h})")
            credit += 1
        else:
            c.differs.append(f"{name}: {w} → {h}")
            c.wrong = c.wrong or name in MUST_MATCH
    c.score = credit / len(want) if want else 0.0
    if c.wrong:
        c.score = min(c.score, 0.3)  # e.g. a different pitch: not a real alternative
    return c


def search_words(ref: PartResult, limit: int = 2) -> list[str]:
    """The reference's most telling spec values, to narrow a category down."""
    a = attributes(ref)
    words = [a[k] for k in KEY_ATTRS if k in a and re.search(r"\d", a[k])]
    words += [a[k] for k in KEY_ATTRS if k in a and a[k] not in words]
    return [w.replace("±", "") for w in words[:limit]]


def find(ref: PartResult, browse, need: int = 1, pins: int | None = None, words: str | None = None,
         limit: int = 40) -> list[Candidate]:
    """In-stock parts from ref's category, closest first.
    browse(parent_category, category, keyword, page=1, brand="") -> list[PartResult] (in-stock parts only)."""
    parent, child = ref.extra.get("Parent category", ""), ref.category
    if words is not None:
        tries = [(words, {}), (words, {"page": 2})]
    else:
        w = search_words(ref, 3)
        layout = pin_layout(ref, pins)
        first = w[0] if w else ""
        tries = [(f"{layout} {first}".strip(), {}), (layout, {})] if layout else []
        tries += [(" ".join(w[:2]), {})]
        tries += [(x, {"brand": ref.manufacturer}) for x in w]  # the maker's own equivalents
        tries += [(f"{first} {pins}P" if pins and first else "", {}), (first, {}), (first, {"page": 2})]
    found: dict[str, PartResult] = {}
    seen = set()
    for kw, opts in tries:
        key = (kw, tuple(sorted(opts.items())))
        if key in seen or (not kw and not opts and found):
            continue
        seen.add(key)
        for r in browse(parent, child, kw, **opts):
            found.setdefault(r.supplier_pn, r)
    target = _norm_mpn(ref.mpn)
    out = []
    for r in found.values():
        if r.supplier_pn == ref.supplier_pn or _norm_mpn(r.mpn) == target or not r.stock:
            continue
        n = pin_count(r)
        if pins and n and n != pins:
            continue  # the board's footprint has a different number of pins
        out.append(compare(ref, r))
    maker = (ref.manufacturer or "").split("(")[0].strip().lower()
    out.sort(key=lambda c: ((c.part.stock or 0) >= need, round(c.score, 2),
                            bool(maker) and maker in c.part.manufacturer.lower(), c.part.stock or 0), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- footprint fit
def holes(footprint_file: Path) -> tuple[list[tuple], list[tuple]]:
    """(pins, mounting holes) of a footprint as (x, y, drill-or-size) tuples."""
    node = sexpr.parse(footprint_file.read_text(encoding="utf-8", errors="replace"))
    pins, mounts = [], []
    for p in sexpr.children(node, "pad"):
        at = sexpr.child(p, "at")
        if not at or len(p) < 4:
            continue
        drill = sexpr.child(p, "drill")
        size = sexpr.child(p, "size")
        d = next((float(x) for x in (drill or [])[1:] if isinstance(x, str) and re.fullmatch(r"[\d.]+", x)), None)
        if d is None and size is not None:
            d = -float(size[1])  # SMD pad: remember its width (negative marks "no hole")
        spot = (float(at[1]), float(at[2]), round(d or 0.0, 2))
        numbered = bool(sexpr.unquote(p[1]).strip())
        (pins if numbered and p[2] != "np_thru_hole" else mounts).append(spot)
    return pins, mounts


def _aligned(a: list[tuple], b: list[tuple], tol: float) -> list[tuple] | None:
    """A rotation + shift of b that puts every point on a point of a; returns the transform or None."""
    if len(a) != len(b) or not a:
        return None
    ax, ay = sum(p[0] for p in a) / len(a), sum(p[1] for p in a) / len(a)
    bx, by = sum(p[0] for p in b) / len(b), sum(p[1] for p in b) / len(b)
    for turn in range(4):
        cos, sin = [(1, 0), (0, 1), (-1, 0), (0, -1)][turn]
        moved = [((p[0] - bx) * cos - (p[1] - by) * sin + ax, (p[0] - bx) * sin + (p[1] - by) * cos + ay, p[2])
                 for p in b]
        left = list(a)
        for m in moved:
            hit = next((q for q in left if abs(q[0] - m[0]) <= tol and abs(q[1] - m[1]) <= tol), None)
            if hit is None:
                break
            left.remove(hit)
        else:
            return [(turn, bx, by, ax, ay)]
    return None


def footprint_fit(board_fp: Path, cand_fp: Path, tol: float = 0.08) -> tuple[bool, str]:
    """Does the candidate's footprint put its pins where the board's footprint has them?"""
    mine_pins, mine_mounts = holes(board_fp)
    their_pins, their_mounts = holes(cand_fp)
    if len(mine_pins) != len(their_pins):
        return False, f"Does not fit: the board's footprint has {len(mine_pins)} pins, this part has {len(their_pins)}."
    t = _aligned(mine_pins, their_pins, tol)
    if not t:
        return False, f"Does not fit: both have {len(mine_pins)} pins, but they are in different positions."
    turn, bx, by, ax, ay = t[0]
    cos, sin = [(1, 0), (0, 1), (-1, 0), (0, -1)][turn]
    moved = [((p[0] - bx) * cos - (p[1] - by) * sin + ax, (p[0] - bx) * sin + (p[1] - by) * cos + ay, p[2])
             for p in their_pins]
    drill_note = ""
    for m in moved:
        q = min(mine_pins, key=lambda q: abs(q[0] - m[0]) + abs(q[1] - m[1]))
        if q[2] > 0 and m[2] > 0 and m[2] > q[2] + 0.11:
            drill_note = f" Its pins want a {m[2]:g} mm hole; the board has {q[2]:g} mm."
            break
    msg = f"Fits: all {len(mine_pins)} pins are in the same positions."
    moved_mounts = [((p[0] - bx) * cos - (p[1] - by) * sin + ax, (p[0] - bx) * sin + (p[1] - by) * cos + ay)
                    for p in their_mounts]
    unmatched = [m for m in moved_mounts
                 if not any(abs(q[0] - m[0]) <= 0.15 and abs(q[1] - m[1]) <= 0.15 for q in mine_mounts)]
    if their_mounts and not unmatched:
        msg += " Mounting holes match too."
    elif unmatched:
        return False, (msg[:-1] + f", but it needs {len(unmatched)} mounting hole(s) the board does not have."
                       + drill_note)
    elif mine_mounts:
        msg += f" The board's {len(mine_mounts)} mounting hole(s) would be left unused."
    return not drill_note, msg + drill_note
