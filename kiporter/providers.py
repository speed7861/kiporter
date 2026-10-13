"""Part search: LCSC (JLCPCB catalogue), Digi-Key, Mouser, Farnell/Newark/element14, TME, Octopart (Nexar).

Every provider returns PartResult objects with the same normalised columns
(stock as a number, lead time in days, unit price, package, ...), so results
from different suppliers line up and sort consistently.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from urllib.parse import quote_plus

import requests

from .config import Settings

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Kiporter/2.0"}
TIMEOUT = 25


class ProviderError(Exception):
    pass


class NotConfigured(ProviderError):
    pass


# canonical field key for each source's own part number
SUPPLIER_KEY = {"LCSC": "lcsc", "Digi-Key": "digikey", "Mouser": "mouser", "Farnell": "farnell",
                "Newark": "farnell", "element14": "farnell", "TME": "tme"}


@dataclass
class PartResult:
    source: str
    mpn: str
    manufacturer: str = ""
    description: str = ""
    package: str = ""
    category: str = ""
    stock: int | None = None
    lead_days: int | None = None
    price: float | None = None  # unit price at the lowest quantity break
    currency: str = ""
    moq: int | None = None
    supplier_pn: str = ""
    datasheet: str = ""
    url: str = ""
    lcsc_id: str = ""
    lifecycle: str = ""
    image_url: str = ""
    extra: dict = field(default_factory=dict)
    # CAD availability (filled in the background): (symbol, footprint, 3D model) and the LCSC part used
    cad: tuple | None = None
    cad_lcsc: str = ""
    cad_source: str = ""  # which CAD source the availability came from

    def __post_init__(self) -> None:
        # one package spelling for every supplier: "8-SOIC (0.154\", 3.90mm)" -> "SOIC-8"
        self.package = normalize_package(self.package)

    # ---- display helpers (identical formatting for every source)
    @property
    def stock_str(self) -> str:
        return f"{self.stock:,}" if self.stock is not None else ""

    @property
    def lead_str(self) -> str:
        if self.lead_days is None:
            return ""
        if self.lead_days <= 0:
            return "stock"
        weeks = self.lead_days / 7
        return f"{weeks:.0f} wk" if weeks >= 1 else f"{self.lead_days} d"

    @property
    def price_str(self) -> str:
        if self.price is None:
            return ""
        sym = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "C$", "": "$"}.get(self.currency, self.currency + " ")
        return f"{sym}{self.price:.4g}"

    def part_data(self) -> dict[str, str]:
        """Canonical field data (see fields.py) written into the imported symbol."""
        d = {
            "manufacturer": self.manufacturer,
            "mpn": self.mpn,
            "description": self.description,
            "datasheet": self.datasheet,
            "package": self.package,
        }
        key = SUPPLIER_KEY.get(self.source)
        if key and self.supplier_pn:
            d[key] = self.supplier_pn
        if self.lcsc_id:
            d["lcsc"] = self.lcsc_id
        return {k: v.strip() for k, v in d.items() if v and v.strip()}


# ---------------------------------------------------------------- helpers
def _int(v) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"\d[\d,\.]*", str(v))
    if not m:
        return None
    try:
        return int(float(m.group(0).replace(",", "")))
    except ValueError:
        return None


def _float(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\d[\d.,]*", str(v))
    if not m:
        return None
    s = m.group(0).rstrip(".,")
    if "," in s and "." in s:  # 1,234.56
        s = s.replace(",", "")
    elif "," in s:  # 0,52 (decimal comma) vs 1,234 (thousands)
        s = s.replace(",", "") if re.search(r",\d{3}$", s) else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _lead_days(text) -> int | None:
    """'84 Days' / '12 Weeks' / 16 (weeks) -> days."""
    if text is None or text == "":
        return None
    s = str(text).lower()
    n = _float(s)
    if n is None:
        return None
    if "week" in s or "wk" in s:
        return int(n * 7)
    return int(n)


_PKG_RE = re.compile(
    r"\b((?:P|T|S|SS|TS|HTS|M|V|W|U|X|L|LF|H|E|D)?(?:SOIC|SOP|SSOP|TSSOP|MSOP|QFN|DFN|QFP|LQFP|TQFP|BGA|LGA|"
    r"SON|SOT|SOD|DIP|PDIP|SIP|TO|DO|SMA|SMB|SMC|SC|WLCSP|CSP|PLCC|DPAK|D2PAK|PowerPAK|MELF)"
    r"(?:[- ]?\d+[A-Z0-9\-]*)?(?:\(\d\))?)\b|\b(0075|01005|0201|0402|0603|0805|1008|1206|1210|1812|2010|2220|2512)\b",
    re.IGNORECASE)


_PKG_PREFIX_RE = re.compile(r"\b(\d{1,3})-?(SOIC|SOP|SSOP|TSSOP|MSOP|QFN|DFN|DIP|LQFP|TQFP|QFP|BGA|SON|SIP)\b",
                            re.IGNORECASE)


def normalize_package(pkg: str) -> str:
    pkg = (pkg or "").strip()
    m = _PKG_PREFIX_RE.match(pkg)
    return f"{m.group(2).upper()}-{m.group(1)}" if m else pkg


def guess_package(*texts: str) -> str:
    for t in texts:
        if not t:
            continue
        m = _PKG_PREFIX_RE.search(t)  # Digi-Key style "8-SOIC" / "8SOIC"
        if m:
            return f"{m.group(2).upper()}-{m.group(1)}"
        m = _PKG_RE.search(t)
        if m:
            return (m.group(1) or m.group(2)).upper()
    return ""


def _http_error(name: str, r: requests.Response) -> ProviderError:
    return ProviderError(f"{name} error {r.status_code}: {r.text[:300]}")


# ---------------------------------------------------------------- base
class Provider:
    name = ""
    key_fields: tuple[str, ...] = ()  # settings attributes that must be filled
    signup_url = ""
    help_text = ""

    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def configured(self) -> bool:
        return all(getattr(self.s, f, "") for f in self.key_fields)

    def search(self, keyword: str, limit: int = 50) -> list[PartResult]:
        raise NotImplementedError

    def test(self) -> str:
        res = self.search("LM358", limit=3)
        return f"OK - {len(res)} results for LM358"


# ---------------------------------------------------------------- LCSC / JLCPCB
JLC_SEARCH = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"


class LCSC(Provider):
    name = "LCSC"
    help_text = "LCSC/JLCPCB needs no account. Every LCSC part comes with a free EasyEDA symbol, footprint and 3D model."

    def search(self, keyword: str, limit: int = 50) -> list[PartResult]:
        try:
            r = requests.post(JLC_SEARCH, json={"keyword": keyword, "currentPage": 1, "pageSize": limit},
                              headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            raise ProviderError(f"LCSC search failed: {e}") from e
        if data.get("code") != 200:
            raise ProviderError(f"LCSC search failed: {data.get('message') or data.get('code')}")
        items = ((data.get("data") or {}).get("componentPageInfo") or {}).get("list") or []
        out = []
        for it in items:
            prices = it.get("componentPrices") or []
            code = it.get("componentCode", "")
            lib_type = it.get("componentLibraryType", "")
            out.append(PartResult(
                source="LCSC",
                mpn=it.get("componentModelEn", ""),
                manufacturer=it.get("componentBrandEn", ""),
                description=(it.get("describe") or "").strip(),
                package=it.get("componentSpecificationEn", ""),
                category=it.get("componentTypeEn", ""),
                stock=_int(it.get("stockCount")),
                price=_float(prices[0]["productPrice"]) if prices else None,
                currency="USD",
                moq=_int(it.get("minPurchaseNum")),
                supplier_pn=code,
                datasheet=it.get("dataManualUrl") or it.get("dataManualOfficialLink") or "",
                url=it.get("lcscGoodsUrl") or f"https://www.lcsc.com/product-detail/{code}.html",
                lcsc_id=code,
                image_url=(f"https://jlcpcb.com/api/file/downloadByFileSystemAccessId/{it['productBigImageAccessId']}"
                           if it.get("productBigImageAccessId") else ""),
                extra={
                    "JLCPCB library": {"base": "Basic", "expand": "Extended"}.get(lib_type, lib_type),
                    "Attributes": "; ".join(f"{a.get('attribute_name_en')}: {a.get('attribute_value_name')}"
                                            for a in (it.get("attributes") or [])),
                },
            ))
        return out


def search_lcsc(keyword: str, limit: int = 50) -> list[PartResult]:
    return LCSC(Settings()).search(keyword, limit)


def _norm_mpn(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def find_lcsc_equivalent(mpn: str) -> PartResult | None:
    """Look for an LCSC part with the same manufacturer part number (has EasyEDA CAD data)."""
    if not mpn:
        return None
    try:
        results = search_lcsc(mpn, limit=20)
    except ProviderError:
        return None
    target = _norm_mpn(mpn)
    exact = [r for r in results if _norm_mpn(r.mpn) == target]
    if exact:
        return max(exact, key=lambda r: r.stock or 0)
    # tolerate packaging suffixes such as -TR / /R7 / #PBF
    loose = [r for r in results if _norm_mpn(r.mpn).startswith(target) or target.startswith(_norm_mpn(r.mpn))]
    loose = [r for r in loose if len(_norm_mpn(r.mpn)) >= max(4, len(target) - 4)]
    return loose[0] if loose else None


# ---------------------------------------------------------------- Digi-Key
class DigiKey(Provider):
    name = "Digi-Key"
    key_fields = ("digikey_client_id", "digikey_client_secret")
    signup_url = "https://developer.digikey.com/"
    help_text = ("1. Sign in at developer.digikey.com with your normal Digi-Key login.\n"
                 "2. My Apps → Create App (Production). Tick 'ProductInformation V4'.\n"
                 "3. Copy the app's Client ID and Client Secret here.")
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._token: str | None = None
        self._token_exp = 0.0

    def reset(self) -> None:
        self._token = None

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        try:
            r = requests.post(self.TOKEN_URL, data={
                "client_id": self.s.digikey_client_id.strip(),
                "client_secret": self.s.digikey_client_secret.strip(),
                "grant_type": "client_credentials",
            }, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise ProviderError(f"Digi-Key login failed: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"Digi-Key login failed ({r.status_code}): {r.text[:300]}")
        d = r.json()
        self._token = d["access_token"]
        self._token_exp = time.time() + float(d.get("expires_in", 600))
        return self._token

    def search(self, keyword: str, limit: int = 50) -> list[PartResult]:
        if not self.configured:
            raise NotConfigured("Digi-Key is not connected")
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "X-DIGIKEY-Client-Id": self.s.digikey_client_id.strip(),
            "X-DIGIKEY-Locale-Site": self.s.digikey_locale_site or "US",
            "X-DIGIKEY-Locale-Language": "en",
            "X-DIGIKEY-Locale-Currency": self.s.digikey_currency or "USD",
            "Content-Type": "application/json",
        }
        body = {"Keywords": keyword, "Limit": min(limit, 50), "Offset": 0}
        if self.s.in_stock_only:
            body["FilterOptionsRequest"] = {"SearchOptions": ["InStock"]}
        try:
            r = requests.post(self.SEARCH_URL, json=body, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise ProviderError(f"Digi-Key search failed: {e}") from e
        if r.status_code != 200:
            raise _http_error("Digi-Key", r)
        d = r.json()
        products = list(d.get("ExactMatches") or []) + list(d.get("Products") or [])
        out, seen = [], set()
        for p in products:
            mpn = p.get("ManufacturerProductNumber", "")
            variations = p.get("ProductVariations") or []
            # prefer the cut-tape / single-unit variation for the part number and price
            var = min(variations, key=lambda v: _int(v.get("MinimumOrderQuantity")) or 1) if variations else {}
            dk_pn = var.get("DigiKeyProductNumber", "")
            if (mpn, dk_pn) in seen:
                continue
            seen.add((mpn, dk_pn))
            desc = p.get("Description") or {}
            params = p.get("Parameters") or []

            def param(*names):
                for x in params:
                    if (x.get("ParameterText") or "").lower() in names:
                        return x.get("ValueText", "")
                return ""
            pricing = var.get("StandardPricing") or []
            out.append(PartResult(
                source="Digi-Key",
                mpn=mpn,
                manufacturer=(p.get("Manufacturer") or {}).get("Name", ""),
                description=desc.get("ProductDescription") or desc.get("DetailedDescription", ""),
                package=param("supplier device package") or param("package / case"),
                category=(p.get("Category") or {}).get("Name", ""),
                stock=_int(p.get("QuantityAvailable")),
                lead_days=_lead_days(f"{p['ManufacturerLeadWeeks']} weeks") if p.get("ManufacturerLeadWeeks") else None,
                price=_float(pricing[0].get("UnitPrice")) if pricing else _float(p.get("UnitPrice")),
                currency=self.s.digikey_currency or "USD",
                moq=_int(var.get("MinimumOrderQuantity")),
                supplier_pn=dk_pn,
                datasheet=p.get("DatasheetUrl") or "",
                url=p.get("ProductUrl") or "",
                lifecycle=(p.get("ProductStatus") or {}).get("Status", ""),
                image_url=p.get("PhotoUrl") or "",
                extra={
                    "Detailed": desc.get("DetailedDescription", ""),
                    "Attributes": "; ".join(f"{x.get('ParameterText')}: {x.get('ValueText')}" for x in params),
                },
            ))
        return out


# ---------------------------------------------------------------- Mouser
class Mouser(Provider):
    name = "Mouser"
    key_fields = ("mouser_api_key",)
    signup_url = "https://www.mouser.com/api-search/"
    help_text = ("1. Sign in to mouser.com with your normal Mouser login.\n"
                 "2. Open mouser.com/api-search → 'Sign up for Search API'.\n"
                 "3. The Search API key arrives by e-mail (also under My Account → APIs). Paste it here.")
    SEARCH_URL = "https://api.mouser.com/api/v1/search/keyword"

    def search(self, keyword: str, limit: int = 50) -> list[PartResult]:
        if not self.configured:
            raise NotConfigured("Mouser is not connected")
        body = {"SearchByKeywordRequest": {"keyword": keyword, "records": min(limit, 50), "startingRecord": 0,
                                           "searchOptions": "InStock" if self.s.in_stock_only else "",
                                           "searchWithYourSignUpLanguage": ""}}
        try:
            r = requests.post(self.SEARCH_URL, params={"apiKey": self.s.mouser_api_key.strip()},
                              json=body, timeout=TIMEOUT)
            d = r.json()
        except (requests.RequestException, ValueError) as e:
            raise ProviderError(f"Mouser search failed: {e}") from e
        if d.get("Errors"):
            raise ProviderError("Mouser: " + "; ".join(e.get("Message", str(e)) for e in d["Errors"]))
        out = []
        for p in (d.get("SearchResults") or {}).get("Parts") or []:
            pbs = p.get("PriceBreaks") or []
            attrs = {a.get("AttributeName", ""): a.get("AttributeValue", "") for a in p.get("ProductAttributes") or []}
            stock = _int(p.get("AvailabilityInStock"))
            if stock is None:
                av = p.get("Availability") or ""
                stock = _int(av) if "stock" in av.lower() else 0
            out.append(PartResult(
                source="Mouser",
                mpn=p.get("ManufacturerPartNumber", ""),
                manufacturer=p.get("Manufacturer", ""),
                description=p.get("Description", ""),
                package=attrs.get("Package / Case") or attrs.get("Package/Case") or guess_package(p.get("Description", "")),
                category=p.get("Category", ""),
                stock=stock,
                lead_days=_lead_days(p.get("LeadTime")),
                price=_float(pbs[0].get("Price")) if pbs else None,
                currency=(pbs[0].get("Currency") if pbs else "") or "USD",
                moq=_int(p.get("Min")),
                supplier_pn=p.get("MouserPartNumber", ""),
                datasheet=p.get("DataSheetUrl") or "",
                url=p.get("ProductDetailUrl") or "",
                lifecycle=p.get("LifecycleStatus") or "",
                image_url=p.get("ImagePath") or "",
                extra={"Attributes": "; ".join(f"{k}: {v}" for k, v in attrs.items())},
            ))
        return out


# ---------------------------------------------------------------- Farnell / Newark / element14
ELEMENT14_STORES = {
    "www.newark.com": "Newark", "canada.newark.com": "Newark", "uk.farnell.com": "Farnell",
    "de.farnell.com": "Farnell", "fr.farnell.com": "Farnell", "it.farnell.com": "Farnell",
    "nl.farnell.com": "Farnell", "au.element14.com": "element14", "sg.element14.com": "element14",
    "in.element14.com": "element14",
}


class Element14(Provider):
    name = "Farnell/Newark"
    key_fields = ("element14_api_key",)
    signup_url = "https://partner.element14.com/member/register"
    help_text = ("1. Register (free) at partner.element14.com and sign in.\n"
                 "2. My Account → Applications → create a key for the 'Product Search API'.\n"
                 "3. Paste the key here and pick your store (www.newark.com, uk.farnell.com, ...).")
    URL = "https://api.element14.com/catalog/products"

    def search(self, keyword: str, limit: int = 50) -> list[PartResult]:
        if not self.configured:
            raise NotConfigured("Farnell/Newark is not connected")
        store = self.s.element14_store or "www.newark.com"
        label = ELEMENT14_STORES.get(store, "Farnell")
        params = {
            "term": f"any:{keyword}", "storeInfo.id": store, "resultsSettings.offset": 0,
            "resultsSettings.numberOfResults": min(limit, 50), "resultsSettings.responseGroup": "large",
            "callInfo.responseDataFormat": "json", "callInfo.apiKey": self.s.element14_api_key.strip(),
        }
        if self.s.in_stock_only:
            params["resultsSettings.refinements.filters"] = "inStock"
        try:
            r = requests.get(self.URL, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise ProviderError(f"{label} search failed: {e}") from e
        if r.status_code != 200:
            raise _http_error(label, r)
        d = r.json()
        ret = d.get("keywordSearchReturn") or d.get("premierFarnellPartNumberReturn") or {}
        out = []
        for p in ret.get("products") or []:
            attrs = {a.get("attributeLabel", ""): a.get("attributeValue", "") for a in p.get("attributes") or []}
            st = p.get("stock") or {}
            prices = sorted(p.get("prices") or [], key=lambda x: x.get("from", 0))
            ds = p.get("datasheets") or []
            sku = p.get("sku", "")
            lead = st.get("leastLeadTime")
            out.append(PartResult(
                source=label,
                mpn=p.get("translatedManufacturerPartNumber", ""),
                manufacturer=p.get("brandName") or p.get("vendorName", ""),
                description=p.get("displayName", ""),
                package=next((v for k, v in attrs.items() if "package" in k.lower() or "case" in k.lower()), "")
                or guess_package(p.get("displayName", "")),
                stock=_int(st.get("level")) if st else _int(p.get("inv")),
                lead_days=_int(lead) if lead not in (None, "", 0) else None,
                price=_float(prices[0].get("cost")) if prices else None,
                currency={"Newark": "USD", "Farnell": "GBP"}.get(label, ""),
                moq=_int(p.get("translatedMinimumOrderQuality")),
                supplier_pn=sku,
                datasheet=ds[0].get("url", "") if ds else "",
                url=f"https://{store}/{sku}" if sku else "",
                lifecycle=p.get("productStatus", ""),
                image_url=(f"https://{store}/productimages/standard/en_GB{(p.get('image') or {}).get('baseName')}"
                           if (p.get("image") or {}).get("baseName") else ""),
                extra={"Attributes": "; ".join(f"{k}: {v}" for k, v in attrs.items())},
            ))
        return out


# ---------------------------------------------------------------- TME
class TME(Provider):
    name = "TME"
    key_fields = ("tme_token", "tme_app_secret")
    signup_url = "https://developers.tme.eu/en/signin"
    help_text = ("1. Sign in at developers.tme.eu with your TME account.\n"
                 "2. Create a new application (private token).\n"
                 "3. Paste the application's Token and App secret here.")
    BASE = "https://api.tme.eu/"

    def _call(self, action: str, params: dict) -> dict:
        url = f"{self.BASE}{action}.json"
        params = dict(params)
        params["Token"] = self.s.tme_token.strip()
        ordered = sorted(params.items())
        encoded = urllib.parse.urlencode(ordered)
        base = "POST&" + urllib.parse.quote(url, "") + "&" + urllib.parse.quote(encoded, "")
        sig = base64.b64encode(hmac.new(self.s.tme_app_secret.strip().encode(), base.encode(),
                                        hashlib.sha1).digest()).decode()
        ordered.append(("ApiSignature", sig))
        try:
            r = requests.post(url, data=urllib.parse.urlencode(ordered),
                              headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=TIMEOUT)
            d = r.json()
        except (requests.RequestException, ValueError) as e:
            raise ProviderError(f"TME request failed: {e}") from e
        if d.get("Status") != "OK":
            raise ProviderError(f"TME: {d.get('Status')} {d.get('ErrorMessage', '')}")
        return d.get("Data") or {}

    def search(self, keyword: str, limit: int = 50) -> list[PartResult]:
        if not self.configured:
            raise NotConfigured("TME is not connected")
        loc = {"Country": self.s.tme_country or "US", "Language": "EN"}
        data = self._call("Products/Search", {**loc, "SearchPlain": keyword,
                                              **({"SearchWithStock": "true"} if self.s.in_stock_only else {})})
        products = (data.get("ProductList") or [])[:limit]
        stock: dict[str, dict] = {}
        if products:
            sp = {f"SymbolList[{i}]": p["Symbol"] for i, p in enumerate(products[:50])}
            try:
                pd = self._call("Products/GetPricesAndStocks",
                                {**loc, **sp, "Currency": self.s.tme_currency or "USD"})
                stock = {x["Symbol"]: x for x in pd.get("ProductList") or []}
            except ProviderError:
                pass
        out = []
        for p in products:
            st = stock.get(p.get("Symbol"), {})
            pl = st.get("PriceList") or []
            page = p.get("ProductInformationPage") or ""
            out.append(PartResult(
                source="TME",
                mpn=p.get("OriginalSymbol") or p.get("Symbol", ""),
                manufacturer=p.get("Producer", ""),
                description=p.get("Description", ""),
                package=guess_package(p.get("Description", ""), p.get("Symbol", "")),
                category=p.get("Category", ""),
                stock=_int(st.get("Amount")) if st else None,
                price=_float(pl[0].get("PriceValue")) if pl else None,
                currency=self.s.tme_currency or "USD",
                moq=_int(p.get("MinAmount")),
                supplier_pn=p.get("Symbol", ""),
                url=("https:" + page) if page.startswith("//") else page,
                image_url=("https:" + p["Photo"]) if str(p.get("Photo", "")).startswith("//") else p.get("Photo", ""),
            ))
        return out


# ---------------------------------------------------------------- Octopart (Nexar)
NEXAR_QUERY = """
query Search($q: String!, $limit: Int!) {
  supSearch(q: $q, limit: $limit) {
    results { part {
      mpn manufacturer { name } shortDescription category { name }
      bestDatasheet { url } octopartUrl totalAvail estimatedFactoryLeadDays
      medianPrice1000 { price currency } bestImage { url }
      specs { attribute { shortname } displayValue }
    } }
  }
}"""


class Nexar(Provider):
    name = "Octopart"
    key_fields = ("nexar_client_id", "nexar_client_secret")
    signup_url = "https://portal.nexar.com/"
    help_text = ("Octopart data via Nexar (stock summed over all distributors).\n"
                 "1. Sign in at portal.nexar.com (free account).\n"
                 "2. Create an application with the 'Supply' scope.\n"
                 "3. Paste the app's Client ID and Client Secret here.")
    TOKEN_URL = "https://identity.nexar.com/connect/token"
    GQL_URL = "https://api.nexar.com/graphql"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._token: str | None = None
        self._exp = 0.0

    def reset(self) -> None:
        self._token = None

    def _get_token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        try:
            r = requests.post(self.TOKEN_URL, data={
                "grant_type": "client_credentials", "client_id": self.s.nexar_client_id.strip(),
                "client_secret": self.s.nexar_client_secret.strip(), "scope": "supply.domain"}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise ProviderError(f"Nexar login failed: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"Nexar login failed ({r.status_code}): {r.text[:300]}")
        d = r.json()
        self._token, self._exp = d["access_token"], time.time() + float(d.get("expires_in", 3600))
        return self._token

    def search(self, keyword: str, limit: int = 50) -> list[PartResult]:
        if not self.configured:
            raise NotConfigured("Octopart/Nexar is not connected")
        try:
            r = requests.post(self.GQL_URL, json={"query": NEXAR_QUERY,
                                                  "variables": {"q": keyword, "limit": min(limit, 50)}},
                              headers={"Authorization": f"Bearer {self._get_token()}"}, timeout=TIMEOUT)
            d = r.json()
        except (requests.RequestException, ValueError) as e:
            raise ProviderError(f"Octopart search failed: {e}") from e
        if d.get("errors"):
            raise ProviderError("Octopart: " + "; ".join(e.get("message", "") for e in d["errors"]))
        out = []
        for res in ((d.get("data") or {}).get("supSearch") or {}).get("results") or []:
            p = res.get("part") or {}
            specs = {(s.get("attribute") or {}).get("shortname", ""): s.get("displayValue", "")
                     for s in p.get("specs") or []}
            mp = p.get("medianPrice1000") or {}
            out.append(PartResult(
                source="Octopart",
                mpn=p.get("mpn", ""),
                manufacturer=(p.get("manufacturer") or {}).get("name", ""),
                description=p.get("shortDescription", ""),
                package=specs.get("case_package", "") or guess_package(p.get("shortDescription", "")),
                category=(p.get("category") or {}).get("name", ""),
                stock=_int(p.get("totalAvail")),
                lead_days=_int(p.get("estimatedFactoryLeadDays")),
                price=_float(mp.get("price")),
                currency=mp.get("currency", "USD"),
                datasheet=(p.get("bestDatasheet") or {}).get("url", ""),
                url=p.get("octopartUrl", ""),
                lifecycle=specs.get("lifecyclestatus", ""),
                image_url=(p.get("bestImage") or {}).get("url", ""),
                extra={"Note": "Stock is the total across all distributors; price is the median @1000."},
            ))
        return out


# ---------------------------------------------------------------- registry
PROVIDER_CLASSES = {"LCSC": LCSC, "Digi-Key": DigiKey, "Mouser": Mouser, "Farnell/Newark": Element14,
                    "TME": TME, "Octopart": Nexar}
ENABLE_FLAGS = {"LCSC": "use_lcsc", "Digi-Key": "use_digikey", "Mouser": "use_mouser",
                "Farnell/Newark": "use_element14", "TME": "use_tme", "Octopart": "use_nexar"}


def make_providers(settings: Settings) -> dict[str, Provider]:
    return {name: cls(settings) for name, cls in PROVIDER_CLASSES.items()}


def best_match(results: list[PartResult], mpn: str) -> PartResult | None:
    """Pick the result matching an MPN exactly (or close), preferring the most stock."""
    t = _norm_mpn(mpn)
    exact = [r for r in results if _norm_mpn(r.mpn) == t]
    pool = exact or [r for r in results if _norm_mpn(r.mpn).startswith(t)]
    return max(pool, key=lambda r: r.stock or 0) if pool else None


# ---------------------------------------------------------------- CAD download sites
def cad_download_links(mpn: str, manufacturer: str = "") -> dict[str, str]:
    q = quote_plus(mpn)
    return {
        "SnapEDA": f"https://www.snapeda.com/search/?q={q}",
        "Ultra Librarian": f"https://app.ultralibrarian.com/search?queryText={q}",
        "SamacSys / Component Search Engine": f"https://componentsearchengine.com/search?term={q}",
    }
