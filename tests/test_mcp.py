"""Offline tests for the MCP server's library confirmation (skipped when the optional `mcp` package is missing)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp.server.mcpserver")

from kiporter import cli, mcp_server  # noqa: E402
from kiporter.mcp_server import LibOption  # noqa: E402

OPTS = [LibOption("Project library 'Project_Parts' (in project demo)", "project", "Project_Parts", True),
        LibOption("Local library 'Kiporter' (shared by all projects)", "global", "Kiporter", True)]


@pytest.fixture
def kctx(monkeypatch):
    monkeypatch.setattr(mcp_server, "library_options", lambda *a: (list(OPTS), "demo"))
    ctx = cli.Ctx(cli.build_parser().parse_args(["import", "C7950"]))
    ctx.settings.default_target = "project"
    return ctx


class FakeMcpCtx:
    """Stands in for the MCP Context: a client that can show forms and answers with `answer`."""

    def __init__(self, answer):
        self.answer = answer
        self.client_capabilities = SimpleNamespace(elicitation=object())
        self.asked = None

    async def elicit(self, message, schema):
        self.asked = (message, schema.model_json_schema()["properties"]["library"])
        if self.answer is None:
            return SimpleNamespace(action="decline")
        return SimpleNamespace(action="accept", data=schema(library=self.answer))


def choose(ctx, kctx, destination=None, library=None, confirmed=False):
    return asyncio.run(mcp_server._choose_target(ctx, kctx, "LM358", destination, library, None, confirmed))


def test_without_forms_the_user_must_be_asked(kctx):
    chosen, answer = choose(None, kctx)
    assert chosen is None and answer["needs_confirmation"]
    assert {o["destination"] for o in answer["options"]} == {"project", "local"}
    # the assistant naming a destination is not enough: it has to say the user confirmed it
    chosen, answer = choose(None, kctx, destination="local")
    assert chosen is None and answer["needs_confirmation"]


def test_confirmed_answer_is_used(kctx):
    chosen, answer = choose(None, kctx, destination="local", confirmed=True)
    assert answer is None and (chosen.kind, chosen.name) == ("global", "Kiporter")
    chosen, _ = choose(None, kctx, destination="project", library="NewLib", confirmed=True)
    assert (chosen.kind, chosen.name) == ("project", "NewLib")


def test_confirmed_project_without_a_project_fails(kctx, monkeypatch):
    monkeypatch.setattr(mcp_server, "library_options", lambda *a: ([OPTS[1]], None))
    with pytest.raises(mcp_server.ToolError):
        choose(None, kctx, destination="project", confirmed=True)


def test_form_answer_wins_over_the_assistants_suggestion(kctx):
    ctx = FakeMcpCtx(OPTS[0].label)
    chosen, answer = choose(ctx, kctx, destination="local", confirmed=True)
    assert answer is None and chosen.kind == "project"
    assert set(ctx.asked[1]["enum"]) == {o.label for o in OPTS}
    assert ctx.asked[1]["default"] == OPTS[1].label  # the suggestion is only the pre-selected answer


def test_declined_form_cancels(kctx):
    chosen, answer = choose(FakeMcpCtx(None), kctx)
    assert chosen is None and answer["cancelled"]


def test_all_tools_registered():
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"search_parts", "import_part", "import_cad_zip", "read_bom", "generate_fabrication"} <= names
