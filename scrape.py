"""Scrape Craigslist sfbay rooms/shared, filter by price, neighborhood,
availability date, and scam signals. Appends new matches to pending.json.

Usage:
    python scrape.py            # append new matches to pending.json
    python scrape.py --print    # print new matches as JSON, don't write
    python scrape.py --all      # ignore sent.json/pending dedupe
"""
import datetime
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
SENT_FILE = HERE / "sent.json"
PENDING_FILE = HERE / "pending.json"
MAX_PRICE = 2000
SEARCH_URL = f"https://sfbay.craigslist.org/search/roo?max_price={MAX_PRICE}"

# SF city only.
ALLOWED_REGIONS = {"sfc"}

# Move-in must be on or before this date (user wants asap / June 1).
AVAIL_CUTOFF = datetime.date(2026, 6, 30)
TODAY = datetime.date.today()

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

# Strong scam tells — any one of these rejects the listing.
SCAM_PATTERNS = [
    r"\bwire transfer\b", r"\bwestern union\b", r"\bmoneygram\b",
    r"\bzelle\b", r"\bcash app\b", r"\bcashapp\b", r"\bvenmo\b",
    r"\bbitcoin\b", r"\bcrypto\b", r"\bgift card\b",
    r"out of (?:the )?(?:country|state|town)", r"\bcurrently abroad\b",
    r"\bmissionary\b", r"god[- ]fearing",
    r"keys?\s+(?:will be\s+)?(?:mailed|shipped|sent)",
    r"deposit\s+before\s+(?:you\s+)?(?:view|see|visit)",
    r"\bno need to (?:see|view|visit)\b",
    r"send (?:me )?(?:your )?(?:money|funds|payment)",
    r"social security number", r"\bssn\b",
]
# Listings limited to a stay shorter than ~3 months.
SHORT_TERM_PATTERNS = [
    r"summer subl(?:et|ease)", r"\bup to (?:one|two|1|2) months?\b",
    r"\b(?:one|two|1|2) months? (?:only|max|maximum)\b",
    r"\bweekly only\b", r"\b30[- ]day\b",
]

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))


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
        m = re.search(r"/(\d{10,})\.html", url)
        out.append({
            "post_id": m.group(1) if m else url,
            "title": title,
            "price": price_el.get_text(strip=True) if price_el else "",
            "location": loc_el.get_text(strip=True) if loc_el else "",
            "url": url,
        })
    return out


def fetch_detail(url: str) -> dict | None:
    """Return {description, address, attrs} for a listing, or None on error."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    body = soup.select_one("#postingbody")
    if body:
        for el in body.select(".print-information, .print-qrcode-container"):
            el.decompose()
        desc = body.get_text("\n", strip=True)
    else:
        desc = ""
    addr_el = soup.select_one(".mapaddress")
    address = addr_el.get_text(strip=True) if addr_el else ""
    if address.lower() in ("google map", ""):
        address = ""
    attrs = " | ".join(
        ag.get_text(" | ", strip=True) for ag in soup.select(".attrgroup")
    )
    return {"description": desc, "address": address, "attrs": attrs}


def region_of(url: str) -> str | None:
    m = re.search(r"craigslist\.org/([a-z]+)/roo/", url)
    return m.group(1) if m else None


def price_under_max(price_str: str) -> bool:
    m = re.search(r"\$?([\d,]+)", price_str)
    return bool(m) and int(m.group(1).replace(",", "")) <= MAX_PRICE


# Card-location values too generic to identify a neighborhood.
GENERIC_LOCATIONS = {"", "san francisco", "city of san francisco", "sf"}


def classify_location(card_loc: str, title: str, address: str,
                      desc: str) -> str | None:
    """Return a matched whitelist neighborhood, or None.

    Tiered so we trust the most reliable signal first:
      1. Craigslist's own card-location tag and the street address.
      2. The listing title.
      3. The description body — but only when the card location is too
         generic to place the listing, so a body landmark mention
         ("close to Alamo Square") can't drag in a non-whitelist area.
    Blacklist is checked on card/title/address only (not the body, where
    a nearby-landmark mention would cause false rejects).
    """
    card = card_loc.lower().strip()
    title_l, addr_l = title.lower(), address.lower()

    for bad in BLACKLIST:
        if bad in card or bad in title_l or bad in addr_l:
            return None

    for good in WHITELIST:
        if good in card or good in addr_l:
            return good
    for good in WHITELIST:
        if good in title_l:
            return good
    if card in GENERIC_LOCATIONS:
        desc_l = desc.lower()
        for good in WHITELIST:
            if good in desc_l:
                return good
    return None


def availability_ok(attrs: str, desc: str) -> tuple[bool, str]:
    """Check the move-in date is on/before AVAIL_CUTOFF.

    Returns (ok, label). If no date is found, assume available now.
    """
    text = f"{attrs}\n{desc}".lower()
    if re.search(r"available\s+now|move[- ]in\s+now|available\s+immediately", text):
        return True, "now"
    m = re.search(r"available\s+([a-z]{3})[a-z]*\.?\s*(\d{1,2})?", text)
    if not m:
        return True, "unspecified"
    mon = MONTHS.get(m.group(1))
    if not mon:
        return True, "unspecified"
    day = int(m.group(2)) if m.group(2) else 1
    year = TODAY.year if mon >= TODAY.month else TODAY.year + 1
    try:
        avail = datetime.date(year, mon, day)
    except ValueError:
        return True, "unspecified"
    label = avail.isoformat()
    return avail <= AVAIL_CUTOFF, label


def looks_fair(desc: str) -> tuple[bool, str]:
    """Return (ok, reason). Rejects too-short, scammy, or short-term posts."""
    if not desc or len(desc) < 80:
        return False, "description too short"
    low = desc.lower()
    for pat in SCAM_PATTERNS:
        if re.search(pat, low):
            return False, f"scam signal: {pat}"
    for pat in SHORT_TERM_PATTERNS:
        if re.search(pat, low):
            return False, f"short-term only: {pat}"
    return True, "ok"


def main():
    list_all = "--all" in sys.argv
    print_only = "--print" in sys.argv
    sent = load_json(SENT_FILE, {})
    pending = load_json(PENDING_FILE, [])
    known = set(sent) | {p["post_id"] for p in pending}

    listings = fetch_search()
    print(f"# {len(listings)} listings on search page", file=sys.stderr)

    new_matches = []
    seen = set()
    skipped = {"avail": 0, "unfair": 0, "hood": 0}
    for li in listings:
        pid = li["post_id"]
        if pid in seen:
            continue
        seen.add(pid)
        if not list_all and pid in known:
            continue
        if region_of(li["url"]) not in ALLOWED_REGIONS:
            continue
        if not price_under_max(li["price"]):
            continue

        detail = fetch_detail(li["url"])
        time.sleep(0.4)
        if detail is None:
            continue

        hood = classify_location(
            li["location"], li["title"], detail["address"], detail["description"]
        )
        if not hood:
            skipped["hood"] += 1
            continue

        fair, reason = looks_fair(detail["description"])
        if not fair:
            skipped["unfair"] += 1
            print(f"#   skip {pid} ({reason})", file=sys.stderr)
            continue

        avail_ok, avail_label = availability_ok(
            detail["attrs"], detail["description"]
        )
        if not avail_ok:
            skipped["avail"] += 1
            print(f"#   skip {pid} (available {avail_label})", file=sys.stderr)
            continue

        li["neighborhood_match"] = hood
        li["address"] = detail["address"]
        li["available"] = avail_label
        li["description"] = detail["description"][:600]
        new_matches.append(li)

    if print_only:
        print(json.dumps(new_matches, indent=2))
    else:
        pending.extend(new_matches)
        save_json(PENDING_FILE, pending)

    print(
        f"# {len(new_matches)} new matches added "
        f"({len(pending)} in queue) | skipped: "
        f"{skipped['hood']} wrong-area, {skipped['unfair']} unfair, "
        f"{skipped['avail']} too-late",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
