"""Where symbols / footprints / 3D models come from.

* EasyEDA   - LCSC's free library; no account (matched by MPN for other suppliers).
* SamacSys  - Component Search Engine (the models Mouser links to). Searching is open;
              downloading needs a free Component Search Engine account.
* Octopart  - Nexar API (needs the Octopart keys): lists every CAD model Octopart knows
              (SnapEDA, Ultra Librarian, SamacSys, ...) with KiCad download links.
* SnapEDA   - login-only website without a public API: found through Octopart when its keys
              are set, otherwise opened in the browser (the downloaded zip is imported).
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

import requests

from . import cad
from .config import Settings, app_data_dir
from .library import StagedPart
from .providers import UA, PartResult, _norm_mpn

SOURCES = ["EasyEDA", "SamacSys", "Octopart", "SnapEDA"]
DEFAULT_ORDER = ["EasyEDA", "SamacSys", "Octopart", "SnapEDA"]

CSE_SEARCH = "https://rs.componentsearchengine.com/alligatorHandler.php"
CSE_MODEL = "https://rs.componentsearchengine.com/ga/model.php"
CSE_SIGNUP = "https://componentsearchengine.com/signup"

_lock = threading.Lock()
_cse_cache: dict[str, list] = {}
_octo_cache: dict[str, list] = {}
_staged: dict[tuple, StagedPart] = {}


class NeedsLogin(RuntimeError):
    pass


class NeedsBrowser(RuntimeError):
    def __init__(self, msg: str, url: str):
        super().__init__(msg)
        self.url = url


@dataclass
class CadHit:
    source: str
    ref: str  # LCSC number, CSE part id, or a download URL
    sym: bool = False
    fp: bool = False
    m3d: bool = False
    detail: str = ""
    page: str = ""
    previews: dict = field(default_factory=dict)  # symbol/footprint/threeD image URLs
    browser_only: bool = False

    @property
    def avail(self) -> tuple[bool, bool, bool]:
        return self.sym, self.fp, self.m3d

    @property
    def label(self) -> str:
        if self.browser_only:
            return f"{self.source} - opens in your browser"
        parts = [n for n, ok in (("Sym", self.sym), ("Fp", self.fp), ("3D", self.m3d)) if ok]
        return f"{self.source} - {'+'.join(parts) or 'nothing'}" + (f"  ({self.detail})" if self.detail else "")


def order(settings: Settings, r: PartResult | None = None) -> list[str]:
    """Sources to use, in the user's order. LCSC parts always use their own EasyEDA model."""
    if r is not None and r.source == "LCSC":
        return ["EasyEDA"]
    o = [s for s in (settings.cad_order or []) if s in SOURCES]
    o += [s for s in SOURCES if s not in o]
    return [s for s in o if s not in (settings.cad_disabled or [])]


def usable(source: str, settings: Settings) -> bool:
    """Can this source be asked at all with the current settings?"""
    if source == "Octopart":
        return bool(settings.nexar_client_id and settings.nexar_client_secret)
    return True


# ==================================================================== finders
def _find_easyeda(r: PartResult) -> list[CadHit]:
    lcsc = cad.lcsc_for(r)
    if not lcsc:
        return []
    s, f, m = cad.availability(lcsc)
    if not (s or f):
        return []
    return [CadHit("EasyEDA", lcsc, s, f, m, f"LCSC {lcsc}", page=f"https://www.lcsc.com/product-detail/{lcsc}.html")]


def cse_search(mpn: str) -> list[dict]:
    key = mpn.strip().upper()
    if key in _cse_cache:
        return _cse_cache[key]
    try:
        r = requests.get(CSE_SEARCH, params={"searchString": mpn, "country": "US"}, headers=UA, timeout=25)
        parts = r.json().get("parts") or [] if r.status_code == 200 else []
    except (requests.RequestException, ValueError):
        parts = []
    with _lock:
        _cse_cache[key] = parts
    return parts


def _find_samacsys(r: PartResult) -> list[CadHit]:
    target = _norm_mpn(r.mpn)
    cands = []
    for p in cse_search(r.mpn):
        if not p.get("ECAD_M") or not p.get("PartID"):
            continue
        pn = _norm_mpn(p.get("PartNo", ""))
        exact = pn == target
        if not exact and not (pn.startswith(target) and len(pn) - len(target) <= 3):
            continue
        try:
            q = int(p.get("Quality") or 0)
        except (TypeError, ValueError):
            q = 0
        hit = CadHit("SamacSys", str(p["PartID"]), True, True, p.get("Has3D") in ("Y", "E"),
                     f"{p.get('PartNo')}, quality {q}" if q else p.get("PartNo", ""),
                     page=f"https://componentsearchengine.com/search?term={quote_plus(r.mpn)}",
                     previews={"image": p.get("ImageL") or ""})
        cands.append((not exact, -q, len(cands), hit))
    cands.sort(key=lambda t: t[:3])
    return [cands[0][3]] if cands else []


OCTO_QUERY = """
query Cad($q: String!) {
  supSearchMpn(q: $q, limit: 3) {
    results { part {
      mpn
      cad { hasKicad has3dModel downloadUrlKicad symbolImageUrl footprintImageUrl addToLibraryUrl }
      cadModels { models {
        source { name } hasSymbol hasFootprint has3dModel level cadSourceUrl
        cadDownloadUrls { kicad } previewUrls { symbol footprint threeD }
      } }
    } }
  }
}"""


def _octopart_models(r: PartResult, settings: Settings) -> list[CadHit]:
    from .providers import Nexar
    key = r.mpn.strip().upper()
    if key in _octo_cache:
        return _octo_cache[key]
    prov = Nexar(settings)
    resp = requests.post(prov.GQL_URL, json={"query": OCTO_QUERY, "variables": {"q": r.mpn}},
                         headers={"Authorization": f"Bearer {prov._get_token()}"}, timeout=30)
    d = resp.json()
    if d.get("errors") and not d.get("data"):
        raise RuntimeError("Octopart: " + "; ".join(e.get("message", "") for e in d["errors"]))
    hits: list[CadHit] = []
    target = _norm_mpn(r.mpn)
    for res in ((d.get("data") or {}).get("supSearchMpn") or {}).get("results") or []:
        part = res.get("part") or {}
        if _norm_mpn(part.get("mpn", "")) != target:
            continue
        for m in ((part.get("cadModels") or {}).get("models") or []):
            url = (m.get("cadDownloadUrls") or {}).get("kicad") or ""
            src = ((m.get("source") or {}).get("name") or "Octopart").strip()
            pv = m.get("previewUrls") or {}
            hits.append(CadHit("Octopart", url, bool(m.get("hasSymbol")), bool(m.get("hasFootprint")),
                               bool(m.get("has3dModel")), f"by {src}" + (f", level {m['level']}" if m.get("level") else ""),
                               page=m.get("cadSourceUrl") or url, previews=pv, browser_only=not url))
        c = part.get("cad") or {}
        if not hits and (c.get("hasKicad") or c.get("downloadUrlKicad")):
            hits.append(CadHit("Octopart", c.get("downloadUrlKicad") or "", True, True, bool(c.get("has3dModel")),
                               "Octopart CAD", page=c.get("addToLibraryUrl") or c.get("downloadUrlKicad") or "",
                               previews={"symbol": c.get("symbolImageUrl"), "footprint": c.get("footprintImageUrl")},
                               browser_only=not c.get("downloadUrlKicad")))
        break
    with _lock:
        _octo_cache[key] = hits
    return hits


def _find_octopart(r: PartResult, settings: Settings) -> list[CadHit]:
    return [h for h in _octopart_models(r, settings) if "snap" not in h.detail.lower()] or \
        [h for h in _octopart_models(r, settings)]


def _find_snapeda(r: PartResult, settings: Settings) -> list[CadHit]:
    if usable("Octopart", settings):
        try:
            snap = [h for h in _octopart_models(r, settings) if "snap" in h.detail.lower()]
        except Exception:  # noqa: BLE001
            snap = []
        for h in snap:
            h.source, h.detail = "SnapEDA", h.detail.replace("by ", "via Octopart, by ")
        if snap:
            return snap
    url = f"https://www.snapeda.com/search/?q={quote_plus(r.mpn)}"
    return [CadHit("SnapEDA", url, page=url, browser_only=True, detail="sign in on snapeda.com to download")]


def find(source: str, r: PartResult, settings: Settings) -> list[CadHit]:
    if source == "EasyEDA":
        return _find_easyeda(r)
    if source == "SamacSys":
        return _find_samacsys(r)
    if source == "Octopart":
        return _find_octopart(r, settings) if usable("Octopart", settings) else []
    if source == "SnapEDA":
        return _find_snapeda(r, settings)
    return []


def find_all(r: PartResult, settings: Settings, log=lambda m: None) -> list[CadHit]:
    """Hits from every usable source, in the user's preferred order."""
    hits: list[CadHit] = []
    for src in order(settings, r):
        if not usable(src, settings):
            continue
        try:
            hits += find(src, r, settings)
        except cad.EasyedaBlocked as e:
            log(str(e))
        except Exception as e:  # noqa: BLE001
            log(f"{src}: {e}")
    return hits


def best(hits: list[CadHit], preferred: str = "auto") -> CadHit | None:
    """First downloadable hit with symbol + footprint (or from the forced source)."""
    pool = [h for h in hits if h.source == preferred] if preferred != "auto" else hits
    for h in pool:
        if not h.browser_only and h.sym and h.fp:
            return h
    return pool[0] if pool else None


def check_availability(r: PartResult, settings: Settings) -> None:
    """Fill r.cad / r.cad_source for the CAD column, using only sources that need no download.

    SamacSys search is free; EasyEDA costs one (cached) request; Octopart uses its API quota."""
    pref = settings.cad_source
    if r.source == "LCSC":
        srcs = ["EasyEDA"]
    else:
        srcs = [pref] if pref != "auto" else [s for s in order(settings, r) if s != "SnapEDA"]
    for src in srcs:
        if src == "SnapEDA" or not usable(src, settings):
            continue
        try:
            hits = find(src, r, settings)
        except cad.EasyedaBlocked:
            raise
        except Exception:  # noqa: BLE001
            continue
        h = next((h for h in hits if h.sym or h.fp), None)
        if not r.package:  # e.g. Mouser gives no package: use the matched LCSC part's
            r.package = cad._equiv_pkg.get(r.mpn.strip().upper(), "")
        if h:
            r.cad, r.cad_source = h.avail, h.source
            if h.source == "EasyEDA":
                r.cad_lcsc = h.ref
            return
    if r.cad is None:
        r.cad, r.cad_source = cad.NONE, ""


# ==================================================================== download
def _zip_dir(source: str) -> Path:
    d = app_data_dir() / "cad_downloads" / source.lower()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_zip(source: str, ref: str, content: bytes) -> Path:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", ref)[:60] or hashlib.md5(ref.encode()).hexdigest()
    p = _zip_dir(source) / f"{name}.zip"
    p.write_bytes(content)
    return p


def cse_test_login(settings: Settings) -> str:
    """Check the Component Search Engine account with a real download of a common part."""
    if not (settings.cse_username and settings.cse_password):
        raise NeedsLogin("Enter your Component Search Engine username and password")
    r = requests.get(CSE_MODEL, params={"partID": "2282"}, auth=(settings.cse_username, settings.cse_password),
                     headers=UA, timeout=45)
    if r.status_code == 401:
        raise RuntimeError("Component Search Engine did not accept the username / password")
    if r.status_code != 200 or not r.content.startswith(b"PK"):
        raise RuntimeError(f"unexpected answer from Component Search Engine (HTTP {r.status_code})")
    return "login works"


def fetch(hit: CadHit, settings: Settings, ki, data: dict | None = None) -> StagedPart:
    """Download (or reuse) the model files for a hit. Raises NeedsLogin / NeedsBrowser."""
    from dataclasses import replace
    from .importers import stage_zip
    if hit.source == "EasyEDA":
        return cad.staged(hit.ref, data)
    if hit.browser_only or not hit.ref:
        raise NeedsBrowser(f"{hit.source} models download in your browser.", hit.page or hit.ref)
    key = (hit.source, hit.ref)
    if key not in _staged:
        cached = _zip_dir(hit.source) / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', hit.ref)[:60]}.zip"
        if cached.exists():
            path = cached
        elif hit.source == "SamacSys":
            if not (settings.cse_username and settings.cse_password):
                raise NeedsLogin("Downloading SamacSys models needs your free Component Search Engine account "
                                 "(Settings → CAD sources).")
            resp = requests.get(CSE_MODEL, params={"partID": hit.ref},
                                auth=(settings.cse_username, settings.cse_password), headers=UA, timeout=60)
            if resp.status_code == 401:
                raise NeedsLogin("Component Search Engine did not accept your username / password "
                                 "(Settings → CAD sources).")
            if resp.status_code != 200 or not resp.content.startswith(b"PK"):
                raise RuntimeError(f"SamacSys download failed (HTTP {resp.status_code})")
            path = _save_zip("SamacSys", hit.ref, resp.content)
        else:  # Octopart / SnapEDA download links
            resp = requests.get(hit.ref, headers=UA, timeout=60, allow_redirects=True)
            if resp.status_code != 200 or not resp.content.startswith(b"PK"):
                raise NeedsBrowser(f"{hit.source} wants you to sign in to download this model.", hit.ref)
            path = _save_zip(hit.source, hit.ref, resp.content)
        part = stage_zip(path, ki, {})
        with _lock:
            _staged[key] = part
    return replace(_staged[key], data=dict(data or {}))
