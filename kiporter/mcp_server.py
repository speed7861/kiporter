"""MCP server: Kiporter's search / import / BOM / fabrication jobs as tools for Claude and other AI apps.

Start it with `kiporter mcp` (stdio). It runs the same code as the command line (cli.py), with the same
settings, API keys and libraries as the GUI. Needs the optional `mcp` package (v2, Python 3.10+).

Importing a part always asks the *user* whether it goes into the project library or a local (shared)
library: through an MCP form (elicitation) when the client supports one, otherwise the tool answers
`needs_confirmation` and the assistant must ask the user and call again with their answer.
"""
from __future__ import annotations

import argparse
from typing import Any, Literal

import anyio.to_thread
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, create_model

from . import __version__, cli
from .cli import LibOption, library_options

INSTRUCTIONS = """Kiporter searches electronic parts at LCSC, Digi-Key, Mouser, Farnell/Newark, TME and Octopart,
and imports their KiCad symbol, footprint and 3D model into the user's libraries. It also reads BOMs,
checks stock and writes fabrication files for KiCad projects.

- Use search_parts to find candidates, then import_part with an exact MPN or an LCSC number (C7950).
- Never decide yourself whether a part goes into the PROJECT library or a LOCAL library: import_part asks
  the user. If it returns needs_confirmation, ask the user that question and call again with their answer.
- Tools that need a project use the one open in KiCad unless `project` (a .kicad_pro or its folder) is given.
- After the first import into a new library the user must restart KiCad (local) or reopen the project
  (project library) once.
"""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

mcp = MCPServer("Kiporter", version=__version__, instructions=INSTRUCTIONS)
Destination = Literal["project", "local"]


# ================================================================ plumbing
def _ctx(argv: list[str]) -> cli.Ctx:
    return cli.Ctx(cli.build_parser().parse_args(argv))


def _out(out: dict) -> dict[str, Any]:
    out = dict(out)
    out.pop("_text", None)
    return cli._jsonable(out)


def _run(argv: list[str]) -> dict[str, Any]:
    """Run a CLI command in this process and return its JSON result."""
    ctx = _ctx(argv)
    try:
        return _out(ctx.args.fn(ctx))
    except cli.CliError as e:
        if e.code == cli.EXIT_CHOICE:  # not a failure: the caller has to pick (candidates, browser download)
            return {"needs_choice": True, "message": str(e), **cli._jsonable(e.data)}
        raise ToolError(str(e)) from e


async def _thread(fn, *args):
    return await anyio.to_thread.run_sync(lambda: fn(*args))


def _opt(argv: list[str], flag: str, value) -> None:
    if value is None or value is False or value == "":
        return
    if value is True:
        argv.append(flag)
    elif isinstance(value, (list, tuple)):
        for v in value:
            argv += [flag, str(v)]
    else:
        argv += [flag, str(value)]


# ================================================================ library confirmation
def _can_elicit(ctx: Context | None) -> bool:
    try:
        caps = ctx.client_capabilities if ctx is not None else None
        return bool(caps and caps.elicitation)
    except Exception:  # noqa: BLE001
        return False


async def confirm_library(ctx: Context | None, what: str, opts: list[LibOption], suggested: LibOption,
                          project_name: str | None) -> LibOption | str | None:
    """Ask the user which library to use. Returns the option, 'cancel', or None if the client cannot ask."""
    if not _can_elicit(ctx):
        return None
    labels = tuple(o.label for o in opts)
    schema = create_model("LibraryChoice", library=(Literal[labels], Field(  # type: ignore[valid-type]
        default=suggested.label, title="Import into",
        description="Project library: stored inside the project folder and shared with it. "
                    "Local library: one library on this computer, shared by all your projects.")))
    msg = (f"Kiporter is about to import {what}.\n\nShould it go into the project library"
           f"{f' of {project_name}' if project_name else ''} or a local library?")
    try:
        res = await ctx.elicit(msg, schema)
    except Exception:  # noqa: BLE001  (client advertised elicitation but could not show it)
        return None
    if res.action != "accept":
        return "cancel"
    return next(o for o in opts if o.label == res.data.library)


async def _choose_target(ctx: Context | None, kctx: cli.Ctx, what: str, destination: str | None,
                         library_name: str | None, project: str | None, user_confirmed: bool
                         ) -> tuple[LibOption | None, dict | None]:
    """(chosen library, None) or (None, the answer to return instead: a question for the user / cancelled)."""
    kind = {"local": "global", "project": "project"}.get(destination or "")
    opts, pname = await _thread(library_options, kctx, project, kind, library_name)
    if not opts:
        raise ToolError("No library to import into.")
    suggested = cli.suggested_option(kctx, opts, pname, kind, library_name)
    choice = await confirm_library(ctx, what, opts, suggested, pname)
    if choice == "cancel":
        return None, {"cancelled": True, "message": "The user cancelled the import. Nothing was changed."}
    if isinstance(choice, LibOption):
        return choice, None
    # the client cannot show a form: the assistant has to ask the user itself
    if destination and user_confirmed:
        if kind == "project" and not pname:
            raise ToolError("No KiCad project found for a project library: open it in KiCad or pass `project`.")
        if not library_name:  # the default library of the chosen kind
            return cli.suggested_option(kctx, opts, pname, kind), None
        match = next((o for o in opts if o.kind == kind and o.name.lower() == library_name.lower()), None)
        return match or LibOption("", kind, library_name, False), None
    return None, {
        "needs_confirmation": True,
        "question": cli.library_question(what, pname),
        "options": [{"destination": o.destination, "library": o.name, "label": o.label, "exists": o.exists}
                    for o in opts],
        "suggested": {"destination": suggested.destination, "library": suggested.name},
        "next_step": "Ask the user this question and wait for the answer. Then call this tool again with "
                     "destination and library set to their choice and user_confirmed=true. Do not choose for them.",
    }


# ================================================================ tools: search
@mcp.tool(annotations=READ)
def search_parts(query: str, sources: list[str] | None = None, limit: int = 20, in_stock_only: bool = False,
                 check_cad: bool = False) -> dict[str, Any]:
    """Search suppliers for parts by part number or keywords (e.g. 'LM358', '10k 0603').

    sources: suppliers to ask (LCSC, Digi-Key, Mouser, Farnell, TME, Octopart); default: the ones enabled in
    Kiporter. check_cad: also find out which results have a symbol / footprint / 3D model (slower).
    Results are sorted by stock, most first."""
    argv = ["search", query, "--limit", str(max(1, min(limit, 50)))]
    _opt(argv, "--source", sources)
    _opt(argv, "--in-stock", in_stock_only)
    _opt(argv, "--cad", check_cad)
    return _run(argv)


@mcp.tool(annotations=READ)
def find_cad_models(query: str, sources: list[str] | None = None, use_first_result: bool = False) -> dict[str, Any]:
    """List the CAD models (symbol, footprint, 3D) available for one part: an LCSC number (C7950) or exact MPN."""
    argv = ["cad", query]
    _opt(argv, "--source", sources)
    _opt(argv, "--first", use_first_result)
    return _run(argv)


# ================================================================ tools: import
@mcp.tool(annotations=WRITE)
async def import_part(query: str, destination: Destination | None = None, library: str | None = None,
                      project: str | None = None, user_confirmed: bool = False,
                      sources: list[str] | None = None, use_first_result: bool = False,
                      cad_source: Literal["EasyEDA", "SamacSys", "Octopart", "SnapEDA"] | None = None,
                      overwrite: bool = False, symbol: bool = True, footprint: bool = True, model_3d: bool = True,
                      ctx: Context | None = None) -> dict[str, Any]:
    """Import a part's KiCad symbol, footprint and 3D model into a library.

    query: an LCSC number (C7950) or an exact manufacturer part number. If several parts match, nothing is
    imported and the candidates are returned (needs_choice).

    The user always decides where the part goes: the PROJECT library (inside the project folder) or a
    LOCAL library (shared by all projects). Kiporter asks them with a form when the app supports it.
    Otherwise this tool returns needs_confirmation with the question and options: ask the user, then call
    again with destination ('project' or 'local'), library (its name) and user_confirmed=true. Never set
    user_confirmed without asking the user."""
    argv = ["import", query]
    _opt(argv, "--project", project)
    _opt(argv, "--source", sources)
    _opt(argv, "--first", use_first_result)
    _opt(argv, "--overwrite", overwrite)
    _opt(argv, "--no-symbol", not symbol)
    _opt(argv, "--no-footprint", not footprint)
    _opt(argv, "--no-3d", not model_3d)
    kctx = _ctx(argv)
    try:
        part = await _thread(cli.resolve_part, kctx, query, sources, use_first_result)
    except cli.CliError as e:
        if e.code == cli.EXIT_CHOICE:
            return {"needs_choice": True, "message": str(e), **cli._jsonable(e.data)}
        raise ToolError(str(e)) from e
    what = f"{part.mpn} ({part.manufacturer}, {part.source} {part.supplier_pn})".replace(", )", ")")
    chosen, answer = await _choose_target(ctx, kctx, what, destination, library, project, user_confirmed)
    if answer is not None:
        answer["part"] = cli.part_dict(part)
        return answer

    def work():
        target = kctx.target(chosen.kind, chosen.name, project)
        return cli.import_part(kctx, part, target, cad_source)
    try:
        return _out(await _thread(work))
    except cli.CliError as e:
        if e.code == cli.EXIT_CHOICE:
            return {"needs_choice": True, "message": str(e), **cli._jsonable(e.data)}
        raise ToolError(str(e)) from e


@mcp.tool(annotations=WRITE)
async def import_cad_zip(zip_path: str, destination: Destination | None = None, library: str | None = None,
                         project: str | None = None, user_confirmed: bool = False, mpn: str | None = None,
                         manufacturer: str | None = None, lcsc: str | None = None, overwrite: bool = False,
                         ctx: Context | None = None) -> dict[str, Any]:
    """Import a KiCad CAD zip the user downloaded (SnapEDA, Ultra Librarian, SamacSys).

    Like import_part, the user chooses the project or a local library: ask them when this tool returns
    needs_confirmation, then call again with destination, library and user_confirmed=true."""
    from pathlib import Path
    argv = ["import-zip", zip_path]
    _opt(argv, "--project", project)
    _opt(argv, "--mpn", mpn)
    _opt(argv, "--manufacturer", manufacturer)
    _opt(argv, "--lcsc", lcsc)
    _opt(argv, "--overwrite", overwrite)
    kctx = _ctx(argv)
    if not Path(zip_path).expanduser().exists():
        raise ToolError(f"File not found: {zip_path}")
    chosen, answer = await _choose_target(ctx, kctx, Path(zip_path).name + (f" ({mpn})" if mpn else ""),
                                          destination, library, project, user_confirmed)
    if answer is not None:
        return answer
    kctx.args = argparse.Namespace(**{**vars(kctx.args), "into": chosen.kind, "lib": chosen.name})

    def work():
        return cli.cmd_import_zip(kctx)
    try:
        return _out(await _thread(work))
    except cli.CliError as e:
        raise ToolError(str(e)) from e


# ================================================================ tools: projects
@mcp.tool(annotations=READ)
def list_projects() -> dict[str, Any]:
    """KiCad version and the user's KiCad projects, the ones open in KiCad first."""
    return _run(["projects"])


@mcp.tool(annotations=READ)
def list_libraries(project: str | None = None) -> dict[str, Any]:
    """The project and local libraries parts can be imported into."""
    argv = ["libraries"]
    _opt(argv, "--project", project)
    return _run(argv)


@mcp.tool(annotations=READ)
def list_formats() -> dict[str, Any]:
    """BOM export formats, board-house profiles, suppliers (and whether they are connected) and CAD sources."""
    return _run(["formats"])


@mcp.tool(annotations=READ)
def check_setup(project: str | None = None) -> dict[str, Any]:
    """Check KiCad's paths and library tables for problems (nothing is changed)."""
    argv = ["check-setup"]
    _opt(argv, "--project", project)
    return _run(argv)


# ================================================================ tools: BOM & fabrication
@mcp.tool(annotations=READ)
def read_bom(project: str | None = None, check_stock: bool = False, boards: int = 1,
             sources: list[str] | None = None) -> dict[str, Any]:
    """A project's BOM grouped by value/footprint/MPN. check_stock: look up stock at the suppliers for
    `boards` boards (stock_status per line: ok, low, none, 'no MPN')."""
    argv = ["bom", "--boards", str(max(1, boards))]
    _opt(argv, "--project", project)
    _opt(argv, "--stock", check_stock)
    _opt(argv, "--source", sources)
    return _run(argv)


@mcp.tool(annotations=WRITE)
def export_bom(format: str = "JLCPCB", project: str | None = None, out_dir: str | None = None, boards: int = 1,
               file_type: Literal["csv", "xlsx"] = "csv", include_dnp: bool = False,
               placement_file: bool = True) -> dict[str, Any]:
    """Write the BOM (and the placement/CPL file when the format has one) for a board house or distributor.
    Formats: see list_formats. Files go to out_dir, or the project folder."""
    argv = ["bom-export", "--format", format, "--boards", str(max(1, boards)), "--file-type", file_type]
    _opt(argv, "--project", project)
    _opt(argv, "--out", out_dir)
    _opt(argv, "--include-dnp", include_dnp)
    _opt(argv, "--no-cpl", not placement_file)
    return _run(argv)


@mcp.tool(annotations=WRITE)
def generate_fabrication(project: str | None = None, profile: str | None = None, boards: int = 1,
                         refill_and_drc: bool = True, make_zip: bool = False) -> dict[str, Any]:
    """Write '<Project> Build' next to the project: Gerbers, drill, BOM, placement file, schematic PDF and a
    DRC report (zones are refilled on a copy; the board file is not changed). make_zip: also write
    '<Project> Build.zip'. profile: board house, see list_formats. Takes a minute or more."""
    argv = ["fab", "--boards", str(max(1, boards))]
    _opt(argv, "--project", project)
    _opt(argv, "--profile", profile)
    _opt(argv, "--no-drc", not refill_and_drc)
    _opt(argv, "--zip", make_zip)
    return _run(argv)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def update_pcb_from_schematic(project: str | None = None) -> dict[str, Any]:
    """Open 'Update PCB from Schematic' in the running KiCad PCB editor (brings new symbols onto the board).
    The user confirms the dialog in KiCad unless Kiporter is set to press Update itself."""
    argv = ["update-pcb"]
    _opt(argv, "--project", project)
    return _run(argv)


def serve() -> None:
    mcp.run("stdio")
