"""Scrape Craigslist sfbay rooms/shared, filter by price + neighborhood,
emit a JSON list of matches that haven't been processed before.

Usage:
    python scrape.py           # prints new matches as JSON
    python scrape.py --all     # ignore sent.json, list everything matching
"""
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
SENT_FILE = HERE / "sent.json"
MAX_PRICE = 2000
SEARCH_URL = f"https://sfbay.craigslist.org/search/roo?max_price={MAX_PRICE}"

# SF city only.
ALLOWED_REGIONS = {"sfc"}

WHITELIST = [
    "north beach", "russian hill", "mission district", "hayes valley",
    "lower haight", "duboce", "nopa", "alamo square", "dogpatch",
    "potrero", "inner sunset", "inner richmond", "bernal heights",
]
BLACKLIST = [
    "tenderloin", "soma", "south of market", "daly city",
    "outer richmond", "outer sunset", "excelsior", "outer mission",
    "fremont", "san jose",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def load_sent() -> dict:
    if SENT_FILE.exists():
        return json.loads(SENT_FILE.read_text())
    return {}


def save_sent(d: dict) -> None:
    SENT_FILE.write_text(json.dumps(d, indent=2))


def fetch_search() -> list[dict]:
    r = requests.get(SEARCH_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for card in soup.select("li.cl-static-search-result"):
        a = card.find("a")
        if not a:
            continue
        url = a["href"]
        title = (card.select_one(".title") or a).get_text(strip=True)
        price_el = card.select_one(".price")
        loc_el = card.select_one(".location")
        price = price_el.get_text(strip=True) if price_el else ""
        location = loc_el.get_text(strip=True) if loc_el else ""
        m = re.search(r"/(\d{10,})\.html", url)
        post_id = m.group(1) if m else url
        out.append({
            "post_id": post_id,
            "title": title,
            "price": price,
            "location": location,
            "url": url,
        })
    return out


def location_matches(location: str, title: str = "") -> str | None:
    """Return matched whitelist keyword, or None if blacklisted/no match."""
    blob = f"{location} {title}".lower()
    for bad in BLACKLIST:
        if bad in blob:
            return None
    for good in WHITELIST:
        if good in blob:
            return good
    return None


def price_under_max(price_str: str) -> bool:
    m = re.search(r"\$?([\d,]+)", price_str)
    if not m:
        return False
    return int(m.group(1).replace(",", "")) <= MAX_PRICE


def fetch_listing_desc(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    body = soup.select_one("#postingbody")
    if not body:
        return ""
    # strip the "QR Code Link" boilerplate
    for el in body.select(".print-information, .print-qrcode-container"):
        el.decompose()
    return body.get_text("\n", strip=True)


def looks_fair(desc: str) -> bool:
    """Very light filter: not empty, not pure spam, reasonable length."""
    if not desc or len(desc) < 80:
        return False
    spam_terms = ["bitcoin", "crypto wallet", "telegram only", "send funds"]
    low = desc.lower()
    if any(s in low for s in spam_terms):
        return False
    return True


def main():
    list_all = "--all" in sys.argv
    sent = load_sent()
    listings = fetch_search()
    print(f"# {len(listings)} listings on search page", file=sys.stderr)

    matches = []
    seen_ids = set()
    for li in listings:
        if li["post_id"] in seen_ids:
            continue
        seen_ids.add(li["post_id"])
        if not list_all and li["post_id"] in sent:
            continue
        # region check: /sfc/, /eby/, etc.
        m = re.search(r"craigslist\.org/([a-z]+)/roo/", li["url"])
        if not m or m.group(1) not in ALLOWED_REGIONS:
            continue
        if not price_under_max(li["price"]):
            continue
        hood = location_matches(li["location"], li["title"])
        if not hood:
            continue
        desc = fetch_listing_desc(li["url"])
        if not looks_fair(desc):
            continue
        li["neighborhood_match"] = hood
        li["description"] = desc[:600]
        matches.append(li)
        time.sleep(0.4)  # be polite

    print(json.dumps(matches, indent=2))
    print(f"# {len(matches)} new matches", file=sys.stderr)


if __name__ == "__main__":
    main()
