"""Discovery of the local KiCad installation, its config and open projects."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WIN and not IS_MAC
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW
FLATPAK_ID = "org.kicad.KiCad"


def _ver_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", v))


def _config_version(full: str) -> str:
    """'9.0.2' -> '9.0' (the name of KiCad's per-version config folder)."""
    parts = re.findall(r"\d+", full)
    return f"{parts[0]}.{parts[1] if len(parts) > 1 else 0}" if parts else ""


# ---------------------------------------------------------------- installation
@dataclass
class KiCadInstall:
    version: str  # config version, e.g. "10.0"
    config_dir: Path
    cli: list[str] | None = None  # command (prefix) that runs kicad-cli
    kicad: list[str] | None = None  # command (prefix) that starts KiCad
    share_dir: Path | None = None  # folder holding symbols/, footprints/, 3dmodels/, template/
    full_version: str = ""
    label: str = ""

    @property
    def major(self) -> int:
        return int(self.version.split(".")[0])

    @property
    def kicad_cli(self) -> str | None:
        """Printable kicad-cli command (None if not available)."""
        return " ".join(self.cli) if self.cli else None

    def cli_cmd(self, *args) -> list[str]:
        if not self.cli:
            raise RuntimeError("kicad-cli was not found - install KiCad 8 or newer, or set its path in Settings")
        return [*self.cli, *map(str, args)]


def _cli_version(cmd: list[str]) -> str:
    try:
        r = subprocess.run([*cmd, "version"], capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        m = re.search(r"\d+\.\d+(\.\d+)?", r.stdout)
        return m.group(0) if m else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _flatpak_dirs() -> list[Path]:
    return [Path.home() / ".local/share/flatpak/app" / FLATPAK_ID, Path("/var/lib/flatpak/app") / FLATPAK_ID]


def find_installs(cli_override: str = "") -> list[KiCadInstall]:
    """Every KiCad installation found on this computer (newest first)."""
    found: list[KiCadInstall] = []
    cfg_root = kicad_config_root()

    def add(full: str, cli: list[str] | None, kicad: list[str] | None, share: Path | None, label: str,
            cfg: Path | None = None) -> None:
        ver = _config_version(full)
        if not ver or any(i.version == ver and i.label == label for i in found):
            return
        found.append(KiCadInstall(ver, (cfg or cfg_root) / ver, cli, kicad, share, full, label))

    if cli_override and Path(cli_override).exists():
        full = _cli_version([cli_override])
        root = Path(cli_override).resolve().parent
        share = next((d for d in (root.parent / "share" / "kicad", root.parent / "SharedSupport",
                                  Path("/usr/share/kicad")) if (d / "symbols").exists()), None)
        add(full, [cli_override], None, share, f"KiCad {full} ({cli_override})")

    if IS_WIN:
        roots = [Path(os.environ[e]) / "KiCad" for e in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")
                 if os.environ.get(e)]
        if os.environ.get("LOCALAPPDATA"):
            roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "KiCad")
        for root in roots:
            if not root.is_dir():
                continue
            for sub in sorted(root.iterdir(), key=lambda d: _ver_key(d.name), reverse=True):
                cli = sub / "bin" / "kicad-cli.exe"
                if cli.exists() and re.match(r"^\d+(\.\d+)*$", sub.name):
                    add(sub.name, [str(cli)], [str(sub / "bin" / "kicad.exe")], sub / "share" / "kicad",
                        f"KiCad {sub.name} ({sub})")
    elif IS_MAC:
        import plistlib
        apps = [Path("/Applications/KiCad/KiCad.app"), Path("/Applications/KiCad.app"),
                Path.home() / "Applications/KiCad/KiCad.app", Path.home() / "Applications/KiCad.app"]
        apps += sorted(Path("/Applications").glob("KiCad*/KiCad.app"))
        for app in dict.fromkeys(apps):
            cli = app / "Contents" / "MacOS" / "kicad-cli"
            if not cli.exists():
                continue
            try:
                full = plistlib.loads((app / "Contents" / "Info.plist").read_bytes()).get(
                    "CFBundleShortVersionString", "")
            except (OSError, ValueError):
                full = ""
            full = full or _cli_version([str(cli)])
            add(full, [str(cli)], ["open", "-a", str(app)], app / "Contents" / "SharedSupport",
                f"KiCad {full} ({app})")
    else:
        for cli in dict.fromkeys(filter(None, [shutil.which("kicad-cli"), "/usr/bin/kicad-cli",
                                                "/usr/local/bin/kicad-cli"])):
            if Path(cli).exists():
                full = _cli_version([cli])
                share = next((d for d in (Path(cli).resolve().parent.parent / "share" / "kicad",
                                          Path("/usr/share/kicad"), Path("/usr/local/share/kicad"))
                              if (d / "symbols").exists() or (d / "template").exists()), None)
                kicad = shutil.which("kicad") or str(Path(cli).parent / "kicad")
                add(full, [cli], [kicad], share, f"KiCad {full} ({cli})")
        for d in _flatpak_dirs():
            if d.exists() and shutil.which("flatpak"):
                cmd = ["flatpak", "run", "--command=kicad-cli", FLATPAK_ID]
                full = _cli_version(cmd)
                share = d / "current" / "active" / "files" / "share" / "kicad"
                add(full, cmd, ["flatpak", "run", FLATPAK_ID], share if share.exists() else None,
                    f"KiCad {full} (Flatpak)", Path.home() / ".var/app" / FLATPAK_ID / "config" / "kicad")
                break
    found.sort(key=lambda i: _ver_key(i.full_version or i.version), reverse=True)
    return found


def kicad_config_root() -> Path:
    if IS_WIN:
        return Path(os.environ.get("APPDATA", Path.home())) / "kicad"
    if IS_MAC:
        return Path.home() / "Library" / "Preferences" / "kicad"
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    native = xdg / "kicad"
    flat = Path.home() / ".var/app" / FLATPAK_ID / "config" / "kicad"
    return native if native.exists() or not flat.exists() else flat


def config_versions(root: Path | None = None) -> list[str]:
    root = root or kicad_config_root()
    if not root.is_dir():
        return []
    vs = [d.name for d in root.iterdir() if d.is_dir() and re.match(r"^\d+\.\d+$", d.name)]
    return sorted(vs, key=_ver_key)


def detect_kicad(preferred: str = "", cli_override: str = "") -> KiCadInstall:
    """Pick the KiCad to work with: the preferred version, else the newest install with a config folder."""
    installs = find_installs(cli_override)
    if preferred:
        for i in installs:
            if i.version == preferred:
                return i
    for i in installs:
        if i.config_dir.exists():
            return i
    if installs:
        return installs[0]
    cfgs = config_versions()
    ver = preferred or (cfgs[-1] if cfgs else "10.0")
    return KiCadInstall(ver, kicad_config_root() / ver, label=f"KiCad {ver} (program not found)")


# ---------------------------------------------------------------- JSON settings
def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def ipc_api_enabled(ki: KiCadInstall) -> bool:
    return bool(read_json(ki.config_dir / "kicad_common.json").get("api", {}).get("enable_server"))


def enable_ipc_api(ki: KiCadInstall) -> bool:
    """Turn on KiCad's IPC API server. Takes effect next time KiCad starts."""
    path = ki.config_dir / "kicad_common.json"
    data = read_json(path)
    if not data:
        return False
    data.setdefault("api", {})["enable_server"] = True
    write_json(path, data)
    return True


def hotkey_for(ki: KiCadInstall, action_substring: str) -> tuple[str | None, str | None]:
    """Return (action_name, hotkey) from the user's hotkey file."""
    try:
        for line in (ki.config_dir / "user.hotkeys").read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if parts and action_substring in parts[0]:
                return parts[0], (parts[1] if len(parts) > 1 and parts[1] else None)
    except OSError:
        pass
    return None, None


# ---------------------------------------------------------------- lib tables
def ensure_lib_table_entry(table: Path, kind: str, name: str, uri: str, descr: str = "") -> bool:
    """Add or update a library entry. kind is 'sym' or 'fp'. Returns True if file changed."""
    root_name = f"{kind}_lib_table"
    if table.exists():
        node = sexpr.parse(table.read_text(encoding="utf-8"))
    else:
        node = [root_name, ["version", "7"]]
    for lib in sexpr.children(node, "lib"):
        n = sexpr.child(lib, "name")
        if n and sexpr.unquote(n[1]) == name:
            u = sexpr.child(lib, "uri")
            if u and sexpr.unquote(u[1]) == uri:
                return False
            if u:
                u[1] = sexpr.quote(uri)
            else:
                lib.append(["uri", sexpr.quote(uri)])
            break
    else:
        node.append([
            "lib", ["name", sexpr.quote(name)], ["type", '"KiCad"'], ["uri", sexpr.quote(uri)],
            ["options", '""'], ["descr", sexpr.quote(descr)],
        ])
    lines = [f"({node[0]}"]
    for c in node[1:]:
        lines.append("\t" + sexpr.dump_inline(c))
    lines.append(")")
    table.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def remove_lib_table_entry(table: Path, name: str) -> bool:
    if not table.exists():
        return False
    node = sexpr.parse(table.read_text(encoding="utf-8"))
    keep = [c for c in node[1:] if not (sexpr.head(c) == "lib" and sexpr.child(c, "name")
                                        and sexpr.unquote(sexpr.child(c, "name")[1]) == name)]
    if len(keep) == len(node) - 1:
        return False
    lines = [f"({node[0]}"] + ["\t" + sexpr.dump_inline(c) for c in keep] + [")"]
    table.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------- path variables
def kicad_env_vars(ki: "KiCadInstall", project_dir: Path | None = None) -> dict[str, str]:
    """Path variables as KiCad sees them (built-in defaults < Configure Paths < OS env)."""
    v: dict[str, str] = {}
    major = ki.major
    if ki.share_dir:
        share = ki.share_dir
        v[f"KICAD{major}_SYMBOL_DIR"] = str(share / "symbols")
        v[f"KICAD{major}_FOOTPRINT_DIR"] = str(share / "footprints")
        v[f"KICAD{major}_3DMODEL_DIR"] = str(share / "3dmodels")
        v[f"KICAD{major}_TEMPLATE_DIR"] = str(share / "template")
    docs = Path.home() / "Documents" / "KiCad" / ki.version
    v[f"KICAD{major}_3RD_PARTY"] = str(docs / "3rdparty")
    v["KICAD_USER_TEMPLATE_DIR"] = str(docs / "template")
    v.update(read_json(ki.config_dir / "kicad_common.json").get("environment", {}).get("vars") or {})
    for k, val in os.environ.items():
        if k.startswith("KICAD"):
            v[k] = val
    if project_dir:
        v["KIPRJMOD"] = str(project_dir)
    return v


_VAR_RE = re.compile(r"\$\{([^}]+)\}|\$\(([^)]+)\)")


def expand(uri: str, env: dict[str, str]) -> str:
    return _VAR_RE.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), uri)


def resolve(uri: str, env: dict[str, str]) -> Path | None:
    s = expand(uri, env)
    return None if "${" in s or "$(" in s else Path(s)


@dataclass
class LibEntry:
    name: str
    type: str
    uri: str
    scope: str  # "global" | "project"
    table: Path
    descr: str = ""

    def path(self, env: dict[str, str]) -> Path | None:
        return resolve(self.uri, env)


def read_lib_table(table: Path, scope: str, env: dict[str, str] | None = None,
                   follow_nested: bool = True) -> list[LibEntry]:
    if not table.exists():
        return []
    try:
        node = sexpr.parse(table.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    out: list[LibEntry] = []
    for lib in sexpr.children(node, "lib"):
        def g(k):
            c = sexpr.child(lib, k)
            return sexpr.unquote(c[1]) if c and len(c) > 1 else ""
        e = LibEntry(g("name"), g("type") or "KiCad", g("uri"), scope, table, g("descr"))
        if e.type.lower() == "table":
            if follow_nested and env is not None:
                p = e.path(env)
                if p and p.exists() and p != table:
                    out += read_lib_table(p, scope, env, follow_nested=False)
            continue
        out.append(e)
    return out


def all_lib_entries(ki: "KiCadInstall", project_dir: Path | None, kind: str) -> list[LibEntry]:
    """Project entries first (they win on nickname clashes), then global ones."""
    env = kicad_env_vars(ki, project_dir)
    fname = f"{kind}-lib-table"
    entries = read_lib_table(project_dir / fname, "project", env) if project_dir else []
    names = {e.name for e in entries}
    entries += [e for e in read_lib_table(ki.config_dir / fname, "global", env) if e.name not in names]
    return entries


def is_stock_kicad_path(p: Path | None, ki: "KiCadInstall") -> bool:
    if p is None or not ki.share_dir:
        return False
    try:
        return str(p.resolve()).lower().startswith(str(ki.share_dir.resolve()).lower())
    except OSError:
        return False


def lib_table_has(table: Path, name: str) -> bool:
    if not table.exists():
        return False
    try:
        node = sexpr.parse(table.read_text(encoding="utf-8"))
    except ValueError:
        return False
    return any(
        sexpr.child(l, "name") and sexpr.unquote(sexpr.child(l, "name")[1]) == name
        for l in sexpr.children(node, "lib")
    )


# ---------------------------------------------------------------- processes / windows
def running_kicad_processes() -> set[str]:
    names = {"kicad.exe", "eeschema.exe", "pcbnew.exe"} if IS_WIN else {"kicad", "eeschema", "pcbnew"}
    try:
        if IS_WIN:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                 creationflags=NO_WINDOW, timeout=10).stdout
            procs = {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line}
        else:
            out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=10).stdout
            procs = {Path(l.strip()).name.lower() for l in out.splitlines()}
    except (OSError, subprocess.SubprocessError):
        return set()
    return procs & names


_KICAD_TITLE_RE = re.compile(r"KiCad|PCB Editor|Schematic Editor|Update PCB from Schematic")


def _titles_unix() -> list[tuple[str, str]]:
    """(handle, title) via AppleScript (macOS) or wmctrl/xdotool (Linux, X11/XWayland)."""
    def run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    out: list[tuple[str, str]] = []
    if IS_MAC:
        txt = run(["osascript", "-e", 'tell application "System Events" to get name of every window of '
                                      '(every process whose name is "kicad")'])
        for t in re.split(r",\s*", txt.strip()):
            if t and t != "missing value":
                out.append((t, t))
        return out
    if shutil.which("wmctrl"):
        for line in run(["wmctrl", "-l"]).splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and _KICAD_TITLE_RE.search(parts[3]):
                out.append((parts[0], parts[3]))
    elif shutil.which("xdotool"):
        for wid in run(["xdotool", "search", "--name", "KiCad|PCB Editor|Schematic Editor"]).split():
            title = run(["xdotool", "getwindowname", wid]).strip()
            if title:
                out.append((wid, title))
    return out


def kicad_window_titles() -> list[tuple]:
    """(handle, title) for KiCad's top-level windows. Empty if the platform offers no way to list them."""
    if not IS_WIN:
        return _titles_unix()
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    result: list[tuple[int, str]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def exe_name(pid: int) -> str:
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return Path(buf.value).name.lower()
        finally:
            kernel32.CloseHandle(h)
        return ""

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if exe_name(pid.value) in ("kicad.exe", "eeschema.exe", "pcbnew.exe"):
            result.append((hwnd, buf.value))
        return True

    _ = psapi  # keep reference (loads psapi for older Windows)
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return result


# ---------------------------------------------------------------- projects
@dataclass
class ProjectInfo:
    pro_file: Path
    is_open: bool = False
    how: set = field(default_factory=set)

    @property
    def name(self) -> str:
        return self.pro_file.stem

    @property
    def dir(self) -> Path:
        return self.pro_file.parent

    @property
    def pcb_file(self) -> Path:
        return self.pro_file.with_suffix(".kicad_pcb")

    @property
    def sch_file(self) -> Path:
        return self.pro_file.with_suffix(".kicad_sch")

    def label(self) -> str:
        mark = "● OPEN  " if self.is_open else ""
        return f"{mark}{self.name}   —  {self.dir}"


def project_name(pro: Path) -> str:
    return pro.name[: -len(".kicad_pro")] if pro.name.endswith(".kicad_pro") else pro.stem


def window_project_names(title: str) -> set[str]:
    """Project names mentioned in a KiCad window title.

    Titles look like 'Proj — KiCad 10.0', '*Proj — PCB Editor' or
    'Sheet [Proj/Sheet] — Schematic Editor'.
    """
    main = re.split(r"\s+[—-]\s+", title)[0].lstrip("*").strip()
    names = {main}
    m = re.search(r"\[([^\]/\\]+)[/\\]", main)
    if m:
        names.add(m.group(1).strip())
        names.add(main.split(" [")[0].strip())
    return {n for n in names if n}


def _project_has_lock(pro: Path) -> bool:
    try:
        # KiCad writes ~<file>.lck next to every open schematic sheet / board
        return any(pro.parent.glob("~*.kicad_*.lck"))
    except OSError:
        pass
    return False


def ipc_open_projects(timeout_ms: int = 800) -> list[Path]:
    """Ask a running KiCad (IPC API) which documents are open."""
    try:
        from kipy import KiCad
        from kipy.proto.common.types import base_types_pb2 as bt
    except ImportError:
        return []
    out: list[Path] = []
    try:
        k = KiCad(timeout_ms=timeout_ms)
        for dt in (bt.DocumentType.DOCTYPE_PCB, bt.DocumentType.DOCTYPE_SCHEMATIC, bt.DocumentType.DOCTYPE_PROJECT):
            try:
                docs = k.get_open_documents(dt)
            except Exception:
                continue
            for d in docs:
                ppath, pname = d.project.path, d.project.name
                if not ppath:
                    continue  # e.g. schematic documents in KiCad 10 carry no project path
                p = Path(ppath)
                if p.name.endswith(".kicad_pro"):
                    cand = p
                else:
                    # project names may contain dots ("Board 1.1"), so never use Path.stem here
                    name = pname[:-len(".kicad_pro")] if pname.endswith(".kicad_pro") else pname
                    if not name and d.board_filename:
                        name = Path(d.board_filename).name.rsplit(".kicad_pcb", 1)[0]
                    cand = p / f"{name}.kicad_pro"
                    if not cand.exists():
                        pros = list(p.glob("*.kicad_pro"))
                        cand = pros[0] if len(pros) == 1 else cand
                if cand not in out:
                    out.append(cand)
    except Exception:
        return out
    return out


def detect_projects(ki: KiCadInstall) -> tuple[list[ProjectInfo], dict]:
    """Return candidate projects (open ones first) plus diagnostic status info."""
    status: dict = {}
    procs = running_kicad_processes()
    status["running"] = bool(procs)
    kjson = read_json(ki.config_dir / "kicad.json").get("system", {})
    history = [Path(p) for p in kjson.get("file_history", [])]
    last_open = [Path(p) for p in kjson.get("open_projects", [])]

    projects: dict[str, ProjectInfo] = {}

    def add(p: Path, open_: bool = False, how: str = "") -> None:
        key = str(p).lower()
        pi = projects.get(key) or ProjectInfo(p)
        projects[key] = pi
        if open_:
            pi.is_open = True
            pi.how.add(how)

    ipc = ipc_open_projects() if procs else []
    status["ipc"] = bool(ipc)
    for p in ipc:
        add(p, True, "ipc")

    titles = [t for _, t in kicad_window_titles()] if procs else []
    status["windows"] = titles
    title_names = set().union(*(window_project_names(t) for t in titles)) if titles else set()
    known = [p for p in dict.fromkeys(last_open + history) if p.exists()]
    for p in known:
        add(p)
    if procs and not ipc:
        # several projects can share a name; prefer the one(s) with KiCad lock files
        for name in title_names:
            same = [p for p in known if project_name(p) == name]
            locked = [p for p in same if _project_has_lock(p)]
            for p in (locked or same[:1]):
                add(p, True, "window")
        if not titles:  # no window info (non-Windows): fall back to lock files
            for p in known:
                if _project_has_lock(p):
                    add(p, True, "lock")
    if procs and not any(pi.is_open for pi in projects.values()):
        for p in last_open:
            if p.exists():
                add(p, True, "last-open")

    ordered = sorted(projects.values(), key=lambda pi: (not pi.is_open,))
    return ordered, status
