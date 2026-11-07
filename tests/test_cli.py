"""Offline tests for the command line (no network, no KiCad needed)."""
from __future__ import annotations

import json

import pytest

from kiporter import cli
from kiporter.providers import PartResult


class FakeProvider:
    key_fields = ()
    configured = True

    def __init__(self, results):
        self.results = results

    def search(self, keyword, limit=50):
        return list(self.results)


def ctx_with(results, argv=("search", "x")):
    ctx = cli.Ctx(cli.build_parser().parse_args(list(argv)))
    ctx._providers = {"LCSC": FakeProvider([r for r in results if r.source == "LCSC"]),
                      "Mouser": FakeProvider([r for r in results if r.source == "Mouser"])}
    return ctx


PARTS = [
    PartResult("Mouser", "LM358DR", "TI", stock=900, supplier_pn="595-LM358DR"),
    PartResult("LCSC", "LM358DR", "TI", stock=100, supplier_pn="C5423", lcsc_id="C5423"),
    PartResult("LCSC", "LM358DR2G", "onsemi", stock=500000, supplier_pn="C7950", lcsc_id="C7950"),
]


def test_parser_knows_every_command():
    ap = cli.build_parser()
    for argv in (["search", "LM358", "--json", "-s", "LCSC"], ["cad", "C7950"], ["import", "C7950", "--into", "global"],
                 ["import-zip", "x.zip"], ["projects"], ["libraries"], ["check-setup"], ["formats"],
                 ["bom", "--stock"], ["bom-export", "-f", "JLCPCB"], ["fab", "--zip"], ["update-pcb"]):
        assert callable(ap.parse_args(argv).fn)


def test_exact_match_prefers_lcsc_and_never_takes_a_prefix():
    ctx = ctx_with(PARTS)
    r = cli.resolve_part(ctx, "lm358dr", ["LCSC", "Mouser"], first=False)
    assert (r.source, r.supplier_pn) == ("LCSC", "C5423")  # exact MPN, LCSC first even with less stock
    with pytest.raises(cli.CliError) as e:  # LM358 must not silently become LM358DR / LM358DR2G
        cli.resolve_part(ctx, "LM358", ["LCSC", "Mouser"], first=False)
    assert e.value.code == cli.EXIT_CHOICE and len(e.value.data["candidates"]) == 3
    assert cli.resolve_part(ctx, "LM358", ["LCSC", "Mouser"], first=True).mpn == "LM358DR2G"  # most stock


def test_supplier_part_number_matches_too():
    r = cli.resolve_part(ctx_with(PARTS), "595-LM358DR", ["Mouser"], first=False)
    assert r.source == "Mouser"


def test_source_names_are_forgiving_and_checked():
    ctx = ctx_with(PARTS)
    assert ctx.sources(["lcsc", "mous"]) == ["LCSC", "Mouser"]
    with pytest.raises(cli.CliError):
        ctx.sources(["NoSuchShop"])


def test_part_dict_is_json_ready():
    r = PartResult("LCSC", "X", stock=5, price=0.1)
    r.cad, r.cad_source = (True, True, False), "EasyEDA"
    d = json.loads(json.dumps(cli.part_dict(r)))
    assert d["cad"] == {"symbol": True, "footprint": True, "model_3d": False, "source": "EasyEDA"}


def test_json_error_and_exit_code(capsys):
    code = cli.main(["search", "LM358", "-s", "NoSuchShop", "--json"])
    assert code == cli.EXIT_USAGE
    assert "NoSuchShop" in json.loads(capsys.readouterr().out)["error"]


def test_table_keeps_last_column_whole():
    long = "x" * 120
    assert long in cli._table([["a", long]], ["A", "Path"])


# ---------------------------------------------------------------- project or local library
OPTS = [cli.LibOption("Project library 'Project_Parts' (in project demo)", "project", "Project_Parts", True),
        cli.LibOption("Local library 'Kiporter' (shared by all projects)", "global", "Kiporter", True)]


class FakeTTY:
    def __init__(self, answers=""):
        self.lines = answers.splitlines(keepends=True)

    def isatty(self):
        return True

    def readline(self):
        return self.lines.pop(0) if self.lines else ""

    def write(self, s):
        pass

    def flush(self):
        pass


@pytest.fixture
def import_ctx(monkeypatch):
    def make(*argv):
        ctx = cli.Ctx(cli.build_parser().parse_args(["import", "C7950", *argv]))
        ctx.settings.default_target = "project"
        monkeypatch.setattr(cli, "library_options", lambda *a: (list(OPTS), "demo"))
        ctx.target = lambda kind, name, project: ({"local": "global"}.get(kind, kind), name)
        return ctx
    return make


def test_import_without_a_terminal_asks_instead_of_guessing(import_ctx):
    with pytest.raises(cli.CliError) as e:
        cli.choose_target(import_ctx("--json"), "LM358")
    assert e.value.code == cli.EXIT_CHOICE and e.value.data["needs_confirmation"]
    assert [o["destination"] for o in e.value.data["options"]] == ["project", "local"]
    assert e.value.data["suggested"] == {"destination": "project", "library": "Project_Parts"}


def test_into_and_unique_lib_need_no_question(import_ctx):
    assert cli.choose_target(import_ctx("--into", "local"), "x") == ("global", None)
    assert cli.choose_target(import_ctx("--lib", "kiporter"), "x") == ("global", "Kiporter")


@pytest.mark.parametrize("typed, expected", [("2\n", ("global", "Kiporter")), ("\n", ("project", "Project_Parts")),
                                             ("9\nx\n1\n", ("project", "Project_Parts"))])
def test_terminal_prompt(import_ctx, monkeypatch, typed, expected):
    monkeypatch.setattr(cli.sys, "stdin", FakeTTY(typed))
    monkeypatch.setattr(cli.sys, "stderr", FakeTTY())
    assert cli.choose_target(import_ctx(), "LM358") == expected


def test_terminal_prompt_cancel(import_ctx, monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", FakeTTY("q\n"))
    monkeypatch.setattr(cli.sys, "stderr", FakeTTY())
    with pytest.raises(cli.CliError, match="Cancelled"):
        cli.choose_target(import_ctx(), "LM358")
