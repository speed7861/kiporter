"""Settings tab, supplier accounts panel (with API-key directions) and the connect-supplier dialog."""
from __future__ import annotations

import tkinter as tk
import webbrowser
from dataclasses import fields as dc_fields
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

from . import __version__, bom, fields, guides, kicad_control, kicad_env, providers, ui
from .config import SETTINGS_FILE, Settings, app_data_dir

if TYPE_CHECKING:
    from .gui import App

APP_TITLE = "Kiporter"
REPO_URL = "https://github.com/speed7861/kiporter"
HOWTO_URL = REPO_URL + "#how-to-use-kiporter"
CONTACT_EMAIL = "speed7861@gmail.com"

SUPPLIER_FIELDS = {
    "LCSC": [],
    "Digi-Key": [("digikey_client_id", "Client ID"), ("digikey_client_secret", "Client Secret"),
                 ("digikey_locale_site", "Locale site (US, CA, UK, DE…)"),
                 ("digikey_currency", "Currency (USD, CAD, EUR…)")],
    "Mouser": [("mouser_api_key", "Search API key")],
    "Farnell/Newark": [("element14_api_key", "API key"),
                       ("element14_store", "Store (www.newark.com, uk.farnell.com, …)")],
    "TME": [("tme_token", "Token"), ("tme_app_secret", "App secret"), ("tme_country", "Country (US, PL…)"),
            ("tme_currency", "Currency")],
    "Octopart": [("nexar_client_id", "Client ID"), ("nexar_client_secret", "Client Secret")],
}


def _is_secret(key: str) -> bool:
    return any(x in key for x in ("secret", "api_key", "token"))


# ==================================================================== suppliers
class SuppliersPanel(ttk.Notebook):
    """One page per supplier: use-in-search switch, credentials (Enter saves + checks them) and directions."""

    def __init__(self, parent, app: "App", focus: str | None = None):
        super().__init__(parent)
        self.app = app
        self.vars: dict[str, tk.StringVar] = {}
        self.enabled: dict[str, tk.BooleanVar] = {}
        self.status: dict[str, tk.StringVar] = {}
        self.status_labels: dict[str, ttk.Label] = {}
        s = app.settings
        for name, flds in SUPPLIER_FIELDS.items():
            prov = app.providers[name]
            page = ttk.Frame(self, padding=10)
            self.add(page, text=f" {name} ")
            top = ttk.Frame(page)
            top.pack(fill="x")
            ev = tk.BooleanVar(value=getattr(s, providers.ENABLE_FLAGS[name]))
            self.enabled[name] = ev
            ttk.Checkbutton(top, text=f"Search {name}", variable=ev).pack(side="left")
            st = tk.StringVar()
            self.status[name] = st
            lab = ttk.Label(top, textvariable=st)
            lab.pack(side="left", padx=12)
            self.status_labels[name] = lab
            g = guides.GUIDES.get(name, {})
            if g.get("portal"):
                ttk.Button(top, text="Open sign-in page…",
                           command=lambda u=g["portal"]: webbrowser.open(u)).pack(side="right", padx=4)
            grid = ttk.Frame(page)
            grid.pack(fill="x", pady=8)
            for r, (key, label) in enumerate(flds):
                ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=2)
                v = tk.StringVar(value=str(getattr(s, key)))
                self.vars[key] = v
                e = ttk.Entry(grid, textvariable=v, width=56, show="•" if _is_secret(key) else "")
                e.grid(row=r, column=1, sticky="ew", pady=2)
                e.bind("<Return>", lambda ev, n=name: self.enter(n))
                v.trace_add("write", lambda *a, n=name: self._typed(n))
                if _is_secret(key):
                    sv = tk.BooleanVar(value=False)
                    ttk.Checkbutton(grid, text="show", variable=sv, command=lambda e=e, sv=sv: e.configure(
                        show="" if sv.get() else "•")).grid(row=r, column=2, padx=4)
            if flds:
                ttk.Button(grid, text="Enter", style="Accent.TButton", width=10,
                           command=lambda n=name: self.enter(n)).grid(row=0, column=3, sticky="n", padx=(8, 0), pady=2)
            grid.columnconfigure(1, weight=1)
            ttk.Label(page, text="How to get the key", style="Bold.TLabel").pack(anchor="w")
            txt = ui.scrolled_text(page, height=9, wrap="word", relief="flat", font=ui.FONTS.get("normal"))
            txt.insert("1.0", guides.guide_text(name))
            txt.configure(state="disabled")
            txt.frame.pack(fill="both", expand=True, pady=(2, 4))
            ttk.Button(page, text="Full guide with all suppliers (web)…",
                       command=lambda: webbrowser.open(guides.GUIDE_URL)).pack(anchor="e")
            if name == focus:
                self.select(page)
            del prov
        self.refresh_status()
        note = ("Keys are stored in your system's credential store." if s.secrets_in_keyring
                else "Keys are stored in the Kiporter settings file.")
        self.note = note

    def _set_status(self, name: str, text: str, style: str) -> None:
        self.status[name].set(text)
        self.status_labels[name].configure(style=style)

    def _keys_filled(self, name: str) -> bool:
        prov = self.app.providers[name]
        return all(self.vars[k].get().strip() for k in prov.key_fields)

    def refresh_status(self) -> None:
        for name in self.status:
            prov = self.app.providers[name]
            if not prov.key_fields:
                self._set_status(name, "✔ No account needed", "Good.TLabel")
            elif prov.configured and self._keys_filled(name):
                self._set_status(name, "✔ Connected", "Good.TLabel")
            else:
                self._set_status(name, "Not connected", "Bad.TLabel")

    def _typed(self, name: str) -> None:
        """Live status while typing: red until every key field is filled."""
        if not self._keys_filled(name):
            self._set_status(name, "Not connected", "Bad.TLabel")
        else:
            self._set_status(name, "Press Enter to save and check the key", "Busy.TLabel")

    def apply(self) -> None:
        s = self.app.settings
        for k, v in self.vars.items():
            setattr(s, k, v.get().strip())
        for name, v in self.enabled.items():
            setattr(s, providers.ENABLE_FLAGS[name], v.get())
            if self.app.providers[name].configured and name in s.declined_connect:
                s.declined_connect.remove(name)
        for prov in self.app.providers.values():
            if hasattr(prov, "reset"):
                prov.reset()
        self.refresh_status()

    def enter(self, name: str) -> None:
        """Save the key(s), update the Search tab straight away, then check them with the supplier."""
        self.apply()
        self.app.settings.save()
        self.app.apply_settings()
        if not self._keys_filled(name):
            self._set_status(name, "Not connected", "Bad.TLabel")
            return
        self._set_status(name, "Checking the key…", "Busy.TLabel")
        prov = self.app.providers[name]
        self.app.run_bg(prov.test,
                        lambda msg: self._set_status(name, "✔ Connected — " + msg, "Good.TLabel"),
                        lambda e: self._set_status(name, "✖ Key not accepted: " + str(e)[:120], "Bad.TLabel"))


class AccountsDialog(tk.Toplevel):
    """Pop-up used when a search needs a supplier that is not connected yet."""

    def __init__(self, app: "App", focus: str | None = None, wait: bool = False):
        super().__init__(app)
        self.app = app
        self.title("Connect suppliers")
        self.geometry("760x560")
        self.transient(app)
        self.panel = SuppliersPanel(self, app, focus)
        self.panel.pack(fill="both", expand=True, padx=10, pady=10)
        foot = ttk.Frame(self)
        foot.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(foot, text=self.panel.note, style="Muted.TLabel").pack(side="left")
        ttk.Button(foot, text="Close", command=self._close).pack(side="right")
        ttk.Button(foot, text="Save", command=self._save, style="Accent.TButton").pack(side="right", padx=6)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grab_set()
        if wait:
            self.wait_window()

    def _save(self) -> None:
        self.panel.apply()
        self.app.settings.save()
        self.app.apply_settings()
        self.app.log("Supplier settings saved.")

    def _close(self) -> None:
        self._save()
        self.destroy()


# ==================================================================== settings tab
class SettingsTab(ttk.Frame):
    def __init__(self, parent, app: "App"):
        super().__init__(parent, padding=8)
        self.app = app
        self.vars: dict[str, tk.Variable] = {}
        self.types: dict[str, type] = {}
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Settings", style="Title.TLabel").pack(side="left")
        self.dirty = tk.StringVar()
        ttk.Label(top, textvariable=self.dirty, foreground="#b3261e").pack(side="left", padx=12)
        ttk.Button(top, text="Save", command=self.save, style="Accent.TButton").pack(side="right")
        ttk.Button(top, text="Revert", command=self.revert).pack(side="right", padx=4)
        ttk.Button(top, text="Reset to defaults", command=self.reset_defaults).pack(side="right")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, pady=(8, 0))
        self._page_general()
        self._page_libraries()
        self._page_fields()
        self._page_search()
        self.suppliers = SuppliersPanel(self._page("Suppliers", padding=4), app)
        self.suppliers.pack(fill="both", expand=True)
        ttk.Label(self.pages["Suppliers"], text=self.suppliers.note, style="Muted.TLabel").pack(anchor="w")
        self._page_cad_sources()
        self._page_kicad()
        self._page_bom()
        self._page_about()
        self.load_from(app.settings)

    # ---------------------------------------------------------------- page helpers
    pages: dict[str, ttk.Frame]

    def _page(self, title: str, padding: int = 12) -> ttk.Frame:
        if not hasattr(self, "pages"):
            self.pages = {}
        f = ttk.Frame(self.nb, padding=padding)
        self.nb.add(f, text=f" {title} ")
        self.pages[title] = f
        f.columnconfigure(1, weight=1)
        f._row = 0  # type: ignore[attr-defined]
        return f

    def _next(self, page) -> int:
        r = page._row
        page._row += 1
        return r

    def section(self, page, text: str) -> None:
        r = self._next(page)
        ttk.Label(page, text=text, style="Bold.TLabel").grid(row=r, column=0, columnspan=3, sticky="w",
                                                             pady=(10 if r else 0, 2))

    def note(self, page, text: str) -> None:
        r = self._next(page)
        ttk.Label(page, text=text, style="Muted.TLabel", wraplength=760, justify="left").grid(
            row=r, column=0, columnspan=3, sticky="w", padx=(14, 0))

    def row(self, page, attr: str, label: str, kind: str = "str", choices: list | None = None,
            width: int = 50) -> None:
        r = self._next(page)
        default = getattr(Settings.defaults(), attr)
        typ = type(default)
        self.types[attr] = typ
        if kind == "bool":
            v = tk.BooleanVar()
            ttk.Checkbutton(page, text=label, variable=v).grid(row=r, column=0, columnspan=3, sticky="w", padx=(14, 0))
        else:
            ttk.Label(page, text=label).grid(row=r, column=0, sticky="w", padx=(14, 8), pady=2)
            v = tk.StringVar()
            if kind == "choice":
                w = ttk.Combobox(page, textvariable=v, values=[c[1] if isinstance(c, tuple) else c for c in choices or []],
                                 state="readonly", width=width - 4)
                self._choice_maps = getattr(self, "_choice_maps", {})
                self._choice_maps[attr] = [c if isinstance(c, tuple) else (c, c) for c in choices or []]
            else:
                w = ttk.Entry(page, textvariable=v, width=width)
            w.grid(row=r, column=1, sticky="w" if kind == "int" else "ew", pady=2)
            if kind in ("dir", "file"):
                ttk.Button(page, text="…", width=3, command=lambda: self._browse(v, kind)).grid(
                    row=r, column=2, sticky="w", padx=4)
        v.trace_add("write", lambda *a: self.dirty.set("● unsaved changes"))
        self.vars[attr] = v

    def _browse(self, v: tk.StringVar, kind: str) -> None:
        if kind == "dir":
            d = filedialog.askdirectory(initialdir=v.get() or str(Path.home()))
        else:
            d = filedialog.askopenfilename(initialdir=str(Path(v.get()).parent) if v.get() else str(Path.home()))
        if d:
            v.set(str(Path(d)))

    # ---------------------------------------------------------------- pages
    def _page_general(self) -> None:
        p = self._page("General")
        self.section(p, "Start-up")
        self.row(p, "ask_target_on_startup", "Ask which library to import into when Kiporter starts", "bool")
        self.row(p, "default_target", "Default import destination", "choice",
                 [("project", "Project library"), ("global", "Local library")])
        self.row(p, "auto_refresh_s", "Re-detect the open KiCad project every (seconds)", "int", width=8)
        self.section(p, "Downloads")
        self.row(p, "watch_downloads", "Auto-import CAD zips (SnapEDA, Ultra Librarian, SamacSys) that I download",
                 "bool")
        self.row(p, "watch_dir", "Folder to watch", "dir")
        self.section(p, "Appearance (applies after restarting Kiporter)")
        self.row(p, "ui_scale", "UI scaling factor (0 = system default, e.g. 1.5)", width=8)
        self.row(p, "font_size", "Font size in points (0 = system default)", "int", width=8)

    def _page_libraries(self) -> None:
        p = self._page("Libraries")
        self.section(p, "Default local library (shared by all projects)")
        self.row(p, "global_lib_dir", "Folder", "dir")
        self.row(p, "global_lib_name", "Library nickname")
        self.section(p, "Default project library")
        self.row(p, "project_lib_subdir", "Sub-folder inside the project")
        self.row(p, "project_lib_name", "Library nickname")
        self.section(p, "What to import")
        self.row(p, "import_symbol", "Symbol", "bool")
        self.row(p, "import_footprint", "Footprint", "bool")
        self.row(p, "import_3d", "3D model", "bool")
        self.row(p, "overwrite_existing", "Overwrite parts that already exist in the library", "bool")
        r = self._next(p)
        bf = ttk.Frame(p)
        bf.grid(row=r, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(bf, text="Re-run automatic setup", command=self._rerun_setup).pack(side="left")
        ttk.Button(bf, text="Open default library folder", command=self._open_lib_dir).pack(side="left", padx=6)

    def _page_fields(self) -> None:
        p = self._page("Symbol fields")
        self.note(p, "Every imported part gets these fields, whatever the supplier, in this order. Supplier "
                     "spellings (MFGR, Manufacturer_Name, Mouser Part Number, …) are folded into them.")
        labels = {"manufacturer": "Manufacturer", "mpn": "Manufacturer part number", "description": "Description",
                  "datasheet": "Datasheet", "package": "Package", "lcsc": "LCSC part #",
                  "digikey": "Digi-Key part #", "mouser": "Mouser part #", "farnell": "Farnell/Newark part #",
                  "tme": "TME part #"}
        self.field_vars: dict[str, tk.StringVar] = {}
        for key in fields.FIELD_ORDER:
            r = self._next(p)
            ttk.Label(p, text=labels[key]).grid(row=r, column=0, sticky="w", padx=(14, 8), pady=1)
            v = tk.StringVar()
            v.trace_add("write", lambda *a: self.dirty.set("● unsaved changes"))
            ttk.Entry(p, textvariable=v, width=28).grid(row=r, column=1, sticky="w")
            self.field_vars[key] = v
        self.row(p, "drop_junk_fields", "Remove stale vendor fields (price/stock links, SnapEDA/Arrow links)", "bool")

    def _page_search(self) -> None:
        p = self._page("Search")
        self.row(p, "search_limit", "Results per supplier (max 50)", "int", width=8)
        self.row(p, "in_stock_only", "Show in-stock parts only", "bool")
        self.section(p, "CAD models")
        self.row(p, "check_cad_all", "Check CAD for every result automatically (one EasyEDA request per part - EasyEDA may block you "
                 "for a few minutes if overused; otherwise parts are checked when selected or filtered)", "bool")
        self.row(p, "cad_filter", "Show", "choice",
                 [("any", "All parts"), ("symfp", "Parts with symbol + footprint"),
                  ("symfp3d", "Parts with symbol + footprint + 3D model")])
        self.section(p, "Preview")
        self.row(p, "show_images", "Download part photos", "bool")
        self.row(p, "auto_preview", "Preview symbol and footprint when a part is selected", "bool")
        self.row(p, "preview_3d", "Render 3D previews (needs KiCad 9+)", "bool")
        r = self._next(p)
        ttk.Button(p, text="Clear CAD cache", command=self._clear_cache).grid(row=r, column=0, sticky="w",
                                                                              padx=14, pady=10)

    def _page_cad_sources(self) -> None:
        from . import cad_sources
        p = self._page("CAD sources")
        self.note(p, "Where symbols, footprints and 3D models come from. 'Auto' (Search tab → CAD from) tries the "
                     "sources in this order; the 'Model from' list above the preview lets you switch per part.")
        self.note(p, "LCSC parts always use their own EasyEDA model. These settings are for parts from Digi-Key, "
                     "Mouser, Farnell/Newark, TME and Octopart.")
        self.section(p, "Sources to use, in order (Auto tries them top to bottom)")
        self.order_vars: list[tk.StringVar] = []
        self.use_vars: dict[str, tk.BooleanVar] = {}
        for i in range(len(cad_sources.SOURCES)):
            r = self._next(p)
            ttk.Label(p, text=f"{i + 1}.").grid(row=r, column=0, sticky="e", padx=(14, 8))
            v = tk.StringVar()
            v.trace_add("write", lambda *a: self.dirty.set("● unsaved changes"))
            ttk.Combobox(p, textvariable=v, values=cad_sources.SOURCES, state="readonly", width=16).grid(
                row=r, column=1, sticky="w", pady=1)
            self.order_vars.append(v)
        r = self._next(p)
        uf = ttk.Frame(p)
        uf.grid(row=r, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(uf, text="Use:").pack(side="left")
        for name in cad_sources.SOURCES:
            bv = tk.BooleanVar(value=True)
            bv.trace_add("write", lambda *a: self.dirty.set("● unsaved changes"))
            ttk.Checkbutton(uf, text=name, variable=bv).pack(side="left", padx=6)
            self.use_vars[name] = bv
        self.section(p, "SamacSys / Component Search Engine (the models Mouser and RS link to)")
        self.note(p, "Finding SamacSys models needs no account. Downloading them needs a free Component Search "
                     "Engine account - the same login as SamacSys Library Loader. Sign up, then enter it here "
                     "and press Enter.")
        self.cse_user = tk.StringVar()
        self.cse_pass = tk.StringVar()
        r = self._next(p)
        ttk.Label(p, text="Username / e-mail").grid(row=r, column=0, sticky="w", padx=(14, 8))
        e1 = ttk.Entry(p, textvariable=self.cse_user, width=40)
        e1.grid(row=r, column=1, sticky="w")
        r = self._next(p)
        ttk.Label(p, text="Password").grid(row=r, column=0, sticky="w", padx=(14, 8))
        e2 = ttk.Entry(p, textvariable=self.cse_pass, width=40, show="•")
        e2.grid(row=r, column=1, sticky="w")
        bf = ttk.Frame(p)
        bf.grid(row=r, column=2, sticky="w", padx=6)
        ttk.Button(bf, text="Enter", style="Accent.TButton", command=self._cse_enter).pack(side="left")
        ttk.Button(bf, text="Create free account…",
                   command=lambda: webbrowser.open(cad_sources.CSE_SIGNUP)).pack(side="left", padx=6)
        for e in (e1, e2):
            e.bind("<Return>", lambda ev: self._cse_enter())
        r = self._next(p)
        self.cse_status = tk.StringVar()
        self.cse_status_label = ttk.Label(p, textvariable=self.cse_status)
        self.cse_status_label.grid(row=r, column=1, columnspan=2, sticky="w")
        self.cse_user.trace_add("write", lambda *a: self._cse_typed())
        self.cse_pass.trace_add("write", lambda *a: self._cse_typed())
        self.section(p, "Octopart")
        self.note(p, "Uses the Octopart (Nexar) keys from the Suppliers page. Octopart lists the CAD models it knows "
                     "for a part (SnapEDA, Ultra Librarian, SamacSys, ...) with KiCad download links; models that "
                     "need a sign-in open in your browser and the downloaded zip is imported automatically.")
        self.section(p, "SnapEDA")
        self.note(p, "SnapEDA has no public download API. With Octopart keys, SnapEDA models are found through "
                     "Octopart; otherwise Kiporter opens the part on snapeda.com (sign in there), and the KiCad zip "
                     "you download is imported automatically.")

    def _cse_typed(self) -> None:
        ok = self.cse_user.get().strip() and self.cse_pass.get().strip()
        self.cse_status.set("Press Enter to save and check the login" if ok else "Not signed in")
        self.cse_status_label.configure(style="Busy.TLabel" if ok else "Bad.TLabel")

    def _cse_enter(self) -> None:
        from . import cad_sources
        s = self.app.settings
        s.cse_username, s.cse_password = self.cse_user.get().strip(), self.cse_pass.get().strip()
        s.save()
        if not (s.cse_username and s.cse_password):
            self._cse_typed()
            return
        self.cse_status.set("Checking the login…")
        self.cse_status_label.configure(style="Busy.TLabel")

        def ok(msg):
            self.cse_status.set("✔ Signed in - " + msg)
            self.cse_status_label.configure(style="Good.TLabel")

        def bad(e):
            self.cse_status.set("✖ " + str(e)[:140])
            self.cse_status_label.configure(style="Bad.TLabel")
        self.app.run_bg(lambda: cad_sources.cse_test_login(s), ok, bad)

    def _page_kicad(self) -> None:
        p = self._page("KiCad")
        installs = kicad_env.find_installs(self.app.settings.kicad_cli_path)
        self.row(p, "kicad_version", "KiCad version to use", "choice",
                 [("", "Newest installed")] + [(i.version, i.label) for i in installs])
        self.row(p, "kicad_cli_path", "kicad-cli path (only if not found automatically)", "file")
        self.section(p, "Update PCB from Schematic button")
        self.row(p, "update_pcb_method", "Method", "choice",
                 [("auto", "KiCad API, then keyboard shortcut"), ("api", "KiCad API only"),
                  ("hotkey", "Keyboard shortcut only")])
        self.row(p, "update_pcb_hotkey", "Shortcut (blank = read from KiCad, normally F8)", width=12)
        self.note(p, "The keyboard fallback needs xdotool on Linux (sudo apt install xdotool) and "
                     "Accessibility permission for Kiporter on macOS.")
        self.section(p, "Detected")
        self.kicad_info = tk.StringVar()
        r = self._next(p)
        ttk.Label(p, textvariable=self.kicad_info, justify="left").grid(row=r, column=0, columnspan=3, sticky="w",
                                                                         padx=14)
        r = self._next(p)
        ttk.Button(p, text="Run KiCad setup check…",
                   command=lambda: self.app.tools_tab.open("setup")).grid(row=r, column=0, sticky="w", padx=14,
                                                                           pady=10)

    def _page_bom(self) -> None:
        p = self._page("BOM")
        self.row(p, "bom_boards", "Boards to build (default)", "int", width=8)
        self.row(p, "bom_format", "Default export format", "choice", bom.FORMAT_NAMES)
        self.row(p, "bom_file_type", "File type", "choice", [("csv", "CSV"), ("xlsx", "Excel (.xlsx)")])
        self.row(p, "bom_include_dnp", "Include Do-Not-Populate parts in exports", "bool")
        self.row(p, "bom_export_cpl", "Also write the pick-and-place (CPL) file for assembly houses", "bool")
        from . import fab
        self.row(p, "fab_profile", "Board house for the Fabrication tab", "choice", [p.name for p in fab.all_profiles(self.app.settings)])

    def _page_about(self) -> None:
        p = self._page("About")
        logo = ui.load_logo(220)
        if logo:
            ttk.Label(p, image=logo).grid(row=0, column=0, columnspan=3, pady=(0, 10))
            p._row = 1
        r = self._next(p)
        ttk.Label(p, text=f"Kiporter {__version__}", style="Title.TLabel").grid(row=r, column=0, columnspan=3,
                                                                              sticky="w")
        self.note(p, "Search LCSC, Digi-Key, Mouser, Farnell/Newark, TME and Octopart; import symbols, footprints "
                     "and 3D models into KiCad; maintain libraries; check and export BOMs.")
        r = self._next(p)
        cf = ttk.Frame(p)
        cf.grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(cf, text="Made by Mark T.  •  Contact:").pack(side="left")
        mail = ttk.Label(cf, text=CONTACT_EMAIL, foreground="#1f5fa8", cursor="hand2")
        mail.pack(side="left", padx=4)
        mail.bind("<Button-1>", lambda e: webbrowser.open(f"mailto:{CONTACT_EMAIL}?subject=Kiporter"))
        ttk.Button(cf, text="Copy e-mail", command=lambda: (self.clipboard_clear(), self.clipboard_append(
            CONTACT_EMAIL))).pack(side="left", padx=6)
        r = self._next(p)
        bf = ttk.Frame(p)
        bf.grid(row=r, column=0, columnspan=3, sticky="w", pady=10)
        ttk.Button(bf, text="How to use Kiporter", style="Accent.TButton",
                   command=lambda: webbrowser.open(HOWTO_URL)).pack(side="left", padx=(0, 6))
        ttk.Button(bf, text="Project on GitHub", command=lambda: webbrowser.open(REPO_URL)).pack(side="left")
        ttk.Button(bf, text="API key guide", command=lambda: webbrowser.open(guides.GUIDE_URL)).pack(side="left",
                                                                                                   padx=6)
        ttk.Button(bf, text="Open settings folder",
                   command=lambda: kicad_control.open_path(app_data_dir())).pack(side="left")
        ttk.Button(bf, text="Open log", command=lambda: kicad_control.open_path(app_data_dir() / "log.txt")).pack(
            side="left", padx=6)
        self.note(p, f"Settings file: {SETTINGS_FILE}")

    # ---------------------------------------------------------------- load / save
    def load_from(self, s: Settings) -> None:
        from . import cad_sources
        full = [x for x in (s.cad_order or []) if x in cad_sources.SOURCES]
        full += [x for x in cad_sources.SOURCES if x not in full]
        for v, name in zip(self.order_vars, full):
            v.set(name)
        for name, bv in self.use_vars.items():
            bv.set(name not in (s.cad_disabled or []))
        self.cse_user.set(s.cse_username)
        self.cse_pass.set(s.cse_password)
        if s.cse_username and s.cse_password:
            self.cse_status.set("✔ Login saved (press Enter to check it)")
            self.cse_status_label.configure(style="Good.TLabel")
        else:
            self._cse_typed()
        maps = getattr(self, "_choice_maps", {})
        for attr, v in self.vars.items():
            val = getattr(s, attr)
            if attr in maps:
                v.set(next((lab for key, lab in maps[attr] if key == val), val))
            elif isinstance(v, tk.BooleanVar):
                v.set(bool(val))
            else:
                v.set(str(val))
        names = fields.field_names(s.field_names)
        for k, v in self.field_vars.items():
            v.set(names[k])
        ki = self.app.ki
        ipc = "enabled" if kicad_env.ipc_api_enabled(ki) else "disabled"
        self.kicad_info.set(f"Program: {ki.label}\nkicad-cli: {ki.kicad_cli or 'not found'}\n"
                            f"Config folder: {ki.config_dir}\nLibraries/3D models: {ki.share_dir or 'not found'}\n"
                            f"IPC API: {ipc}")
        self.dirty.set("")

    def save(self) -> None:
        s = self.app.settings
        maps = getattr(self, "_choice_maps", {})
        for attr, v in self.vars.items():
            raw = v.get()
            if attr in maps:
                raw = next((key for key, lab in maps[attr] if lab == raw), raw)
            typ = self.types[attr]
            try:
                val = bool(raw) if typ is bool else int(float(raw or 0)) if typ is int else \
                    float(raw or 0) if typ is float else str(raw).strip()
            except ValueError:
                messagebox.showwarning(APP_TITLE, f"'{raw}' is not a valid number.")
                return
            setattr(s, attr, val)
        s.search_limit = max(1, min(int(s.search_limit), 50))
        s.field_names = {k: v.get().strip() for k, v in self.field_vars.items()
                         if v.get().strip() and v.get().strip() != fields.DEFAULT_FIELD_NAMES[k]}
        self.suppliers.apply()
        order = [v.get() for v in self.order_vars if v.get()]
        s.cad_order = list(dict.fromkeys(order))
        s.cad_disabled = [n for n, bv in self.use_vars.items() if not bv.get()]
        s.cse_username, s.cse_password = self.cse_user.get().strip(), self.cse_pass.get().strip()
        s.save()
        self.app.apply_settings()
        self.load_from(s)
        self.app.log("Settings saved.")

    def revert(self) -> None:
        self.load_from(self.app.settings)

    def reset_defaults(self) -> None:
        if not messagebox.askyesno(APP_TITLE, "Reset all settings (except supplier keys) to their defaults?"):
            return
        keep = {f.name for f in dc_fields(Settings) if f.name.startswith(("digikey", "mouser", "element14", "tme",
                                                                           "nexar"))}
        cur = self.app.settings
        tmp = Settings.defaults()
        for k in keep:
            setattr(tmp, k, getattr(cur, k))
        self.load_from(tmp)
        self.dirty.set("● defaults loaded - click Save to keep them")

    # ---------------------------------------------------------------- actions
    def _rerun_setup(self) -> None:
        self.save()
        self.app.auto_setup()
        messagebox.showinfo(APP_TITLE, "Automatic setup finished - see the log for details.")

    def _open_lib_dir(self) -> None:
        d = Path(self.app.settings.global_lib_dir)
        d.mkdir(parents=True, exist_ok=True)
        kicad_control.open_path(d)

    def _clear_cache(self) -> None:
        from . import cad
        cad._avail.clear()
        cad._equiv.clear()
        cad.save_cache()
        messagebox.showinfo(APP_TITLE, "CAD availability cache cleared.")
