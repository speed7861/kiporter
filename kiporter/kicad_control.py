"""Driving a running KiCad: IPC API first, keyboard shortcut fallback (Windows, macOS, Linux/X11)."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from .kicad_env import IS_LINUX, IS_MAC, IS_WIN, KiCadInstall, ProjectInfo, hotkey_for, kicad_window_titles

UPDATE_PCB_ACTIONS = [
    "common.Control.updatePcbFromSchematic",  # KiCad 10
    "pcbnew.EditorControl.updatePcbFromSchematic",  # KiCad 9
]


def _kicad(timeout_ms: int):
    from kipy import KiCad  # imported lazily: optional dependency
    return KiCad(client_name=f"kiporter-{os.getpid()}-{time.time_ns()}", timeout_ms=timeout_ms)


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
# Window "handles" are platform specific: an HWND on Windows, an X11 window id on Linux,
# and the window title on macOS (AppleScript addresses windows by name).
_VK_MODS = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12}
_MAC_KEYCODES = {"F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97, "F7": 98, "F8": 100,
                 "F9": 101, "F10": 109, "F11": 103, "F12": 111}


def _vk(key: str) -> int | None:
    k = key.strip().upper()
    if k.startswith("F") and k[1:].isdigit():
        return 0x6F + int(k[1:])
    if len(k) == 1 and (k.isalpha() or k.isdigit()):
        return ord(k)
    return {"ESC": 0x1B, "TAB": 0x09, "SPACE": 0x20, "ENTER": 0x0D, "RETURN": 0x0D}.get(k)


def _run(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _osa_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _focus_window(handle) -> bool:
    if IS_WIN:
        import ctypes
        user32 = ctypes.windll.user32
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, 9)  # SW_RESTORE
        # tapping Alt lifts Windows' foreground lock
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        ok = bool(user32.SetForegroundWindow(handle))
    elif IS_MAC:
        ok = _run(["osascript", "-e", 'tell application "System Events" to tell (first process whose name '
                   f'is "kicad") to (set frontmost to true) & (perform action "AXRaise" of (first window whose '
                   f'name is {_osa_str(str(handle))}))'])
    else:
        ok = _run(["xdotool", "windowactivate", "--sync", str(handle)]) if shutil.which("xdotool") else \
            _run(["wmctrl", "-i", "-a", str(handle)])
    time.sleep(0.35)
    return ok


def _send_hotkey(hotkey: str) -> bool:
    parts = [p for p in hotkey.replace(" ", "").split("+") if p]
    if not parts:
        return False
    mods, key = [p.lower() for p in parts[:-1]], parts[-1]
    if IS_WIN:
        import ctypes
        user32 = ctypes.windll.user32
        vk = _vk(key)
        if vk is None:
            return False
        codes = [_VK_MODS[m] for m in mods if m in _VK_MODS]
        for m in codes:
            user32.keybd_event(m, 0, 0, 0)
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
        for m in reversed(codes):
            user32.keybd_event(m, 0, 2, 0)
        return True
    if IS_MAC:
        # KiCad's "Ctrl" is the Command key on macOS
        using = [{"ctrl": "command down", "shift": "shift down", "alt": "option down"}[m]
                 for m in mods if m in ("ctrl", "shift", "alt")]
        using_s = f" using {{{', '.join(using)}}}" if using else ""
        code = _MAC_KEYCODES.get(key.upper())
        press = f"key code {code}" if code else f"keystroke {_osa_str(key.lower())}"
        return _run(["osascript", "-e", f'tell application "System Events" to {press}{using_s}'])
    if not shutil.which("xdotool"):
        return False
    return _run(["xdotool", "key", "--clearmodifiers", "+".join(mods + [key])])


def _find_window(kind: str, project: ProjectInfo | None):
    wins = [(h, t) for h, t in kicad_window_titles() if kind in t]
    if project:
        mine = [(h, t) for h, t in wins if project.name in t]
        wins = mine or wins
    return wins[0][0] if wins else None


UPDATE_DIALOG_TITLE = "Update PCB from Schematic"


def _find_dialog(title: str):
    return next((h for h, t in kicad_window_titles() if t == title), None)


# the dialog's tick boxes: (settings key, label in KiCad)
UPDATE_OPTIONS = [
    ("relink", "Re-link footprints to schematic symbols based on their reference designators"),
    ("replace", "Replace footprints with those specified by symbols"),
    ("delete_extra", "Delete footprints with no symbols"),
    ("override_locks", "Override locks"),
    ("group", "Group footprints based on symbol group"),
]
UPDATE_BUTTON = "Update PCB"
_BM_GETCHECK, _BM_CLICK = 0x00F0, 0x00F5


def _dialog_buttons(dlg) -> dict[str, int]:
    """Label -> handle of every button and tick box in a KiCad dialog (Windows only)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found: dict[str, int] = {}
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value.lower() == "button":
            text = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, text, 256)
            label = text.value.replace("&", "").strip()
            if label:
                found.setdefault(label, hwnd)
        return True
    user32.EnumChildWindows(dlg, proc(each), 0)
    return found


def _send(hwnd, msg: int) -> int:
    """SendMessage with a time limit, so a busy KiCad cannot hang Kiporter."""
    import ctypes
    from ctypes import wintypes
    res = wintypes.DWORD(0)
    ctypes.windll.user32.SendMessageTimeoutW(hwnd, msg, 0, 0, 0x0002, 3000, ctypes.byref(res))
    return res.value


def apply_update_options(options: dict[str, str] | None, press_update: bool = False, wait_s: float = 6.0) -> str:
    """Tick / untick the dialog's options ('on' / 'off' / '' = leave) and optionally press Update PCB.
    Returns a short note for the log ('' when there was nothing to do)."""
    wanted = {k: v for k, v in (options or {}).items() if v in ("on", "off")}
    if not wanted and not press_update:
        return ""
    if not IS_WIN:
        return "setting the dialog's options automatically only works on Windows"
    end = time.time() + wait_s
    dlg = _find_dialog(UPDATE_DIALOG_TITLE)
    while not dlg and time.time() < end:
        time.sleep(0.2)
        dlg = _find_dialog(UPDATE_DIALOG_TITLE)
    if not dlg:
        return "the dialog did not open in time, so its options were left alone"
    time.sleep(0.4)  # let KiCad finish building the dialog
    buttons = _dialog_buttons(dlg)
    changed, missing = [], []
    for key, label in UPDATE_OPTIONS:
        if key not in wanted:
            continue
        hwnd = buttons.get(label)
        if not hwnd:
            missing.append(label)
            continue
        if bool(_send(hwnd, _BM_GETCHECK) & 1) != (wanted[key] == "on"):
            _send(hwnd, _BM_CLICK)
            changed.append(f"{'ticked' if wanted[key] == 'on' else 'unticked'} '{label}'")
            time.sleep(0.3)  # KiCad re-checks the changes after every tick
    notes = [", ".join(changed) if changed else "options already as configured"] if wanted else []
    if missing:
        notes.append("not in this KiCad version: " + ", ".join(f"'{m}'" for m in missing))
    if press_update:
        hwnd = buttons.get(UPDATE_BUTTON)
        if hwnd:
            time.sleep(0.5)
            _send(hwnd, _BM_CLICK)
            notes.append("pressed Update PCB")
        else:
            notes.append("could not find the Update PCB button")
    return "; ".join(notes)


def update_pcb_from_schematic(ki: KiCadInstall, project: ProjectInfo | None, method: str = "auto",
                              hotkey: str = "", options: dict[str, str] | None = None,
                              press_update: bool = False) -> tuple[bool, str]:
    """method: 'auto' (API, then keyboard), 'api' or 'hotkey'. options / press_update: see apply_update_options."""
    ok, msg = _open_update_dialog(ki, project, method, hotkey)
    if ok:
        note = apply_update_options(options, press_update)
        if note:
            msg += f" - {note}"
    return ok, msg


def _open_update_dialog(ki: KiCadInstall, project: ProjectInfo | None, method: str, hotkey: str) -> tuple[bool, str]:
    dlg = _find_dialog(UPDATE_DIALOG_TITLE)
    if dlg:
        _focus_window(dlg)
        return True, "The 'Update PCB from Schematic' dialog is already open in KiCad"
    action, key = hotkey_for(ki, "updatePcbFromSchematic")
    msg = "keyboard method selected"
    if method in ("auto", "api"):
        names = ([action] if action else []) + [n for n in UPDATE_PCB_ACTIONS if n != action]
        ok, msg = _run_action_ipc(names)
        if ok:
            win = _find_dialog(UPDATE_DIALOG_TITLE) or _find_window("PCB Editor", project)
            if win:
                _focus_window(win)
            return True, msg
        if method == "api":
            return False, msg
    if IS_LINUX and not shutil.which("xdotool"):
        return False, f"{msg}. For the keyboard fallback install xdotool (sudo apt install xdotool)."
    key = hotkey or key or "F8"
    # Both the PCB editor and the schematic editor run "Update PCB from Schematic" on F8
    for kind in ("PCB Editor", "Schematic Editor"):
        win = _find_window(kind, project)
        if win:
            _focus_window(win)
            if _send_hotkey(key):
                extra = " (macOS: allow Kiporter under Privacy & Security > Accessibility)" if IS_MAC else ""
                return True, f"Sent {key} (Update PCB from Schematic) to the {kind}{extra}"
    return False, (f"{msg}. No KiCad PCB/Schematic Editor window found - open the project "
                   "in KiCad first.")


def open_in_kicad(ki: KiCadInstall, project: ProjectInfo) -> tuple[bool, str]:
    try:
        if ki.kicad:
            subprocess.Popen([*ki.kicad, str(project.pro_file)], close_fds=True)
        else:
            open_path(project.pro_file)
        return True, f"Opening {project.pro_file.name} in KiCad"
    except OSError as e:
        return False, str(e)


def open_path(p: str | Path) -> None:
    """Open a file/folder/URL with the operating system's default application."""
    if IS_WIN:
        os.startfile(str(p))  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])
