"""Suggest an LCSC part for BOM lines that have no LCSC number.

A part that is already at LCSC - the line has an LCSC number, or LCSC lists the same manufacturer part
number - never gets a different part suggested, whatever its stock.

Suggestions come from LCSC only, so the LCSC column of the BOM (what JLCPCB assembles from) can be filled in.
Three ways of being "close", best first:
  * same part - LCSC stocks the very same manufacturer part number;
  * same family - the part number starts the same way (LM358DR -> LM358DR2G) and the package agrees;
  * same value and package - for plain resistors, capacitors and inductors (10k 0603 -> any 10k 0603).
Suggestions are a starting point: tolerance, voltage rating and suffix differences are not checked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .providers import PartResult, _norm_mpn, guess_package

KINDS = {"R": "resistor", "C": "capacitor", "L": "inductor"}
SAME_PART = 10.0  # score of a suggestion that is the very same manufacturer part number
_MULT = {"": 1.0, "r": 1.0, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
         "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
_NUM = r"(\d+(?:\.\d+)?)"
# values as suppliers write them inside a description
_IN_TEXT = {
    "resistor": re.compile(_NUM + r"\s*([kKmMG]?)\s*(?:Ω|Ω|ohms?|OHMS?|Ohms?)"),
    "capacitor": re.compile(_NUM + r"\s*([pnuµμPNU])[fF]"),
    "inductor": re.compile(_NUM + r"\s*([nuµμmNU])[hH]"),
}
# values as people type them in a schematic: 10k, 4k7, 4R7, 100n, 0.1uF, 4u7
_SCHEMATIC = re.compile(r"^\s*" + _NUM + r"\s*([RrkKmMGpnuµμ]?)(\d+)?\s*(?:Ω|Ω|ohms?|[FfHh])?(?![A-Za-z0-9])")


_CHIP = re.compile(r"(?<!\d)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\d)")


@dataclass
class Alternative:
    part: PartResult
    reason: str
    score: float = 0.0

    def text(self) -> str:
        p = self.part
        bits = [b for b in (p.stock_str and f"{p.stock_str} in stock", p.price_str) if b]
        return f"{p.supplier_pn}  {p.mpn}" + (f" ({', '.join(bits)})" if bits else "") + f" - {self.reason}"

    @property
    def exact(self) -> bool:
        return self.score >= SAME_PART


_PART_NUMBER = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9][A-Za-z0-9\-/.,#+]{4,}$")


def value_part_number(ln) -> str:
    """The Value field when it holds a part number (MMBT2222A, 1N4148WS) on a line with no MPN field."""
    v = (ln.value or "").strip()
    if ln.mpn or ln.lcsc or not _PART_NUMBER.match(v):
        return ""
    kind = kind_of(ln.refs)
    if kind and schematic_value(v, kind) is not None:
        return ""  # 4k7, 100n ... are values, not part numbers
    return v


def generic_pick(ln, results: list[PartResult], need: int) -> Alternative | None:
    """For a part number taken from the Value field (no manufacturer known): the best-stocked part whose
    number starts with it, in the line's package - e.g. MMBT2222A -> MMBT2222ALT1G."""
    target = _norm_mpn(value_part_number(ln))
    if len(target) < 5:
        return None
    want = line_package(ln)
    out = [Alternative(r, f"same part number ({value_part_number(ln)}), best stocked - check the maker and suffix")
           for r in results if _norm_mpn(r.mpn).startswith(target) and _package_ok(want, r) is not False]
    out = [a for a in out if a.part.stock]
    if not out:
        return None
    return max(out, key=lambda a: ((a.part.stock or 0) >= need, _package_ok(want, a.part) is True, a.part.stock or 0))


def kind_of(refs: list[str]) -> str | None:
    """resistor / capacitor / inductor when every reference is a plain R, C or L."""
    kinds = set()
    for ref in refs:
        m = re.match(r"^([A-Za-z]+)\d", ref)
        kinds.add(KINDS.get(m.group(1).upper()) if m else None)
    return kinds.pop() if len(kinds) == 1 else None


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 0.005 * max(abs(a), abs(b))


def values_in(text: str, kind: str) -> list[float]:
    out = []
    for num, unit in _IN_TEXT[kind].findall(text or ""):
        if kind != "resistor":
            unit = unit.lower() if unit not in ("µ", "μ") else unit
        out.append(float(num) * _MULT[unit])
    return out


def schematic_value(value: str, kind: str) -> float | None:
    """'4k7' -> 4700.0, '100n' -> 1e-7. None when the text is not a plain value (e.g. a part number)."""
    m = _SCHEMATIC.match(value or "")
    if not m:
        return None
    num, unit, frac = m.groups()
    if frac:  # 4k7 style: the unit letter stands where the decimal point is
        if "." in num or not unit:
            return None
        num = f"{num}.{frac}"
    if kind == "resistor":
        if unit in ("p", "n", "u", "µ", "μ"):
            return None
    elif unit in ("", "R", "r", "k", "K", "M", "G"):
        return None  # a bare number is not a capacitance or inductance
    return float(num) * _MULT[unit.lower() if unit in ("R",) else unit]


def line_value(ln, kind: str) -> float | None:
    v = schematic_value(ln.value, kind)
    if v is None:  # value field holds a part number: look in the description instead
        found = values_in(f"{ln.description} {ln.value}", kind)
        v = found[0] if found else None
    return v


def line_package(ln) -> str:
    # footprint names use underscores (R_0603_1608Metric), which hide the package from a word match
    texts = [ln.data.get("package", "").replace("_", " "), ln.footprint_name.replace("_", " ")]
    found = guess_package(*texts)
    if not found:  # chip size glued to a letter, as in EasyEDA's "R0603" / "C0402"
        m = _CHIP.search(" ".join(texts))
        found = m.group(1) if m else ""
    return found


def format_value(v: float, kind: str) -> str:
    if kind == "resistor":
        for mult, suffix in ((1e6, "M"), (1e3, "k"), (1, "")):
            if v >= mult:
                return f"{v / mult:g}{suffix}" if suffix else f"{v:g} ohm"
        return f"{v:g} ohm"
    unit = "F" if kind == "capacitor" else "H"
    for mult, suffix in ((1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if v >= mult * 0.9999:
            return f"{v / mult:g}{suffix}{unit}"
    return f"{v:g}{unit}"


def keyword(ln) -> str | None:
    """Search words for a same-value part, e.g. '10k 0603 resistor'. None if the line is not a plain passive."""
    kind = kind_of(ln.refs)
    if not kind:
        return None
    v = line_value(ln, kind)
    pkg = line_package(ln)
    if v is None or not pkg:
        return None
    return f"{format_value(v, kind)} {pkg} {kind}"


def _text(r: PartResult) -> str:
    return " ".join([r.mpn, r.description, r.package, r.category, *(str(x) for x in r.extra.values())])


def _package_ok(want: str, r: PartResult) -> bool | None:
    """True / False, or None when the candidate's package is unknown."""
    if not want:
        return None
    have = r.package or guess_package(r.description)
    if have:
        return have.upper() == want.upper()
    return True if re.search(rf"(?<![A-Za-z0-9]){re.escape(want)}(?![A-Za-z0-9])", _text(r), re.I) else None


def _rank(alts: list[Alternative], need: int) -> Alternative | None:
    alts = [a for a in alts if a.part.stock]
    if not alts:
        return None
    # enough stock first, then the closest match, then a comfortable stock level, then price
    return max(alts, key=lambda a: ((a.part.stock or 0) >= need, a.score, (a.part.stock or 0) >= 10 * need,
                                    -(a.part.price if a.part.price is not None else 1e9), a.part.stock or 0))


def family_pick(ln, results: list[PartResult], need: int) -> Alternative | None:
    """A part whose number starts the same way and whose package agrees."""
    target = _norm_mpn(ln.mpn)
    if len(target) < 5:
        return None
    want = line_package(ln)
    out = []
    for r in results:
        cand = _norm_mpn(r.mpn)
        if not cand or cand == target:
            continue
        common = next((i for i, (a, b) in enumerate(zip(target, cand)) if a != b), min(len(target), len(cand)))
        if common < max(5, int(0.6 * len(target))) or _package_ok(want, r) is False:
            continue
        same_mfr = bool(ln.manufacturer) and ln.manufacturer.split()[0].lower() in r.manufacturer.lower()
        out.append(Alternative(r, "same family, check the suffix", common / len(target) + (0.2 if same_mfr else 0)))
    return _rank(out, need)


def passive_pick(ln, results: list[PartResult], need: int) -> Alternative | None:
    """A resistor / capacitor / inductor with the same value in the same package."""
    kind = kind_of(ln.refs)
    v = line_value(ln, kind) if kind else None
    want = line_package(ln)
    if v is None or not want:
        return None
    target = _norm_mpn(ln.mpn)
    out = []
    for r in results:
        if target and _norm_mpn(r.mpn) == target:
            continue
        if _package_ok(want, r) is not True or not any(_close(v, x) for x in values_in(_text(r), kind)):
            continue
        out.append(Alternative(r, f"same value and package ({format_value(v, kind)} {want}), check tolerance and rating"))
    return _rank(out, need)


def same_part_pick(ln, results: list[PartResult], need: int) -> Alternative | None:
    """LCSC's listing of the very same manufacturer part number (even when it is out of stock)."""
    target = _norm_mpn(ln.mpn)
    same = [Alternative(r, "same part at LCSC", SAME_PART) for r in results if target and _norm_mpn(r.mpn) == target]
    if not same:
        return None
    return max(same, key=lambda a: ((a.part.stock or 0) >= need, a.part.stock or 0))


def suggest(ln, results: list[PartResult], need: int, search=None) -> Alternative | None:
    """LCSC suggestion for a line without an LCSC number. results: LCSC's answer for the line's part number;
    search(keyword) -> results is used for a same-value lookup when LCSC does not list the part."""
    if ln.lcsc:
        return None  # already an LCSC part
    if value_part_number(ln):
        return generic_pick(ln, results, need)
    found = same_part_pick(ln, results, need)
    if found is not None:
        return found  # LCSC lists this very part: nothing else to recommend
    found = family_pick(ln, results, need) if ln.mpn else None
    if (found is None or (found.part.stock or 0) < need) and search is not None:
        kw = keyword(ln)
        if kw:
            found = _rank([a for a in (found, passive_pick(ln, search(kw), need)) if a], need)
    return found


def switch(ln, alt: Alternative) -> None:
    """Make the suggested LCSC part the line's part (kept in ln.original so it can be undone)."""
    if ln.original is None:
        ln.original = (dict(ln.data), dict(ln.offers))
    p = alt.part
    ln.data["lcsc"] = p.supplier_pn
    if not alt.exact:  # a different part: its own number and maker go into the BOM
        ln.data["mpn"] = p.mpn
        if p.manufacturer:
            ln.data["manufacturer"] = p.manufacturer
        ln.offers = {s: ("re-check" if s != "LCSC" else o) for s, o in ln.offers.items()}
    ln.offers["LCSC"] = p


def switch_back(ln) -> bool:
    if ln.original is None:
        return False
    ln.data, ln.offers = ln.original
    ln.original = None
    return True


def best(ln, need: int) -> Alternative | None:
    """The suggestion for a line (from ln.alts), if it has one."""
    return next((a for a in ln.alts.values() if a is not None), None)
