import os
import re
import json
import time
import random
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

MAX_PRICE = 1700

NEARBY_TOWNS = [
    "rijswijk", "den haag", "'s-gravenhage", "s-gravenhage",
    "delft", "voorburg", "leidschendam", "wateringen", "nootdorp",
    "pijnacker", "schipluiden", "ypenburg",
]

# Fast, lightweight sources - plain HTTP requests, no browser needed.
SOURCES = {
    "pararius": "https://www.pararius.nl/huurwoningen/rijswijk/10km",
    "vbt": "https://vbtverhuurmakelaars.nl/woningen",
    "funda": "https://www.funda.nl/zoeken/huur/?selected_area=%5B%22rijswijk-zh%22%5D",
    "123wonen": "https://www.123wonen.nl/huurwoningen/in/rijswijk",
    "huurwoningen": "https://www.huurwoningen.nl/in/rijswijk/?price=600-1750&radius=5&filters%5Boffer_type%5D%5B0%5D=none",
    "nationaalgrondbezit": "https://nationaalgrondbezit.nl/huuraanbod",
}

STATE_FILE = "seen_listings.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
})

# Common junk paths to never treat as a listing, used by the generic
# (browser-rendered) extractor.
NAV_PATH_EXCLUDE = [
    "contact", "privacy", "cookie", "voorwaarden", "terms", "about",
    "over-ons", "faq", "vacature", "vacatures", "nieuws", "news",
    "service", "services", "offices", "office", "reviews", "review",
    "sale", "koop", "kopen", "sold", "verkocht", "login", "inloggen",
    "registreren", "account", "sitemap", "algemene-voorwaarden",
    "disclaimer", "privacybeleid", "werkwijze", "vestig",
]


# ---------------------------------------------------------------------------
# Plain HTTP fetch (fast path)
# ---------------------------------------------------------------------------

def fetch(url, referer=None, warm_up_url=None):
    """GET a page with plain requests. Optionally visits a homepage first
    to pick up cookies and look like a normal browser session."""
    if warm_up_url:
        try:
            SESSION.get(warm_up_url, timeout=15)
        except Exception:
            pass
    headers = {"Referer": referer} if referer else {}
    r = SESSION.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------------
# State handling
#
# State now tracks two things:
#   - "seen": every listing URL we've ever recorded, across all sites
#   - "baselined": which site names have successfully completed at least
#     one real scrape before
#
# Why: the first time ANY site successfully returns data - whether it's
# brand new, or an old site that used to fail and just started working -
# everything it finds looks "new" against empty history. Tracking baseline
# status per site (not globally) means each site gets exactly one silent
# baseline run automatically, whenever that happens, with no manual
# seen_listings.json resets ever required again.
# ---------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return set(), set(), None
    with open(STATE_FILE) as f:
        data = json.load(f)
    if isinstance(data, list):
        # Legacy format from before per-site baselining existed: a flat
        # list of seen URLs with no baseline tracking. Treat every site as
        # not-yet-baselined so each one gets exactly one silent catch-up
        # cycle going forward - safer than assuming, costs one skipped
        # cycle of alerts at most.
        return set(data), set(), None
    return (
        set(data.get("seen", [])),
        set(data.get("baselined", [])),
        data.get("last_update_id"),
    )


def save_state(seen, baselined, last_update_id):
    with open(STATE_FILE, "w") as f:
        json.dump({
            "seen": sorted(seen),
            "baselined": sorted(baselined),
            "last_update_id": last_update_id,
        }, f, indent=2)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured - would have sent:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
        if r.status_code != 200:
            print("Telegram send failed:", r.status_code, r.text)
    except Exception as e:
        print("Telegram send error:", e)


def build_welcome_text():
    return (
        "Hi! 👋 I'm your Rijswijk apartment watcher.\n\n"
        f"I check {len(SCRAPERS)} rental sites every 15 minutes - Pararius, VBT, Funda, "
        "123Wonen, Huurwoningen.nl, NationaalGrondbezit, Devilee, Real Estate Partners, "
        "Bjornd, Verra, Vesteda, Oost West, REBO, and Schep - and I'll message you the "
        f"moment something new shows up under €{MAX_PRICE}, roughly within 10km of Rijswijk.\n\n"
        "No need to do anything else - just leave me running."
    )

GREETING_TRIGGERS = {"/start", "hi", "hello", "hallo", "hoi", "hey"}


def handle_incoming_messages(last_update_id):
    """Poll for any messages you've sent the bot since the last run (e.g.
    /start after clearing chat history, or just saying hi) and reply with
    a short intro. Returns the new last_update_id to persist."""
    if not TELEGRAM_TOKEN:
        return last_update_id
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"timeout": 0}
        if last_update_id is not None:
            params["offset"] = last_update_id + 1
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if not data.get("ok"):
            print("getUpdates failed:", data)
            return last_update_id

        newest_id = last_update_id
        for update in data.get("result", []):
            newest_id = update["update_id"] if newest_id is None else max(newest_id, update["update_id"])
            msg = update.get("message", {})
            text = (msg.get("text") or "").strip().lower()
            chat_id = msg.get("chat", {}).get("id")

            if chat_id is None:
                continue
            # Only reply to the configured chat, and only to greeting-like messages
            if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
                continue
            if text in GREETING_TRIGGERS:
                send_telegram(build_welcome_text())

        return newest_id
    except Exception as e:
        print("Telegram getUpdates error:", e)
        return last_update_id


# ---------------------------------------------------------------------------
# Shared parsing helpers
# ---------------------------------------------------------------------------

def parse_price(text):
    m = re.search(r"€\s*([\d.,]{3,9})", text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").split(",")[0]
    try:
        return int(raw)
    except ValueError:
        return None


def find_listings(html, base_url, url_pattern, require_any_keyword=None, exclude_any_keyword=None):
    """Precise extractor for sites where we know the exact detail-page URL
    pattern. Climbs from each matching link up to the nearest ancestor
    that contains a euro sign, to read the price without needing CSS
    class names (which change often)."""
    soup = BeautifulSoup(html, "html.parser")
    results = {}
    for a in soup.find_all("a", href=re.compile(url_pattern)):
        href = urljoin(base_url, a["href"])

        text = a.get_text(" ", strip=True)
        container = a
        for _ in range(8):
            if "€" in text:
                break
            if container.parent is None:
                break
            container = container.parent
            text = container.get_text(" ", strip=True)

        text_low = text.lower()

        if require_any_keyword and not any(t in text_low for t in require_any_keyword):
            continue
        if exclude_any_keyword and any(w in text_low for w in exclude_any_keyword):
            continue

        price = parse_price(text)
        if href not in results or (results[href]["price"] is None and price is not None):
            results[href] = {"url": href, "price": price}
    return list(results.values())


def find_listings_generic(html, base_url, require_any_keyword=None):
    """Fallback extractor for sites we've never test-scraped and don't
    know exact URL patterns for. Keeps any same-domain link that isn't
    obvious navigation junk and has a euro sign somewhere in its nearby
    text - structural signal instead of guessed CSS classes or paths."""
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc
    results = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.netloc != base_domain:
            continue

        path_low = parsed.path.lower()
        if any(f"/{kw}" in path_low or path_low.strip("/") == kw for kw in NAV_PATH_EXCLUDE):
            continue
        if len(parsed.path.strip("/")) < 3:
            continue  # homepage or near-empty path, not a listing

        text = a.get_text(" ", strip=True)
        container = a
        for _ in range(6):
            if "€" in text:
                break
            if container.parent is None:
                break
            container = container.parent
            text = container.get_text(" ", strip=True)

        if "€" not in text:
            continue

        text_low = text.lower()
        if require_any_keyword and not any(t in text_low for t in require_any_keyword):
            continue

        price = parse_price(text)
        if full not in results or (results[full]["price"] is None and price is not None):
            results[full] = {"url": full, "price": price}

    return list(results.values())


# ---------------------------------------------------------------------------
# Playwright (real browser) support
# ---------------------------------------------------------------------------

_PLAYWRIGHT_CTX = {"pw": None, "browser": None, "available": False}


def start_browser():
    """Launch one shared headless browser for the whole run. Returns True
    if successful; if Playwright/Chromium isn't available, every browser-
    dependent scraper below will simply fail gracefully and get skipped."""
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        _PLAYWRIGHT_CTX["pw"] = pw
        _PLAYWRIGHT_CTX["browser"] = browser
        _PLAYWRIGHT_CTX["available"] = True
        return True
    except Exception as e:
        print(f"Playwright unavailable, JS-rendered sites will be skipped: {e}")
        return False


def stop_browser():
    try:
        if _PLAYWRIGHT_CTX["browser"]:
            _PLAYWRIGHT_CTX["browser"].close()
        if _PLAYWRIGHT_CTX["pw"]:
            _PLAYWRIGHT_CTX["pw"].stop()
    except Exception:
        pass


def render_page(url, wait_ms=3500, timeout_ms=45000):
    """Load a URL in a real (headless) browser and return the fully
    rendered HTML, including anything built by client-side JavaScript."""
    if not _PLAYWRIGHT_CTX["available"]:
        raise RuntimeError("Playwright browser is not available")

    browser = _PLAYWRIGHT_CTX["browser"]
    context = browser.new_context(
        user_agent=BROWSER_USER_AGENT,
        locale="nl-NL",
        viewport={"width": 1366, "height": 900},
        timezone_id="Europe/Amsterdam",
    )
    # small stealth touch: hide the automation flag most basic bot checks look for
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    page = context.new_page()
    try:
        page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    except Exception as e:
        print(f"  (render warning for {url}: {e}, using what loaded so far)")
    page.wait_for_timeout(wait_ms)
    html = page.content()
    context.close()
    return html


def render_with_retry(url, attempts=2, **kwargs):
    last_err = None
    for i in range(attempts):
        try:
            return render_page(url, **kwargs)
        except Exception as e:
            last_err = e
            time.sleep(1.5)
    raise last_err


# ---------------------------------------------------------------------------
# Shared filter-builder for the "Realmark-style" platform used by
# Devilee, Real Estate Partners, Bjornd and Verra - they all serialize
# identical filter-state JSON into the URL hash fragment.
# ---------------------------------------------------------------------------

def build_realmark_hash(price_max=MAX_PRICE, sort="addedDesc"):
    state = {
        "view": "grid",
        "sort": sort,
        "searchTerms": ["address", "zipcode", "city", "state"],
        "address": "",
        "title": "",
        "salesRentals": "rentals",
        "salesPriceMin": 0, "salesPriceMax": 9999999999,
        "devSalesPriceMin": 0, "devSalesPriceMax": 9999999999,
        "rentalsPriceMin": 0, "rentalsPriceMax": price_max,
        "devRentalsPriceMin": 0, "devRentalsPriceMax": 9999999999,
        "surfaceMin": 0, "surfaceMax": 9999999999,
        "unitsMin": 0, "unitsMax": 9999999999,
        "devSurfaceMin": 0, "devSurfaceMax": 9999999999,
        "plotSurfaceMin": 0, "plotSurfaceMax": 9999999999,
        "roomsMin": 0, "roomsMax": 9999999999,
        "bedroomsMin": 0, "bedroomsMax": 9999999999,
        "bathroomsMin": 0, "bathroomsMax": 9999999999,
        "city": [], "district": [], "mainType": [], "buildType": [],
        "tag": [], "country": [], "state": [], "listingsType": [],
        "ignoreType": [], "categories": [],
        "status": "available", "statusStrict": False,
        "includeIsBought": False,
        "user": "", "branch": "",
        "apartmentType": "", "houseType": "",
        "archiveTime": 15778463, "page": 1, "grouped": True,
    }
    return "#" + json.dumps(state, separators=(",", ":")).replace('"', "%22")


# Sites on the shared JS platform - one hash builder covers all of them.
REALMARK_SITES = {
    "Devilee": "https://www.devilee.nl/nl/aanbod/tehuur?salesRentals=rental",
    "RealEstatePartners": "https://www.real-estatepartners.nl/en/houses-for-rent-thehague?salesRentals=rentals",
    "Bjornd": "https://www.bjornd.nl/en/rental-listings?salesRentals=rentals",
    "Verra": "https://www.verra.nl/en/listings/rental?salesRentals=rentals",
}

# Other JS-rendered sites with their own bespoke frontends. These use the
# generic (best-effort) extractor since we don't have a confirmed exact
# URL pattern for them - worth spot-checking their results occasionally.
OTHER_JS_SITES = {
    "Vesteda": (
        "https://www.vesteda.com/nl/woning-zoeken?placeType=1&sortType=0"
        "&radius=10&s=Rijswijk%2C+Nederland&sc=woning"
        "&latitude=52.035576&longitude=4.3128963&filters=&priceFrom=0&priceTo=1700"
    ),
    "OostWest": "https://oostwestmakelaars.nl/en/huur",
    "REBO": "https://www.rebogroep.nl/nl/particulier/ons-aanbod/huren",
}

# Schep confirmed detail-page pattern:
# https://zoeken.schepvastgoedmanagers.nl/{City}/{Street}/{numeric-id}/tehuur.html
SCHEP_URL = "https://zoeken.schepvastgoedmanagers.nl/huur/woningen"
SCHEP_PATTERN = r"/[^/]+/[^/]+/\d+/tehuur\.html"


def scrape_realmark_site(base_url):
    url = base_url + build_realmark_hash(MAX_PRICE)
    html = render_with_retry(url)
    return find_listings_generic(html, base_url, require_any_keyword=NEARBY_TOWNS)


def scrape_other_js_site(url):
    html = render_with_retry(url)
    return find_listings_generic(html, url, require_any_keyword=NEARBY_TOWNS)


def scrape_schep():
    html = render_with_retry(SCHEP_URL)
    return find_listings(
        html, SCHEP_URL, SCHEP_PATTERN, require_any_keyword=NEARBY_TOWNS
    )


# ---------------------------------------------------------------------------
# Fast (plain-request) scrapers, each with a browser-rendered fallback if
# the site returns a 403 - some sites block plain scripts but let a real
# browser through; others block by IP regardless, in which case this
# fallback will also fail and get logged, which is expected.
# ---------------------------------------------------------------------------

def scrape_pararius():
    url = SOURCES["pararius"]
    pattern = r"/(appartement|huis|studio|kamer)-te-huur/"
    try:
        html = fetch(url, warm_up_url="https://www.pararius.nl/")
        return find_listings(html, url, pattern)
    except requests.exceptions.HTTPError:
        if _PLAYWRIGHT_CTX["available"]:
            print("  Pararius: plain request blocked, retrying with real browser...")
            html = render_with_retry(url)
            return find_listings(html, url, pattern)
        raise


def scrape_vbt():
    base = SOURCES["vbt"]
    all_listings = []

    html = fetch(base)
    all_listings.extend(
        find_listings(html, base, r"/woning/[a-z0-9-]+$", require_any_keyword=NEARBY_TOWNS)
    )

    for page in range(2, 6):
        try:
            page_html = fetch(f"{base}/{page}", referer=base)
            page_listings = find_listings(
                page_html, base, r"/woning/[a-z0-9-]+$", require_any_keyword=NEARBY_TOWNS
            )
            if not page_listings:
                break
            all_listings.extend(page_listings)
        except Exception as e:
            print("VBT pagination stopped early:", e)
            break

    return all_listings


def scrape_funda():
    url = SOURCES["funda"]
    pattern = r"/detail/huur/[a-z0-9-]+/[a-z0-9-]+/\d+/"
    try:
        html = fetch(url, warm_up_url="https://www.funda.nl/")
        return find_listings(html, url, pattern)
    except requests.exceptions.HTTPError:
        if _PLAYWRIGHT_CTX["available"]:
            print("  Funda: plain request blocked, retrying with real browser...")
            html = render_with_retry(url)
            return find_listings(html, url, pattern)
        raise


def scrape_123wonen():
    url = SOURCES["123wonen"]
    html = fetch(url, warm_up_url="https://www.123wonen.nl/")
    return find_listings(
        html, url, r"/huur/[a-z0-9+%.-]+/[a-z0-9+%.-]+/[a-z0-9+%.-]+",
        require_any_keyword=NEARBY_TOWNS,
        exclude_any_keyword=["verhuurd"],
    )


def scrape_huurwoningen():
    base = SOURCES["huurwoningen"]
    pattern = r"/huren/[a-z-]+/[0-9a-f]{6,10}/[a-z0-9-]+/"
    all_listings = []
    try:
        html = fetch(base, warm_up_url="https://www.huurwoningen.nl/")
    except requests.exceptions.HTTPError:
        if _PLAYWRIGHT_CTX["available"]:
            print("  Huurwoningen.nl: plain request blocked, retrying with real browser...")
            html = render_with_retry(base)
        else:
            raise

    all_listings.extend(find_listings(html, base, pattern, require_any_keyword=NEARBY_TOWNS))

    for page in range(2, 4):
        try:
            page_html = fetch(f"{base}&page={page}", referer=base)
            page_listings = find_listings(
                page_html, base, pattern, require_any_keyword=NEARBY_TOWNS,
            )
            if not page_listings:
                break
            all_listings.extend(page_listings)
        except Exception as e:
            print("huurwoningen.nl pagination stopped early:", e)
            break

    return all_listings


def scrape_nationaalgrondbezit():
    url = SOURCES["nationaalgrondbezit"]
    html = fetch(url)
    return find_listings(
        html, url, r"/huuraanbod/[^/\"]+/[a-z0-9-]+",
        require_any_keyword=NEARBY_TOWNS,
    )


# ---------------------------------------------------------------------------
# Build the final list of scrapers to run
# ---------------------------------------------------------------------------

SCRAPERS = {
    "Pararius": scrape_pararius,
    "VBT": scrape_vbt,
    "Funda": scrape_funda,
    "123Wonen": scrape_123wonen,
    "Huurwoningen.nl": scrape_huurwoningen,
    "NationaalGrondbezit": scrape_nationaalgrondbezit,
}

for _name, _url in REALMARK_SITES.items():
    SCRAPERS[_name] = (lambda u=_url: scrape_realmark_site(u))

for _name, _url in OTHER_JS_SITES.items():
    SCRAPERS[_name] = (lambda u=_url: scrape_other_js_site(u))

SCRAPERS["Schep"] = scrape_schep


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    browser_ok = start_browser()
    print(f"Playwright browser available: {browser_ok}")

    try:
        seen, baselined, last_update_id = load_state()

        new_last_update_id = handle_incoming_messages(last_update_id)

        new_seen = set(seen)
        new_baselined = set(baselined)
        matches_by_site = {}

        for name, scraper in SCRAPERS.items():
            try:
                listings = scraper()
            except Exception as e:
                print(f"{name}: scrape failed - {e}")
                continue

            print(f"{name}: found {len(listings)} listing(s) on the page(s) checked")

            site_is_new = name not in baselined

            for item in listings:
                url, price = item["url"], item["price"]
                if url in seen:
                    continue
                new_seen.add(url)
                if site_is_new:
                    continue  # this site's first-ever successful run - silent baseline
                if price is not None and price > MAX_PRICE:
                    continue
                price_txt = f"€{price}" if price is not None else "price unknown"
                matches_by_site.setdefault(name, []).append(f"{price_txt} - {url}")

            if site_is_new:
                print(f"  ({name}: first successful run - recording baseline, no alerts this time)")
                new_baselined.add(name)

            # be a little polite between sites, especially the browser-rendered ones
            time.sleep(random.uniform(0.5, 1.5))

        total_matches = sum(len(v) for v in matches_by_site.values())

        if total_matches == 0:
            print("No new matching listings this run.")
        else:
            for site_name, lines in matches_by_site.items():
                # batch up to 10 listings per message and throttle sends,
                # to stay well under Telegram's rate limits
                for i in range(0, len(lines), 10):
                    chunk = lines[i:i + 10]
                    text = f"New listings on {site_name}:\n\n" + "\n\n".join(chunk)
                    send_telegram(text)
                    time.sleep(1.2)
            print(f"Sent notifications for {total_matches} new listing(s).")

        save_state(new_seen, new_baselined, new_last_update_id)

    finally:
        stop_browser()


if __name__ == "__main__":
    main()
