"""Driving a running KiCad: IPC API first, keyboard shortcut fallback (Windows)."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .kicad_env import IS_WIN, KiCadInstall, ProjectInfo, hotkey_for, kicad_window_titles

UPDATE_PCB_ACTIONS = [
    "common.Control.updatePcbFromSchematic",  # KiCad 10
    "pcbnew.EditorControl.updatePcbFromSchematic",  # KiCad 9
]


def _kicad(timeout_ms: int):
    from kipy import KiCad  # imported lazily: optional dependency
    return KiCad(client_name=f"part-importer-{os.getpid()}-{time.time_ns()}", timeout_ms=timeout_ms)


def ipc_available() -> bool:
    try:
        _kicad(700).ping()
        return True
    except Exception:
        return False


def ipc_version() -> str:
    try:
        return _kicad(700).get_version().full_version
    except Exception:
        return ""


def _run_action_ipc(names: list[str]) -> tuple[bool, str]:
    try:
        from kipy.proto.common import commands
        from kipy.proto.common.types import base_types_pb2 as bt
    except ImportError:
        return False, "kicad-python not installed"
    try:
        k = _kicad(1000)
        k.ping()
        if not k.get_open_documents(bt.DocumentType.DOCTYPE_PCB):
            return False, "PCB editor is not open"
    except Exception as e:
        return False, f"IPC API not reachable ({e})"
    for name in names:
        try:
            resp = _kicad(1500).run_action(name)
        except Exception as e:  # kipy raises its own ConnectionError class
            # The Update PCB dialog is modal, so KiCad only answers after it closes.
            if "receiv" in str(e).lower() or "timed out" in str(e).lower():
                return True, "Opened 'Update PCB from Schematic' via the KiCad API"
            return False, str(e)
        status = getattr(resp, "status", 0)
        if status == commands.RunActionStatus.RAS_OK:
            return True, f"Ran '{name}' via IPC API"
        if status == commands.RunActionStatus.RAS_FRAME_NOT_OPEN:
            return False, "PCB editor is not open"
    return False, "KiCad did not accept the action"


# ---------------------------------------------------------------- keyboard fallback
_VK_MODS = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12}


def _vk(key: str) -> int | None:
    k = key.strip().upper()
    if k.startswith("F") and k[1:].isdigit():
        return 0x6F + int(k[1:])
    if len(k) == 1 and (k.isalpha() or k.isdigit()):
        return ord(k)
    return {"ESC": 0x1B, "TAB": 0x09, "SPACE": 0x20, "ENTER": 0x0D, "RETURN": 0x0D}.get(k)


def _focus_window(hwnd: int) -> bool:
    import ctypes
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # tapping Alt lifts Windows' foreground lock
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    ok = bool(user32.SetForegroundWindow(hwnd))
    time.sleep(0.35)
    return ok


def _send_hotkey(hotkey: str) -> bool:
    import ctypes
    user32 = ctypes.windll.user32
    parts = [p for p in hotkey.replace(" ", "").split("+") if p]
    mods = [_VK_MODS[p.lower()] for p in parts[:-1] if p.lower() in _VK_MODS]
    vk = _vk(parts[-1]) if parts else None
    if vk is None:
        return False
    for m in mods:
        user32.keybd_event(m, 0, 0, 0)
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)
    for m in reversed(mods):
        user32.keybd_event(m, 0, 2, 0)
    return True


def _find_window(kind: str, project: ProjectInfo | None) -> int | None:
    wins = [(h, t) for h, t in kicad_window_titles() if kind in t]
    if project:
        mine = [(h, t) for h, t in wins if project.name in t]
        wins = mine or wins
    return wins[0][0] if wins else None


UPDATE_DIALOG_TITLE = "Update PCB from Schematic"


def _find_dialog(title: str) -> int | None:
    return next((h for h, t in kicad_window_titles() if t == title), None)


def update_pcb_from_schematic(ki: KiCadInstall, project: ProjectInfo | None) -> tuple[bool, str]:
    if IS_WIN:
        dlg = _find_dialog(UPDATE_DIALOG_TITLE)
        if dlg:
            _focus_window(dlg)
            return True, "The 'Update PCB from Schematic' dialog is already open in KiCad"
    action, key = hotkey_for(ki, "updatePcbFromSchematic")
    names = ([action] if action else []) + [n for n in UPDATE_PCB_ACTIONS if n != action]
    ok, msg = _run_action_ipc(names)
    if ok:
        if IS_WIN:
            win = _find_dialog(UPDATE_DIALOG_TITLE) or _find_window("PCB Editor", project)
            if win:
                _focus_window(win)
        return True, msg
    if not IS_WIN:
        return False, msg
    key = key or "F8"
    # Both the PCB editor and the schematic editor run "Update PCB from Schematic" on F8
    for kind in ("PCB Editor", "Schematic Editor"):
        win = _find_window(kind, project)
        if win:
            _focus_window(win)
            if _send_hotkey(key):
                return True, f"Sent {key} (Update PCB from Schematic) to the {kind}"
    return False, (f"{msg}. No KiCad PCB/Schematic Editor window found - open the project "
                   "in KiCad first.")


def open_in_kicad(ki: KiCadInstall, project: ProjectInfo) -> tuple[bool, str]:
    exe = ki.exe("kicad")
    try:
        if exe:
            subprocess.Popen([str(exe), str(project.pro_file)], close_fds=True)
        elif IS_WIN:
            os.startfile(str(project.pro_file))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(project.pro_file)])
        return True, f"Opening {project.pro_file.name} in KiCad"
    except OSError as e:
        return False, str(e)


def open_path(p: str | Path) -> None:
    if IS_WIN:
        os.startfile(str(p))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(p)])
