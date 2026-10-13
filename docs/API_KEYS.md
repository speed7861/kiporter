# Getting supplier API keys

Kiporter searches suppliers through their official APIs. LCSC works without an account. All the others give out free API keys: you sign in with your normal account and create a key, then paste it into **Settings -> Suppliers**. Keys are stored in your operating system's credential store (Windows Credential Manager, macOS Keychain, or Linux Secret Service).

> Supplier portals change from time to time. If a step looks different, look for "API", "Developer" or "Apps" in your account menu.

## LCSC

Portal: <https://www.lcsc.com/>

No account or key needed.

1. Nothing to do - LCSC search (through JLCPCB's public parts catalogue) works out of the box.
2. Every LCSC part has a free EasyEDA symbol, footprint and (usually) 3D model, which Kiporter converts to KiCad automatically.

**Limits:** Fair use - Kiporter waits between bulk requests.

## Digi-Key

Portal: <https://developer.digikey.com/>

Free. Uses your normal Digi-Key login. You need a Client ID and Client Secret.

1. Open developer.digikey.com and click 'Log in'. Use your normal digikey.com account (create one for free if you don't have one).
2. If asked, create an Organization (any name, e.g. your name or company).
3. Open the organization, choose 'Production Apps' and click 'Create Production App'.
4. Name it 'Kiporter'. For 'OAuth Callback' enter https://localhost (Kiporter does not use it, but the form requires one).
5. Under 'API Products' tick 'ProductInformation V4', then click 'Add Production App'.
6. Open the new app and copy the 'Client ID' and 'Client Secret'.
7. In Kiporter: Settings -> Suppliers -> Digi-Key, paste both values, choose your locale site (US, CA, UK, DE, ...) and currency, then press Enter.

**Limits:** Free tier: about 1,000 searches per day and 120 per minute.

## Mouser

Portal: <https://www.mouser.com/api-search/>

Free. Uses your normal Mouser login. You need a 'Search API' key.

1. Sign in at mouser.com (register for free if needed).
2. Open mouser.com/api-search and click 'Sign up for Search API'.
3. Fill in the short form and accept the terms. The Search API key is shown on screen and e-mailed to you. You can find it again under My Account -> 'APIs'.
4. Make sure you copy the SEARCH API key (not the Cart or Order API key).
5. In Kiporter: Settings -> Suppliers -> Mouser, paste the key and press Enter.

**Limits:** Free: 30 searches per minute, 1,000 per day. The BOM check paces itself to stay under this.

## Farnell/Newark

Portal: <https://partner.element14.com/>

Free. Separate developer account on the element14 partner portal. You need an API key.

1. Open partner.element14.com and click 'Register' (this is separate from your shop login).
2. Confirm your e-mail address and sign in.
3. Register an application for the 'Product Search API (REST)'. The API key is shown under My Account / your applications.
4. In Kiporter: Settings -> Suppliers -> Farnell/Newark, paste the key and pick your store: www.newark.com (US), canada.newark.com, uk.farnell.com, de.farnell.com, au.element14.com, sg.element14.com, in.element14.com, ...
5. Press Enter to save and check it.

**Limits:** Free: about 2 calls per second and 1,000 per day.

## TME

Portal: <https://developers.tme.eu/>

Free. Uses your tme.eu login. You need an application Token and App secret.

1. Sign in at developers.tme.eu with your tme.eu account (register for free if needed).
2. Open 'My applications' and add a new application named 'Kiporter'.
3. Copy the application's private Token and its Application secret.
4. In Kiporter: Settings -> Suppliers -> TME, paste both, set your country code (US, PL, DE, ...) and currency, then press Enter.

**Limits:** Fair-use limits apply (a few requests per second).

## Octopart

Portal: <https://portal.nexar.com/>

Octopart data via Nexar. Free plan with a monthly quota. You need a Client ID and Secret.

1. Sign up at portal.nexar.com (you can sign in with Google or GitHub).
2. Create a new App and enable the 'Supply' scope (Design & Supply).
3. Open the app's 'Authorization' tab and copy the Client ID and Client Secret.
4. In Kiporter: Settings -> Suppliers -> Octopart, paste both and press Enter.
5. Octopart sums the stock of every distributor, which is handy for spotting parts nobody has.

**Limits:** Free plan: a limited number of matched parts per month; pricing data may need a paid plan.
