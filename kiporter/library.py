"""Target KiCad libraries (project or global) and installing parts into them."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import fields, sexpr
from .config import Settings
from .kicad_env import (KiCadInstall, LibEntry, all_lib_entries, ensure_lib_table_entry,
                        is_stock_kicad_path, kicad_env_vars)

MODEL_EXTS = (".step", ".stp", ".wrl", ".wrz")
EMPTY_SYM_LIB_VERSION = "20241209"


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "part"


def _uri_dir(uri: str) -> str:
    return uri.replace("\\", "/").rsplit("/", 1)[0]


@dataclass
class LibraryTarget:
    kind: str  # "project" | "global"
    name: str  # symbol library nickname
    sym_file: Path
    sym_uri: str
    fp_name: str  # footprint library nickname
    fp_dir: Path
    fp_uri: str
    model_dir: Path
    model_uri_prefix: str
    table_dir: Path  # folder holding the sym-lib-table / fp-lib-table to register in
    existing: bool = False  # already listed in a lib table

    def model_uri(self, filename: str) -> str:
        return f"{self.model_uri_prefix}/{filename}"

    @property
    def base_dir(self) -> Path:
        return self.sym_file.parent

    def label(self) -> str:
        new = "" if self.existing else "  (new)"
        return f"{self.name}{new}   —  {self.sym_file.parent}"

    def ensure(self) -> list[str]:
        """Create library files/folders and register them in the lib tables."""
        msgs: list[str] = []
        self.sym_file.parent.mkdir(parents=True, exist_ok=True)
        self.fp_dir.mkdir(parents=True, exist_ok=True)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        if not self.sym_file.exists():
            self.sym_file.write_text(
                f'(kicad_symbol_lib\n\t(version {EMPTY_SYM_LIB_VERSION})\n'
                f'\t(generator "kiporter")\n)\n', encoding="utf-8")
            msgs.append(f"Created symbol library {self.sym_file}")
        if not self.existing:
            self.table_dir.mkdir(parents=True, exist_ok=True)
            descr = "Parts imported by Kiporter"
            if ensure_lib_table_entry(self.table_dir / "sym-lib-table", "sym", self.name, self.sym_uri, descr):
                msgs.append(f"Registered symbol library '{self.name}' in {self.table_dir / 'sym-lib-table'}")
            if ensure_lib_table_entry(self.table_dir / "fp-lib-table", "fp", self.fp_name, self.fp_uri, descr):
                msgs.append(f"Registered footprint library '{self.fp_name}' in {self.table_dir / 'fp-lib-table'}")
        return msgs


def _make(kind: str, name: str, base: Path, uri_prefix: str, table_dir: Path) -> LibraryTarget:
    return LibraryTarget(
        kind=kind, name=name,
        sym_file=base / f"{name}.kicad_sym", sym_uri=f"{uri_prefix}/{name}.kicad_sym",
        fp_name=name, fp_dir=base / f"{name}.pretty", fp_uri=f"{uri_prefix}/{name}.pretty",
        model_dir=base / f"{name}.3dshapes", model_uri_prefix=f"{uri_prefix}/{name}.3dshapes",
        table_dir=table_dir,
    )


def project_target(settings: Settings, project_dir: Path, name: str | None = None) -> LibraryTarget:
    sub = settings.project_lib_subdir.strip("/\\")
    return _make("project", name or settings.project_lib_name, project_dir / sub if sub else project_dir,
                 "${KIPRJMOD}" + (f"/{sub}" if sub else ""), project_dir)


def global_target(settings: Settings, ki: KiCadInstall, name: str | None = None,
                  folder: Path | None = None) -> LibraryTarget:
    base = folder or Path(settings.global_lib_dir)
    return _make("global", name or settings.global_lib_name, base, base.as_posix(), ki.config_dir)


def target_from_entry(entry: LibEntry, ki: KiCadInstall, project_dir: Path | None,
                      fp_entries: dict[str, LibEntry]) -> LibraryTarget | None:
    env = kicad_env_vars(ki, project_dir)
    sym_file = entry.path(env)
    if not sym_file or sym_file.suffix != ".kicad_sym" or is_stock_kicad_path(sym_file, ki):
        return None
    if not sym_file.parent.exists():
        return None
    fp = fp_entries.get(entry.name)
    fp_dir = fp.path(env) if fp else None
    if fp and fp_dir:
        fp_uri, fp_name = fp.uri, fp.name
    else:
        fp_name = entry.name
        fp_uri = f"{_uri_dir(entry.uri)}/{sym_file.stem}.pretty"
        fp_dir = sym_file.parent / f"{sym_file.stem}.pretty"
    model_prefix = fp_uri[:-len(".pretty")] + ".3dshapes" if fp_uri.endswith(".pretty") \
        else f"{_uri_dir(fp_uri)}/{fp_name}.3dshapes"
    model_dir = fp_dir.with_suffix(".3dshapes") if fp_dir.suffix == ".pretty" else fp_dir.parent / f"{fp_name}.3dshapes"
    uses_prj = "KIPRJMOD" in entry.uri
    kind = "project" if entry.scope == "project" or uses_prj else "global"
    t = LibraryTarget(kind=kind, name=entry.name, sym_file=sym_file, sym_uri=entry.uri,
                      fp_name=fp_name, fp_dir=fp_dir, fp_uri=fp_uri, model_dir=model_dir,
                      model_uri_prefix=model_prefix, table_dir=entry.table.parent, existing=True)
    if not fp:  # symbol lib registered without a footprint lib: register the footprint lib on first use
        t.existing = False
    return t


def target_from_files(sym_file: Path, ki: KiCadInstall) -> LibraryTarget:
    """A library given as files (not necessarily registered): NAME.kicad_sym + NAME.pretty + NAME.3dshapes."""
    base, name = sym_file.parent, sym_file.stem
    t = _make("global", name, base, base.as_posix(), ki.config_dir)
    t.existing = True  # never register a library just because a tool looked at it
    return t


def list_targets(settings: Settings, ki: KiCadInstall, project_dir: Path | None, kind: str) -> list[LibraryTarget]:
    """Writable libraries for 'project' or 'global' imports; the default library is always first."""
    if kind == "project" and not project_dir:
        return []
    sym_entries = all_lib_entries(ki, project_dir, "sym")
    fp_entries = {e.name: e for e in all_lib_entries(ki, project_dir, "fp")}
    default = project_target(settings, project_dir) if kind == "project" else global_target(settings, ki)
    out: list[LibraryTarget] = []
    for e in sym_entries:
        if e.type.lower() != "kicad":
            continue
        t = target_from_entry(e, ki, project_dir, fp_entries)
        if t and t.kind == kind:
            out.append(t)
    found_default = next((t for t in out if t.name == default.name), None)
    if found_default:
        out.remove(found_default)
        out.insert(0, found_default)
    else:
        out.insert(0, default)
    return out


# ---------------------------------------------------------------- staged parts
@dataclass
class StagedPart:
    """Files for one part, extracted/converted into a temp folder, ready to install."""
    symbol_files: list[Path] = field(default_factory=list)
    footprint_files: list[Path] = field(default_factory=list)
    model_files: list[Path] = field(default_factory=list)
    data: dict[str, str] = field(default_factory=dict)  # canonical field data (fields.FIELD_ORDER keys)
    source: str = ""


@dataclass
class InstallResult:
    symbols: list[str] = field(default_factory=list)
    footprints: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def model_key(path_str: str) -> str:
    return re.split(r"[\\/]", path_str)[-1].lower()


def pick_model(fp_name: str, ref: str | None, models: list[Path]) -> Path | None:
    if not models:
        return None
    by_name = {m.name.lower(): m for m in models}
    if ref:
        k = model_key(ref)
        if k in by_name:
            return by_name[k]
        stem = k.rsplit(".", 1)[0]
        for m in models:
            if m.stem.lower() == stem:
                return m
    for m in models:
        if m.stem.lower() == fp_name.lower():
            return m
    steps = [m for m in models if m.suffix.lower() in (".step", ".stp")]
    return (steps or models)[0]


def new_model_node(uri: str) -> list:
    return ["model", sexpr.quote(uri), ["offset", ["xyz", "0", "0", "0"]],
            ["scale", ["xyz", "1", "1", "1"]], ["rotate", ["xyz", "0", "0", "0"]]]


def install(target: LibraryTarget, part: StagedPart, overwrite: bool = False,
            want_symbol: bool = True, want_footprint: bool = True, want_3d: bool = True,
            settings: Settings | None = None) -> InstallResult:
    settings = settings or Settings()
    names = fields.field_names(settings.field_names)
    res = InstallResult()
    res.messages += target.ensure()

    # ---- 3D models
    installed_models: list[Path] = []
    if want_3d:
        for m in part.model_files:
            dest = target.model_dir / safe_name(m.name)
            if dest.exists() and not overwrite:
                res.skipped.append(f"3D model {dest.name} (already exists)")
            else:
                shutil.copy2(m, dest)
                res.models.append(dest.name)
            installed_models.append(dest)

    # ---- footprints
    fp_names: list[str] = []
    for f in part.footprint_files:
        node = sexpr.parse(f.read_text(encoding="utf-8", errors="replace"))
        # KiCad identifies footprints by file name; keep the internal name in sync
        fp_name = safe_name(f.stem)
        if len(node) > 1 and isinstance(node[1], str):
            node[1] = sexpr.quote(fp_name)
        fp_names.append(fp_name)
        if not want_footprint:
            continue
        model_nodes = sexpr.children(node, "model")
        if installed_models:
            if model_nodes:
                for mn in model_nodes:
                    m = pick_model(fp_name, sexpr.unquote(mn[1]), installed_models)
                    if m:
                        mn[1] = sexpr.quote(target.model_uri(m.name))
            else:
                m = pick_model(fp_name, None, installed_models)
                node.append(new_model_node(target.model_uri(m.name)))
        dest = target.fp_dir / f"{fp_name}.kicad_mod"
        if dest.exists() and not overwrite:
            res.skipped.append(f"footprint {fp_name} (already exists)")
            continue
        dest.write_text(sexpr.dump(node) + "\n", encoding="utf-8")
        res.footprints.append(f"{target.fp_name}:{fp_name}")

    # ---- symbols
    if want_symbol and part.symbol_files:
        lib = sexpr.parse(target.sym_file.read_text(encoding="utf-8"))
        ver_node = sexpr.child(lib, "version")
        existing = {sexpr.unquote(s[1]): s for s in sexpr.children(lib, "symbol")}
        changed = False
        for sf in part.symbol_files:
            src = sexpr.parse(sf.read_text(encoding="utf-8", errors="replace"))
            src_ver = sexpr.child(src, "version")
            if src_ver and ver_node and src_ver[1].isdigit() and ver_node[1].isdigit() \
                    and int(src_ver[1]) > int(ver_node[1]):
                ver_node[1] = src_ver[1]
            for sym in sexpr.children(src, "symbol"):
                name = sexpr.unquote(sym[1]).split(":")[-1]
                sym[1] = sexpr.quote(name)
                if not sexpr.child(sym, "extends"):
                    fields.normalize_symbol(sym, names, settings.drop_junk_fields)
                    if fp_names:  # point the symbol at the imported footprint
                        cur = (fields.get_property(sym, "Footprint") or "").split(":")[-1]
                        chosen = cur if cur in fp_names else fp_names[0]
                        fields.set_property(sym, "Footprint", f"{target.fp_name}:{chosen}")
                    data = dict(part.data)
                    if not data.get("package") and fp_names:
                        data["package"] = fp_names[0]
                    fields.apply_part_data(sym, data, names)
                if name in existing:
                    if not overwrite:
                        res.skipped.append(f"symbol {name} (already exists)")
                        continue
                    lib[lib.index(existing[name])] = sym
                else:
                    lib.append(sym)
                existing[name] = sym
                res.symbols.append(f"{target.name}:{name}")
                changed = True
        if changed:
            target.sym_file.write_text(sexpr.dump(lib) + "\n", encoding="utf-8")
    return res
