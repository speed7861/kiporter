"""Persistent application settings.

Stored in the per-user config folder (Windows %APPDATA%\\Kiporter, macOS
~/Library/Application Support/Kiporter, Linux ~/.config/kiporter). API secrets go
to the operating system's credential store (Windows Credential Manager, macOS
Keychain, Linux Secret Service) via `keyring` when available, otherwise to the
settings file.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "Kiporter"
KEYRING_SERVICE = "Kiporter"
_OLD_KEYRING_SERVICE = "KiCadPartImporter"
SECRET_FIELDS = ("digikey_client_secret", "mouser_api_key", "element14_api_key",
                 "tme_token", "tme_app_secret", "nexar_client_secret")


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
        d = base / APP_NAME
        old = base / "KiCadPartImporter"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_NAME
        old = None
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kiporter"
        old = None
    if not d.exists() and old and old.exists():  # carry over settings from the pre-Kiporter name
        shutil.copytree(old, d, dirs_exist_ok=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


SETTINGS_FILE = app_data_dir() / "settings.json"


def _keyring():
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


@dataclass
class Settings:
    # ---- libraries
    global_lib_dir: str = str(Path.home() / "KiCad" / "Kiporter")
    global_lib_name: str = "Kiporter"
    global_lib_choice: str = ""  # nickname of the chosen local library ("" = global_lib_name)
    project_lib_subdir: str = "libs"
    project_lib_name: str = "Project_Parts"
    project_lib_choice: str = ""
    default_target: str = "project"  # "project" | "global"
    ask_target_on_startup: bool = True
    import_symbol: bool = True
    import_footprint: bool = True
    import_3d: bool = True
    overwrite_existing: bool = False
    # ---- supplier credentials
    digikey_client_id: str = ""
    digikey_client_secret: str = ""
    digikey_locale_site: str = "US"
    digikey_currency: str = "USD"
    mouser_api_key: str = ""
    element14_api_key: str = ""
    element14_store: str = "www.newark.com"
    tme_token: str = ""
    tme_app_secret: str = ""
    tme_country: str = "US"
    tme_currency: str = "USD"
    nexar_client_id: str = ""
    nexar_client_secret: str = ""
    # ---- search
    use_lcsc: bool = True
    use_digikey: bool = True
    use_mouser: bool = True
    use_element14: bool = False
    use_tme: bool = False
    use_nexar: bool = False
    in_stock_only: bool = False
    search_limit: int = 50
    check_cad: bool = True  # look up symbol/footprint/3D availability for every result
    cad_filter: str = "any"  # "any" | "symfp" | "symfp3d"
    auto_preview: bool = True
    show_images: bool = True
    preview_3d: bool = True
    declined_connect: list = field(default_factory=list)
    # ---- symbol fields
    field_names: dict = field(default_factory=dict)
    drop_junk_fields: bool = True
    # ---- downloads watcher
    watch_dir: str = str(Path.home() / "Downloads")
    watch_downloads: bool = True
    # ---- KiCad
    kicad_version: str = ""  # "" = newest installed
    kicad_cli_path: str = ""  # explicit kicad-cli executable (optional)
    update_pcb_method: str = "auto"  # "auto" | "api" | "hotkey"
    update_pcb_hotkey: str = ""  # "" = read from KiCad's hotkey settings (default F8)
    auto_refresh_s: int = 15
    # ---- appearance
    ui_scale: float = 0.0  # 0 = system default
    font_size: int = 0  # 0 = system default
    # ---- BOM
    bom_boards: int = 1
    bom_format: str = "JLCPCB"
    bom_file_type: str = "csv"
    bom_include_dnp: bool = False
    bom_export_cpl: bool = True
    fab_profile: str = "JLCPCB"
    custom_fab_profiles: list = field(default_factory=list)
    # ---- misc
    first_run_done: bool = False
    last_project: str = ""

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
        kr = _keyring()
        if kr:
            for k in SECRET_FIELDS:
                if not getattr(s, k):
                    for service in (KEYRING_SERVICE, _OLD_KEYRING_SERVICE):
                        try:
                            v = kr.get_password(service, k)
                        except Exception:
                            v = None
                        if v:
                            setattr(s, k, v)
                            break
        return s

    @classmethod
    def defaults(cls) -> "Settings":
        return cls()

    def save(self) -> None:
        data = asdict(self)
        kr = _keyring()
        if kr:
            for k in SECRET_FIELDS:
                try:
                    if data[k]:
                        kr.set_password(KEYRING_SERVICE, k, data[k])
                    else:
                        try:
                            kr.delete_password(KEYRING_SERVICE, k)
                        except Exception:
                            pass
                    data[k] = ""  # stored in the credential manager, not on disk
                except Exception:
                    pass
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @property
    def secrets_in_keyring(self) -> bool:
        return _keyring() is not None
