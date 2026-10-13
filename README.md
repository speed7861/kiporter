<p align="center"><img src="kiporter/assets/logo.png" width="140" alt="Kiporter logo"></p>

# Kiporter

**A stand-alone KiCad companion for Windows, macOS and Linux.** Search parts at six suppliers,
preview them, and import the symbol, footprint and 3D model into your project or your own
libraries. Kiporter also keeps your libraries tidy, checks BOM stock and lead times, and
produces ready-to-order fabrication files. It runs outside KiCad (no plugin to start from the
PCB editor) and finds your open project automatically.

Inspired by [Import-LIB-KiCad-Plugin](https://github.com/Steffen-W/Import-LIB-KiCad-Plugin).

## Features

### Search & Import
- **One search, six suppliers:**
  - LCSC (no account needed)
  - Digi-Key
  - Mouser
  - Farnell / Newark / element14
  - TME
  - Octopart (via Nexar)
- **Same columns for every supplier:** stock quantity, lead time, price, MOQ, package and
  supplier part number. Results are **sorted by stock** (most available first), and an
  *in stock only* filter is available.
- **CAD column and filter:** shows whether a symbol, footprint and 3D model are available.
  You can show only parts with a **symbol + footprint**, or **symbol + footprint + 3D model**.
- **Previews:** part photo, the **symbol** and **footprint** drawn in the app, and a **3D render**
  produced by KiCad itself.
- **One-click import:**
  - LCSC parts are converted from EasyEDA.
  - For other suppliers, Kiporter matches the MPN to LCSC. If there's no match, it opens
    SnapEDA / Ultra Librarian / SamacSys and **auto-imports the ZIP you download**.
- **Consistent symbol fields across all sources:** Manufacturer, MPN, Package, Datasheet,
  Description and each supplier's part number. Vendor spellings such as `MFGR` or
  `Manufacturer_Name` are folded into these names.
- **Choose the destination library at start-up:** the project library, one of your existing
  local libraries, or a new one. Kiporter registers it in KiCad's library tables and links
  the 3D paths automatically.
- **Update PCB from Schematic** button (uses the KiCad API, or presses the F8 shortcut as a fallback).

### BOM
- **View the project BOM:** references, quantities, SMD/THT type, MPNs, supplier part numbers
  and DNP status.
- **Stock & lead-time check** at every connected supplier, for the number of boards you plan
  to build.
- **Export for board houses:** JLCPCB, PCBWay, ALLPCB, Seeed Fusion, NextPCB, Eurocircuits,
  Aisler, MacroFab, with pick-and-place/CPL files.
- **Export for distributor BOM tools:** Digi-Key, Mouser and LCSC.
- Exports as CSV or Excel.

### Fabrication
One click creates a `<Project> Build` folder containing:
- the schematic PDF, the BOM and a DRC report;
- a `Gerbers` sub-folder with Gerbers, drill files and the placement file, set up for the
  board house you pick;
- an upload ZIP.

Zones are refilled and DRC is run on a copy first, so your board file is never changed.

### Library Tools
- **Clean up & auto-link:** links symbols to footprints and footprints to 3D models, repairs
  broken or legacy `${KISYS3DMOD}` paths, normalizes fields and fills missing data.
- **Merge libraries** (including 3D models), and optionally re-point a project at the merged library.
- **Make project self-contained:** copies every part a project uses into its project library.
- **Browse & preview** any library.
- **KiCad setup check:** finds hidden stock libraries, broken library-table entries and a
  disabled API.
- **Back up a library** to a ZIP file.

## Install

You need **KiCad 8 or newer** (KiCad 9+ is required for 3D previews) and an internet connection.

| OS | How |
|---|---|
| **Windows** | Download or clone this repo and double-click **`Install.bat`**. It installs Python if needed, creates the Desktop and Start Menu shortcuts, and starts Kiporter. |
| **macOS** | In Terminal: `./install.sh`. This creates `~/Applications/Kiporter.app`. It needs Python 3.9+ with Tk; the script offers to install it with Homebrew. |
| **Linux** | In a terminal: `./install.sh`. This adds Kiporter to the app menu and a `kiporter` command. On Ubuntu/Debian it offers to install `python3-venv python3-tk`. Optionally install `xdotool wmctrl`. |

Kiporter works with standard KiCad installs:
- Windows: `Program Files` or per-user installs
- macOS: `/Applications/KiCad`
- Linux: distribution packages and the Flatpak

On first start, Kiporter creates and registers a default local library, enables KiCad's API,
and offers to connect suppliers.

## Supplier API keys

LCSC works immediately. Digi-Key, Mouser, Farnell/Newark, TME and Octopart each give out
**free API keys**: you sign in with your normal account and create a key. Step-by-step
directions are in **Settings → Suppliers** inside the app, and in
**[docs/API_KEYS.md](docs/API_KEYS.md)**. Keys are stored in your system's credential store
(Windows Credential Manager, macOS Keychain or Linux Secret Service).

## Notes
- After a *new* library is registered, restart KiCad once (local library) or reopen the
  project (project library). Later imports show up without restarting.
- Board houses update their BOM/CPL templates from time to time. Check the preview in
  Kiporter's export dialog and the shop's own page before ordering.
- On macOS, the Update-PCB keyboard fallback needs Accessibility permission. On Linux/X11 it
  needs `xdotool`.

## Development

```bash
python -m pip install -r requirements.txt pytest
python -m pytest tests        # offline tests, run on Windows/macOS/Linux in CI
python run.pyw                # start the app
```

| Module | Purpose |
|---|---|
| `kiporter/gui.py` | Main window, Search & Import tab |
| `kiporter/settings_gui.py` | Settings tab, supplier accounts and directions |
| `kiporter/tools_gui.py` | BOM tab, Library Tools, library browser |
| `kiporter/fab.py`, `fab_gui.py` | Fabrication outputs (Build folder, Gerbers, ZIP) |
| `kiporter/providers.py` | Supplier search (normalized results) |
| `kiporter/cad.py`, `render.py` | CAD availability, previews (canvas + kicad-cli 3D) |
| `kiporter/bom.py` | BOM reading and board-house / distributor formats |
| `kiporter/library.py`, `libtools.py`, `fields.py` | Library install, maintenance, field rules |
| `kiporter/kicad_env.py`, `kicad_control.py` | KiCad discovery (all OSes), lib tables, Update PCB |
