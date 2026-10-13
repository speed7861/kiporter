"""CAD availability (symbol / footprint / 3D model) and cached preview data.

Availability comes from EasyEDA/LCSC - the source Kiporter can import from
automatically. Results from other suppliers are matched to an LCSC part by MPN.
Everything is cached: availability on disk, downloaded CAD data per session.
"""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

from .config import app_data_dir
from .library import StagedPart
from .providers import PartResult, find_lcsc_equivalent

_lock = threading.Lock()
_CACHE_FILE = app_data_dir() / "cad_cache.json"
_avail: dict[str, list] = {}
_equiv: dict[str, str] = {}
_cad_json: dict[str, dict] = {}
_staged: dict[str, StagedPart] = {}
_loaded = False

NONE = (False, False, False)


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        _avail.update(data.get("avail", {}))
        _equiv.update(data.get("equiv", {}))
    except (OSError, ValueError):
        pass


def save_cache() -> None:
    with _lock:
        try:
            _CACHE_FILE.write_text(json.dumps({"avail": _avail, "equiv": _equiv}), encoding="utf-8")
        except OSError:
            pass


_pace_lock = threading.Lock()
_last_call = [0.0]
MIN_GAP_S = 0.35  # spacing between EasyEDA requests across all threads


def _pace() -> None:
    import time
    with _pace_lock:
        wait = _last_call[0] + MIN_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def cad_data(lcsc_id: str) -> dict | None:
    """Raw EasyEDA CAD JSON for an LCSC part (cached for this session)."""
    lcsc_id = lcsc_id.strip().upper()
    if lcsc_id in _cad_json:
        return _cad_json[lcsc_id]
    import time
    from easyeda2kicad.easyeda.easyeda_api import EasyedaApi
    data = None
    for attempt in range(3):  # EasyEDA answers 403 when hit too fast: back off and retry
        _pace()
        data = EasyedaApi().get_cad_data_of_component(lcsc_id=lcsc_id) or None
        if data:
            break
        time.sleep(0.8 * (attempt + 1))
    if data:
        with _lock:
            _cad_json[lcsc_id] = data
    return data


def availability(lcsc_id: str) -> tuple[bool, bool, bool]:
    """(symbol, footprint, 3D model) available at EasyEDA for this LCSC part."""
    _load()
    lcsc_id = lcsc_id.strip().upper()
    if lcsc_id in _avail:
        return tuple(_avail[lcsc_id])  # type: ignore[return-value]
    data = cad_data(lcsc_id)
    if not data:
        res = NONE
    else:
        shapes = ((data.get("packageDetail") or {}).get("dataStr") or {}).get("shape") or []
        res = (bool((data.get("dataStr") or {}).get("shape")), bool(shapes),
               any(str(x).startswith("SVGNODE~") for x in shapes))
    if res != NONE:  # never cache a miss - it may have been a temporary API error
        with _lock:
            _avail[lcsc_id] = list(res)
    return res


def lcsc_for(r: PartResult) -> str:
    """LCSC part number to use for CAD data ('' if none)."""
    if r.lcsc_id:
        return r.lcsc_id
    _load()
    key = r.mpn.strip().upper()
    if key in _equiv:
        return _equiv[key]
    eq = find_lcsc_equivalent(r.mpn)
    with _lock:
        _equiv[key] = eq.supplier_pn if eq else ""
    return _equiv[key]


def check(r: PartResult) -> tuple[bool, bool, bool]:
    """Fill r.cad / r.cad_lcsc."""
    lcsc = lcsc_for(r)
    r.cad_lcsc = lcsc
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
        return "…"
    s, f, m = r.cad
    parts = [n for n, ok in (("Sym", s), ("Fp", f), ("3D", m)) if ok]
    return "+".join(parts) if parts else "—"


def staged(lcsc_id: str, data: dict | None = None) -> StagedPart:
    """Symbol/footprint/3D files for an LCSC part, converted once per session and reused."""
    from .importers import stage_lcsc
    lcsc_id = lcsc_id.strip().upper()
    if lcsc_id not in _staged:
        part = stage_lcsc(lcsc_id, {}, cad=cad_data(lcsc_id))
        with _lock:
            _staged[lcsc_id] = part
    base = _staged[lcsc_id]
    merged = {"lcsc": lcsc_id, **(data or {})}
    return replace(base, data=merged)


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
    d = app_data_dir() / "preview"
    d.mkdir(exist_ok=True)
    return d
