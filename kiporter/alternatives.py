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
from difflib import SequenceMatcher

from .providers import PartResult, _norm_mpn, guess_package, tidy_description

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
_SCHEMATIC = re.compile(r"^\s*" + _NUM + r"\s*([RrkKmMGpnuµμPNU]?)(\d+)?\s*(?:Ω|Ω|ohms?|[FfHh])?(?![A-Za-z0-9])")


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
        if unit in ("p", "n", "u", "µ", "μ", "P", "N", "U"):
            return None
    elif unit in ("P", "N", "U"):
        unit = unit.lower()  # 0.1U, 100N: capitals are common in schematics
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


def keyword(ln, ref: PartResult | None = None) -> str | None:
    """Search words for a same-value part, e.g. '10k 0603 resistor'. None if the line is not a plain passive.
    ref: LCSC's listing of the line's part, used when the line itself does not give the value or package."""
    kind = kind_of(ln.refs)
    if not kind:
        return None
    v = line_value(ln, kind)
    pkg = line_package(ln)
    if ref is not None:
        if v is None:
            found = values_in(_text(ref), kind)
            v = found[0] if found else None
        pkg = pkg or ref.package
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


_DIELECTRIC = re.compile(r"(?<![A-Za-z0-9])(X7R|X5R|X7S|X7T|X6S|X8R|X8L|C0G|NP0|Y5V|Z5U)(?![A-Za-z0-9])", re.I)
_VOLT_ATTR = re.compile(r"Voltage[^:;]*:\s*(\d+(?:\.\d+)?)\s*(k?)V", re.I)
_VOLT = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*(k?)V(?![A-Za-z0-9])")
_TOL = re.compile(r"±\s*(\d+(?:\.\d+)?)\s*%")


def specs(text: str) -> dict:
    """Dielectric, rated voltage and tolerance as far as a description gives them."""
    out: dict = {}
    m = _DIELECTRIC.search(text or "")
    if m:
        out["dielectric"] = m.group(1).upper().replace("NP0", "C0G")
    m = _VOLT_ATTR.search(text or "") or _VOLT.search(text or "")
    if m:
        out["volts"] = float(m.group(1)) * (1000 if m.group(2) else 1)
    m = _TOL.search(text or "")
    if m:
        out["tol"] = float(m.group(1))
    return out


def passive_pick(ln, results: list[PartResult], need: int, ref: PartResult | None = None) -> Alternative | None:
    """A resistor / capacitor / inductor with the same value in the same package.
    ref: the part being replaced (LCSC's own listing of it) - its dielectric, voltage and tolerance must be met."""
    kind = kind_of(ln.refs)
    if not kind:
        return None
    ref_text = _text(ref) if ref is not None else ""
    v = line_value(ln, kind)
    if v is None:
        found = values_in(ref_text, kind)
        v = found[0] if found else None
    want = line_package(ln) or (ref.package if ref is not None else "")
    if v is None or not want:
        return None
    wanted = specs(f"{ref_text} {ln.description}")
    target = _norm_mpn(ln.mpn)
    ref_mfr = (ref.manufacturer if ref is not None else ln.manufacturer or "").lower()
    out = []
    for r in results:
        if target and _norm_mpn(r.mpn) == target:
            continue
        if _package_ok(want, r) is not True or not any(_close(v, x) for x in values_in(_text(r), kind)):
            continue
        have = specs(_text(r))
        score = 0.0
        if "dielectric" in wanted:
            if have.get("dielectric") != wanted["dielectric"]:
                continue  # an X7R part is not replaced by Y5V
            score += 1
        if "volts" in wanted and "volts" in have:
            if have["volts"] < wanted["volts"]:
                continue
            score += 1 if have["volts"] == wanted["volts"] else 0.5
        if "tol" in wanted and "tol" in have:
            if have["tol"] > wanted["tol"]:
                continue
            score += 0.5
        if ref_mfr and ref_mfr.split()[0] in r.manufacturer.lower():
            score += 0.5
        if target:
            score += 0.5 * SequenceMatcher(None, target, _norm_mpn(r.mpn)).ratio()
        met = [str(x) for x in (wanted.get("dielectric"), f"{have['volts']:g}V" if "volts" in wanted and "volts" in have
                                else None, f"±{have['tol']:g}%" if "tol" in wanted and "tol" in have else None) if x]
        detail = ", ".join([f"{format_value(v, kind)} {want}"] + met)
        out.append(Alternative(r, f"same value and package ({detail})"
                               + ("" if len(met) == 3 else ", check tolerance and rating"), score))
    return _rank(out, need)


def same_part_pick(ln, results: list[PartResult], need: int) -> Alternative | None:
    """LCSC's listing of the very same manufacturer part number (even when it is out of stock)."""
    target = _norm_mpn(ln.mpn)
    def same_number(r: PartResult) -> bool:
        cand = _norm_mpn(r.mpn)  # all-digit numbers (Molex) are often written without their leading zeros
        return cand == target or (target.isdigit() and cand.lstrip("0") == target.lstrip("0"))
    same = [Alternative(r, "same part at LCSC", SAME_PART) for r in results if target and same_number(r)]
    if not same:
        return None
    return max(same, key=lambda a: ((a.part.stock or 0) >= need, a.part.stock or 0))


def _replacement(ln, results: list[PartResult], need: int, search, ref: PartResult | None) -> Alternative | None:
    """An in-stock part to use instead: same family, or same value and package (meeting ref's ratings)."""
    found = family_pick(ln, results, need) if ln.mpn else None
    if (found is None or (found.part.stock or 0) < need or kind_of(ln.refs)) and search is not None:
        kw = keyword(ln, ref)
        if kw:
            want = specs(f"{_text(ref) if ref is not None else ''} {ln.description}").get("dielectric")
            hits = list(search(f"{kw.rsplit(' ', 1)[0]} {want}")) if want else []
            if len(hits) < 10:
                hits += list(search(kw))
            found = _rank([a for a in (found, passive_pick(ln, hits, need, ref)) if a], need)
    return found


def _other_listing(ln, need: int, search) -> Alternative | None:
    """LCSC sometimes lists one part twice (0026013115 and 26013115): find the listing that has stock."""
    if not ln.mpn or search is None:
        return None
    words = [ln.mpn] + ([ln.mpn.lstrip("0")] if ln.mpn.isdigit() and ln.mpn.startswith("0") else [])
    hits = [r for w in words for r in search(w) if r.supplier_pn != ln.lcsc]
    pick = same_part_pick(ln, hits, need)
    return pick if pick is not None and (pick.part.stock or 0) >= need else None


def suggest(ln, results: list[PartResult], need: int, search=None) -> Alternative | None:
    """LCSC suggestion for a line. results: LCSC's answer for the line's LCSC or part number;
    search(keyword) -> results is used for same-value lookups.

    A part that LCSC has enough of is never replaced. A different part is suggested only when LCSC
    does not list the part, or lists it without enough stock."""
    if ln.lcsc:
        own = next((r for r in results if r.supplier_pn == ln.lcsc), None)
        if own is not None and (own.stock or 0) >= need:
            return None  # already an LCSC part, and in stock
        other = _other_listing(ln, need, search)
        if other is not None:
            other.reason = f"same part, another LCSC listing; {ln.lcsc} has {(own.stock if own else 0) or 0} in stock"
            return other
        found = _replacement(ln, results, need, search, own)
        if found is not None and own is not None:
            found.reason += f"; {ln.lcsc} has {own.stock or 0} in stock"
        return found
    if value_part_number(ln):
        return generic_pick(ln, results, need)
    same = same_part_pick(ln, results, need)
    if same is not None and (same.part.stock or 0) >= need:
        return same  # LCSC has this very part: nothing else to recommend
    found = _replacement(ln, results, need, search, same.part if same else None)
    if found is not None and (found.part.stock or 0) >= need:
        if same is not None:
            found.reason += f"; LCSC's own {same.part.supplier_pn} has {same.part.stock or 0} in stock"
        return found
    return same or found


def switch(ln, alt: Alternative) -> None:
    """Make the suggested LCSC part the line's part (kept in ln.original so it can be undone)."""
    if ln.original is None:
        ln.original = (dict(ln.data), dict(ln.offers), ln.value, ln.description, ln.pulled_description)
    p = alt.part
    # a Value that is itself the old part number (MMBT2222A) follows the part; a real value (10k) stays
    value_is_number = bool(value_part_number(ln)) or (bool(ln.mpn) and _norm_mpn(ln.value) == _norm_mpn(ln.mpn))
    ln.data["lcsc"] = p.supplier_pn
    if not alt.exact:  # a different part: its own number and maker go into the BOM
        if value_is_number:
            ln.value = p.mpn
        ln.data["mpn"] = p.mpn
        if p.manufacturer:
            ln.data["manufacturer"] = p.manufacturer
        if p.source == "LCSC":  # a different part: describe the new one
            ln.description, ln.pulled_description = tidy_description(p), True
        ln.offers = {s: ("re-check" if s != "LCSC" else o) for s, o in ln.offers.items()}
    ln.offers["LCSC"] = p


def switch_back(ln) -> bool:
    if ln.original is None:
        return False
    ln.data, ln.offers, ln.value, ln.description, ln.pulled_description = ln.original
    ln.original = None
    return True


def best(ln, need: int) -> Alternative | None:
    """The suggestion for a line (from ln.alts), if it has one."""
    return next((a for a in ln.alts.values() if a is not None), None)
