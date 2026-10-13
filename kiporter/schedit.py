"""Write part fields (LCSC number, MPN, manufacturer) into the symbols already placed in a schematic.

Nothing is added or re-placed: the symbols and footprints stay where they are, only their fields change.
The schematic must not be open in KiCad's Schematic Editor - KiCad would overwrite the files on its next save.
"""
from __future__ import annotations

import copy
from pathlib import Path

from . import fields, sexpr


def sheet_files(root: Path) -> list[Path]:
    """The root schematic and every sub-sheet file it uses."""
    found: list[Path] = []
    todo = [root]
    while todo:
        f = todo.pop()
        if f in found or not f.is_file():
            continue
        found.append(f)
        try:
            node = sexpr.parse(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for sheet in sexpr.children(node, "sheet"):
            for p in sexpr.children(sheet, "property"):
                if len(p) > 2 and sexpr.unquote(p[1]) in ("Sheetfile", "Sheet file"):
                    todo.append((f.parent / sexpr.unquote(p[2])).resolve())
    return found


def symbol_refs(sym: list) -> set[str]:
    """Every reference a placed symbol goes by (its own, and one per sheet instance)."""
    refs = set()
    ref = fields.get_property(sym, "Reference")
    if ref:
        refs.add(ref)
    for inst in sexpr.children(sym, "instances"):
        for proj in sexpr.children(inst, "project"):
            for path in sexpr.children(proj, "path"):
                r = sexpr.child(path, "reference")
                if r and len(r) > 1:
                    refs.add(sexpr.unquote(r[1]))
    return refs


def _set_field(sym: list, key: str, value: str, names: dict[str, str]) -> bool:
    """Set the field for a canonical key, reusing whatever spelling the symbol already has.
    The key "value" is the symbol's Value."""
    if key == "value":
        return fields.set_property(sym, "Value", value)
    fill_only = key.endswith("?")  # "description?": write it only where the symbol has none
    key = key.rstrip("?")
    known = [n.lower() for n in dict.fromkeys([names[key]] + fields.ALIASES[key])]
    props = [p for p in sexpr.children(sym, "property") if len(p) > 2]
    if fill_only and any(sexpr.unquote(p[1]).lower() in known and sexpr.unquote(p[2]).strip() not in ("", "~")
                         for p in props):
        return False
    for p in props:
        if sexpr.unquote(p[1]).lower() in known:
            if sexpr.unquote(p[2]) == value:
                return False
            p[2] = sexpr.quote(value)
            return True
    # new field: copy a hidden one so the syntax matches this file's KiCad version
    template = next((p for p in props if sexpr.unquote(p[1]) in ("Datasheet", "Footprint")), None)
    if template is None:
        new = fields.new_property(names[key], value)
    else:
        new = copy.deepcopy(template)
        new[1], new[2] = sexpr.quote(names[key]), sexpr.quote(value)
    at = max(i for i, c in enumerate(sym) if sexpr.head(c) == "property") if props else len(sym) - 1
    sym.insert(at + 1, new)
    return True


def set_fields(root: Path, updates: dict[str, dict[str, str]], names: dict[str, str]) -> tuple[int, list[Path], dict]:
    """updates: reference -> {canonical key: value}. Returns (symbols changed, files to write, new file texts).
    Nothing is written here; see write()."""
    changed = 0
    texts: dict[Path, str] = {}
    for f in sheet_files(root):
        node = sexpr.parse(f.read_text(encoding="utf-8"))
        touched = False
        for sym in sexpr.children(node, "symbol"):
            if not sexpr.child(sym, "lib_id"):
                continue
            hit = next((updates[r] for r in symbol_refs(sym) if r in updates), None)
            if not hit:
                continue
            if any([_set_field(sym, k, v, names) for k, v in hit.items() if v]):
                changed += 1
                touched = True
        if touched:
            texts[f] = sexpr.dump(node) + "\n"
    return changed, list(texts), texts


def write(texts: dict) -> None:
    for f, text in texts.items():
        f.write_text(text, encoding="utf-8", newline="\n")
