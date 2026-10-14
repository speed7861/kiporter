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
INNER = "In*.Cu"  # placeholder: every inner copper layer the board has
# every layer a custom profile can choose from (in plotting order)
ALL_LAYERS = ["F.Cu", INNER, "B.Cu", "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen", "F.Mask", "B.Mask",
              "Edge.Cuts", "F.Fab", "B.Fab", "F.Courtyard", "B.Courtyard", "User.Drawings", "User.Comments",
              "User.Eco1", "User.Eco2", "Margin", "User.1", "User.2"]
DEFAULT_LAYERS = ["F.Cu", INNER, "B.Cu", "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen", "F.Mask", "B.Mask",
                  "Edge.Cuts"]

# Every option a profile can set. Built-in profiles override a few; custom profiles store all of them.
DEFAULT_OPTIONS: dict = {
    "layers": DEFAULT_LAYERS,
    "board_plot_params": False,  # use the plot settings saved in the board instead of the options below
    "protel_ext": True,  # .gtl/.gbl/... file names (off = KiCad's .gbr names)
    "x2": True,  # Gerber X2 attributes
    "netlist": True,  # netlist attributes (X2 only)
    "subtract_soldermask": False,
    "disable_aperture_macros": False,
    "use_drill_origin": False,
    "precision": 6,
    "exclude_refdes": False,
    "exclude_value": False,
    "drill_format": "excellon",  # excellon | gerber
    "drill_units": "mm",  # mm | in
    "drill_zeros": "decimal",  # decimal | suppressleading | suppresstrailing | keep
    "drill_origin": "absolute",  # absolute | plot
    "drill_oval": "alternate",  # alternate | route
    "drill_separate_th": False,  # separate PTH / NPTH files
    "drill_map": "gerberx2",  # none | pdf | gerberx2 | svg | dxf | ps
    "bom_format": "Generic (all fields)",
    "notes": "",
    "url": "",
}


@dataclass
class FabProfile:
    name: str
    options: dict = field(default_factory=dict)
    custom: bool = False

    def opt(self, key: str):
        return self.options.get(key, DEFAULT_OPTIONS[key])

    @property
    def bom_format(self) -> str | None:
        return self.opt("bom_format") or None

    @property
    def notes(self) -> str:
        return self.opt("notes")

    @property
    def url(self) -> str:
        return self.opt("url")

    def layers(self, pcb_text: str) -> list[str]:
        out: list[str] = []
        for l in self.opt("layers"):
            out += inner_layers(pcb_text) if l == INNER else [l]
        return list(dict.fromkeys(out))

    @property
    def gerber_flags(self) -> list[str]:
        if self.opt("board_plot_params"):
            return ["--board-plot-params"]
        f = []
        if not self.opt("protel_ext"):
            f.append("--no-protel-ext")
        if not self.opt("x2"):
            f.append("--no-x2")
        if not self.opt("netlist"):
            f.append("--no-netlist")
        if self.opt("subtract_soldermask"):
            f.append("--subtract-soldermask")
        if self.opt("disable_aperture_macros"):
            f.append("--disable-aperture-macros")
        if self.opt("use_drill_origin"):
            f.append("--use-drill-file-origin")
        if self.opt("exclude_refdes"):
            f.append("--exclude-refdes")
        if self.opt("exclude_value"):
            f.append("--exclude-value")
        return f + ["--precision", str(self.opt("precision"))]

    @property
    def drill_flags(self) -> list[str]:
        f = ["--format", self.opt("drill_format"), "--drill-origin", self.opt("drill_origin")]
        if self.opt("drill_format") == "excellon":
            f += ["--excellon-units", self.opt("drill_units"), "--excellon-zeros-format", self.opt("drill_zeros"),
                  "--excellon-oval-format", self.opt("drill_oval")]
            if self.opt("drill_separate_th"):
                f.append("--excellon-separate-th")
        if self.opt("drill_map") != "none":
            f += ["--generate-map", "--map-format", self.opt("drill_map")]
        return f


PROFILES: list[FabProfile] = [
    FabProfile("JLCPCB", {"x2": False, "subtract_soldermask": True, "bom_format": "JLCPCB",
                          "notes": "Upload the Gerber zip on jlcpcb.com. For assembly, upload the BOM and the CPL file "
                                   "from the Gerbers folder in the assembly step and check part rotations in the "
                                   "preview.", "url": "https://jlcpcb.com/quote"}),
    FabProfile("PCBWay", {"x2": False, "bom_format": "PCBWay",
                          "notes": "Upload the Gerber zip; for assembly add the BOM (.xlsx preferred) and the "
                                   "placement file.", "url": "https://www.pcbway.com/orderonline.aspx"}),
    FabProfile("OSH Park", {"drill_units": "in", "drill_map": "none", "bom_format": None,
                            "notes": "Upload the Gerber zip (OSH Park also accepts the .kicad_pcb directly). "
                                     "No assembly service.", "url": "https://oshpark.com/"}),
    FabProfile("ALLPCB", {"x2": False, "bom_format": "ALLPCB", "url": "https://www.allpcb.com/"}),
    FabProfile("Seeed Fusion", {"x2": False, "drill_map": "none", "bom_format": "Seeed Fusion",
                                "url": "https://www.seeedstudio.com/fusion_pcb.html"}),
    FabProfile("NextPCB", {"x2": False, "bom_format": "NextPCB", "url": "https://www.nextpcb.com/"}),
    FabProfile("Eurocircuits", {"bom_format": "Eurocircuits",
                                "notes": "Eurocircuits reads Gerber X2 attributes, so X2 is kept on.",
                                "url": "https://www.eurocircuits.com/"}),
    FabProfile("Aisler", {"drill_map": "none", "bom_format": "Aisler",
                          "notes": "Aisler also accepts the KiCad project directly.", "url": "https://aisler.net/"}),
    FabProfile("MacroFab", {"drill_map": "none", "bom_format": "MacroFab", "url": "https://macrofab.com/"}),
    FabProfile("Generic (KiCad defaults)", {"drill_map": "pdf"}),
    FabProfile("Use the board's own plot settings", {
        "board_plot_params": True, "drill_map": "pdf",
        "notes": "Plots exactly what is set in KiCad's PCB Editor → File → Plot dialog (layers and "
                 "options saved in the board)."}),
]
PROFILE_NAMES = [p.name for p in PROFILES]


def custom_profiles(settings: Settings | None) -> list[FabProfile]:
    out = []
    for d in (settings.custom_fab_profiles if settings else []) or []:
        if d.get("name"):
            out.append(FabProfile(d["name"], {k: v for k, v in d.items() if k in DEFAULT_OPTIONS}, custom=True))
    return out


def all_profiles(settings: Settings | None) -> list[FabProfile]:
    custom = custom_profiles(settings)
    names = {c.name for c in custom}
    return [p for p in PROFILES if p.name not in names] + custom


def get_profile(name: str, settings: Settings | None = None) -> FabProfile:
    return next((p for p in all_profiles(settings) if p.name == name), PROFILES[0])


def save_custom_profile(settings: Settings, name: str, options: dict) -> None:
    rest = [d for d in settings.custom_fab_profiles if d.get("name") != name]
    settings.custom_fab_profiles = rest + [{"name": name, **{k: options.get(k, v) for k, v in DEFAULT_OPTIONS.items()}}]
    settings.save()


def delete_custom_profile(settings: Settings, name: str) -> None:
    settings.custom_fab_profiles = [d for d in settings.custom_fab_profiles if d.get("name") != name]
    settings.save()


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


def zip_path_for(project_file: Path) -> Path:
    """The production zip: '<Project> Build.zip', next to the project file."""
    return project_file.parent / f"{build_dir_for(project_file).name}.zip"


def make_zip(build_dir: Path, zip_path: Path) -> Path:
    """Zip the whole Build folder (schematic PDF, BOM, reports and the Gerbers sub-folder) into one file,
    keeping the folder layout. Written to a temporary name first, so a failed run never leaves half a zip."""
    tmp = zip_path.with_name(zip_path.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(build_dir.rglob("*")):
            if f.is_file() and f.suffix.lower() != ".zip":
                z.write(f, (Path(build_dir.name) / f.relative_to(build_dir)).as_posix())
    tmp.replace(zip_path)
    return zip_path


def generate(project_file: Path, ki: KiCadInstall, settings: Settings, opts: FabOptions,
             log: Log = print) -> FabResult:
    prof = get_profile(opts.profile, settings)
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
            layers = prof.layers(text)

            # 4) Gerbers
            if opts.gerbers:
                log(f"Plotting Gerbers for {prof.name} ({len(layers)} layers)…")
                layer_args = [] if prof.opt("board_plot_params") else ["--layers", ",".join(layers)]
                r = _run(ki, "pcb", "export", "gerbers", work_pcb, "-o", gdir, *layer_args, "--check-zones",
                         *prof.gerber_flags)
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
        res.zip_file = make_zip(build, zip_path_for(project_file))
        log(f"Production zip: {res.zip_file}")
    return res
