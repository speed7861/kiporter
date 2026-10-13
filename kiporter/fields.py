"""One consistent set of symbol fields, whatever the part's source.

Every importer (LCSC/EasyEDA, Digi-Key, Mouser, Farnell, TME, Octopart, vendor
zips) and the library clean-up tool write the same canonical field names, in
the same order. Vendor-specific spellings are folded into them.
"""
from __future__ import annotations

from . import sexpr

# canonical key -> default KiCad field name (user can rename in Settings)
DEFAULT_FIELD_NAMES: dict[str, str] = {
    "manufacturer": "Manufacturer",
    "mpn": "MPN",
    "description": "Description",
    "datasheet": "Datasheet",
    "package": "Package",
    "lcsc": "LCSC Part",
    "digikey": "Digi-Key PN",
    "mouser": "Mouser PN",
    "farnell": "Farnell PN",
    "tme": "TME PN",
}
FIELD_ORDER = list(DEFAULT_FIELD_NAMES)

# canonical key -> spellings used by EasyEDA, SamacSys, Ultra Librarian, SnapEDA, older libs
ALIASES: dict[str, list[str]] = {
    "manufacturer": ["Manufacturer", "Manufacturer_Name", "Manufacturer Name", "MFGR", "MFG", "MFR",
                     "Mfr", "MF", "Mfg Name", "Vendor", "Brand", "MANUFACTURER"],
    "mpn": ["MPN", "Manufacturer_Part_Number", "Manufacturer Part Number", "Mfr Part Number",
            "Mfr. Part No.", "MFR_PN", "MFPN", "MP", "PartNumber", "Part Number", "Part_Number"],
    "description": ["Description", "DESC", "Desc", "Description_1"],
    "datasheet": ["Datasheet", "DataSheet", "Datasheet URL", "Datasheet_Link"],
    "package": ["Package", "PACKAGE", "Package / Case", "Case", "Case/Package", "Package_Name"],
    "lcsc": ["LCSC Part", "LCSC", "LCSC Part #", "LCSC_Part", "LCSC PN", "JLCPCB Part #", "JLC"],
    "digikey": ["Digi-Key PN", "Digi-Key_PN", "DigiKey PN", "Digikey Part Number", "Digi-Key Part Number",
                "DK_PN", "DigiKey"],
    "mouser": ["Mouser PN", "Mouser_PN", "Mouser Part Number", "Mouser"],
    "farnell": ["Farnell PN", "Farnell_PN", "Farnell Part Number", "Newark PN", "Newark Part Number",
                "element14 PN"],
    "tme": ["TME PN", "TME_PN", "TME Part Number", "TME"],
}

# stale price/stock/marketing fields that vendor zips stuff into symbols
JUNK_FIELDS = {
    "mouser price/stock", "arrow part number", "arrow price/stock", "mouser testing part number",
    "mouser testing price/stock", "price", "availability", "check_prices", "snapeda_link",
    "purchase-url", "description_1", "height", "rs part number", "rs price/stock",
}

KEEP_ALWAYS = {"reference", "value", "footprint", "datasheet", "description"}


def field_names(overrides: dict | None = None) -> dict[str, str]:
    names = dict(DEFAULT_FIELD_NAMES)
    for k, v in (overrides or {}).items():
        if k in names and v:
            names[k] = v
    return names


def _alias_lookup(names: dict[str, str]) -> dict[str, str]:
    """lower-case spelling -> canonical key."""
    out: dict[str, str] = {}
    for key, spellings in ALIASES.items():
        for s in spellings:
            out[s.lower()] = key
        out[names[key].lower()] = key
    return out


def _empty(v: str) -> bool:
    return v.strip() in ("", "~")


def new_property(name: str, value: str) -> list:
    return ["property", sexpr.quote(name), sexpr.quote(value), ["at", "0", "0", "0"],
            ["effects", ["font", ["size", "1.27", "1.27"]], ["hide", "yes"]]]


def get_property(sym: list, name: str) -> str | None:
    for p in sexpr.children(sym, "property"):
        if len(p) > 2 and sexpr.unquote(p[1]).lower() == name.lower():
            return sexpr.unquote(p[2])
    return None


def set_property(sym: list, name: str, value: str, force: bool = True) -> bool:
    """Set (or add, hidden) a symbol property. Returns True if anything changed."""
    for p in sexpr.children(sym, "property"):
        if len(p) > 2 and sexpr.unquote(p[1]).lower() == name.lower():
            if sexpr.unquote(p[1]) != name:
                p[1] = sexpr.quote(name)
            if (force or _empty(sexpr.unquote(p[2]))) and sexpr.unquote(p[2]) != value:
                p[2] = sexpr.quote(value)
                return True
            return False
    idx = max((i for i, c in enumerate(sym) if sexpr.head(c) == "property"), default=len(sym) - 1)
    sym.insert(idx + 1, new_property(name, value))
    return True


def normalize_symbol(sym: list, names: dict[str, str], drop_junk: bool = True) -> list[str]:
    """Fold alias fields into canonical names; returns a list of human readable changes."""
    changes: list[str] = []
    lookup = _alias_lookup(names)
    props = [p for p in sexpr.children(sym, "property") if len(p) > 2]
    canonical_present = {sexpr.unquote(p[1]).lower() for p in props}
    for p in props:
        pname = sexpr.unquote(p[1])
        low = pname.lower()
        if low in KEEP_ALWAYS and low not in ("description", "datasheet"):
            continue
        if drop_junk and low in JUNK_FIELDS:
            sym.remove(p)
            changes.append(f"removed stale field '{pname}'")
            continue
        key = lookup.get(low)
        if not key:
            continue
        target = names[key]
        if pname == target:
            continue
        val = sexpr.unquote(p[2])
        if target.lower() in canonical_present and target.lower() != low:
            # canonical field already exists: keep its value, fill it if empty, drop alias
            cur = get_property(sym, target) or ""
            if _empty(cur) and not _empty(val):
                set_property(sym, target, val)
                changes.append(f"'{pname}' → '{target}' = {val}")
            else:
                changes.append(f"removed duplicate field '{pname}'")
            sym.remove(p)
        else:
            p[1] = sexpr.quote(target)
            canonical_present.add(target.lower())
            changes.append(f"renamed '{pname}' → '{target}'")
    if changes:
        reorder(sym, names)
    return changes


DISPLAY_ORDER = ["description", "manufacturer", "mpn", "package", "lcsc", "digikey", "mouser", "farnell", "tme"]


def reorder(sym: list, names: dict[str, str]) -> None:
    """Same field order on every symbol: Reference, Value, Footprint, Datasheet, Description,
    Manufacturer, MPN, Package, supplier part numbers, then anything else."""
    props = [c for c in sym if sexpr.head(c) == "property"]
    if not props:
        return
    rank = {"reference": 0, "value": 1, "footprint": 2, "datasheet": 3}
    for i, key in enumerate(DISPLAY_ORDER):
        rank.setdefault(names[key].lower(), 4 + i)
    order = sorted(props, key=lambda p: (rank.get(sexpr.unquote(p[1]).lower(), 100), props.index(p)))
    first = sym.index(props[0])
    rest = [c for c in sym if sexpr.head(c) != "property"]
    sym[:] = rest[:first] + order + rest[first:]


def apply_part_data(sym: list, data: dict[str, str], names: dict[str, str]) -> None:
    """Write part data (canonical keys) into the symbol, then put the fields in the standard order."""
    for key in FIELD_ORDER:
        val = data.get(key, "")
        if val:
            # keep an existing description written by the symbol author
            set_property(sym, names[key], val, force=key != "description")
    reorder(sym, names)
