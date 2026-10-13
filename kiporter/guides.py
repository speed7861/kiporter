"""Step-by-step directions for getting each supplier's free API key.

Shown on Settings -> Suppliers and written to docs/API_KEYS.md (see `write_markdown`).
Supplier portals change their layout from time to time - the steps describe the
flow as it was when this was written.
"""
from __future__ import annotations

GUIDE_URL = "https://github.com/speed7861/kiporter/blob/main/docs/API_KEYS.md"

GUIDES: dict[str, dict] = {
    "LCSC": {
        "portal": "https://www.lcsc.com/",
        "summary": "No account or key needed.",
        "steps": [
            "Nothing to do - LCSC search (through JLCPCB's public parts catalogue) works out of the box.",
            "Every LCSC part has a free EasyEDA symbol, footprint and (usually) 3D model, which Kiporter "
            "converts to KiCad automatically.",
        ],
        "limits": "Fair use - Kiporter waits between bulk requests.",
    },
    "Digi-Key": {
        "portal": "https://developer.digikey.com/",
        "summary": "Free. Uses your normal Digi-Key login. You need a Client ID and Client Secret.",
        "steps": [
            "Open developer.digikey.com and click 'Log in'. Use your normal digikey.com account "
            "(create one for free if you don't have one).",
            "If asked, create an Organization (any name, e.g. your name or company).",
            "Open the organization, choose 'Production Apps' and click 'Create Production App'.",
            "Name it 'Kiporter'. For 'OAuth Callback' enter https://localhost (Kiporter does not use it, "
            "but the form requires one).",
            "Under 'API Products' tick 'ProductInformation V4', then click 'Add Production App'.",
            "Open the new app and copy the 'Client ID' and 'Client Secret'.",
            "In Kiporter: Settings -> Suppliers -> Digi-Key, paste both values, choose your locale site "
            "(US, CA, UK, DE, ...) and currency, then click 'Test connection'.",
        ],
        "limits": "Free tier: about 1,000 searches per day and 120 per minute.",
    },
    "Mouser": {
        "portal": "https://www.mouser.com/api-search/",
        "summary": "Free. Uses your normal Mouser login. You need a 'Search API' key.",
        "steps": [
            "Sign in at mouser.com (register for free if needed).",
            "Open mouser.com/api-search and click 'Sign up for Search API'.",
            "Fill in the short form and accept the terms. The Search API key is shown on screen and e-mailed "
            "to you. You can find it again under My Account -> 'APIs'.",
            "Make sure you copy the SEARCH API key (not the Cart or Order API key).",
            "In Kiporter: Settings -> Suppliers -> Mouser, paste the key and click 'Test connection'.",
        ],
        "limits": "Free: 30 searches per minute, 1,000 per day. The BOM check paces itself to stay under this.",
    },
    "Farnell/Newark": {
        "portal": "https://partner.element14.com/",
        "summary": "Free. Separate developer account on the element14 partner portal. You need an API key.",
        "steps": [
            "Open partner.element14.com and click 'Register' (this is separate from your shop login).",
            "Confirm your e-mail address and sign in.",
            "Register an application for the 'Product Search API (REST)'. The API key is shown under "
            "My Account / your applications.",
            "In Kiporter: Settings -> Suppliers -> Farnell/Newark, paste the key and pick your store: "
            "www.newark.com (US), canada.newark.com, uk.farnell.com, de.farnell.com, au.element14.com, "
            "sg.element14.com, in.element14.com, ...",
            "Click 'Test connection'.",
        ],
        "limits": "Free: about 2 calls per second and 1,000 per day.",
    },
    "TME": {
        "portal": "https://developers.tme.eu/",
        "summary": "Free. Uses your tme.eu login. You need an application Token and App secret.",
        "steps": [
            "Sign in at developers.tme.eu with your tme.eu account (register for free if needed).",
            "Open 'My applications' and add a new application named 'Kiporter'.",
            "Copy the application's private Token and its Application secret.",
            "In Kiporter: Settings -> Suppliers -> TME, paste both, set your country code (US, PL, DE, ...) "
            "and currency, then click 'Test connection'.",
        ],
        "limits": "Fair-use limits apply (a few requests per second).",
    },
    "Octopart": {
        "portal": "https://portal.nexar.com/",
        "summary": "Octopart data via Nexar. Free plan with a monthly quota. You need a Client ID and Secret.",
        "steps": [
            "Sign up at portal.nexar.com (you can sign in with Google or GitHub).",
            "Create a new App and enable the 'Supply' scope (Design & Supply).",
            "Open the app's 'Authorization' tab and copy the Client ID and Client Secret.",
            "In Kiporter: Settings -> Suppliers -> Octopart, paste both and click 'Test connection'.",
            "Octopart sums the stock of every distributor, which is handy for spotting parts nobody has.",
        ],
        "limits": "Free plan: a limited number of matched parts per month; pricing data may need a paid plan.",
    },
}


def guide_text(name: str) -> str:
    g = GUIDES.get(name)
    if not g:
        return ""
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(g["steps"], 1))
    return f"{g['summary']}\n\n{steps}\n\nLimits: {g['limits']}"


def write_markdown() -> str:
    out = ["# Getting supplier API keys", "",
           "Kiporter searches suppliers through their official APIs. LCSC works without an account. All the "
           "others give out free API keys: you sign in with your normal account and create a key, then "
           "paste it into **Settings -> Suppliers**. Keys are stored in your operating system's credential "
           "store (Windows Credential Manager, macOS Keychain, or Linux Secret Service).", "",
           "> Supplier portals change from time to time. If a step looks different, look for "
           "\"API\", \"Developer\" or \"Apps\" in your account menu.", ""]
    for name, g in GUIDES.items():
        out += [f"## {name}", "", f"Portal: <{g['portal']}>", "", g["summary"], ""]
        out += [f"{i}. {s}" for i, s in enumerate(g["steps"], 1)]
        out += ["", f"**Limits:** {g['limits']}", ""]
    return "\n".join(out)
