# KiCad Part Importer

A stand-alone desktop app, similar to
[Import-LIB-KiCad-Plugin](https://github.com/Steffen-W/Import-LIB-KiCad-Plugin), that runs
outside KiCad. You don't need to open it from the PCB editor.

## Search & Import

* **One search box covers six suppliers:**
  * LCSC (no account needed)
  * Digi-Key
  * Mouser
  * Farnell, Newark or element14
  * TME
  * Octopart (through Nexar)
* **Every source fills the same columns:** stock quantity, lead time, unit price, MOQ,
  package and supplier part number. Results are **sorted by quantity in stock** (click any
  column header to re-sort). Out-of-stock parts are greyed out, and an "In stock only"
  filter is available.
* **Import with one click:** double-click a result.
  * LCSC parts are converted from EasyEDA, including the 3D model.
  * For other suppliers, the app first looks for the same MPN at LCSC (free model). If there
    isn't one, it opens SnapEDA, Ultra Librarian or SamacSys and **auto-imports the ZIP you
    download** into your Downloads folder.
* **Consistent part fields.** Every imported symbol gets the same fields, in the same order:
  * Datasheet, Description, Manufacturer, MPN, Package
  * `LCSC Part`, `Digi-Key PN`, `Mouser PN`, `Farnell PN`, `TME PN`
  * Vendor spellings (such as `MFGR`, `Manufacturer_Name`, `Manufacturer_Part_Number` or
    `Mouser Part Number`) are folded into these names.
  * Stale price/stock links are removed.
  * You can rename the fields in Settings.
* **Choose the destination at start-up:** you're asked which library to import into. Pick a
  project library or any of your **local libraries**: the default `PartImporter`, your
  existing libraries (for example EasyEDA, Snapeda, PLEKTRON), or a new one. You can change
  it at any time in the "Import into" panel.
* **Project detection and Update PCB:** the app detects the open KiCad project. The **Update
  PCB from Schematic** button works through the KiCad API, or falls back to pressing F8 in
  the editor.

## Library Tools tab

| Tool | What it does |
|---|---|
| **Clean up & auto-link** | Links symbols to footprints (by name or package), relinks wrong library prefixes, attaches 3D models to footprints and repairs broken or legacy `${KISYS3DMOD}` paths (also to KiCad's stock models). Normalizes field names, can fill missing manufacturer, datasheet and LCSC data from LCSC, and lists unused footprints and 3D models. Every fix can be selected individually, and a backup is made first. |
| **Merge libraries** | Combines several libraries, with their footprints and 3D models, into a new or existing library. Name clashes are renamed, kept or overwritten, and footprint and 3D links are rewritten. Optionally removes the old libraries from KiCad's tables and re-points your project at the merged library. |
| **Make project self-contained** | Copies every symbol, footprint and 3D model a project uses into its project library and updates the schematic and PCB references (backup first; the project must be closed). |
| **KiCad setup check** | Finds path variables that hide KiCad's stock libraries or 3D models, library-table entries pointing at missing files, and a disabled IPC API. |
| **Back up a library** | Saves a library's symbols, footprints and 3D models to one ZIP file. |

## BOM Stock Check tab

Reads the selected project's BOM using `kicad-cli`. For each line it shows stock and lead
time at every enabled supplier, the cheapest source with enough stock, and a status for the
number of boards you plan to build. Lines are colored green (in stock), orange (short) or
red (unavailable). You can export the result to CSV, and double-click a line to search for it.

## Install (one time)

1. Double-click **`Install.bat`**. It installs Python if needed and creates a private
   environment in `%LOCALAPPDATA%\KiCadPartImporter`. It adds Desktop and Start Menu
   shortcuts, then starts the app.
2. On first start, the app creates and registers the default local library, enables KiCad's
   IPC API and offers to **connect suppliers**. Each supplier tab explains how to get the
   free API key by signing in with your normal account. The **Test connection** button
   checks it.
   * Keys are stored in the Windows Credential Manager, not in a file.
   * If you search with a supplier that isn't connected yet, the app asks whether you want to
     connect it now.

After a new library is registered for the first time, restart KiCad (local library) or reopen
the project (project library) once.

## Files

| File | Purpose |
|---|---|
| `Install.bat` / `install.ps1` | One-click installer and shortcut creator |
| `run.pyw` | Launcher (no console window) |
| `partimporter/gui.py` | Main window, destination prompt, supplier and settings dialogs |
| `partimporter/tools_gui.py` | Library Tools and BOM Stock Check tabs |
| `partimporter/providers.py` | LCSC, Digi-Key, Mouser, Farnell/Newark, TME and Octopart search (normalized results) |
| `partimporter/fields.py` | Canonical symbol fields, aliases, field order |
| `partimporter/importers.py` | EasyEDA conversion and vendor ZIP extraction |
| `partimporter/library.py` | Library targets, registration, installing parts |
| `partimporter/libtools.py` | Clean-up, merge, remap, localize, BOM check, setup check, backup |
| `partimporter/kicad_env.py` | KiCad install, path variables, library tables, open-project detection |
| `partimporter/kicad_control.py` | Update PCB (IPC API or hotkey), opening projects |
