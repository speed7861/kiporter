"""Library maintenance tools.

* clean-up / auto-link   - scan a library, link symbols->footprints->3D models, fix fields
* merge                  - combine several libraries into one (with 3D models)
* remap project          - point a project's schematic/PCB at renamed/merged libraries
* localize project       - copy every part a project uses into its project library
* BOM check              - stock / lead time / price for every BOM line
* backup                 - zip a library (symbols + footprints + 3D models)
"""
from __future__ import annotations

import copy
import csv
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import fields, sexpr
from .config import Settings
from .kicad_env import (NO_WINDOW, KiCadInstall, all_lib_entries, is_stock_kicad_path, kicad_env_vars,
                        remove_lib_table_entry, resolve)
from .library import (MODEL_EXTS, LibraryTarget, model_key, new_model_node, safe_name)

Log = Callable[[str], None]


def _nolog(_: str) -> None:
    pass


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _read_node(p: Path) -> list:
    return sexpr.parse(p.read_text(encoding="utf-8", errors="replace"))


def _write_node(p: Path, node: list) -> None:
    p.write_text(sexpr.dump(node) + "\n", encoding="utf-8")


def backup_files(files: list[Path], backup_root: Path) -> Path:
    dest = backup_root / _ts()
    for f in files:
        if f.is_file():
            d = dest / f.name
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, d)
        elif f.is_dir():
            shutil.copytree(f, dest / f.name, dirs_exist_ok=True)
    return dest


def validate_sym_lib(ki: KiCadInstall, sym_file: Path) -> str | None:
    """Parse a symbol library with kicad-cli; returns an error message or None."""
    if not ki.kicad_cli or not sym_file.exists():
        return None
    with tempfile.TemporaryDirectory() as td:
        try:
            r = subprocess.run([str(ki.kicad_cli), "sym", "upgrade", "--force", str(sym_file), "-o",
                                str(Path(td) / "check.kicad_sym")], capture_output=True, text=True,
                               timeout=180, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            return str(e)
        return None if r.returncode == 0 else (r.stderr or r.stdout or "kicad-cli could not read the library")


# ================================================================ footprint lookup across libraries
class FootprintIndex:
    """Resolves 'Lib:Footprint' ids using the global + project footprint tables."""

    def __init__(self, ki: KiCadInstall, project_dir: Path | None):
        self.env = kicad_env_vars(ki, project_dir)
        self.libs: dict[str, Path] = {}
        for e in all_lib_entries(ki, project_dir, "fp"):
            p = e.path(self.env)
            if p:
                self.libs[e.name] = p
        self._cache: dict[str, set[str]] = {}

    def names(self, lib: str) -> set[str]:
        if lib not in self._cache:
            p = self.libs.get(lib)
            try:
                self._cache[lib] = {f.stem for f in p.glob("*.kicad_mod")} if p and p.is_dir() else set()
            except OSError:
                self._cache[lib] = set()
        return self._cache[lib]

    def exists(self, fpid: str) -> bool:
        lib, _, name = fpid.partition(":")
        return bool(name) and name in self.names(lib)

    def file(self, fpid: str) -> Path | None:
        lib, _, name = fpid.partition(":")
        p = self.libs.get(lib)
        f = p / f"{name}.kicad_mod" if p else None
        return f if f and f.exists() else None

    def find_anywhere(self, name: str, skip_stock: bool = False) -> list[str]:
        return [f"{lib}:{name}" for lib in self.libs if name in self.names(lib)]


def _model_search_dirs(target: LibraryTarget) -> list[Path]:
    dirs = [target.model_dir]
    try:
        dirs += [d for d in target.base_dir.glob("*.3dshapes") if d.is_dir() and d != target.model_dir]
    except OSError:
        pass
    return [d for d in dirs if d.exists()]


def _find_model_file(dirs: list[Path], ref_or_name: str) -> Path | None:
    key = model_key(ref_or_name)
    stem = key.rsplit(".", 1)[0] if "." in key else key
    best: Path | None = None
    for d in dirs:
        try:
            files = [f for f in d.iterdir() if f.suffix.lower() in MODEL_EXTS]
        except OSError:
            continue
        for f in files:
            if f.name.lower() == key:
                return f
            if f.stem.lower() == stem or _norm(f.stem) == _norm(stem):
                if best is None or f.suffix.lower() in (".step", ".stp"):
                    best = f
    return best


class StockModels:
    """Index of KiCad's bundled 3D models, used to repair legacy (${KISYS3DMOD}, KiCad 5 folder) paths."""

    def __init__(self, ki: KiCadInstall):
        self.var = f"${{KICAD{ki.major}_3DMODEL_DIR}}"
        self.by_stem: dict[str, str] = {}
        root = ki.root / "share" / "kicad" / "3dmodels" if ki.root else None
        if root and root.is_dir():
            for d in root.glob("*.3dshapes"):
                for f in d.iterdir():
                    if f.suffix.lower() not in MODEL_EXTS:
                        continue
                    key = f.stem.lower()
                    # prefer STEP over WRL
                    if key not in self.by_stem or f.suffix.lower() in (".step", ".stp"):
                        self.by_stem[key] = f"{self.var}/{d.name}/{f.name}"

    def find(self, ref_or_name: str) -> str | None:
        k = model_key(ref_or_name)
        return self.by_stem.get(k.rsplit(".", 1)[0] if "." in k else k)


# ================================================================ clean-up / auto-link
@dataclass
class Issue:
    category: str  # Fields | Footprint link | 3D model | Unused | Info
    item: str
    detail: str
    fix: str = ""
    action: tuple | None = None
    selected: bool = True


def _match_footprint(keys: list[str], fp_names: list[str]) -> tuple[str | None, bool]:
    """Best footprint for the given keys (package, symbol name, value). Returns (name, confident)."""
    scored: list[tuple[int, str]] = []
    for fp in fp_names:
        n = _norm(fp)
        s = 0
        for i, k in enumerate(keys):
            kn = _norm(k)
            if len(kn) < 3:
                continue
            if n == kn:
                s = max(s, 100 - i)
            elif len(kn) >= 4 and (n.startswith(kn) or kn in n):
                s = max(s, 70 - i - min(len(n) - len(kn), 20) // 4)
            elif len(n) >= 4 and n in kn:
                s = max(s, 55 - i)
        if s:
            scored.append((s, fp))
    if not scored:
        return None, False
    scored.sort(key=lambda t: (-t[0], len(t[1])))
    top = scored[0]
    confident = top[0] >= 90 or (top[0] >= 50 and (len(scored) == 1 or scored[1][0] < top[0]))
    return top[1], confident


def scan_library(target: LibraryTarget, ki: KiCadInstall, project_dir: Path | None, settings: Settings,
                 lookup_online: bool = False, log: Log = _nolog) -> list[Issue]:
    issues: list[Issue] = []
    names = fields.field_names(settings.field_names)
    env = kicad_env_vars(ki, project_dir)
    fpx = FootprintIndex(ki, project_dir)
    fp_files = sorted(target.fp_dir.glob("*.kicad_mod")) if target.fp_dir.is_dir() else []
    own_fps = [f.stem for f in fp_files]
    used_fps: set[str] = set()

    # ---------------- symbols
    lib = _read_node(target.sym_file) if target.sym_file.exists() else ["kicad_symbol_lib"]
    syms = sexpr.children(lib, "symbol")
    seen_lower: dict[str, str] = {}
    log(f"Scanning {len(syms)} symbols and {len(fp_files)} footprints in '{target.name}'…")
    for sym in syms:
        name = sexpr.unquote(sym[1])
        if name.lower() in seen_lower:
            issues.append(Issue("Info", name, f"duplicate of symbol '{seen_lower[name.lower()]}' (differs only in case)"))
        seen_lower[name.lower()] = name
        is_alias = sexpr.child(sym, "extends") is not None

        # fields
        probe = copy.deepcopy(sym)
        changes = fields.normalize_symbol(probe, names, settings.drop_junk_fields)
        if changes:
            issues.append(Issue("Fields", name, "; ".join(changes), "normalise field names",
                                ("normalize", name)))

        # footprint link
        fp_val = fields.get_property(sym, "Footprint")
        if is_alias and not fp_val:
            continue
        fp_val = (fp_val or "").strip()
        pkg = fields.get_property(sym, names["package"]) or ""
        value = fields.get_property(sym, "Value") or ""
        if not fp_val:
            cand, sure = _match_footprint([pkg, name, value], own_fps)
            if cand:
                issues.append(Issue("Footprint link", name, "no footprint assigned",
                                    f"link → {target.fp_name}:{cand}",
                                    ("set_fp", name, f"{target.fp_name}:{cand}"), selected=sure))
                used_fps.add(cand)
            else:
                issues.append(Issue("Footprint link", name, "no footprint assigned and no matching footprint found"))
            continue
        lib_part, _, fp_name = fp_val.rpartition(":")
        if lib_part and fpx.exists(fp_val):
            if lib_part == target.fp_name:
                used_fps.add(fp_name)
            continue
        # broken or missing library prefix
        if fp_name in own_fps:
            issues.append(Issue("Footprint link", name, f"footprint '{fp_val}' not found",
                                f"relink → {target.fp_name}:{fp_name}",
                                ("set_fp", name, f"{target.fp_name}:{fp_name}")))
            used_fps.add(fp_name)
            continue
        elsewhere = fpx.find_anywhere(fp_name)
        if elsewhere:
            issues.append(Issue("Footprint link", name, f"footprint '{fp_val}' not found",
                                f"relink → {elsewhere[0]}", ("set_fp", name, elsewhere[0]),
                                selected=len(elsewhere) == 1))
            continue
        cand, sure = _match_footprint([fp_name, pkg, name], own_fps)
        if cand:
            issues.append(Issue("Footprint link", name, f"footprint '{fp_val}' not found",
                                f"relink → {target.fp_name}:{cand} (closest match)",
                                ("set_fp", name, f"{target.fp_name}:{cand}"), selected=False))
        else:
            issues.append(Issue("Footprint link", name, f"footprint '{fp_val}' not found in any library"))

    if lookup_online:
        from .providers import find_lcsc_equivalent
        for sym in syms:
            if sexpr.child(sym, "extends") is not None:
                continue
            name = sexpr.unquote(sym[1])
            mpn = fields.get_property(sym, names["mpn"]) or ""
            missing = [k for k in ("manufacturer", "datasheet", "lcsc")
                       if not (fields.get_property(sym, names[k]) or "").strip(" ~")]
            if mpn and missing:
                r = find_lcsc_equivalent(mpn)
                if r:
                    data = {k: v for k, v in r.part_data().items() if k in missing}
                    if data:
                        issues.append(Issue("Fields", name, f"missing {', '.join(missing)}",
                                            "fill from LCSC: " + ", ".join(f"{names[k]}={v[:40]}" for k, v in data.items()),
                                            ("fill", name, data)))

    # ---------------- footprints / 3D models
    search_dirs = _model_search_dirs(target)
    stock = StockModels(ki)
    used_models: set[str] = set()
    for f in fp_files:
        try:
            node = _read_node(f)
        except ValueError as e:
            issues.append(Issue("Info", f.stem, f"footprint cannot be parsed: {e}"))
            continue
        models = sexpr.children(node, "model")
        if not models:
            m = _find_model_file(search_dirs, f.stem)
            stock_uri = None if m else stock.find(f.stem)
            if m:
                issues.append(Issue("3D model", f.stem, "footprint has no 3D model",
                                    f"add {m.name}", ("add_model", f.stem, str(m))))
                used_models.add(m.stem.lower())
            elif stock_uri:
                issues.append(Issue("3D model", f.stem, "footprint has no 3D model",
                                    f"add KiCad stock model {stock_uri}", ("add_model_uri", f.stem, stock_uri)))
            else:
                issues.append(Issue("3D model", f.stem, "footprint has no 3D model (none found)"))
            continue
        for idx, mn in enumerate(models):
            ref = sexpr.unquote(mn[1])
            if ref.startswith("kicad-embed://"):
                continue
            p = resolve(ref, env)
            if p and p.exists():
                used_models.add(p.stem.lower())
                continue
            m = _find_model_file(search_dirs, ref)
            stock_uri = None if m else stock.find(ref)
            if m:
                issues.append(Issue("3D model", f.stem, f"3D model path broken: {ref}",
                                    f"relink → {m.name}", ("set_model", f.stem, idx, str(m))))
                used_models.add(m.stem.lower())
            elif stock_uri and stock_uri != ref:
                issues.append(Issue("3D model", f.stem, f"3D model path broken: {ref}",
                                    f"relink → KiCad stock model {stock_uri}",
                                    ("set_model_uri", f.stem, idx, stock_uri)))
            else:
                issues.append(Issue("3D model", f.stem, f"3D model path broken and file not found: {ref}"))

    # ---------------- unused items
    for fp in own_fps:
        if fp not in used_fps:
            issues.append(Issue("Unused", fp, "footprint not used by any symbol in this library "
                                "(may be used by other libraries/projects)"))
    if target.model_dir.is_dir():
        for m in target.model_dir.iterdir():
            if m.suffix.lower() in MODEL_EXTS and m.stem.lower() not in used_models:
                issues.append(Issue("Unused", m.name, "3D model not referenced by any footprint",
                                    "delete file", ("delete_model", str(m)), selected=False))
    log(f"Scan finished: {len(issues)} findings, {sum(1 for i in issues if i.action)} with automatic fixes.")
    return issues


def apply_fixes(target: LibraryTarget, issues: list[Issue], ki: KiCadInstall, settings: Settings,
                log: Log = _nolog) -> str:
    chosen = [i for i in issues if i.action and i.selected]
    if not chosen:
        return "Nothing selected."
    names = fields.field_names(settings.field_names)
    fp_actions = ("add_model", "set_model", "add_model_uri", "set_model_uri")
    to_backup = [target.sym_file] + [target.fp_dir / f"{i.action[1]}.kicad_mod" for i in chosen
                                     if i.action[0] in fp_actions]
    bdir = backup_files(to_backup, target.base_dir / ".partimporter-backup")
    log(f"Backup written to {bdir}")

    sym_actions = [i.action for i in chosen if i.action[0] in ("normalize", "set_fp", "fill")]
    if sym_actions:
        lib = _read_node(target.sym_file)
        by_name = {sexpr.unquote(s[1]): s for s in sexpr.children(lib, "symbol")}
        for act in sym_actions:
            sym = by_name.get(act[1])
            if sym is None:
                continue
            if act[0] == "normalize":
                fields.normalize_symbol(sym, names, settings.drop_junk_fields)
            elif act[0] == "set_fp":
                fields.set_property(sym, "Footprint", act[2])
            elif act[0] == "fill":
                for k, v in act[2].items():
                    fields.set_property(sym, names[k], v, force=False)
        _write_node(target.sym_file, lib)

    fp_nodes: dict[str, list] = {}
    for i in chosen:
        act = i.action
        if act[0] in fp_actions:
            fp_file = target.fp_dir / f"{act[1]}.kicad_mod"
            node = fp_nodes.get(act[1]) or _read_node(fp_file)
            fp_nodes[act[1]] = node
            if act[0] == "add_model_uri":
                node.append(new_model_node(act[2]))
                continue
            if act[0] == "set_model_uri":
                sexpr.children(node, "model")[act[2]][1] = sexpr.quote(act[3])
                continue
            src = Path(act[-1])
            if src.parent != target.model_dir:  # model lives in another folder: copy it next to the library
                target.model_dir.mkdir(parents=True, exist_ok=True)
                dst = target.model_dir / src.name
                if not dst.exists():
                    shutil.copy2(src, dst)
                src = dst
            uri = target.model_uri(src.name)
            if act[0] == "add_model":
                node.append(new_model_node(uri))
            else:
                sexpr.children(node, "model")[act[2]][1] = sexpr.quote(uri)
        elif act[0] == "delete_model":
            Path(act[1]).unlink(missing_ok=True)
    for name, node in fp_nodes.items():
        _write_node(target.fp_dir / f"{name}.kicad_mod", node)

    err = validate_sym_lib(ki, target.sym_file)
    msg = f"Applied {len(chosen)} fixes to '{target.name}'. Backup: {bdir}"
    if err:
        msg += f"\nWARNING: kicad-cli reports a problem with the symbol library: {err[:300]}"
    log(msg)
    return msg


# ================================================================ merge
@dataclass
class MergeResult:
    sym_map: dict[str, str] = field(default_factory=dict)  # "OldLib:Sym" -> "NewLib:Sym2"
    fp_map: dict[str, str] = field(default_factory=dict)  # "OldFpLib:Fp" -> "NewFpLib:Fp2"
    model_map: dict[str, str] = field(default_factory=dict)  # old model uri -> new model uri
    symbols: int = 0
    footprints: int = 0
    models: int = 0
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _free_name(name: str, taken: set[str]) -> str:
    i = 2
    while f"{name}_{i}".lower() in taken:
        i += 1
    return f"{name}_{i}"


def _rename_symbol(sym: list, new: str) -> None:
    old = sexpr.unquote(sym[1]).split(":")[-1]
    sym[1] = sexpr.quote(new)
    for sub in sexpr.children(sym, "symbol"):  # unit sub-symbols are named <parent>_<unit>_<style>
        sn = sexpr.unquote(sub[1])
        if sn.startswith(old + "_"):
            sub[1] = sexpr.quote(new + sn[len(old):])


def _copy_model(src: Path, dest_dir: Path, res: MergeResult) -> str:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / safe_name(src.name)
    if dst.exists():
        if dst.stat().st_size == src.stat().st_size:
            return dst.name
        taken = {f.name.lower() for f in dest_dir.iterdir()}
        stem, ext = src.stem, src.suffix
        i = 2
        while f"{stem}_{i}{ext}".lower() in taken:
            i += 1
        dst = dest_dir / f"{stem}_{i}{ext}"
    shutil.copy2(src, dst)
    res.models += 1
    return dst.name


def _import_footprint(src_file: Path, fp_lib_name: str, dest: LibraryTarget, env: dict, conflict: str,
                      taken: set[str], res: MergeResult, log: Log, rename_suffix: str = "") -> None:
    name = src_file.stem
    old_id = f"{fp_lib_name}:{name}"
    if old_id in res.fp_map:
        return
    try:
        node = _read_node(src_file)
    except (ValueError, OSError) as e:
        res.warnings.append(f"cannot read footprint {src_file}: {e}")
        return
    new = name
    dst = dest.fp_dir / f"{name}.kicad_mod"
    if name.lower() in taken:
        same = dst.exists() and dst.read_bytes() == src_file.read_bytes()
        if same or conflict == "skip":
            res.fp_map[old_id] = f"{dest.fp_name}:{name}"
            if not same:
                res.skipped.append(f"footprint {name} (exists)")
            return
        if conflict == "rename":
            new = f"{name}_{rename_suffix}" if rename_suffix and f"{name}_{rename_suffix}".lower() not in taken \
                else _free_name(name, taken)
            dst = dest.fp_dir / f"{new}.kicad_mod"
    if len(node) > 1 and isinstance(node[1], str):
        node[1] = sexpr.quote(new)
    for mn in sexpr.children(node, "model"):
        ref = sexpr.unquote(mn[1])
        p = resolve(ref, env)
        if p and p.exists():
            fname = _copy_model(p, dest.model_dir, res)
            mn[1] = sexpr.quote(dest.model_uri(fname))
            res.model_map[ref] = dest.model_uri(fname)
        else:
            res.warnings.append(f"{name}: 3D model not found ({ref}) - path left unchanged")
    _write_node(dst, node)
    taken.add(new.lower())
    res.fp_map[old_id] = f"{dest.fp_name}:{new}"
    res.footprints += 1


def merge_libraries(sources: list[LibraryTarget], dest: LibraryTarget, ki: KiCadInstall,
                    project_dir: Path | None, settings: Settings, conflict: str = "rename",
                    normalize: bool = True, log: Log = _nolog) -> MergeResult:
    res = MergeResult()
    names = fields.field_names(settings.field_names)
    for m in dest.ensure():
        log(m)
    backup = backup_files([dest.sym_file], dest.base_dir / ".partimporter-backup")
    log(f"Backup of destination: {backup}")
    env = kicad_env_vars(ki, project_dir)
    fpx = FootprintIndex(ki, project_dir)
    taken_fp = {f.stem.lower() for f in dest.fp_dir.glob("*.kicad_mod")}

    # 1) footprints of every source library
    for src in sources:
        if src.fp_dir.is_dir() and src.fp_dir != dest.fp_dir:
            files = sorted(src.fp_dir.glob("*.kicad_mod"))
            log(f"Footprints: {len(files)} from {src.fp_name}")
            for f in files:
                _import_footprint(f, src.fp_name, dest, env, conflict, taken_fp, res, log, safe_name(src.fp_name))

    # 2) symbols
    lib = _read_node(dest.sym_file)
    ver = sexpr.child(lib, "version")
    existing = {sexpr.unquote(s[1]).lower(): s for s in sexpr.children(lib, "symbol")}
    for src in sources:
        if not src.sym_file.exists() or src.sym_file == dest.sym_file:
            continue
        node = _read_node(src.sym_file)
        sv = sexpr.child(node, "version")
        if sv and ver and sv[1].isdigit() and ver[1].isdigit() and int(sv[1]) > int(ver[1]):
            ver[1] = sv[1]
        syms = sexpr.children(node, "symbol")
        log(f"Symbols: {len(syms)} from {src.name}")
        renamed: dict[str, str] = {}
        for sym in syms:
            name = sexpr.unquote(sym[1]).split(":")[-1]
            new = name
            if name.lower() in existing:
                if conflict == "skip":
                    res.sym_map[f"{src.name}:{name}"] = f"{dest.name}:{name}"
                    res.skipped.append(f"symbol {name} (exists)")
                    continue
                if conflict == "rename":
                    new = _free_name(name, set(existing))
                    _rename_symbol(sym, new)
                    renamed[name] = new
            # re-point footprint to the merged copy
            fp_val = fields.get_property(sym, "Footprint") or ""
            if fp_val:
                if fp_val in res.fp_map:
                    fields.set_property(sym, "Footprint", res.fp_map[fp_val])
                elif ":" not in fp_val and f"{src.fp_name}:{fp_val}" in res.fp_map:
                    fields.set_property(sym, "Footprint", res.fp_map[f"{src.fp_name}:{fp_val}"])
                elif ":" in fp_val and not fpx.exists(fp_val):
                    res.warnings.append(f"{name}: footprint {fp_val} not found")
            if normalize and sexpr.child(sym, "extends") is None:
                fields.normalize_symbol(sym, names, settings.drop_junk_fields)
            if new.lower() in existing and conflict == "overwrite":
                lib[lib.index(existing[new.lower()])] = sym
            else:
                lib.append(sym)
            existing[new.lower()] = sym
            res.sym_map[f"{src.name}:{name}"] = f"{dest.name}:{new}"
            res.symbols += 1
        # aliases (extends) that pointed at a renamed parent
        for sym in syms:
            ext = sexpr.child(sym, "extends")
            if ext and sexpr.unquote(ext[1]) in renamed:
                ext[1] = sexpr.quote(renamed[sexpr.unquote(ext[1])])
    _write_node(dest.sym_file, lib)
    err = validate_sym_lib(ki, dest.sym_file)
    if err:
        res.warnings.append(f"kicad-cli reports a problem with the merged library: {err[:300]}")
    log(f"Merged {res.symbols} symbols, {res.footprints} footprints, {res.models} 3D models into '{dest.name}'.")
    return res


def unregister_libraries(sources: list[LibraryTarget], log: Log = _nolog) -> None:
    for s in sources:
        for table, nick in ((s.table_dir / "sym-lib-table", s.name), (s.table_dir / "fp-lib-table", s.fp_name)):
            if remove_lib_table_entry(table, nick):
                log(f"Removed '{nick}' from {table}")


# ================================================================ project remap
class ProjectLocked(Exception):
    pass


def project_files(project_dir: Path) -> tuple[list[Path], list[Path]]:
    return sorted(project_dir.glob("*.kicad_sch")), sorted(project_dir.glob("*.kicad_pcb"))


def check_project_closed(project_dir: Path) -> None:
    locks = list(project_dir.glob("~*.kicad_sch.lck")) + list(project_dir.glob("~*.kicad_pcb.lck"))
    if locks:
        raise ProjectLocked("The project's schematic/PCB is open in KiCad. Close the Schematic and PCB "
                            "editors (or the project) first, then run this tool again.\n"
                            "Lock files: " + ", ".join(l.name for l in locks))


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


def _remap_sch_text(text: str, sym_map: dict[str, str], fp_map: dict[str, str]) -> tuple[str, int]:
    count = 0
    # cached library symbols: rename the block and its unit sub-symbols
    for old, new in sym_map.items():
        if old == new:
            continue
        token = f'(symbol "{old}"'
        pos = text.find(token)
        while pos != -1:
            end = _block_end(text, pos)
            block = text[pos:end]
            old_base, new_base = old.split(":")[-1], new.split(":")[-1]
            block = block.replace(token, f'(symbol "{new}"', 1)
            if old_base != new_base:
                block = re.sub(r'\(symbol "' + re.escape(old_base) + r'(_\d+_\d+)"',
                               lambda m: f'(symbol "{new_base}{m.group(1)}"', block)
            text = text[:pos] + block + text[end:]
            count += 1
            pos = text.find(token, pos + len(block))

    def lib_id(m):
        nonlocal count
        v = m.group(2)
        if v in sym_map:
            count += 1
            return f'{m.group(1)}"{sym_map[v]}"'
        return m.group(0)
    text = re.sub(r'(\(lib_id\s+)"([^"]*)"', lib_id, text)

    def fp_prop(m):
        nonlocal count
        v = m.group(2)
        if v in fp_map:
            count += 1
            return f'{m.group(1)}"{fp_map[v]}"'
        return m.group(0)
    text = re.sub(r'(\(property\s+"Footprint"\s+)"([^"]*)"', fp_prop, text)
    return text, count


def _remap_pcb_text(text: str, fp_map: dict[str, str], model_map: dict[str, str]) -> tuple[str, int]:
    count = 0

    def fp(m):
        nonlocal count
        v = m.group(2)
        if v in fp_map:
            count += 1
            return f'{m.group(1)}"{fp_map[v]}"'
        return m.group(0)
    text = re.sub(r'(\(footprint\s+)"([^"]*)"', fp, text)
    if model_map:
        def mdl(m):
            nonlocal count
            v = m.group(2)
            if v in model_map:
                count += 1
                return f'{m.group(1)}"{model_map[v]}"'
            return m.group(0)
        text = re.sub(r'(\(model\s+)"([^"]*)"', mdl, text)
    return text, count


def remap_project(project_dir: Path, sym_map: dict[str, str], fp_map: dict[str, str],
                  model_map: dict[str, str] | None = None, log: Log = _nolog) -> int:
    check_project_closed(project_dir)
    schs, pcbs = project_files(project_dir)
    bdir = backup_files(schs + pcbs, project_dir / "part-importer-backup")
    log(f"Project backup: {bdir}")
    total = 0
    for f in schs:
        text = f.read_text(encoding="utf-8")
        new, n = _remap_sch_text(text, sym_map, fp_map)
        if n:
            f.write_text(new, encoding="utf-8")
            log(f"  {f.name}: {n} references updated")
            total += n
    for f in pcbs:
        text = f.read_text(encoding="utf-8")
        new, n = _remap_pcb_text(text, fp_map, model_map or {})
        if n:
            f.write_text(new, encoding="utf-8")
            log(f"  {f.name}: {n} references updated")
            total += n
    return total


# ================================================================ localize project
@dataclass
class LocalizeResult:
    symbols: int = 0
    footprints: int = 0
    models: int = 0
    references: int = 0
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def localize_project(project_dir: Path, target: LibraryTarget, ki: KiCadInstall, settings: Settings,
                     include_stock: bool = False, log: Log = _nolog) -> LocalizeResult:
    """Copy every symbol/footprint/3D model the project uses into its project library and re-point it."""
    check_project_closed(project_dir)
    out = LocalizeResult()
    names = fields.field_names(settings.field_names)
    env = kicad_env_vars(ki, project_dir)
    sym_entries = {e.name: e for e in all_lib_entries(ki, project_dir, "sym")}
    fpx = FootprintIndex(ki, project_dir)

    def stock_lib(nick: str, kind: str) -> bool:
        if kind == "sym":
            e = sym_entries.get(nick)
            return bool(e and is_stock_kicad_path(e.path(env), ki))
        p = fpx.libs.get(nick)
        return is_stock_kicad_path(p, ki)

    for m in target.ensure():
        log(m)
    schs, pcbs = project_files(project_dir)

    # ---- collect symbols from the schematics' embedded library cache
    cached: dict[str, list] = {}
    used_fp_ids: set[str] = set()
    for f in schs:
        node = _read_node(f)
        ls = sexpr.child(node, "lib_symbols")
        for sym in sexpr.children(ls, "symbol") if ls else []:
            cached.setdefault(sexpr.unquote(sym[1]), sym)
        used_fp_ids |= set(re.findall(r'\(property\s+"Footprint"\s+"([^"]+:[^"]+)"', f.read_text(encoding="utf-8")))
    for f in pcbs:
        used_fp_ids |= set(re.findall(r'\(footprint\s+"([^"]+:[^"]+)"', f.read_text(encoding="utf-8")))

    # ---- footprints (and their 3D models)
    res = MergeResult()
    taken = {f.stem.lower() for f in target.fp_dir.glob("*.kicad_mod")}
    for fid in sorted(used_fp_ids):
        lib, _, name = fid.partition(":")
        if lib == target.fp_name or (not include_stock and stock_lib(lib, "fp")):
            continue
        src = fpx.file(fid)
        if not src:
            out.warnings.append(f"footprint {fid} not found in any library (left unchanged)")
            continue
        _import_footprint(src, lib, target, env, "rename", taken, res, log, safe_name(lib))
    out.footprints, out.models = res.footprints, res.models

    # ---- symbols
    tlib = _read_node(target.sym_file)
    existing = {sexpr.unquote(s[1]).lower(): s for s in sexpr.children(tlib, "symbol")}
    sym_map: dict[str, str] = {}
    for full, sym in cached.items():
        lib, _, name = full.rpartition(":")
        if not lib or lib == target.name or (not include_stock and stock_lib(lib, "sym")):
            continue
        sym = copy.deepcopy(sym)
        new = name
        if name.lower() in existing:
            if sexpr.dump_inline(existing[name.lower()][2:]) == sexpr.dump_inline(sym[2:]):
                sym_map[full] = f"{target.name}:{name}"
                continue
            new = f"{name}_{safe_name(lib)}"
            if new.lower() in existing:
                new = _free_name(new, set(existing))
        _rename_symbol(sym, new)
        fp_val = fields.get_property(sym, "Footprint") or ""
        if fp_val in res.fp_map:
            fields.set_property(sym, "Footprint", res.fp_map[fp_val])
        fields.normalize_symbol(sym, names, settings.drop_junk_fields)
        tlib.append(sym)
        existing[new.lower()] = sym
        sym_map[full] = f"{target.name}:{new}"
        out.symbols += 1
    _write_node(target.sym_file, tlib)

    out.references = remap_project(project_dir, sym_map, res.fp_map, res.model_map, log)
    out.warnings += res.warnings
    err = validate_sym_lib(ki, target.sym_file)
    if err:
        out.warnings.append(f"kicad-cli reports a problem with {target.sym_file.name}: {err[:300]}")
    log(f"Localized {out.symbols} symbols, {out.footprints} footprints, {out.models} 3D models; "
        f"{out.references} references updated.")
    return out


# ================================================================ BOM check
@dataclass
class BomLine:
    refs: str
    qty: int
    value: str
    footprint: str
    mpn: str = ""
    manufacturer: str = ""
    lcsc: str = ""
    dnp: bool = False
    offers: dict = field(default_factory=dict)  # source -> PartResult | str (error)


def read_bom(project: Path, ki: KiCadInstall, settings: Settings) -> list[BomLine]:
    sch = project.with_suffix(".kicad_sch")
    if not ki.kicad_cli:
        raise RuntimeError("kicad-cli not found")
    names = fields.field_names(settings.field_names)
    wanted = {k: list(dict.fromkeys([names[k]] + fields.ALIASES[k])) for k in ("mpn", "manufacturer", "lcsc")}
    extra = [n for k in wanted for n in wanted[k]]
    field_list = ["Reference", "Value", "Footprint", "${QUANTITY}", "${DNP}"] + extra
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "bom.csv"
        cmd = [str(ki.kicad_cli), "sch", "export", "bom", str(sch), "-o", str(out),
               "--fields", ",".join(field_list), "--labels", ",".join(field_list),
               "--group-by", ",".join(["Value", "Footprint"] + wanted["mpn"]), "--ref-range-delimiter", ""]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"kicad-cli BOM export failed: {r.stderr or r.stdout}")
        rows = list(csv.DictReader(out.read_text(encoding="utf-8-sig").splitlines()))

    def first(row, key):
        for n in wanted[key]:
            v = (row.get(n) or "").strip()
            if v and v != "~":
                return v
        return ""
    lines = []
    for row in rows:
        lines.append(BomLine(
            refs=row.get("Reference", ""), qty=int(row.get("${QUANTITY}") or 0 or 0),
            value=row.get("Value", ""), footprint=row.get("Footprint", ""),
            mpn=first(row, "mpn"), manufacturer=first(row, "manufacturer"), lcsc=first(row, "lcsc"),
            dnp=(row.get("${DNP}") or "").strip().upper() in ("DNP", "YES", "TRUE", "1"),
        ))
    return lines


def write_bom_report(lines: list[BomLine], sources: list[str], boards: int, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        head = ["References", "Qty/board", f"Qty x{boards}", "Value", "Footprint", "MPN", "Manufacturer"]
        for s in sources:
            head += [f"{s} PN", f"{s} stock", f"{s} lead", f"{s} price"]
        w.writerow(head)
        for ln in lines:
            row = [ln.refs, ln.qty, ln.qty * boards, ln.value, ln.footprint, ln.mpn, ln.manufacturer]
            for s in sources:
                o = ln.offers.get(s)
                if o is not None and not isinstance(o, str):
                    row += [o.supplier_pn, o.stock if o.stock is not None else "", o.lead_str, o.price_str]
                else:
                    row += [o or "", "", "", ""]
            w.writerow(row)


# ================================================================ KiCad setup check
def check_setup(ki: KiCadInstall, project_dir: Path | None, log: Log = _nolog) -> list[Issue]:
    from .kicad_env import ipc_api_enabled, read_json
    issues: list[Issue] = []
    vars_ = read_json(ki.config_dir / "kicad_common.json").get("environment", {}).get("vars") or {}
    share = ki.root / "share" / "kicad" if ki.root else None
    checks = {"3DMODEL_DIR": ("3dmodels", "*.3dshapes", "3D models"),
              "SYMBOL_DIR": ("symbols", "*.kicad_sym", "symbol libraries"),
              "FOOTPRINT_DIR": ("footprints", "*.pretty", "footprint libraries")}
    for suffix, (sub, pattern, what) in checks.items():
        var = f"KICAD{ki.major}_{suffix}"
        if var not in vars_ or not share:
            continue
        default = share / sub
        override = Path(vars_[var])
        if override.resolve() == default.resolve() if override.exists() else False:
            continue
        n_def = len(list(default.glob(pattern))) if default.exists() else 0
        n_ovr = len(list(override.glob(pattern))) if override.exists() else 0
        if n_def and n_ovr < n_def // 2:
            issues.append(Issue("Path variables", var,
                                f"overridden to {override}, which holds {n_ovr} of KiCad's {n_def} stock {what}. "
                                f"Stock parts will show missing {what}.",
                                f"reset to KiCad default ({default})", ("reset_var", var)))
    env = kicad_env_vars(ki, project_dir)
    for kind in ("sym", "fp"):
        for e in all_lib_entries(ki, project_dir, kind):
            p = e.path(env)
            label = f"{'symbol' if kind == 'sym' else 'footprint'} library '{e.name}' ({e.scope})"
            if p is None:
                issues.append(Issue("Library table", e.name, f"{label}: undefined path variable in {e.uri}",
                                    "remove entry", ("remove_entry", str(e.table), e.name), selected=False))
            elif not p.exists():
                issues.append(Issue("Library table", e.name, f"{label}: file not found {p}",
                                    "remove entry", ("remove_entry", str(e.table), e.name), selected=False))
    if not ipc_api_enabled(ki):
        issues.append(Issue("KiCad API", "IPC API", "KiCad's IPC API is disabled (needed for Update PCB)",
                            "enable it", ("enable_ipc",)))
    if not ki.kicad_cli:
        issues.append(Issue("Info", "kicad-cli", "kicad-cli not found - legacy zip conversion and BOM check unavailable"))
    log(f"Setup check: {len(issues)} findings.")
    return issues


def apply_setup_fixes(ki: KiCadInstall, issues: list[Issue], log: Log = _nolog) -> str:
    from .kicad_env import enable_ipc_api, read_json, running_kicad_processes, write_json
    chosen = [i for i in issues if i.action and i.selected]
    if not chosen:
        return "Nothing selected."
    if running_kicad_processes():
        return "Close KiCad first - it rewrites its settings files on exit and would undo these fixes."
    cfg = ki.config_dir / "kicad_common.json"
    backup_files([cfg, ki.config_dir / "sym-lib-table", ki.config_dir / "fp-lib-table"],
                 ki.config_dir / "part-importer-backup")
    for i in chosen:
        act = i.action
        if act[0] == "reset_var":
            data = read_json(cfg)
            data.get("environment", {}).get("vars", {}).pop(act[1], None)
            write_json(cfg, data)
            log(f"Reset {act[1]} to KiCad's default")
        elif act[0] == "remove_entry":
            if remove_lib_table_entry(Path(act[1]), act[2]):
                log(f"Removed '{act[2]}' from {act[1]}")
        elif act[0] == "enable_ipc":
            enable_ipc_api(ki)
            log("Enabled KiCad IPC API")
    return f"Applied {len(chosen)} fixes. A backup of KiCad's settings is in {ki.config_dir / 'part-importer-backup'}."


# ================================================================ backup
def backup_library_zip(target: LibraryTarget, dest_zip: Path, log: Log = _nolog) -> int:
    n = 0
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        if target.sym_file.exists():
            z.write(target.sym_file, target.sym_file.name)
            n += 1
        for d in (target.fp_dir, target.model_dir):
            if d.is_dir():
                for f in d.rglob("*"):
                    if f.is_file():
                        z.write(f, f"{d.name}/{f.relative_to(d).as_posix()}")
                        n += 1
    log(f"Backed up {n} files of '{target.name}' to {dest_zip}")
    return n
