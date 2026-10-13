"""CAD availability (symbol / footprint / 3D model) and cached preview data.

CAD comes from EasyEDA/LCSC - the only big CAD library that can be downloaded
without logging in. Results from other suppliers are matched to an LCSC part by MPN.

EasyEDA's CDN blocks a computer for a while when it sends too many requests, so:
* nothing is fetched unless needed (selected part, CAD filter, import),
* every answer is cached on disk (CAD JSON and the converted KiCad files),
* requests are paced, and after a "blocked" answer Kiporter backs off for a few
  minutes instead of retrying (retrying only makes the block last longer).
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path

from .config import app_data_dir
from .library import MODEL_EXTS, StagedPart
from .providers import PartResult, find_lcsc_equivalent

EASYEDA_URL = "https://easyeda.com/api/products/{}/components?version=6.4.19.5"
HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Referer": "https://easyeda.com/",
}
MIN_GAP_S = 1.0  # at most about one EasyEDA request per second
BACKOFF_MIN = [2, 5, 15, 30]  # minutes to wait after successive blocks

_lock = threading.Lock()
_CACHE_FILE = app_data_dir() / "cad_cache.json"
_avail: dict[str, list] = {}
_equiv: dict[str, str] = {}
_equiv_pkg: dict[str, str] = {}  # MPN -> package of the matched LCSC part
_cad_json: dict[str, dict] = {}
_staged: dict[str, StagedPart] = {}
_loaded = False
_blocked_until = [0.0]
_block_level = [0]
_pace_lock = threading.Lock()
_last_call = [0.0]

NONE = (False, False, False)


class EasyedaBlocked(RuntimeError):
    pass


class EasyedaNoData(RuntimeError):
    pass


def _dir(name: str) -> Path:
    d = app_data_dir() / name
    d.mkdir(exist_ok=True)
    return d


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        _avail.update(data.get("avail", {}))
        _equiv.update(data.get("equiv", {}))
        _equiv_pkg.update(data.get("equiv_pkg", {}))
    except (OSError, ValueError):
        pass


def save_cache() -> None:
    with _lock:
        try:
            _CACHE_FILE.write_text(json.dumps({"avail": _avail, "equiv": _equiv, "equiv_pkg": _equiv_pkg}),
                                   encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------- block handling
def blocked_for() -> float:
    """Seconds until Kiporter will talk to EasyEDA again (0 = not blocked)."""
    return max(0.0, _blocked_until[0] - time.time())


def blocked_message() -> str:
    until = time.strftime("%H:%M", time.localtime(_blocked_until[0]))
    return ("EasyEDA has temporarily blocked requests from this computer (too many CAD lookups in a short "
            f"time). Kiporter waits and tries again after {until}. Parts you have already viewed still work; "
            "meanwhile 'Find CAD elsewhere' (SnapEDA / Ultra Librarian / SamacSys) is an alternative.")


def _trip() -> None:
    lvl = min(_block_level[0], len(BACKOFF_MIN) - 1)
    _blocked_until[0] = time.time() + BACKOFF_MIN[lvl] * 60
    _block_level[0] += 1


def _pace() -> None:
    with _pace_lock:
        wait = _last_call[0] + MIN_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _fetch(lcsc_id: str) -> dict:
    """One request to EasyEDA. Raises EasyedaBlocked / EasyedaNoData."""
    import requests
    if blocked_for():
        raise EasyedaBlocked(blocked_message())
    _pace()
    try:
        r = requests.get(EASYEDA_URL.format(lcsc_id), headers=HEADERS, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"Could not reach EasyEDA: {e}") from e
    if r.status_code in (403, 429):
        _trip()
        raise EasyedaBlocked(blocked_message())
    try:
        data = r.json()
    except ValueError as e:
        raise RuntimeError(f"EasyEDA sent an unexpected answer ({r.status_code})") from e
    _block_level[0] = 0
    if not data.get("success", True) or not data.get("result"):
        raise EasyedaNoData(f"EasyEDA has no CAD data for {lcsc_id}")
    return data["result"]


def cad_data(lcsc_id: str) -> dict:
    """Raw EasyEDA CAD JSON for an LCSC part - from memory, disk cache, or one request."""
    lcsc_id = lcsc_id.strip().upper()
    if lcsc_id in _cad_json:
        return _cad_json[lcsc_id]
    f = _dir("easyeda_cache") / f"{lcsc_id}.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            _cad_json[lcsc_id] = data
            return data
        except (OSError, ValueError):
            pass
    data = _fetch(lcsc_id)
    with _lock:
        _cad_json[lcsc_id] = data
    try:
        f.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return data


def _avail_from(data: dict) -> tuple[bool, bool, bool]:
    shapes = ((data.get("packageDetail") or {}).get("dataStr") or {}).get("shape") or []
    return (bool((data.get("dataStr") or {}).get("shape")), bool(shapes),
            any(str(x).startswith("SVGNODE~") for x in shapes))


def cached_availability(lcsc_id: str) -> tuple | None:
    _load()
    v = _avail.get(lcsc_id.strip().upper())
    return tuple(v) if v is not None else None


def availability(lcsc_id: str) -> tuple[bool, bool, bool]:
    """(symbol, footprint, 3D model) available at EasyEDA. May raise EasyedaBlocked."""
    lcsc_id = lcsc_id.strip().upper()
    cached = cached_availability(lcsc_id)
    if cached is not None:
        return cached  # type: ignore[return-value]
    try:
        res = _avail_from(cad_data(lcsc_id))
    except EasyedaNoData:
        res = NONE
    with _lock:
        _avail[lcsc_id] = list(res)
    return res


# ---------------------------------------------------------------- results
def lcsc_for(r: PartResult) -> str:
    """LCSC part number to use for CAD data ('' if none). Uses JLCPCB search, not EasyEDA."""
    if r.lcsc_id:
        return r.lcsc_id
    _load()
    key = r.mpn.strip().upper()
    if key in _equiv:
        return _equiv[key]
    eq = find_lcsc_equivalent(r.mpn)
    with _lock:
        _equiv[key] = eq.supplier_pn if eq else ""
        if eq and eq.package:
            _equiv_pkg[key] = eq.package
    return _equiv[key]


def fill_known(r: PartResult) -> None:
    """Fill r.cad from the caches only (no network)."""
    _load()
    lcsc = r.lcsc_id or _equiv.get(r.mpn.strip().upper())
    if lcsc is None:
        return
    r.cad_lcsc = lcsc
    if not lcsc:
        r.cad = NONE
        return
    cached = cached_availability(lcsc)
    if cached is not None:
        r.cad = cached
        r.cad_source = "EasyEDA"
    if not r.package:
        r.package = _equiv_pkg.get(r.mpn.strip().upper(), "")


def check(r: PartResult) -> tuple[bool, bool, bool]:
    """Fill r.cad / r.cad_lcsc. May raise EasyedaBlocked (then r.cad stays unknown)."""
    lcsc = lcsc_for(r)
    r.cad_lcsc = lcsc
    if not r.package:  # e.g. Mouser gives no package: take it from the matched LCSC part
        r.package = _equiv_pkg.get(r.mpn.strip().upper(), "")
    r.cad = availability(lcsc) if lcsc else NONE
    return r.cad


def score(r: PartResult) -> int:
    """3 = symbol+footprint+3D, 2 = symbol+footprint, 1 = something, 0 = nothing, -1 = unknown."""
    if r.cad is None:
        return -1
    s, f, m = r.cad
    return 3 if s and f and m else 2 if s and f else 1 if s or f else 0


def label(r: PartResult) -> str:
    if r.cad is None:
        return "?"
    s, f, m = r.cad
    parts = [n for n, ok in (("Sym", s), ("Fp", f), ("3D", m)) if ok]
    if not parts:
        return "—"
    return "+".join(parts) + (f"  · {r.cad_source}" if r.cad_source else "")


# ---------------------------------------------------------------- converted parts (kept on disk)
def _load_staged(d: Path, lcsc_id: str) -> StagedPart | None:
    syms = sorted(d.glob("*.kicad_sym"))
    fps = sorted((d / "footprints").glob("*.kicad_mod"))
    if not syms and not fps:
        return None
    models = sorted(p for p in (d / "3d").glob("*") if p.suffix.lower() in MODEL_EXTS)
    # the footprint references the .wrl (EasyEDA bakes its offset into it); keep it first
    models.sort(key=lambda p: p.suffix.lower() != ".wrl")
    return StagedPart(syms, fps, models, {}, f"EasyEDA {lcsc_id}")


def staged(lcsc_id: str, data: dict | None = None) -> StagedPart:
    """Symbol/footprint/3D files for an LCSC part, converted once and kept on disk."""
    from .importers import stage_lcsc
    lcsc_id = lcsc_id.strip().upper()
    if lcsc_id not in _staged:
        d = _dir("parts") / lcsc_id
        part = _load_staged(d, lcsc_id) if d.exists() else None
        if part is None:
            try:
                raw = cad_data(lcsc_id)
            except EasyedaNoData as e:
                raise RuntimeError(f"EasyEDA has no CAD model for {lcsc_id} - use 'Find CAD elsewhere'.") from e
            tmp = stage_lcsc(lcsc_id, {}, cad=raw)
            (d / "footprints").mkdir(parents=True, exist_ok=True)
            (d / "3d").mkdir(exist_ok=True)
            for f in tmp.symbol_files:
                shutil.copy2(f, d / f.name)
            for f in tmp.footprint_files:
                shutil.copy2(f, d / "footprints" / f.name)
            for f in tmp.model_files:
                shutil.copy2(f, d / "3d" / f.name)
            part = _load_staged(d, lcsc_id) or tmp
            with _lock:
                _avail[lcsc_id] = list(_avail_from(raw))
        with _lock:
            _staged[lcsc_id] = part
    base = _staged[lcsc_id]
    return replace(base, data={"lcsc": lcsc_id, **(data or {})})


def image_bytes(url: str, timeout: int = 20) -> bytes | None:
    if not url:
        return None
    import requests
    from .providers import UA
    try:
        r = requests.get(url, headers={**UA, "Referer": "https://jlcpcb.com/"}, timeout=timeout)
        if r.status_code == 200 and len(r.content) > 200:
            return r.content
    except requests.RequestException:
        pass
    return None


def preview_dir() -> Path:
    return _dir("preview")
