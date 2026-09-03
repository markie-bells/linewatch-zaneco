#!/usr/bin/env python3
"""
Linewatch Zamboanga — sync script.

Fetches ZANECO's "Power Interruption Update" category page, finds notice
posts not already in data/notices.json, downloads each notice's card
image(s), OCRs them with Tesseract, parses the OCR text into structured
entries, and rewrites data/notices.json. Also prunes notices that are more
than 2 days in the past.

Run manually with:  python scripts/sync.py
(needs: pip install -r scripts/requirements.txt, and the tesseract-ocr
system package installed — see the GitHub Actions workflow for the exact
apt-get command, or on macOS: brew install tesseract; on Ubuntu/Debian:
sudo apt-get install tesseract-ocr)
"""

import json
import re
import sys
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    import pytesseract
    from PIL import Image
    from io import BytesIO
except ImportError:
    print("Missing OCR dependencies. Run: pip install -r scripts/requirements.txt", file=sys.stderr)
    raise

CATEGORY_URL = "https://zaneco.ph/category/power-interruption-update/"
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "notices.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LinewatchZamboangaBot/1.0; +https://github.com/)"}
STALE_DAYS = 2  # prune notices whose every entry is this many days in the past

MONTHS = {m.upper(): i for i, m in enumerate(
    ["January","February","March","April","May","June","July","August",
     "September","October","November","December"], start=1)}


# ---------------------------------------------------------------- fetching

def fetch_category_posts():
    """Return [{title, url, date_posted}] for every post on the category page."""
    resp = requests.get(CATEGORY_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []
    seen = set()
    # WordPress themes vary; be liberal: any <article> or heading link that
    # points at a zaneco.ph dated permalink (/YYYY/MM/DD/slug/).
    for a in soup.select("article a[href], h1 a[href], h2 a[href], h3 a[href]"):
        href = a.get("href", "")
        if not href:
            continue
        href = urljoin(CATEGORY_URL, href)
        if not re.search(r"zaneco\.ph/\d{4}/\d{2}/\d{2}/", href):
            continue
        if href in seen:
            continue
        seen.add(href)
        title = a.get_text(strip=True)
        if title:
            posts.append({"title": title, "url": href})
    return posts


def fetch_post_images(post_url):
    """Return a list of real image URLs for a notice post (handles lazy-load)."""
    resp = requests.get(post_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    article = soup.find("article") or soup
    urls = []
    for img in article.find_all("img"):
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
        if not src or src.startswith("data:"):
            continue
        urls.append(urljoin(post_url, src))
    return urls


def ocr_image(image_url):
    resp = requests.get(image_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content)).convert("L")  # grayscale helps OCR
    return pytesseract.image_to_string(img)


def image_hash(image_url):
    resp = requests.get(image_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return hashlib.sha256(resp.content).hexdigest()


# ---------------------------------------------------------------- parsing

WEEKDAYS = ["MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY","SUNDAY"]

def parse_card_text(text, year_hint):
    """Parse OCR'd text from one ZANECO notice card into a structured entry.
    Returns None if the text doesn't look like a notice card (OCR failure)."""
    upper = text.upper()

    month_match = re.search(r"\b(" + "|".join(MONTHS.keys()) + r")\b", upper)
    day_match = re.search(r"\b(" + "|".join(MONTHS.keys()) + r")\D{0,6}(\d{1,2})\b", upper)
    if not month_match or not day_match:
        return None
    month = MONTHS[month_match.group(1)]
    day = int(day_match.group(2))

    time_match = re.search(
        r"(\d{1,2}[:.]\d{2}\s*[AP]\.?M\.?)\s*[-–—~]\s*(\d{1,2}[:.]\d{2}\s*[AP]\.?M\.?)",
        upper)
    dur_match = re.search(r"([\d.]+)\s*HOURS?\s*DURATION", upper)

    # Reason/cause block: text between "REASON/CAUSE" and "AFFECTED AREA"
    reason = None
    r_match = re.search(r"REASON\s*/?\s*CAUSE(.*?)AFFECTED\s*AREA", text, re.IGNORECASE | re.DOTALL)
    if r_match:
        reason = re.sub(r"\s+", " ", r_match.group(1)).strip(" :\n")

    # Affected area block: "AFFECTED AREA/S" then "MUNICIPALITY:" then bullets
    municipality = None
    areas = []
    a_match = re.search(r"AFFECTED\s*AREA/?S?(.*)$", text, re.IGNORECASE | re.DOTALL)
    if a_match:
        block = a_match.group(1)
        lines = [l.strip(" •*-\n\t") for l in block.splitlines() if l.strip()]
        if lines:
            first = lines[0].rstrip(":")
            municipality = first if first else None
            areas = [l for l in lines[1:] if l]

    emergency = "EMERGENCY" in upper

    if not (time_match and dur_match and reason and municipality):
        return None  # OCR didn't produce a clean, trustworthy read — caller should mark pending

    try:
        date_str = f"{year_hint:04d}-{month:02d}-{day:02d}"
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None

    entry = {
        "date": date_str,
        "timeStart": time_match.group(1).replace(".", "").upper().replace("AM", " AM").replace("PM", " PM").strip(),
        "timeEnd": time_match.group(2).replace(".", "").upper().replace("AM", " AM").replace("PM", " PM").strip(),
        "duration": f"{dur_match.group(1)} hrs",
        "municipality": municipality.title(),
        "areas": areas,
        "reason": reason,
    }
    if emergency:
        entry["emergency"] = True
    # collapse doubled spaces from the AM/PM fix above
    entry["timeStart"] = re.sub(r"\s+", " ", entry["timeStart"])
    entry["timeEnd"] = re.sub(r"\s+", " ", entry["timeEnd"])
    return entry


def guess_area_system(title):
    for code in ("DAS", "SAS", "LAS", "PAS", "NGCP"):
        if code in title.upper():
            return code
    return "OTHER"


def earliest_date_from_title(title, year_hint):
    """Best-effort lead date straight from the post title, for pending_detail docs."""
    m = re.search(r"(" + "|".join(MONTHS.keys()) + r")\D{0,6}(\d{1,2})", title.upper())
    if not m:
        return None
    month = MONTHS[m.group(1)]
    day = int(m.group(2))
    try:
        d = datetime(year_hint, month, day)
    except ValueError:
        return None
    return d.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- main

def load_data():
    if DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text())
    return {"lastCheckedAt": None, "sourceUrl": CATEGORY_URL, "notices": []}


def save_data(data):
    DATA_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def known_urls(data):
    urls = set()
    for n in data["notices"]:
        if n.get("sourceUrl"):
            urls.add(n["sourceUrl"])
        if n.get("altSourceUrl"):
            urls.add(n["altSourceUrl"])
    return urls


def slugify_id(area_system, entries, lead_date):
    dates = sorted({e["date"] for e in entries}) if entries else ([lead_date] if lead_date else [])
    if not dates:
        return f"{area_system.lower()}-unknown"
    if len(dates) == 1:
        return f"{area_system.lower()}-{dates[0]}"
    return f"{area_system.lower()}-{dates[0]}-to-{dates[-1][-2:]}"


def main():
    now = datetime.now(timezone.utc)
    data = load_data()
    existing_urls = known_urls(data)

    try:
        posts = fetch_category_posts()
    except Exception as exc:
        print(f"Could not fetch category page:
