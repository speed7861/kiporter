"""Project BOM: read it with kicad-cli, check stock, export in board-house / distributor formats.

Board houses change their templates from time to time; every format below
lists the columns its upload page asked for when it was written. The generic
format contains every field and can be re-mapped by hand if a shop changes.
"""
from __future__ import annotations

import csv
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import fields
from .config import Settings
from .kicad_env import NO_WINDOW, KiCadInstall


# ==================================================================== data
@dataclass
class BomLine:
    refs: list[str]
    qty: int
    value: str
    footprint: str  # "Lib:Name"
    description: str = ""
    datasheet: str = ""
    dnp: bool = False
    data: dict = field(default_factory=dict)  # canonical field data (mpn, manufacturer, lcsc, digikey, ...)
    mount: str = ""  # SMD | THT | Mixed
    offers: dict = field(default_factory=dict)  # supplier -> PartResult | str

    @property
    def mpn(self) -> str:
        return self.data.get("mpn", "")

    @property
    def manufacturer(self) -> str:
        return self.data.get("manufacturer", "")

    @property
    def lcsc(self) -> str:
        return self.data.get("lcsc", "")

    @property
    def footprint_name(self) -> str:
        return self.footprint.split(":", 1)[-1]

    @property
    def package(self) -> str:
        return self.data.get("package") or self.footprint_name

    @property
    def refs_str(self) -> str:
        return ",".join(self.refs)


@dataclass
class Placement:
    ref: str
    value: str
    package: str
    x: float
    y: float
    rot: float
    side: str  # top | bottom


def _natural(ref: str) -> tuple:
    m = re.match(r"([A-Za-z_#]*)(\d*)(.*)", ref)
    return (m.group(1), int(m.group(2)) if m.group(2) else 0, m.group(3)) if m else (ref, 0, "")


def _block_end(text: str, start: int) -> int:
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def mount_types(pcb: Path) -> dict[str, str]:
    """Reference -> 'SMD' / 'THT' / '' from the footprints' attributes on the board."""
    out: dict[str, str] = {}
    if not pcb.exists():
        return out
    text = pcb.read_text(encoding="utf-8", errors="replace")
    for m in re.finditer(r'\(footprint\s+"', text):
        block = text[m.start():_block_end(text, m.start())]
        ref = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', block) or \
            re.search(r'\(fp_text\s+reference\s+"?([^"\s)]+)', block)
        if not ref:
            continue
        attr = re.search(r"\(attr\s+([^)]*)\)", block)
        a = attr.group(1) if attr else ""
        out[ref.group(1)] = "THT" if "through_hole" in a else "SMD" if "smd" in a else ""
    return out


# ==================================================================== reading
def read_bom(project_file: Path, ki: KiCadInstall, settings: Settings) -> list[BomLine]:
    sch = project_file.with_suffix(".kicad_sch")
    if not sch.exists():
        raise RuntimeError(f"Schematic not found: {sch}")
    names = fields.field_names(settings.field_names)
    keys = [k for k in fields.FIELD_ORDER if k not in ("description", "datasheet")]
    wanted = {k: list(dict.fromkeys([names[k]] + fields.ALIASES[k])) for k in keys}
    extra = list(dict.fromkeys(n for k in keys for n in wanted[k]))
    flist = ["Reference", "Value", "Footprint", "Datasheet", "Description", "${QUANTITY}", "${DNP}"] + extra
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "bom.csv"
        r = subprocess.run(ki.cli_cmd("sch", "export", "bom", sch, "-o", out, "--fields", ",".join(flist),
                                      "--labels", ",".join(flist),
                                      "--group-by", ",".join(["Value", "Footprint", "${DNP}"] + wanted["mpn"]),
                                      "--ref-delimiter", ",", "--ref-range-delimiter", ""),
                           capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"kicad-cli BOM export failed: {(r.stderr or r.stdout).strip()}")
        rows = list(csv.DictReader(out.read_text(encoding="utf-8-sig").splitlines()))
    mounts = mount_types(project_file.with_suffix(".kicad_pcb"))
    lines = []
    for row in rows:
        data = {}
        for k in keys:
            for n in wanted[k]:
                v = (row.get(n) or "").strip()
                if v and v != "~":
                    data[k] = v
                    break
        refs = sorted([x.strip() for x in (row.get("Reference") or "").split(",") if x.strip()], key=_natural)
        kinds = {mounts.get(rf, "") for rf in refs} - {""}
        desc = (row.get("Description") or "").strip()
        ds = (row.get("Datasheet") or "").strip()
        lines.append(BomLine(
            refs=refs, qty=int(row.get("${QUANTITY}") or len(refs) or 0), value=(row.get("Value") or "").strip(),
            footprint=(row.get("Footprint") or "").strip(), description="" if desc == "~" else desc,
            datasheet="" if ds == "~" else ds,
            dnp=(row.get("${DNP}") or "").strip().upper() in ("DNP", "YES", "TRUE", "1", "X"),
            data=data, mount=kinds.pop() if len(kinds) == 1 else ("Mixed" if kinds else ""),
        ))
    lines.sort(key=lambda l: _natural(l.refs[0]) if l.refs else ("", 0, ""))
    return lines


def read_positions(project_file: Path, ki: KiCadInstall) -> list[Placement]:
    pcb = project_file.with_suffix(".kicad_pcb")
    if not pcb.exists():
        raise RuntimeError(f"PCB not found: {pcb}")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "pos.csv"
        r = subprocess.run(ki.cli_cmd("pcb", "export", "pos", pcb, "-o", out, "--format", "csv", "--units", "mm",
                                      "--side", "both"),
                           capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"kicad-cli position export failed: {(r.stderr or r.stdout).strip()}")
        rows = list(csv.DictReader(out.read_text(encoding="utf-8-sig").splitlines()))
    res = []
    for row in rows:
        try:
            res.append(Placement(row["Ref"], row.get("Val", ""), row.get("Package", ""), float(row["PosX"]),
                                 float(row["PosY"]), float(row["Rot"]), row.get("Side", "top").lower()))
        except (KeyError, ValueError):
            continue
    return sorted(res, key=lambda p: _natural(p.ref))


# ==================================================================== formats
@dataclass
class BomFormat:
    name: str
    kind: str  # "assembly" | "distributor" | "generic"
    columns: list[tuple[str, str]]  # (header, key)
    cpl: str | None = None  # placement format to offer alongside
    multiply_qty: bool = False  # distributor carts: quantity x boards
    ref_sep: str = ","
    needs: tuple[str, ...] = ()  # fields the shop really needs (warned about when missing)
    notes: str = ""
    url: str = ""


FORMATS: list[BomFormat] = [
    BomFormat("JLCPCB", "assembly",
              [("Comment", "value"), ("Designator", "refs"), ("Footprint", "footprint"), ("LCSC Part #", "lcsc")],
              cpl="JLCPCB", needs=("lcsc",),
              notes="Upload together with the CPL file. JLCPCB matches parts by 'LCSC Part #'. Check part rotations "
                    "in JLCPCB's placement preview - some footprints need a rotation offset.",
              url="https://jlcpcb.com/help/article/bill-of-materials-for-pcb-assembly"),
    BomFormat("PCBWay", "assembly",
              [("Item #", "item"), ("*Designator", "refs"), ("*Qty", "qty"), ("Manufacturer", "manufacturer"),
               ("*Mfg Part #", "mpn"), ("Description / Value", "value_desc"), ("*Package/Footprint", "package"),
               ("Type", "mount"), ("Your Instructions / Notes", "notes")],
              cpl="Generic", needs=("mpn",), notes="PCBWay prefers .xlsx. Columns marked * are mandatory.",
              url="https://www.pcbway.com/assembly-file-requirements.html"),
    BomFormat("ALLPCB", "assembly",
              [("No.", "item"), ("Designator", "refs"), ("Quantity", "qty"), ("Manufacturer", "manufacturer"),
               ("Manufacturer Part Number", "mpn"), ("Description", "value_desc"), ("Package", "package"),
               ("Type", "mount")],
              cpl="Generic", needs=("mpn",), url="https://www.allpcb.com/"),
    BomFormat("Seeed Fusion", "assembly",
              [("Designator", "refs"), ("Manufacturer Part Number or Seeed SKU", "mpn"), ("Qty", "qty"),
               ("Manufacturer", "manufacturer"), ("Description", "value_desc"), ("Package", "package")],
              cpl="Generic", needs=("mpn",), url="https://www.seeedstudio.com/fusion_pcb.html"),
    BomFormat("NextPCB", "assembly",
              [("No.", "item"), ("Designator", "refs"), ("Quantity", "qty"), ("Manufacturer", "manufacturer"),
               ("Manufacturer Part Number", "mpn"), ("Description", "value_desc"), ("Package/Footprint", "package"),
               ("Type", "mount")],
              cpl="Generic", needs=("mpn",), url="https://www.nextpcb.com/"),
    BomFormat("Eurocircuits", "assembly",
              [("Designator", "refs"), ("Quantity", "qty"), ("Manufacturer", "manufacturer"), ("MPN", "mpn"),
               ("Description", "value_desc"), ("Package", "package"), ("Type", "mount")],
              cpl="Generic", needs=("mpn",), url="https://www.eurocircuits.com/"),
    BomFormat("Aisler", "assembly",
              [("Designator", "refs"), ("Quantity", "qty"), ("Value", "value"), ("Footprint", "footprint"),
               ("Manufacturer", "manufacturer"), ("MPN", "mpn")],
              cpl="Generic", needs=("mpn",), url="https://aisler.net/"),
    BomFormat("MacroFab", "assembly",
              [("Designator", "refs"), ("Quantity", "qty"), ("Manufacturer", "manufacturer"), ("MPN", "mpn"),
               ("Description", "value_desc"), ("Package", "package"), ("Type", "mount"), ("Populate", "populate")],
              cpl="Generic", needs=("mpn",), url="https://macrofab.com/"),
    BomFormat("Digi-Key BOM Manager", "distributor",
              [("Quantity", "qty"), ("Digi-Key Part Number", "digikey"), ("Manufacturer Part Number", "mpn"),
               ("Manufacturer", "manufacturer"), ("Customer Reference", "refs_space"), ("Description", "value_desc")],
              multiply_qty=True, needs=("mpn",), url="https://www.digikey.com/BOM"),
    BomFormat("Mouser BOM Tool", "distributor",
              [("Mouser Part Number", "mouser"), ("Manufacturer Part Number", "mpn"), ("Manufacturer", "manufacturer"),
               ("Quantity", "qty"), ("Customer Part Number", "refs_space"), ("Description", "value_desc")],
              multiply_qty=True, needs=("mpn",), url="https://www.mouser.com/Bom/"),
    BomFormat("LCSC BOM Tool", "distributor",
              [("Quantity", "qty"), ("LCSC Part Number", "lcsc"), ("Manufacture Part Number", "mpn"),
               ("Manufacturer", "manufacturer"), ("Customer #", "refs_space"), ("Description", "value_desc")],
              multiply_qty=True, needs=("lcsc",), url="https://www.lcsc.com/bom"),
    BomFormat("Generic (all fields)", "generic",
              [("Item", "item"), ("References", "refs"), ("Qty", "qty"), ("Value", "value"), ("Footprint", "footprint"),
               ("Package", "package"), ("Type", "mount"), ("Manufacturer", "manufacturer"), ("MPN", "mpn"),
               ("Description", "description"), ("Datasheet", "datasheet"), ("LCSC", "lcsc"),
               ("Digi-Key PN", "digikey"), ("Mouser PN", "mouser"), ("Farnell PN", "farnell"), ("TME PN", "tme"),
               ("DNP", "dnp")], cpl="Generic"),
]
FORMAT_NAMES = [f.name for f in FORMATS]

CPL_FORMATS = {
    "JLCPCB": [("Designator", "ref"), ("Mid X", "x_mm"), ("Mid Y", "y_mm"), ("Layer", "layer"), ("Rotation", "rot")],
    "Generic": [("Designator", "ref"), ("Value", "value"), ("Footprint", "package"), ("Mid X", "x"), ("Mid Y", "y"),
                ("Rotation", "rot"), ("Layer", "layer")],
}


def get_format(name: str) -> BomFormat:
    return next((f for f in FORMATS if f.name == name), FORMATS[0])


def _cell(line: BomLine, key: str, item: int, boards: int, fmt: BomFormat) -> str | int:
    if key == "item":
        return item
    if key == "refs":
        return fmt.ref_sep.join(line.refs)
    if key == "refs_space":
        return " ".join(line.refs)
    if key == "qty":
        return line.qty * (boards if fmt.multiply_qty else 1)
    if key == "value":
        return line.value
    if key == "footprint":
        return line.footprint_name
    if key == "package":
        return line.package
    if key == "mount":
        return line.mount
    if key == "value_desc":
        return line.description or line.value
    if key == "description":
        return line.description
    if key == "datasheet":
        return line.datasheet
    if key == "dnp":
        return "DNP" if line.dnp else ""
    if key == "populate":
        return "No" if line.dnp else "Yes"
    if key == "notes":
        return "Do not populate" if line.dnp else ""
    return line.data.get(key, "")


def bom_rows(lines: list[BomLine], fmt: BomFormat, boards: int = 1, include_dnp: bool = False) -> list[list]:
    rows = [[h for h, _ in fmt.columns]]
    for i, ln in enumerate([l for l in lines if include_dnp or not l.dnp], start=1):
        rows.append([_cell(ln, k, i, boards, fmt) for _, k in fmt.columns])
    return rows


def cpl_rows(places: list[Placement], fmt_name: str, exclude: set[str] | None = None) -> list[list]:
    cols = CPL_FORMATS.get(fmt_name, CPL_FORMATS["Generic"])
    rows = [[h for h, _ in cols]]
    for p in places:
        if exclude and p.ref in exclude:
            continue
        vals = {"ref": p.ref, "value": p.value, "package": p.package, "x": f"{p.x:.4f}", "y": f"{p.y:.4f}",
                "x_mm": f"{p.x:.4f}mm", "y_mm": f"{p.y:.4f}mm", "rot": f"{p.rot:g}",
                "layer": "Top" if p.side.startswith("t") or p.side == "front" else "Bottom"}
        rows.append([vals[k] for _, k in cols])
    return rows


def missing_fields(lines: list[BomLine], fmt: BomFormat) -> list[str]:
    out = []
    for ln in lines:
        if ln.dnp:
            continue
        for k in fmt.needs:
            if not ln.data.get(k):
                out.append(f"{ln.refs_str}: no {k.upper() if k == 'mpn' else k}")
    return out


def write_table(rows: list[list], path: Path, sheet: str = "BOM") -> Path:
    if path.suffix.lower() == ".xlsx":
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font
        except ImportError as e:
            raise RuntimeError("Excel export needs the 'openpyxl' package (run the installer again)") from e
        wb = Workbook()
        ws = wb.active
        ws.title = sheet[:31]
        for r in rows:
            ws.append(r)
        for c in ws[1]:
            c.font = Font(bold=True)
        for col in ws.columns:
            width = max((len(str(c.value or "")) for c in col), default=8)
            ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 8), 60)
        wb.save(path)
    else:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerows(rows)
    return path


def export(lines: list[BomLine], fmt: BomFormat, out_dir: Path, base: str, boards: int = 1,
           include_dnp: bool = False, file_type: str = "csv", places: list[Placement] | None = None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^A-Za-z0-9]+", "_", fmt.name).strip("_")
    written = [write_table(bom_rows(lines, fmt, boards, include_dnp), out_dir / f"{base}_BOM_{tag}.{file_type}")]
    if places is not None and fmt.cpl:
        dnp_refs = set() if include_dnp else {r for l in lines if l.dnp for r in l.refs}
        written.append(write_table(cpl_rows(places, fmt.cpl, dnp_refs), out_dir / f"{base}_CPL_{tag}.{file_type}",
                                   "CPL"))
    return written


def write_stock_report(lines: list[BomLine], sources: list[str], boards: int, path: Path) -> Path:
    head = ["References", "Qty/board", f"Qty x{boards}", "Value", "Footprint", "MPN", "Manufacturer"]
    for s in sources:
        head += [f"{s} PN", f"{s} stock", f"{s} lead", f"{s} price"]
    rows = [head]
    for ln in lines:
        row = [ln.refs_str, ln.qty, ln.qty * boards, ln.value, ln.footprint_name, ln.mpn, ln.manufacturer]
        for s in sources:
            o = ln.offers.get(s)
            if o is not None and not isinstance(o, str):
                row += [o.supplier_pn, o.stock if o.stock is not None else "", o.lead_str, o.price_str]
            else:
                row += [o or "", "", "", ""]
        rows.append(row)
    return write_table(rows, path, "Stock")


# ==================================================================== KiCad's own BOM templates
# kicad-cli's --preset option writes empty files for the built-in presets in some KiCad
# versions, so every preset is translated into explicit --fields/--labels/--group-by arguments.
KICAD_LABELS = {"Reference": "Refs", "${QUANTITY}": "Qty", "${DNP}": "DNP", "${ITEM_NUMBER}": "#",
                "${EXCLUDE_FROM_BOM}": "Exclude from BOM", "${EXCLUDE_FROM_BOARD}": "Exclude from Board",
                "${EXCLUDE_FROM_SIM}": "Exclude from Simulation"}
_STD = ["Reference", "Value", "Datasheet", "Footprint", "${QUANTITY}", "${DNP}"]
KICAD_BUILTIN_PRESETS = {
    "Grouped By Value": (_STD, ["Value", "${DNP}"]),
    "Grouped By Value and Footprint": (_STD, ["Value", "Footprint", "${DNP}"]),
    "Attributes": (["Reference", "Value", "${DNP}", "${EXCLUDE_FROM_BOM}", "${EXCLUDE_FROM_SIM}",
                    "${EXCLUDE_FROM_BOARD}"], []),
}
KICAD_BUILTIN_FORMATS = {
    "CSV": {"field_delimiter": ",", "string_delimiter": '"', "ref_delimiter": ",", "ref_range_delimiter": ""},
    "TSV": {"field_delimiter": "\t", "string_delimiter": "", "ref_delimiter": ",", "ref_range_delimiter": ""},
    "Semicolons": {"field_delimiter": ";", "string_delimiter": "'", "ref_delimiter": ",",
                   "ref_range_delimiter": ""},
}
KICAD_PREFIX = "KiCad: "


@dataclass
class KicadTemplate:
    name: str  # shown in the format list, starts with KICAD_PREFIX
    kind: str  # "preset" | "plugin"
    fields: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    sort_field: str = "Reference"
    sort_asc: bool = True
    filter: str = ""
    exclude_dnp: bool = False
    command: list[str] = field(default_factory=list)  # plugin: [script, extension]
    source: str = ""


def _read_json(p: Path) -> dict:
    import json
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _from_preset(name: str, p: dict, source: str) -> KicadTemplate | None:
    ordered = [f for f in p.get("fields_ordered") or [] if f.get("name")]
    shown = [f for f in ordered if f.get("show")]
    if not shown:
        return None
    return KicadTemplate(
        name=KICAD_PREFIX + name, kind="preset", fields=[f["name"] for f in shown],
        labels=[f.get("label") or KICAD_LABELS.get(f["name"], f["name"]) for f in shown],
        group_by=[f["name"] for f in ordered if f.get("group_by")], sort_field=p.get("sort_field") or "Reference",
        sort_asc=bool(p.get("sort_asc", True)), filter=p.get("filter_string") or "",
        exclude_dnp=bool(p.get("exclude_dnp")), source=source)


def _plugin_dirs(ki: KiCadInstall) -> list[Path]:
    dirs = []
    if ki.cli and Path(ki.cli[0]).exists():
        dirs.append(Path(ki.cli[0]).parent / "scripting" / "plugins")
    if ki.share_dir:
        dirs += [ki.share_dir / "plugins", ki.share_dir / "scripting" / "plugins"]
    return [d for d in dirs if d.is_dir()]


def kicad_templates(project_file: Path, ki: KiCadInstall) -> list[KicadTemplate]:
    """BOM templates KiCad knows about: project settings and presets, built-in presets, BOM plugin scripts."""
    out: list[KicadTemplate] = []
    sch = _read_json(project_file).get("schematic", {})
    if sch.get("bom_settings"):
        t = _from_preset("Project BOM settings (as set up in KiCad)", sch["bom_settings"], "project")
        if t:
            out.append(t)
    for p in sch.get("bom_presets") or []:
        t = _from_preset(f"{p.get('name', 'Preset')} (project preset)", p, "project")
        if t:
            out.append(t)
    for name, (flds, groups) in KICAD_BUILTIN_PRESETS.items():
        out.append(KicadTemplate(KICAD_PREFIX + name, "preset", list(flds), [KICAD_LABELS.get(f, f) for f in flds],
                                 list(groups), source="built-in"))
    seen: set[str] = set()
    for plugin in (_read_json(ki.config_dir / "eeschema.json").get("bom", {}) or {}).get("plugins") or []:
        m = re.search(r'["\']?([^"\']+\.py)["\']?', plugin.get("command", ""))
        ext = re.search(r"%O(\.\w+)", plugin.get("command", ""))
        if m and Path(m.group(1)).exists() and Path(m.group(1)).name not in seen:
            seen.add(Path(m.group(1)).name)
            out.append(KicadTemplate(f"{KICAD_PREFIX}{plugin.get('name') or Path(m.group(1)).stem} (BOM plugin)",
                                     "plugin", command=[m.group(1), ext.group(1) if ext else ".csv"], source="user"))
    for d in _plugin_dirs(ki):
        for script in sorted(d.glob("bom*.py")):
            if script.name in seen:
                continue
            seen.add(script.name)
            ext = ".html" if "html" in script.stem else ".txt" if "txt" in script.stem else ".csv"
            out.append(KicadTemplate(f"{KICAD_PREFIX}{script.stem} (BOM plugin)", "plugin",
                                     command=[str(script), ext], source="kicad"))
    return out


def kicad_formats(project_file: Path) -> dict[str, dict]:
    sch = _read_json(project_file).get("schematic", {})
    fmts = dict(KICAD_BUILTIN_FORMATS)
    if sch.get("bom_fmt_settings"):
        fmts["Project format (as set up in KiCad)"] = sch["bom_fmt_settings"]
    for p in sch.get("bom_fmt_presets") or []:
        fmts[f"{p.get('name', 'Format')} (project preset)"] = p
    return fmts


def _python_exe() -> str:
    import sys
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def export_kicad_template(t: KicadTemplate, project_file: Path, ki: KiCadInstall, out_dir: Path, base: str,
                          fmt: dict | None = None) -> list[Path]:
    sch = project_file.with_suffix(".kicad_sch")
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^A-Za-z0-9]+", "_", t.name[len(KICAD_PREFIX):]).strip("_")
    if t.kind == "plugin":
        script, ext = t.command
        with tempfile.TemporaryDirectory() as td:
            xml = Path(td) / "netlist.xml"
            r = subprocess.run(ki.cli_cmd("sch", "export", "python-bom", sch, "-o", xml), capture_output=True,
                               text=True, timeout=300, creationflags=NO_WINDOW)
            if not xml.exists():
                raise RuntimeError(f"kicad-cli netlist export failed: {(r.stderr or r.stdout).strip()}")
            out = out_dir / f"{base}_BOM_{tag}{ext}"
            r = subprocess.run([_python_exe(), script, str(xml), str(out)], capture_output=True, text=True,
                               timeout=300, creationflags=NO_WINDOW, cwd=str(Path(script).parent))
        made = [p for p in out_dir.glob(f"{base}_BOM_{tag}*") if p.is_file()]
        if not made:
            raise RuntimeError(f"BOM plugin produced no file: {(r.stderr or r.stdout).strip()[:400]}")
        return made
    fmt = fmt or KICAD_BUILTIN_FORMATS["CSV"]
    ext = ".tsv" if fmt.get("field_delimiter") == "\t" else ".csv"
    out = out_dir / f"{base}_BOM_{tag}{ext}"
    args = ["sch", "export", "bom", sch, "-o", out, "--fields", ",".join(t.fields), "--labels",
            ",".join(l.replace(",", " ") for l in t.labels), "--sort-field", t.sort_field,
            "--field-delimiter", fmt.get("field_delimiter", ","), "--string-delimiter", fmt.get("string_delimiter", ""),
            "--ref-delimiter", fmt.get("ref_delimiter", ","), "--ref-range-delimiter", fmt.get("ref_range_delimiter", "")]
    if t.group_by:
        args += ["--group-by", ",".join(t.group_by)]
    if t.filter:
        args += ["--filter", t.filter]
    if t.exclude_dnp:
        args.append("--exclude-dnp")
    if fmt.get("keep_tabs"):
        args.append("--keep-tabs")
    if fmt.get("keep_line_breaks"):
        args.append("--keep-line-breaks")
    r = subprocess.run(ki.cli_cmd(*args), capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"kicad-cli BOM export failed: {(r.stderr or r.stdout).strip()}")
    return [out]


def preview_rows(path: Path, limit: int = 60) -> list[list[str]]:
    """Rows of a written CSV/TSV for previewing (HTML/TXT plugin output: lines)."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() in (".csv", ".tsv"):
        sample = text[:4000]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            return list(csv.reader(text.splitlines(), dialect))[:limit]
        except csv.Error:
            return list(csv.reader(text.splitlines()))[:limit]
    return [[line] for line in text.splitlines()[:limit]]
