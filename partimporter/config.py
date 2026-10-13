"""Persistent application settings (stored in %APPDATA%/KiCadPartImporter).

API secrets are kept in the Windows Credential Manager (via `keyring`) when it is
available, and only fall back to the JSON file otherwise.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

KEYRING_SERVICE = "KiCadPartImporter"
SECRET_FIELDS = ("digikey_client_secret", "mouser_api_key", "element14_api_key",
                 "tme_token", "tme_app_secret", "nexar_client_secret")


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / "KiCadPartImporter"
    d.mkdir(parents=True, exist_ok=True)
    return d


SETTINGS_FILE = app_data_dir() / "settings.json"


def _keyring():
    try:
        import keyring
        keyring.get_keyring()
        return keyring
    except Exception:
        return None


@dataclass
class Settings:
    # Global ("local") libraries
    global_lib_dir: str = str(Path.home() / "KiCad" / "PartImporter")
    global_lib_name: str = "PartImporter"
    global_lib_choice: str = ""  # nickname of the chosen local library ("" = global_lib_name)
    # Project library
    project_lib_subdir: str = "libs"
    project_lib_name: str = "Project_Parts"
    project_lib_choice: str = ""
    default_target: str = "project"  # "project" | "global"
    ask_target_on_startup: bool = True
    # Supplier credentials
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
    # Search sources enabled
    use_lcsc: bool = True
    use_digikey: bool = True
    use_mouser: bool = True
    use_element14: bool = False
    use_tme: bool = False
    use_nexar: bool = False
    in_stock_only: bool = False
    declined_connect: list = field(default_factory=list)
    # Symbol fields
    field_names: dict = field(default_factory=dict)
    drop_junk_fields: bool = True
    # Downloads watcher
    watch_dir: str = str(Path.home() / "Downloads")
    watch_downloads: bool = True
    # Misc
    kicad_version: str = ""
    first_run_done: bool = False
    last_project: str = ""
    bom_boards: int = 1

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
                    try:
                        setattr(s, k, kr.get_password(KEYRING_SERVICE, k) or "")
                    except Exception:
                        pass
        return s

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
