"""Turn an LCSC part or a vendor CAD zip into a StagedPart (files in a temp dir)."""
from __future__ import annotations

import subprocess
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .kicad_env import NO_WINDOW, KiCadInstall
from .library import MODEL_EXTS, StagedPart


class ImportError_(Exception):
    pass


def _tmpdir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


# ---------------------------------------------------------------- EasyEDA / LCSC
_api_cache: list = []


def _easyeda_api(cls):
    """One EasyEDA client for the whole session (building it loads the certificate store each time)."""
    if not _api_cache:
        _api_cache.append(cls())
    return _api_cache[0]


def stage_lcsc(lcsc_id: str, data: dict[str, str] | None = None, cad: dict | None = None,
               models: bool = True) -> StagedPart:
    try:
        from easyeda2kicad.easyeda.easyeda_api import EasyedaApi
        from easyeda2kicad.easyeda.easyeda_importer import (
            Easyeda3dModelImporter, EasyedaFootprintImporter, EasyedaSymbolImporter)
        from easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad
        from easyeda2kicad.kicad.export_kicad_footprint import ExporterFootprintKicad
        from easyeda2kicad.kicad.export_kicad_symbol import ExporterSymbolKicad
    except ImportError as e:
        raise ImportError_("easyeda2kicad is not installed - run the installer again") from e

    lcsc_id = lcsc_id.strip().upper()
    api = _easyeda_api(EasyedaApi)
    cad = cad or api.get_cad_data_of_component(lcsc_id=lcsc_id)
    if not cad:
        raise ImportError_(f"EasyEDA has no CAD data for {lcsc_id}")

    tmp = _tmpdir(f"lcsc_{lcsc_id}_")
    out = tmp / "staging"
    part = StagedPart(data=dict(data or {}), source=f"EasyEDA {lcsc_id}")
    part.data.setdefault("lcsc", lcsc_id)

    def fetch_model() -> list[Path]:
        if not models:
            return []
        try:
            model = Exporter3dModelKicad(model_3d=Easyeda3dModelImporter(
                easyeda_cp_cad_data=cad, download_raw_3d_model=True, api=api).output)
            if model.output and model.export(output_dir=f"{out}.3dshapes", overwrite=True):
                md = Path(f"{out}.3dshapes")
                # the footprint references the .wrl (offsets are baked into it); keep STEP too
                return sorted(md.glob("*.wrl")) + sorted(md.glob("*.step"))
        except Exception:  # 3D models are optional; never fail the import for them
            pass
        return []

    # the 3D model is a separate download: fetch it while the symbol and footprint convert
    with ThreadPoolExecutor(max_workers=1) as pool:
        model_job = pool.submit(fetch_model)

        sym = EasyedaSymbolImporter(easyeda_cp_cad_data=cad).get_symbol()
        sym_path = f"{out}.kicad_sym"
        ExporterSymbolKicad(symbol=sym, lib_path=sym_path).save_to_lib(
            lib_path=sym_path, footprint_lib_name="staging", overwrite=True)
        part.symbol_files.append(Path(sym_path))

        fp = EasyedaFootprintImporter(easyeda_cp_cad_data=cad).get_footprint()
        fp_dir = Path(f"{out}.pretty")
        fp_dir.mkdir(parents=True, exist_ok=True)
        fp_file = fp_dir / f"{fp.info.name}.kicad_mod"
        ExporterFootprintKicad(footprint=fp).export(
            footprint_full_path=str(fp_file), model_3d_path=Path(f"{out}.3dshapes").as_posix())
        part.footprint_files.append(fp_file)

        part.model_files += model_job.result()
    return part


# ---------------------------------------------------------------- vendor zip files
def _header(p: Path, n: int = 200) -> str:
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read(n)
    except OSError:
        return ""


def _run_cli(ki: KiCadInstall, args: list[str]) -> bool:
    if not ki.cli:
        return False
    try:
        r = subprocess.run(ki.cli_cmd(*args), capture_output=True, text=True,
                           timeout=120, creationflags=NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def stage_zip(zip_path: Path, ki: KiCadInstall, data: dict[str, str] | None = None) -> StagedPart:
    """Extract KiCad data from a SnapEDA / Ultra Librarian / SamacSys (Mouser) zip."""
    if not zipfile.is_zipfile(zip_path):
        raise ImportError_(f"{zip_path.name} is not a zip file")
    tmp = _tmpdir("cadzip_")
    ex = tmp / "x"
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (ex / info.filename).resolve()
            if not str(target).startswith(str(ex.resolve())):
                continue  # skip path-traversal entries
            if info.is_dir():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())

    files = [p for p in ex.rglob("*") if p.is_file()]
    part = StagedPart(data=dict(data or {}), source=zip_path.name)

    syms = [p for p in files if p.suffix.lower() == ".kicad_sym"]
    if not syms:
        legacy = [p for p in files if p.suffix.lower() == ".lib" and "EESchema-LIBRARY" in _header(p)]
        for i, lib in enumerate(legacy):
            out = tmp / f"converted_{i}.kicad_sym"
            if _run_cli(ki, ["sym", "upgrade", str(lib), "-o", str(out)]) and out.exists():
                syms.append(out)
    part.symbol_files = syms

    fps = [p for p in files if p.suffix.lower() == ".kicad_mod"]
    if not fps:
        legacy_mods = [p for p in files if p.suffix.lower() == ".mod" and "PCBNEW-LibModule" in _header(p)]
        for i, mod in enumerate(legacy_mods):
            out = tmp / f"converted_{i}.pretty"
            if _run_cli(ki, ["fp", "upgrade", str(mod), "-o", str(out)]):
                fps += sorted(out.glob("*.kicad_mod"))
    seen: set[str] = set()
    for f in fps:
        if f.name.lower() not in seen:
            seen.add(f.name.lower())
            part.footprint_files.append(f)

    models = [p for p in files if p.suffix.lower() in MODEL_EXTS]
    seen.clear()
    for m in models:
        if m.name.lower() not in seen:
            seen.add(m.name.lower())
            part.model_files.append(m)

    if not part.symbol_files and not part.footprint_files:
        raise ImportError_(
            f"No KiCad symbol or footprint found in {zip_path.name}. "
            "Make sure you downloaded the KiCad format (or 'All formats').")
    return part
