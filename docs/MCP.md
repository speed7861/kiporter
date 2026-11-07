# Kiporter MCP server

Kiporter can run as an [MCP](https://modelcontextprotocol.io) server, so AI apps such as
Claude Code and Claude Desktop can search parts, import them into KiCad, read BOMs and make
fabrication files as built-in tools, without going through the window.

It uses the same settings, supplier API keys and libraries as the Kiporter app. Anything you
set up under **Settings** in the app applies here too.

## 1. Install the MCP package (once)

The server needs the optional `mcp` package (version 2, Python 3.10 or newer), installed into
Kiporter's own Python:

| OS | Command |
|---|---|
| Windows (PowerShell) | `& "$env:LOCALAPPDATA\Kiporter\venv\Scripts\python.exe" -m pip install "mcp>=2,<3"` |
| macOS | `~/Library/Application\ Support/Kiporter/venv/bin/python -m pip install "mcp>=2,<3"` |
| Linux | `~/.local/share/kiporter/venv/bin/python -m pip install "mcp>=2,<3"` |

Running the installer again (Install.bat / install.sh) does not remove it.

## 2. Connect it to your AI app

The server is started with `run.pyw mcp`, using Kiporter's Python. In the examples below,
replace `C:\path\to\kiporter` with the folder you unzipped Kiporter into.

### Claude Code

Windows (PowerShell):

```powershell
claude mcp add kiporter --scope user -- "$env:LOCALAPPDATA\Kiporter\venv\Scripts\python.exe" "C:\path\to\kiporter\run.pyw" mcp
```

macOS / Linux (the installer's `kiporter` command):

```bash
claude mcp add kiporter --scope user -- kiporter mcp
```

On macOS the command lives at `~/Library/Application Support/Kiporter/kiporter`. Use that full
path if `kiporter` is not on your `PATH`.

Check it with `claude mcp list`, or `/mcp` inside Claude Code.

### Claude Desktop

Open **Settings → Developer → Edit Config** and add Kiporter to `claude_desktop_config.json`
(Windows example; use the full paths on your computer):

```json
{
  "mcpServers": {
    "kiporter": {
      "command": "C:\\Users\\you\\AppData\\Local\\Kiporter\\venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\kiporter\\run.pyw", "mcp"]
    }
  }
}
```

Restart Claude Desktop afterwards.

### Other MCP apps

Any app that can start a stdio MCP server works: the command is Kiporter's Python with the
arguments `run.pyw mcp` (or `kiporter mcp` on macOS/Linux).

## Choosing the project or a local library

Kiporter **always asks you** where an imported part goes. The AI does not decide this:

- **Project library:** stored inside the project folder, so it travels with the project.
- **Local library:** one library on this computer, shared by all your projects.

How you are asked depends on the app:

- **Apps that support MCP forms (elicitation):** a small form opens with a list of the
  project's libraries and your local libraries. The AI's suggestion is pre-selected, but your
  choice is the one used. Choosing **Decline** or **Cancel** cancels the import, and nothing is
  changed.
- **Other apps:** Kiporter imports nothing yet. It returns the question and the list of
  libraries, and the AI asks you in the chat. It can only import after it calls again with
  your answer and states that you confirmed it (`user_confirmed`).

This applies to both `import_part` and `import_cad_zip`. If you pick the project library but
no project is open in KiCad (and none was named), the import stops with an error instead of
using a local library.

## Tools

| Tool | What it does | Changes files? |
|---|---|---|
| `search_parts` | Search the suppliers by part number or keywords; optionally check CAD availability | no |
| `find_cad_models` | Which CAD models (symbol, footprint, 3D) exist for one part | no |
| `import_part` | Import a part's symbol, footprint and 3D model (**asks project or local**) | yes |
| `import_cad_zip` | Import a CAD zip downloaded from SnapEDA, Ultra Librarian or SamacSys (**asks project or local**) | yes |
| `list_projects` | KiCad version and your projects, open ones first | no |
| `list_libraries` | The project and local libraries parts can go into | no |
| `list_formats` | BOM formats, board houses, suppliers (connected or not), CAD sources | no |
| `check_setup` | Check KiCad paths and library tables for problems | no |
| `read_bom` | The project's BOM, optionally with stock checked for N boards | no |
| `export_bom` | BOM (and CPL placement file) for a board house or distributor | yes (writes export files) |
| `generate_fabrication` | `<Project> Build` folder: Gerbers, drill, BOM, CPL, PDF, DRC; optional zip | yes (writes the Build folder) |
| `update_pcb_from_schematic` | Opens *Update PCB from Schematic* in the running KiCad | KiCad dialog |

Tools that need a project use the one open in KiCad, unless a `project` (a `.kicad_pro` file
or its folder) is given.

`import_part` takes an LCSC number (`C7950`) or an **exact** manufacturer part number. If the
query matches several parts (for example `LM35` matches LM35DZ, LM35CAZ and more), nothing is
imported. The tool returns the candidates (`needs_choice`) so the right one can be picked.

After the first import into a **new** library, restart KiCad (local library) or reopen the
project (project library) once so KiCad loads it.

## Example requests

- "Find an LM358 in SOIC-8 with good stock at LCSC and import it."
- "Check stock for my open project's BOM for 10 boards. Which lines are short?"
- "Export the BOM and CPL for JLCPCB, then make the fabrication zip."

## Troubleshooting

- **"The MCP server needs the 'mcp' package":** do step 1. It needs Python 3.10 or newer.
- **The app shows no Kiporter tools:** run the command from step 2 in a terminal, then add
  `--help` after `mcp`. If it prints the help text, the paths are right. Restart the AI app
  after any configuration change.
- **A supplier is missing from searches:** connect it in the Kiporter app under
  **Settings → Suppliers**. `list_formats` shows which suppliers are connected.
- **Without MCP:** the same jobs are available as a command line (`kiporter --help`), which also asks project or local before importing. See
  *Command line* in the [README](../README.md).
