"""Command-line interface: the same search / import / BOM / fabrication jobs as the GUI, without a window.

Meant for scripts and AI agents as much as for people. Every command can print JSON (--json) to stdout;
progress messages go to stderr, so stdout stays machine-readable. Exit code 0 = success, 1 = the job
failed, 2 = bad arguments, 3 = the part needs a choice (several candidates, or a browser-only download).

    python -m kiporter search "LM358" --json
    python -m kiporter import C7950 --into project --json
    python -m kiporter bom --stock --boards 5 --json
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import re
import sys
from pathlib import Path

from . import __version__, bom, cad, cad_sources, fab, importers, kicad_env, library, libtools, providers
from .config import Settings
from .providers import PartResult

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_CHOICE = 0, 1, 2, 3
LCSC_ID = re.compile(r"^C\d{3,}$", re.I)


class CliError(Exception):
    def __init__(self, msg: str, code: int = EXIT_FAIL, **data):
        super().__init__(msg)
        self.code = code
        self.data = data


# ================================================================ output helpers
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _jsonable(o):
    if isinstance(o, Path):
        return str(o)
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: _jsonable(getattr(o, f.name)) for f in dataclasses.fields(o)}
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(v) for v in o]
    return o


def part_dict(r: PartResult) -> dict:
    d = {
        "source": r.source, "mpn": r.mpn, "manufacturer": r.manufacturer, "description": r.description,
        "package": r.package, "category": r.category, "stock": r.stock, "lead_days": r.lead_days,
        "price": r.price, "currency": r.currency, "moq": r.moq, "supplier_pn": r.supplier_pn,
        "lcsc_id": r.lcsc_id, "lifecycle": r.lifecycle, "datasheet": r.datasheet, "url": r.url,
    }
    if r.cad is not None:
        d["cad"] = {"symbol": r.cad[0], "footprint": r.cad[1], "model_3d": r.cad[2], "source": r.cad_source}
    return d


def hit_dict(h: cad_sources.CadHit) -> dict:
    return {"source": h.source, "ref": h.ref, "symbol": h.sym, "footprint": h.fp, "model_3d": h.m3d,
            "detail": h.detail, "browser_only": h.browser_only, "page": h.page}


def _table(rows: list[list], headers: list[str]) -> str:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    widths = [w if i == len(widths) - 1 else min(w, 48) for i, w in enumerate(widths)]
    fmt = lambda r: "  ".join(c[:w].ljust(w) for c, w in zip(r, widths)).rstrip()  # noqa: E731
    return "\n".join([fmt(headers), fmt(["-" * w for w in widths])] + [fmt(r) for r in rows])


# ================================================================ shared context
class Ctx:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.settings = Settings.load()
        self._ki: kicad_env.KiCadInstall | None = None
        self._providers: dict | None = None

    @property
    def ki(self) -> kicad_env.KiCadInstall:
        if self._ki is None:
            self._ki = kicad_env.detect_kicad(self.settings.kicad_version, self.settings.kicad_cli_path)
        return self._ki

    @property
    def providers(self) -> dict[str, providers.Provider]:
        if self._providers is None:
            self._providers = providers.make_providers(self.settings)
        return self._providers

    # ---- suppliers
    def sources(self, wanted: list[str] | None) -> list[str]:
        """Suppliers to ask: the ones named on the command line, else the ones enabled in Settings."""
        names = list(self.providers)
        if wanted:
            out = []
            for w in wanted:
                m = next((n for n in names if n.lower().replace("-", "").startswith(w.lower().replace("-", ""))),
                         None)
                if not m:
                    raise CliError(f"Unknown source '{w}'. Known: {', '.join(names)}", EXIT_USAGE)
                if not self.providers[m].configured and self.providers[m].key_fields:
                    raise CliError(f"{m} is not connected: add its API key in Kiporter → Settings → Suppliers.")
                out.append(m)
            return out
        out = [n for n, flag in providers.ENABLE_FLAGS.items()
               if getattr(self.settings, flag, False) and (self.providers[n].configured
                                                           or not self.providers[n].key_fields)]
        return out or ["LCSC"]

    # ---- projects
    def project(self, path: str | None, required: bool = True) -> Path | None:
        """A .kicad_pro: from --project (file or folder), else the project open in KiCad, else the last one."""
        if path:
            p = Path(path).expanduser().resolve()
            if p.is_dir():
                pros = sorted(p.glob("*.kicad_pro"))
                if not pros:
                    raise CliError(f"No .kicad_pro in {p}")
                p = pros[0]
            elif p.suffix != ".kicad_pro":
                p = p.with_suffix(".kicad_pro")
            if not p.exists():
                raise CliError(f"Project not found: {p}")
            return p
        projects, _ = kicad_env.detect_projects(self.ki)
        open_ = [pi.pro_file for pi in projects if pi.is_open]
        if open_:
            return open_[0]
        last = Path(self.settings.last_project) if self.settings.last_project else None
        if last and last.exists():
            return last
        if required:
            raise CliError("No project given and none is open in KiCad: pass --project PATH.", EXIT_USAGE)
        return None

    # ---- libraries
    def target(self, into: str | None, lib: str | None, project: str | None) -> library.LibraryTarget:
        kind = {"local": "global"}.get(into or "", into) or self.settings.default_target or "project"
        pro = self.project(project, required=(kind == "project"))
        pdir = pro.parent if pro else None
        libs = library.list_targets(self.settings, self.ki, pdir, kind)
        if lib:
            t = next((t for t in libs if t.name.lower() == lib.lower()), None)
            if t:
                return t
            # a new library of that name, created and registered on first import
            return library.project_target(self.settings, pdir, lib) if kind == "project" \
                else library.global_target(self.settings, self.ki, lib)
        if kind == "global" and self.settings.global_lib_choice:
            t = next((t for t in libs if t.name == self.settings.global_lib_choice), None)
            if t:
                return t
        if not libs:
            raise CliError("No library to import into: pass --project PATH or --into global.", EXIT_USAGE)
        return libs[0]


# ================================================================ search
def run_search(ctx: Ctx, query: str, sources: list[str], limit: int) -> tuple[list[PartResult], dict]:
    results: list[PartResult] = []
    errors: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sources) or 1) as pool:
        futs = {pool.submit(ctx.providers[n].search, query, limit): n for n in sources}
        for f in concurrent.futures.as_completed(futs):
            n = futs[f]
            try:
                results += f.result()
            except Exception as e:  # noqa: BLE001
                errors[n] = str(e)
                log(f"{n}: {e}")
    results.sort(key=lambda r: -(r.stock or 0))
    return results, errors


def fill_cad(ctx: Ctx, query: str, results: list[PartResult]) -> None:
    try:
        cad.learn(query)  # one catalogue request answers most LCSC parts at once
    except Exception:  # noqa: BLE001
        pass
    for r in results:
        try:
            cad.fill_known(r)
            if r.cad is None:
                cad_sources.check_availability(r, ctx.settings)
        except cad.EasyedaBlocked as e:
            log(str(e))
            break
        except Exception as e:  # noqa: BLE001
            log(f"CAD check {r.mpn}: {e}")
    cad.save_cache()


def cmd_search(ctx: Ctx) -> dict:
    a = ctx.args
    srcs = ctx.sources(a.source)
    log(f"Searching {', '.join(srcs)} for '{a.query}'…")
    results, errors = run_search(ctx, a.query, srcs, a.limit)
    if a.in_stock:
        results = [r for r in results if (r.stock or 0) > 0]
    if a.cad:
        fill_cad(ctx, a.query, results)
    out = {"query": a.query, "sources": srcs, "errors": errors, "count": len(results),
           "results": [part_dict(r) for r in results]}
    out["_text"] = _table([[r.source, r.mpn, r.manufacturer, r.package, r.stock_str, r.lead_str, r.price_str,
                            r.supplier_pn, (cad.label(r) if r.cad is not None else ""), r.description[:60]]
                           for r in results],
                          ["Source", "MPN", "Manufacturer", "Package", "Stock", "Lead", "Price", "Supplier PN",
                           "CAD", "Description"]) if results else "No results."
    return out


def resolve_part(ctx: Ctx, query: str, sources: list[str] | None, first: bool) -> PartResult:
    """One part for a query: an LCSC number (C7950), else an exact MPN match at the chosen suppliers."""
    if LCSC_ID.match(query):
        res = providers.search_lcsc(query.upper(), 10)
        r = next((r for r in res if r.supplier_pn.upper() == query.upper() or r.lcsc_id.upper() == query.upper()),
                 None)
        if not r:
            raise CliError(f"LCSC part {query} not found.")
        return r
    srcs = ctx.sources(sources)
    log(f"Looking up '{query}' at {', '.join(srcs)}…")
    results, _ = run_search(ctx, query, srcs, 20)
    if not results:
        raise CliError(f"No supplier lists '{query}'.")
    # exact part number only (best_match also accepts prefixes: LM35 must not become LM358);
    # prefer LCSC, whose model is always EasyEDA's, then the most stock
    t = providers._norm_mpn(query)
    exact = [r for r in results if providers._norm_mpn(r.mpn) == t or providers._norm_mpn(r.supplier_pn) == t]
    if exact:
        return max(exact, key=lambda r: (r.source == "LCSC", r.stock or 0))
    if first:
        return results[0]
    raise CliError(f"No exact match for '{query}'. Use an exact MPN / LCSC number, or --first.", EXIT_CHOICE,
                   candidates=[part_dict(x) for x in results[:15]])


def cmd_cad(ctx: Ctx) -> dict:
    a = ctx.args
    r = resolve_part(ctx, a.query, a.source, a.first)
    log(f"Looking for CAD models of {r.mpn} ({r.source})…")
    hits = cad_sources.find_all(r, ctx.settings, log)
    cad.save_cache()
    best = cad_sources.best(hits, a.cad_source or ctx.settings.cad_source)
    out = {"part": part_dict(r), "hits": [hit_dict(h) for h in hits], "best": hit_dict(best) if best else None,
           "download_links": providers.cad_download_links(r.mpn, r.manufacturer) if not hits else {}}
    out["_text"] = f"{r.mpn} ({r.source} {r.supplier_pn})\n" + (
        _table([[h.source, h.sym, h.fp, h.m3d, h.browser_only, h.detail] for h in hits],
               ["Source", "Symbol", "Footprint", "3D", "Browser only", "Detail"]) if hits
        else "No CAD model found. Try:\n" + "\n".join(f"  {k}: {v}" for k, v in out["download_links"].items()))
    return out


def install_result(res: library.InstallResult, target: library.LibraryTarget) -> dict:
    return {"library": target.name, "kind": target.kind, "symbol_file": str(target.sym_file),
            "footprint_library": target.fp_name, "symbols": res.symbols, "footprints": res.footprints,
            "models": res.models, "skipped": res.skipped, "messages": res.messages,
            "new_library": any("Registered" in m for m in res.messages)}


def _install_opts(ctx: Ctx) -> dict:
    a = ctx.args
    return dict(overwrite=a.overwrite, want_symbol=not a.no_symbol, want_footprint=not a.no_footprint,
                want_3d=not a.no_3d, settings=ctx.settings)


def _import_text(out: dict) -> str:
    lines = [f"Imported into {out['library']} ({out['kind']}):"]
    lines += [f"  + symbol     {s}" for s in out["symbols"]]
    lines += [f"  + footprint  {f}" for f in out["footprints"]]
    lines += [f"  + 3D model   {m}" for m in out["models"]]
    lines += [f"  = skipped    {s}  (use --overwrite to replace)" for s in out["skipped"]]
    lines += [f"  {m}" for m in out["messages"]]
    if out["new_library"]:
        lines.append("New library registered: restart KiCad (local) or reopen the project (project) once.")
    return "\n".join(lines)


def cmd_import(ctx: Ctx) -> dict:
    a = ctx.args
    if a.into:  # fail fast on a missing project before searching
        ctx.target(a.into, a.lib, a.project)
    r = resolve_part(ctx, a.query, a.source, a.first)
    target = choose_target(ctx, f"{r.mpn} ({r.manufacturer}, {r.source} {r.supplier_pn})".replace(", )", ")"))
    return import_part(ctx, r, target, a.cad_source)


def import_part(ctx: Ctx, r: PartResult, target: library.LibraryTarget, cad_source: str | None = None) -> dict:
    """Find the best CAD model of a resolved part and install it into target (options from ctx.args)."""
    log(f"Looking for CAD models of {r.mpn} ({r.source})…")
    hits = cad_sources.find_all(r, ctx.settings, log)
    h = cad_sources.best(hits, cad_source or ctx.settings.cad_source)
    if not h:
        raise CliError(f"No CAD model found for {r.mpn}.", EXIT_CHOICE, part=part_dict(r),
                       download_links=providers.cad_download_links(r.mpn, r.manufacturer))
    data = r.part_data()
    if h.source == "EasyEDA":
        data["lcsc"] = h.ref
    log(f"Importing {r.mpn} from {h.source} into {target.name}…")
    try:
        staged = cad_sources.fetch(h, ctx.settings, ctx.ki, data)
    except cad_sources.NeedsBrowser as e:
        raise CliError(f"{e} Download the KiCad zip yourself, then run: kiporter import-zip FILE", EXIT_CHOICE,
                       part=part_dict(r), url=e.url)
    except cad_sources.NeedsLogin as e:
        raise CliError(f"{e} Add the login in Kiporter → Settings → CAD sources.")
    finally:
        cad.save_cache()
    out = install_result(library.install(target, staged, **_install_opts(ctx)), target)
    out.update(part=part_dict(r), cad_source=h.source)
    out["_text"] = _import_text(out)
    return out


def cmd_import_zip(ctx: Ctx) -> dict:
    a = ctx.args
    z = Path(a.zip).expanduser().resolve()
    if not z.exists():
        raise CliError(f"File not found: {z}")
    target = choose_target(ctx, z.name + (f" ({a.mpn})" if a.mpn else ""))
    data = {k: v for k, v in (("mpn", a.mpn), ("manufacturer", a.manufacturer), ("lcsc", a.lcsc)) if v}
    from . import symstyle
    staged = symstyle.restyle_part(importers.stage_zip(z, ctx.ki, data), ctx.settings.symbol_style)
    out = install_result(library.install(target, staged, **_install_opts(ctx)), target)
    out["_text"] = _import_text(out)
    return out


# ================================================================ choosing the library (project or local)
@dataclasses.dataclass
class LibOption:
    label: str
    kind: str  # project | global
    name: str
    exists: bool

    @property
    def destination(self) -> str:
        return "project" if self.kind == "project" else "local"


def library_options(ctx: Ctx, project: str | None, wanted_kind: str | None = None, wanted_name: str | None = None
                    ) -> tuple[list[LibOption], str | None]:
    """Every library a part could go to (project ones first), plus the project's name if there is one."""
    pro = ctx.project(project, required=False)
    pdir = pro.parent if pro else None
    opts: list[LibOption] = []
    for kind in ("project", "global"):
        for t in library.list_targets(ctx.settings, ctx.ki, pdir, kind):
            where = f"in project {pro.stem}, travels with it" if kind == "project" else "shared by all projects"
            new = "" if t.sym_file.exists() else ", new"
            opts.append(LibOption(f"{'Project' if kind == 'project' else 'Local'} library '{t.name}' ({where}{new})",
                                  kind, t.name, t.sym_file.exists()))
    if wanted_kind and wanted_name and (wanted_kind != "project" or pdir) and \
            not any(o.kind == wanted_kind and o.name.lower() == wanted_name.lower() for o in opts):
        word = "project" if wanted_kind == "project" else "local"
        opts.append(LibOption(f"New {word} library '{wanted_name}'", wanted_kind, wanted_name, False))
    return opts, (pro.stem if pro else None)


def suggested_option(ctx: Ctx, opts: list[LibOption], pname: str | None, kind: str | None = None,
                     name: str | None = None) -> LibOption:
    """The answer to pre-select: what was asked for, else the GUI's default (project / local)."""
    for o in opts:
        if kind and o.kind == kind and (not name or o.name.lower() == name.lower()):
            return o
    default_kind = kind or ("project" if pname and ctx.settings.default_target == "project" else "global")
    return next((o for o in opts if o.kind == default_kind), opts[0])


def library_question(what: str, pname: str | None) -> str:
    return (f"Import {what} into the project library{f' of {pname}' if pname else ''} (stored in the project "
            "folder, travels with the project) or into a local library (shared by all your projects)?")


def choose_target(ctx: Ctx, what: str) -> library.LibraryTarget:
    """The library to import into. --into decides; otherwise the user is asked in the terminal. When nobody
    can be asked (no terminal, --json: a script or an agent), nothing is imported and the options are
    returned, so the caller asks the user and runs again with --into."""
    a = ctx.args
    if a.into:
        return ctx.target(a.into, a.lib, a.project)
    opts, pname = library_options(ctx, a.project)
    if not opts:
        raise CliError("No library to import into: pass --project PATH or --into local.", EXIT_USAGE)
    if a.lib:  # a library named on its own is enough when only one library has that name
        same = [o for o in opts if o.name.lower() == a.lib.lower()]
        if len(same) == 1:
            return ctx.target(same[0].kind, same[0].name, a.project)
    sugg = suggested_option(ctx, opts, pname)
    question = library_question(what, pname)
    if a.json or not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise CliError(f"{question} Nothing was imported: ask the user, then run again with --into project or "
                       "--into local (and --lib NAME for a specific library).", EXIT_CHOICE,
                       needs_confirmation=True, question=question,
                       options=[{"destination": o.destination, "library": o.name, "label": o.label,
                                 "exists": o.exists} for o in opts],
                       suggested={"destination": sugg.destination, "library": sugg.name})
    log(question)
    for i, o in enumerate(opts, 1):
        log(f"  {i}) {o.label}{'   <- default' if o is sugg else ''}")
    while True:
        sys.stderr.write(f"Choose 1-{len(opts)} (Enter = {opts.index(sugg) + 1}, q = cancel): ")
        sys.stderr.flush()
        ans = sys.stdin.readline()
        if not ans or ans.strip().lower() in ("q", "quit", "n", "no"):
            raise CliError("Cancelled. Nothing was imported.")
        ans = ans.strip()
        if not ans:
            pick = sugg
            break
        if ans.isdigit() and 1 <= int(ans) <= len(opts):
            pick = opts[int(ans) - 1]
            break
    return ctx.target(pick.kind, pick.name, a.project)


# ================================================================ projects & libraries
def cmd_projects(ctx: Ctx) -> dict:
    projects, status = kicad_env.detect_projects(ctx.ki)
    out = {"kicad": {"version": ctx.ki.version, "cli": ctx.ki.kicad_cli, "running": status.get("running")},
           "projects": [{"name": p.name, "path": str(p.pro_file), "open": p.is_open} for p in projects]}
    out["_text"] = f"KiCad {ctx.ki.version}\n" + (
        _table([["● OPEN" if p.is_open else "", p.name, p.pro_file] for p in projects], ["", "Project", "Path"])
        if projects else "No projects found.")
    return out


def cmd_libraries(ctx: Ctx) -> dict:
    a = ctx.args
    pro = ctx.project(a.project, required=False)
    pdir = pro.parent if pro else None
    libs = [(k, t) for k in ("project", "global") for t in library.list_targets(ctx.settings, ctx.ki, pdir, k)]
    out = {"project": str(pro) if pro else None, "default_target": ctx.settings.default_target,
           "libraries": [{"kind": k, "name": t.name, "symbol_file": str(t.sym_file), "footprint_library": t.fp_name,
                          "footprint_dir": str(t.fp_dir), "exists": t.sym_file.exists()} for k, t in libs]}
    out["_text"] = _table([[k, t.name, "yes" if t.sym_file.exists() else "new", t.sym_file] for k, t in libs],
                          ["Kind", "Library", "Exists", "Symbol file"])
    return out


def cmd_check_setup(ctx: Ctx) -> dict:
    pro = ctx.project(ctx.args.project, required=False)
    issues = libtools.check_setup(ctx.ki, pro.parent if pro else None, log)
    out = {"issues": [{"category": i.category, "item": i.item, "detail": i.detail, "fix": i.fix} for i in issues]}
    out["_text"] = _table([[i.category, i.item, i.detail, i.fix] for i in issues],
                          ["Category", "Item", "Detail", "Fix"]) if issues else "No problems found."
    return out


# ================================================================ BOM
def line_dict(ln: bom.BomLine, need: int | None = None) -> dict:
    d = {"refs": ln.refs, "qty": ln.qty, "value": ln.value, "footprint": ln.footprint, "dnp": ln.dnp,
         "mount": ln.mount, "description": ln.description, "fields": ln.data}
    if ln.offers:
        d["offers"] = {s: (o if isinstance(o, str) or o is None else part_dict(o)) for s, o in ln.offers.items()}
    if need is not None:
        d["need"] = need
        d["stock_status"] = bom.stock_status(ln, need)
    return d


def check_stock(ctx: Ctx, lines: list[bom.BomLine], sources: list[str]) -> None:
    def worker(name: str) -> None:
        prov = ctx.providers[name]
        for ln in lines:
            q = (ln.lcsc if name == "LCSC" and ln.lcsc else "") or ln.mpn or ln.lcsc
            if not q:
                continue
            try:
                res = prov.search(q, limit=10)
                best = next((r for r in res if r.supplier_pn == q), None) if q == ln.lcsc \
                    else providers.best_match(res, q)
                ln.offers[name] = best or "not found"
            except Exception as e:  # noqa: BLE001
                ln.offers[name] = "error"
                log(f"{name} {q}: {e}")

    for ln in lines:
        ln.offers = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sources) or 1) as pool:
        list(pool.map(worker, sources))


def cmd_bom(ctx: Ctx) -> dict:
    a = ctx.args
    pro = ctx.project(a.project)
    log(f"Reading the BOM of {pro.name}…")
    lines = bom.read_bom(pro, ctx.ki, ctx.settings)
    srcs: list[str] = []
    if a.stock:
        srcs = ctx.sources(a.source)
        log(f"Checking stock at {', '.join(srcs)}…")
        check_stock(ctx, [l for l in lines if not l.dnp], srcs)
    need = (lambda ln: ln.qty * a.boards) if a.stock else (lambda ln: None)
    out = {"project": str(pro), "boards": a.boards, "sources": srcs,
           "lines": [line_dict(ln, need(ln)) for ln in lines]}
    rows = []
    for ln in lines:
        row = [ln.refs_str, ln.qty, ln.value, ln.footprint_name, ln.mpn, ln.lcsc, "DNP" if ln.dnp else ""]
        if a.stock:
            row.append(bom.stock_status(ln, ln.qty * a.boards))
        rows.append(row)
    out["_text"] = _table(rows, ["Refs", "Qty", "Value", "Footprint", "MPN", "LCSC", "DNP"] + (
        ["Stock"] if a.stock else []))
    return out


def cmd_bom_export(ctx: Ctx) -> dict:
    a = ctx.args
    pro = ctx.project(a.project)
    names = [f.name for f in bom.FORMATS]
    fmt = next((f for f in bom.FORMATS if f.name.lower() == a.format.lower()), None)
    out_dir = Path(a.out).expanduser().resolve() if a.out else pro.parent
    if not fmt:
        # a BOM preset made in KiCad (Symbol Fields Table -> Export), or a KiCad BOM plugin
        t = bom.find_template(a.format if bom.is_kicad_choice(a.format) else bom.KICAD_PREFIX + a.format,
                              pro, ctx.ki, ctx.settings)
        if not t:
            kicad = [bom.plain_name(n) for n in bom.template_choices(pro, ctx.ki, ctx.settings)]
            raise CliError(f"Unknown BOM format '{a.format}'. Known: {', '.join(names)}; KiCad presets: "
                           f"{', '.join(kicad)}", EXIT_USAGE)
        kfmt = bom.find_kicad_format(a.kicad_format, pro, ctx.ki)
        files = bom.export_kicad_template(t, pro, ctx.ki, out_dir, pro.stem, kfmt)
        out = {"format": t.name, "files": [str(f) for f in files], "missing": [], "notes": ""}
        out["_text"] = "\n".join(f"Wrote {f}" for f in files)
        return out
    lines = bom.read_bom(pro, ctx.ki, ctx.settings)
    places = bom.read_positions(pro, ctx.ki) if (fmt.cpl and not a.no_cpl) else None
    files = bom.export(lines, fmt, out_dir, pro.stem, a.boards, a.include_dnp, a.file_type, places)
    missing = bom.missing_fields(lines, fmt)
    out = {"format": fmt.name, "files": [str(f) for f in files], "missing": missing, "notes": fmt.notes}
    out["_text"] = "\n".join([f"Wrote {f}" for f in files] + [f"Missing: {m}" for m in missing])
    return out


def cmd_formats(ctx: Ctx) -> dict:
    out = {"bom_formats": [{"name": f.name, "kind": f.kind, "cpl": f.cpl} for f in bom.FORMATS],
           "fab_profiles": [p.name for p in fab.all_profiles(ctx.settings)],
           "sources": {n: {"enabled": bool(getattr(ctx.settings, flag, False)),
                           "connected": ctx.providers[n].configured or not ctx.providers[n].key_fields}
                       for n, flag in providers.ENABLE_FLAGS.items()},
           "cad_sources": cad_sources.order(ctx.settings)}
    pro = ctx.project(getattr(ctx.args, "project", None), required=False)
    if pro:
        out["kicad_bom_presets"] = [bom.plain_name(n) for n in bom.template_choices(pro, ctx.ki, ctx.settings)]
        out["kicad_bom_output_formats"] = list(bom.kicad_formats(pro, ctx.ki))
    out["_text"] = ("BOM formats:  " + ", ".join(f["name"] for f in out["bom_formats"]) +
                    (f"\nKiCad BOM presets ({pro.stem}): " + ", ".join(out["kicad_bom_presets"]) if pro else "") +
                    "\nFab profiles: " + ", ".join(out["fab_profiles"]) +
                    "\nSuppliers:    " + ", ".join(f"{n}{'' if s['connected'] else ' (not connected)'}"
                                                   for n, s in out["sources"].items()) +
                    "\nCAD sources:  " + ", ".join(out["cad_sources"]))
    return out


# ================================================================ fabrication
def cmd_fab(ctx: Ctx) -> dict:
    a = ctx.args
    pro = ctx.project(a.project)
    profiles = [p.name for p in fab.all_profiles(ctx.settings)]
    prof = a.profile or ctx.settings.fab_profile
    if prof not in profiles:
        raise CliError(f"Unknown board house '{prof}'. Known: {', '.join(profiles)}", EXIT_USAGE)
    bf = a.bom_format or ""
    if bf and not bom.is_kicad_choice(bf) and bf.lower() not in (f.name.lower() for f in bom.FORMATS):
        bf = bom.KICAD_PREFIX + bf  # a KiCad preset given by its own name
    bf = next((f.name for f in bom.FORMATS if f.name.lower() == bf.lower()), bf)
    opts = fab.FabOptions(profile=prof, refill_drc=not a.no_drc, drc_report=not a.no_drc_report, boards=a.boards,
                          file_type=ctx.settings.bom_file_type or "csv", bom_format=bf,
                          bom_kicad_format=a.kicad_format or "")
    res = fab.generate(pro, ctx.ki, ctx.settings, opts, log)
    if a.zip:
        res.zip_file = fab.make_zip(res.build_dir, fab.zip_path_for(pro))
    out = {"build_dir": str(res.build_dir), "gerber_dir": str(res.gerber_dir), "files": [str(f) for f in res.files],
           "zip": str(res.zip_file) if res.zip_file else None, "drc": res.drc_summary, "warnings": res.warnings}
    out["_text"] = "\n".join([f"Build folder: {res.build_dir}", f"DRC: {res.drc_summary}"] +
                             ([f"Zip: {res.zip_file}"] if res.zip_file else []) +
                             [f"! {w}" for w in res.warnings])
    return out


def cmd_update_pcb(ctx: Ctx) -> dict:
    from . import kicad_control
    pro = ctx.project(ctx.args.project, required=False)
    pi = kicad_env.ProjectInfo(pro) if pro else None
    s = ctx.settings
    options = {key: getattr(s, f"update_pcb_{key}", "") for key, _ in kicad_control.UPDATE_OPTIONS}
    ok, msg = kicad_control.update_pcb_from_schematic(ctx.ki, pi, s.update_pcb_method, s.update_pcb_hotkey,
                                                      options, s.update_pcb_press)
    if not ok:
        raise CliError(msg)
    return {"ok": ok, "message": msg, "_text": msg}


# ================================================================ argument parsing
def _add_target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--into", choices=["project", "local", "global"],
                   help="project library or local library (global = local). Without it you are asked; "
                        "when nobody can be asked (no terminal, --json) nothing is imported (exit 3)")
    p.add_argument("--lib", help="library nickname (created and registered if it does not exist)")
    p.add_argument("--project", help="KiCad project (.kicad_pro or its folder); default: the one open in KiCad")
    p.add_argument("--overwrite", action="store_true", help="replace parts that already exist")
    p.add_argument("--no-symbol", action="store_true")
    p.add_argument("--no-footprint", action="store_true")
    p.add_argument("--no-3d", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kiporter", description="Kiporter command line. Without a command, the GUI "
                                 "starts. Add --json for machine-readable output.")
    ap.add_argument("--version", action="version", version=f"Kiporter {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print JSON to stdout")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    def add(name, fn, help_):
        p = sub.add_parser(name, parents=[common], help=help_, description=help_)
        p.set_defaults(fn=fn)
        return p

    src_help = "supplier to ask (repeatable): LCSC, Digi-Key, Mouser, Farnell, TME, Octopart; default: enabled ones"
    p = add("search", cmd_search, "search suppliers for parts")
    p.add_argument("query")
    p.add_argument("-s", "--source", action="append", help=src_help)
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--in-stock", action="store_true", help="only parts in stock")
    p.add_argument("--cad", action="store_true", help="also check which results have symbol/footprint/3D (slower)")

    p = add("cad", cmd_cad, "list the CAD models available for one part")
    p.add_argument("query", help="LCSC number (C7950) or exact MPN")
    p.add_argument("-s", "--source", action="append", help=src_help)
    p.add_argument("--first", action="store_true", help="use the top result when no exact MPN match")
    p.add_argument("--cad-source", choices=cad_sources.SOURCES)

    p = add("import", cmd_import, "import a part's symbol, footprint and 3D model into a library")
    p.add_argument("query", help="LCSC number (C7950) or exact MPN")
    p.add_argument("-s", "--source", action="append", help=src_help)
    p.add_argument("--first", action="store_true", help="use the top result when no exact MPN match")
    p.add_argument("--cad-source", choices=cad_sources.SOURCES, help="force a CAD source")
    _add_target_args(p)

    p = add("import-zip", cmd_import_zip, "import a downloaded KiCad CAD zip (SnapEDA, Ultra Librarian, SamacSys)")
    p.add_argument("zip")
    p.add_argument("--mpn")
    p.add_argument("--manufacturer")
    p.add_argument("--lcsc")
    _add_target_args(p)

    add("projects", cmd_projects, "list KiCad projects (open ones first)")
    p = add("libraries", cmd_libraries, "list the libraries parts can be imported into")
    p.add_argument("--project")
    p = add("check-setup", cmd_check_setup, "check KiCad paths and library tables for problems")
    p.add_argument("--project")
    p = add("formats", cmd_formats, "list BOM formats (and the project's KiCad BOM presets), board houses, "
                                    "suppliers and CAD sources")
    p.add_argument("--project")

    p = add("bom", cmd_bom, "read a project's BOM, optionally with stock and lead times")
    p.add_argument("--project")
    p.add_argument("--stock", action="store_true", help="check stock at the suppliers")
    p.add_argument("-s", "--source", action="append", help=src_help)
    p.add_argument("--boards", type=int, default=1)

    p = add("bom-export", cmd_bom_export, "export the BOM (and CPL) for a board house or distributor")
    p.add_argument("--project")
    p.add_argument("-f", "--format", default="JLCPCB",
                   help="a board-house format or the name of a BOM preset made in KiCad; see 'kiporter formats'")
    p.add_argument("--kicad-format", help="KiCad format preset for a KiCad BOM preset (default: the project's)")
    p.add_argument("-o", "--out", help="output folder (default: the project folder)")
    p.add_argument("--boards", type=int, default=1)
    p.add_argument("--file-type", choices=["csv", "xlsx"], default="csv")
    p.add_argument("--include-dnp", action="store_true")
    p.add_argument("--no-cpl", action="store_true", help="skip the placement file")

    p = add("fab", cmd_fab, "generate the '<Project> Build' folder (Gerbers, drill, BOM, CPL, PDF, DRC)")
    p.add_argument("--project")
    p.add_argument("--profile", help="board house (see 'kiporter formats'); default: the GUI's choice")
    p.add_argument("--boards", type=int, default=1)
    p.add_argument("--no-drc", action="store_true", help="skip zone refill and DRC")
    p.add_argument("--no-drc-report", action="store_true", help="run DRC but leave its report out of the Build folder")
    p.add_argument("--bom-format", help="override the board house's BOM format (a format or a KiCad BOM preset)")
    p.add_argument("--kicad-format", help="KiCad format preset for a KiCad BOM preset (default: the project's)")
    p.add_argument("--zip", action="store_true", help="also create '<Project> Build.zip'")

    p = add("update-pcb", cmd_update_pcb, "run 'Update PCB from Schematic' in the open KiCad")
    p.add_argument("--project")

    p = sub.add_parser("mcp", help="run the MCP server (stdio) for Claude and other AI apps",
                       description="Run Kiporter as an MCP server over stdio. Needs: pip install \"mcp>=2\"")
    p.set_defaults(fn=None, mcp=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # part descriptions contain Ω, µ, ±: never crash on a cp1252 console
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = build_parser()
    args = ap.parse_args(argv)
    if getattr(args, "mcp", False):
        try:
            from .mcp_server import serve
        except ImportError as e:
            log(f"The MCP server needs the 'mcp' package ({e}). Install it with:\n"
                f'  "{sys.executable}" -m pip install "mcp>=2"')
            return EXIT_FAIL
        serve()
        return EXIT_OK
    if not getattr(args, "fn", None):
        ap.print_help()
        return EXIT_USAGE
    as_json = args.json
    try:
        out = args.fn(Ctx(args))
        code = EXIT_OK
    except CliError as e:
        out, code = {"error": str(e), **e.data}, e.code
    except Exception as e:  # noqa: BLE001
        out, code = {"error": f"{type(e).__name__}: {e}"}, EXIT_FAIL
    if as_json:
        out.pop("_text", None)
        print(json.dumps(_jsonable(out), indent=2, ensure_ascii=False, default=str))
    elif "error" in out:
        log(f"Error: {out['error']}")
        for o in out.get("options", []):
            log(f"  --into {o['destination']:7} --lib {o['library']:20} {o['label']}")
        for c in out.get("candidates", []):
            log(f"  {c['source']:9} {c['mpn']:24} {c['manufacturer'][:20]:20} {c['supplier_pn']}")
        if out.get("url"):
            log(f"  {out['url']}")
    else:
        print(out.get("_text", ""))
    return code


if __name__ == "__main__":
    sys.exit(main())
