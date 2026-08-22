import os
import re
import json
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

MAX_PRICE = 1700

SOURCES = {
    "pararius": "https://www.pararius.nl/huurwoningen/rijswijk/10km",
    "vbt": "https://vbtverhuurmakelaars.nl/woningen",
    "funda": "https://www.funda.nl/zoeken/huur/?selected_area=%5B%22rijswijk-zh%22%5D",
    "123wonen": "https://www.123wonen.nl/huurwoningen/in/rijswijk",
    "huurwoningen": "https://www.huurwoningen.nl/in/rijswijk/?price=600-1750&radius=5&filters%5Boffer_type%5D%5B0%5D=none",
}

NEARBY_TOWNS = [
    "rijswijk", "den haag", "'s-gravenhage", "s-gravenhage", "delft",
    "voorburg", "leidschendam", "wateringen", "nootdorp", "pijnacker",
    "schipluiden", "ypenburg",
]

STATE_FILE = "seen_listings.json"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
})


def fetch(url, referer=None, warm_up_url=None):
    if warm_up_url:
        try:
            SESSION.get(warm_up_url, timeout=15)
        except Exception:
            pass
    headers = {"Referer": referer} if referer else {}
    r = SESSION.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.text


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


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

        if require_any_keyword and not any(town in text_low for town in require_any_keyword):
            continue

        if exclude_any_keyword and any(word in text_low for word in exclude_any_keyword):
            continue

        price = parse_price(text)
        if href not in results or (results[href]["price"] is None and price is not None):
            results[href] = {"url": href, "price": price}
    return list(results.values())


def scrape_pararius():
    url = SOURCES["pararius"]
    html = fetch(url, warm_up_url="https://www.pararius.nl/")
    return find_listings(html, url, r"/(appartement|huis|studio|kamer)-te-huur/")


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
    html = fetch(url, warm_up_url="https://www.funda.nl/")
    return find_listings(html, url, r"/detail/huur/[a-z0-9-]+/[a-z0-9-]+/\d+/")


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
    all_listings = []

    html = fetch(base)
    all_listings.extend(
        find_listings(html, base, r"/huren/[a-z-]+/[0-9a-f]{6,10}/[a-z0-9-]+/",
                      require_any_keyword=NEARBY_TOWNS)
    )

    for page in range(2, 4):
        try:
            page_html = fetch(f"{base}&page={page}", referer=base)
            page_listings = find_listings(
                page_html, base, r"/huren/[a-z-]+/[0-9a-f]{6,10}/[a-z0-9-]+/",
                require_any_keyword=NEARBY_TOWNS,
            )
            if not page_listings:
                break
            all_listings.extend(page_listings)
        except Exception as e:
            print("huurwoningen.nl pagination stopped early:", e)
            break

    return all_listings


SCRAPERS = {
    "Pararius": scrape_pararius,
    "VBT": scrape_vbt,
    "Funda": scrape_funda,
    "123Wonen": scrape_123wonen,
    "Huurwoningen.nl": scrape_huurwoningen,
}


def main():
    seen = load_seen()
    first_run = len(seen) == 0
    new_seen = set(seen)
    new_matches = []

    for name, scraper in SCRAPERS.items():
        try:
            listings = scraper()
        except Exception as e:
            print(f"{name}: scrape failed - {e}")
            continue

        print(f"{name}: found {len(listings)} listing(s) on the page(s) checked")

        for item in listings:
            url, price = item["url"], item["price"]
            if url in seen:
                continue
            new_seen.add(url)
            if first_run:
                continue
            if price is not None and price > MAX_PRICE:
                continue
            price_txt = f"€{price}" if price is not None else "price unknown"
            new_matches.append(f"New listing on {name} - {price_txt}\n{url}")

    if first_run:
        print("First run: recorded current listings as the baseline. "
              "No notifications sent this time - future new listings will alert.")
    elif new_matches:
        for msg in new_matches:
            send_telegram(msg)
        print(f"Sent {len(new_matches)} notification(s).")
    else:
        print("No new matching listings this run.")

    save_seen(new_seen)


if __name__ == "__main__":
    main()
