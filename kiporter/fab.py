"""Fabrication outputs: '<Project> Build' folder with schematic PDF, BOM, DRC report and
a 'Gerbers' sub-folder with Gerbers, drill and placement files, plus an upload zip.

Zones are refilled and DRC is run on a temporary copy of the board, so the
user's .kicad_pcb is never modified (safe even while it is open in KiCad).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import bom
from .config import Settings
from .kicad_env import NO_WINDOW, KiCadInstall

Log = Callable[[str], None]

BASE_LAYERS = ["F.Cu", "B.Cu", "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen", "F.Mask", "B.Mask",
               "Edge.Cuts"]


@dataclass
class FabProfile:
    name: str
    bom_format: str | None  # name in bom.FORMATS
    gerber_flags: list[str] = field(default_factory=list)
    drill_flags: list[str] = field(default_factory=list)
    extra_layers: list[str] = field(default_factory=list)
    notes: str = ""
    url: str = ""


_DRILL_MM = ["--format", "excellon", "--excellon-units", "mm", "--excellon-zeros-format", "decimal",
             "--drill-origin", "absolute", "--excellon-oval-format", "alternate"]

PROFILES: list[FabProfile] = [
    FabProfile("JLCPCB", "JLCPCB", ["--no-x2", "--subtract-soldermask"],
               _DRILL_MM + ["--generate-map", "--map-format", "gerberx2"],
               notes="Upload the Gerber zip on jlcpcb.com. For assembly, upload the BOM and the CPL file from the "
                     "Gerbers folder in the assembly step and check part rotations in the preview.",
               url="https://jlcpcb.com/quote"),
    FabProfile("PCBWay", "PCBWay", ["--no-x2"], _DRILL_MM + ["--generate-map", "--map-format", "gerberx2"],
               notes="Upload the Gerber zip; for assembly add the BOM (.xlsx preferred) and the placement file.",
               url="https://www.pcbway.com/orderonline.aspx"),
    FabProfile("OSH Park", None, [], ["--format", "excellon", "--excellon-units", "in",
                                      "--excellon-zeros-format", "decimal", "--drill-origin", "absolute"],
               notes="Upload the Gerber zip (OSH Park also accepts the .kicad_pcb directly). No assembly service.",
               url="https://oshpark.com/"),
    FabProfile("ALLPCB", "ALLPCB", ["--no-x2"], _DRILL_MM + ["--generate-map", "--map-format", "gerberx2"],
               url="https://www.allpcb.com/"),
    FabProfile("Seeed Fusion", "Seeed Fusion", ["--no-x2"], _DRILL_MM, url="https://www.seeedstudio.com/fusion_pcb.html"),
    FabProfile("NextPCB", "NextPCB", ["--no-x2"], _DRILL_MM + ["--generate-map", "--map-format", "gerberx2"],
               url="https://www.nextpcb.com/"),
    FabProfile("Eurocircuits", "Eurocircuits", [], _DRILL_MM + ["--generate-map", "--map-format", "gerberx2"],
               notes="Eurocircuits reads Gerber X2 attributes, so X2 is kept on.", url="https://www.eurocircuits.com/"),
    FabProfile("Aisler", "Aisler", [], _DRILL_MM, notes="Aisler also accepts the KiCad project directly.",
               url="https://aisler.net/"),
    FabProfile("MacroFab", "MacroFab", [], _DRILL_MM, url="https://macrofab.com/"),
    FabProfile("Generic (KiCad defaults)", "Generic (all fields)", [],
               _DRILL_MM + ["--generate-map", "--map-format", "pdf"]),
]
PROFILE_NAMES = [p.name for p in PROFILES]


def get_profile(name: str) -> FabProfile:
    return next((p for p in PROFILES if p.name == name), PROFILES[0])


@dataclass
class FabOptions:
    profile: str = "JLCPCB"
    refill_drc: bool = True
    schematic_pdf: bool = True
    bom: bool = True
    gerbers: bool = True
    drill: bool = True
    placement: bool = True
    make_zip: bool = True
    boards: int = 1
    file_type: str = "csv"


@dataclass
class FabResult:
    build_dir: Path
    gerber_dir: Path
    files: list[Path] = field(default_factory=list)
    zip_file: Path | None = None
    drc_summary: str = ""
    warnings: list[str] = field(default_factory=list)


def build_dir_for(project_file: Path) -> Path:
    return project_file.parent / f"{project_file.stem} Build"


def _run(ki: KiCadInstall, *args, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(ki.cli_cmd(*args), capture_output=True, text=True, timeout=timeout,
                          creationflags=NO_WINDOW)


def inner_layers(pcb_text: str) -> list[str]:
    head = pcb_text[:40000]
    layers_block = head[head.find("(layers"):head.find("(setup")] if "(layers" in head else ""
    names = re.findall(r'"(In\d+\.Cu)"', layers_block)
    return sorted(dict.fromkeys(names), key=lambda n: int(re.search(r"\d+", n).group(0)))


def _work_copy(project_file: Path, td: Path) -> Path:
    """Copy the project's KiCad files to a temp folder (board, schematics, project, lib tables)."""
    src = project_file.parent
    for f in src.iterdir():
        if f.is_file() and (f.suffix.startswith(".kicad_") or f.name in ("sym-lib-table", "fp-lib-table")):
            shutil.copy2(f, td / f.name)
    return td / project_file.with_suffix(".kicad_pcb").name


def make_zip(gerber_dir: Path, zip_path: Path, include_assembly: bool = False) -> Path:
    exts_skip = set() if include_assembly else {".csv", ".xlsx", ".rpt", ".pdf", ".txt"}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(gerber_dir.iterdir()):
            if f.is_file() and f.suffix.lower() not in exts_skip:
                z.write(f, f.name)
    return zip_path


def generate(project_file: Path, ki: KiCadInstall, settings: Settings, opts: FabOptions,
             log: Log = print) -> FabResult:
    prof = get_profile(opts.profile)
    name = project_file.stem
    build = build_dir_for(project_file)
    gdir = build / "Gerbers"
    build.mkdir(exist_ok=True)
    gdir.mkdir(exist_ok=True)
    res = FabResult(build, gdir)
    pcb = project_file.with_suffix(".kicad_pcb")
    sch = project_file.with_suffix(".kicad_sch")
    if (project_file.parent / f"~{pcb.name}.lck").exists():
        res.warnings.append("The PCB is open in KiCad - unsaved changes are not included. Save in KiCad first.")

    with tempfile.TemporaryDirectory(prefix="kiporter_fab_") as tds:
        td = Path(tds)
        work_pcb = _work_copy(project_file, td) if pcb.exists() else None

        # 1) refill zones + DRC (on the copy)
        if opts.refill_drc and work_pcb:
            log("Refilling zones and running DRC…")
            rpt = build / f"{name}-DRC.rpt"
            r = _run(ki, "pcb", "drc", work_pcb, "-o", rpt, "--format", "report", "--refill-zones", "--save-board",
                     "--severity-error", "--severity-warning", "--units", "mm",
                     *(["--schematic-parity"] if sch.exists() else []))
            if rpt.exists():
                txt = rpt.read_text(encoding="utf-8", errors="replace")
                counts = {k: int(n) for n, k in re.findall(r"Found (\d+) (DRC violations|unconnected pads|"
                                                              r"schematic parity issues|Footprint errors)", txt)}
                res.drc_summary = ", ".join(f"{v} {k}" for k, v in counts.items()) or "no problems found"
                res.files.append(rpt)
                log(f"DRC: {res.drc_summary}  (report: {rpt.name})")
                if any(counts.values()):
                    res.warnings.append(f"DRC reports {res.drc_summary} - check {rpt.name} before ordering.")
            elif r.returncode != 0:
                res.warnings.append(f"DRC failed: {(r.stderr or r.stdout).strip()[:300]}")

        # 2) schematic PDF
        if opts.schematic_pdf and sch.exists():
            log("Plotting schematic PDF…")
            out = build / f"{name}-Schematic.pdf"
            r = _run(ki, "sch", "export", "pdf", sch, "-o", out)
            if out.exists():
                res.files.append(out)
            else:
                res.warnings.append(f"Schematic PDF failed: {(r.stderr or r.stdout).strip()[:300]}")

        # 3) BOM (+ CPL in the Gerbers folder)
        lines = None
        if (opts.bom or opts.placement) and sch.exists():
            try:
                lines = bom.read_bom(project_file, ki, settings)
            except Exception as e:  # noqa: BLE001
                res.warnings.append(f"BOM failed: {e}")
        fmt = bom.get_format(prof.bom_format or "Generic (all fields)")
        if opts.bom and lines is not None:
            log(f"Writing BOM ({fmt.name})…")
            res.files += bom.export(lines, fmt, build, name, opts.boards, settings.bom_include_dnp, opts.file_type)
            miss = bom.missing_fields(lines, fmt)
            if miss:
                res.warnings.append(f"{len(miss)} BOM line(s) lack data {fmt.name} needs (e.g. {miss[0]}).")

        if work_pcb and (opts.gerbers or opts.drill or opts.placement):
            text = work_pcb.read_text(encoding="utf-8", errors="replace")
            layers = BASE_LAYERS[:2] + inner_layers(text) + BASE_LAYERS[2:] + prof.extra_layers

            # 4) Gerbers
            if opts.gerbers:
                log(f"Plotting Gerbers for {prof.name} ({len(layers)} layers)…")
                r = _run(ki, "pcb", "export", "gerbers", work_pcb, "-o", gdir, "--layers", ",".join(layers),
                         "--check-zones", *prof.gerber_flags)
                if r.returncode != 0:
                    res.warnings.append(f"Gerber export failed: {(r.stderr or r.stdout).strip()[:300]}")
            # 5) drill
            if opts.drill:
                log("Writing drill files…")
                r = _run(ki, "pcb", "export", "drill", work_pcb, "-o", str(gdir) + "/", *prof.drill_flags)
                if r.returncode != 0:
                    res.warnings.append(f"Drill export failed: {(r.stderr or r.stdout).strip()[:300]}")
            # 6) placement
            if opts.placement:
                log("Writing placement (pick-and-place) file…")
                try:
                    places = bom.read_positions(work_pcb.with_suffix(".kicad_pro"), ki)
                    dnp = {r for l in (lines or []) if l.dnp for r in l.refs} if not settings.bom_include_dnp else set()
                    cpl_fmt = fmt.cpl or "Generic"
                    out = gdir / f"{name}-CPL-{re.sub(r'[^A-Za-z0-9]+', '_', prof.name).strip('_')}.{opts.file_type}"
                    res.files.append(bom.write_table(bom.cpl_rows(places, cpl_fmt, dnp), out, "CPL"))
                except Exception as e:  # noqa: BLE001
                    res.warnings.append(f"Placement export failed: {e}")
        elif not pcb.exists():
            res.warnings.append("No .kicad_pcb found - Gerbers/drill/placement skipped.")

    res.files += [f for f in gdir.iterdir() if f.is_file() and f not in res.files]
    if opts.make_zip and any(gdir.iterdir()):
        res.zip_file = make_zip(gdir, build / f"{name}-Gerbers-{re.sub(r'[^A-Za-z0-9]+', '_', prof.name).strip('_')}.zip")
        log(f"Upload zip: {res.zip_file.name}")
    return res
