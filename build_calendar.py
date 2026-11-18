"""Builds mma.ics from the 'Scheduled events' tables on Wikipedia.

Run by GitHub Actions a few times a day. Events that get cancelled or dropped
from Wikipedia disappear from the next build; date changes update in place.
"""
import hashlib
import re
import sys
from datetime import date, datetime, timedelta, timezone
from io import StringIO

import pandas as pd
import requests
from dateutil import parser as dateparser

API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "mma-calendar/1.0 (personal calendar feed)"}

THIS_YEAR = date.today().year
YEARS = [THIS_YEAR, THIS_YEAR + 1]

# promotion name -> list of Wikipedia pages that hold its event tables.
# A page that doesn't exist is skipped and logged, so extra guesses are harmless.
SOURCES = {
    "UFC": [f"{y} in UFC" for y in YEARS],
    "Rizin": [f"{y} in Rizin Fighting Federation" for y in YEARS],
    "Glory": [f"{y} in Glory" for y in YEARS],
    "Oktagon": [f"{y} in Oktagon MMA" for y in YEARS],
    "ONE": [f"{y} in ONE Championship" for y in YEARS],
}


def fetch_tables(page):
    r = requests.get(
        API,
        params={"action": "parse", "page": page, "prop": "text",
                "format": "json", "formatversion": 2, "redirects": 1},
        headers=HEADERS, timeout=30,
    )
    data = r.json()
    if "error" in data:
        print(f"  [skip] {page}: {data['error'].get('info')}")
        return []
    html = data["parse"]["text"]
    try:
        return pd.read_html(StringIO(html))
    except ValueError:
        return []


def clean(value):
    text = re.sub(r"\[[^\]]*\]", "", str(value)).strip()
    return "" if text.lower() in ("nan", "tbd", "tba") else text


def flatten_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(c[-1]) for c in df.columns]
    df.columns = [str(c).strip() for c in df.columns]
    return df


def parse_date(text):
    text = clean(text)
    if not text:
        return None
    try:
        return dateparser.parse(text, fuzzy=False, default=datetime(2000, 1, 1)).date()
    except (ValueError, OverflowError):
        return None


def events_from_page(promo, page):
    events = []
    cutoff = date.today() - timedelta(days=1)
    for df in fetch_tables(page):
        df = flatten_columns(df)
        lower = {c.lower(): c for c in df.columns}
        event_col = lower.get("event") or next(
            (c for l, c in lower.items() if "event" in l and l != "#"), None)
        date_col = lower.get("date") or next(
            (c for l, c in lower.items() if "date" in l), None)
        if not event_col or not date_col:
            continue
        place_cols = [c for l, c in lower.items()
                      if any(k in l for k in ("venue", "arena", "stadium", "city", "country", "location"))]
        for _, row in df.iterrows():
            name = clean(row[event_col])
            day = parse_date(row[date_col])
            if not name or not day or day < cutoff:
                continue
            place = ", ".join(p for p in (clean(row[c]) for c in place_cols) if p)
            events.append({"promo": promo, "name": name, "date": day, "place": place})
    return events


def make_uid(ev, name_counts):
    key = f"{ev['promo']}|{ev['name']}"
    if name_counts[key] > 1:  # generic names like 'UFC Fight Night' repeat
        key += f"|{ev['date']}"
    return hashlib.sha1(key.encode()).hexdigest()[:16] + "@mma-calendar"


def esc(text):
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(events):
    counts = {}
    for ev in events:
        k = f"{ev['promo']}|{ev['name']}"
        counts[k] = counts.get(k, 0) + 1
    now = "20260101T000000Z"  # fixed so the file only changes when events change
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//mma-calendar//EN", "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:MMA & Kickboxing",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
    ]
    for ev in events:
        title = ev["name"]
        if ev["promo"].lower() not in title.lower():
            title = f"{ev['promo']}: {title}"
        end = ev["date"] + timedelta(days=1)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{make_uid(ev, counts)}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{ev['date']:%Y%m%d}",
            f"DTEND;VALUE=DATE:{end:%Y%m%d}",
            f"SUMMARY:{esc(title)}",
        ]
        if ev["place"]:
            lines.append(f"LOCATION:{esc(ev['place'])}")
        lines += ["TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Official-site scrapers. Each returns a list of event dicts, or [] on any
# problem; main() falls back to Wikipedia when an official source comes back thin.
# ---------------------------------------------------------------------------
import lxml.html

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en",
}
FR_CITIES = ["paris", "strasbourg", "marseille", "lyon", "nice", "lille", "toulouse",
             "bordeaux", "nantes", "montpellier", "cannes", "rennes", "levallois"]


def page_lines_and_links(url):
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    doc = lxml.html.fromstring(r.text)
    for bad in doc.xpath("//script|//style|//noscript"):
        bad.drop_tree()
    lines = [re.sub(r"\s+", " ", t).strip() for t in doc.itertext()]
    return [l for l in lines if l], doc.xpath("//a/@href")


def fetch_ares():
    lines, links = page_lines_and_links("https://www.aresfighting.com/events/?filter=upcoming")
    events = []
    for i, line in enumerate(lines):
        m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", line)
        if not m or i + 1 >= len(lines):
            continue
        day = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        title = lines[i + 1]
        if day < date.today() - timedelta(days=1) or not title.upper().startswith("ARES"):
            continue
        events.append({"promo": "ARES", "name": title, "date": day, "place": ""})
    # best-effort city: from the title, else from the Ticketmaster link order
    tm = [l.lower() for l in links if "ticketmaster" in l.lower()]
    for idx, ev in enumerate(events):
        hay = ev["name"].lower() + " " + (tm[idx] if len(tm) == len(events) else "")
        city = next((c for c in FR_CITIES if c in hay), "")
        ev["place"] = f"{city.title()}, France" if city else "France"
    return events


def fetch_oktagon():
    lines, _ = page_lines_and_links("https://oktagonmma.com/en/events")
    events = []
    for i, line in enumerate(lines):
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})(.*)", line)
        if not m:
            continue
        day = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if day < date.today() - timedelta(days=1):
            continue
        venue = m.group(4).strip(" ,")
        title = None
        for j in list(range(i - 1, max(i - 4, -1), -1)) + list(range(i + 1, min(i + 4, len(lines)))):
            cand = lines[j]
            if re.search(r"oktagon|cage game", cand, re.I) and not re.search(r"\d{2}\.\d{2}\.\d{4}", cand):
                title = cand
                break
        if title:
            events.append({"promo": "Oktagon", "name": title, "date": day, "place": venue})
    return events


MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def parse_raf(lines):
    """Parse the 'UP NEXT' cards on realamericanfreestyle.com.

    Each card renders as: code (RAF15) / matchup (CEJUDO VS SONG) / date
    (NOV 28, 2026) / city (CHICAGO, IL).
    """
    events, seen = [], set()
    for i, line in enumerate(lines):
        m = re.fullmatch(r"([A-Z]{3}) (\d{1,2}), (\d{4})", line)
        if not m or m.group(1) not in MONTHS or i < 2 or i + 1 >= len(lines):
            continue
        day = date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))
        code = next((lines[j] for j in range(i - 1, max(i - 4, -1), -1)
                     if re.fullmatch(r"RAF[\w -]*", lines[j], re.I)), None)
        if not code or day < date.today() - timedelta(days=1):
            continue
        matchup = lines[i - 1]
        name = code.upper()
        if matchup.upper() != name and re.search(r"\bVS\.?\b", matchup, re.I):
            name = f"{name}: {matchup.title().replace(' Vs ', ' vs ')}"
        if name in seen:
            continue
        seen.add(name)
        events.append({"promo": "RAF", "name": name, "date": day,
                       "place": lines[i + 1].title().replace(", Ny", ", NY")
                       .replace(", Il", ", IL").replace(", Nc", ", NC")})
    return events


def fetch_raf():
    lines, _ = page_lines_and_links("https://www.realamericanfreestyle.com/")
    return parse_raf(lines)


def parse_one(lines):
    """Parse the UPCOMING EVENTS list on onefc.com/events.

    Each entry: title / 'NOV 20 (FRI) 11:30AM GMT' (no year) / venue.
    """
    noise = {"BUY TICKETS", "HOW TO WATCH", "WATCH NOW", "FINISHED", "LIVE RESULTS",
             "BUY TICKETS VIA PIA", "UPCOMING EVENTS"}
    start = next((i for i, l in enumerate(lines) if l.upper() == "UPCOMING EVENTS"), 0)
    end = next((i for i, l in enumerate(lines) if l.upper() == "PAST EVENTS" and i > start), len(lines))
    lines = lines[start:end]
    today = date.today()
    events, seen = [], set()
    for i, line in enumerate(lines):
        m = re.match(r"([A-Z]{3}) (\d{1,2}) \([A-Z]{3}\)", line)
        if not m or m.group(1) not in MONTHS:
            continue
        title = next((lines[j] for j in range(i - 1, max(i - 4, -1), -1)
                      if lines[j].upper() not in noise), None)
        if not title:
            continue
        day = date(today.year, MONTHS[m.group(1)], int(m.group(2)))
        if day < today - timedelta(days=30):  # no year shown: a date well in the past means next year
            day = day.replace(year=today.year + 1)
        if day < today - timedelta(days=1) or (title, day) in seen:
            continue
        seen.add((title, day))
        place = lines[i + 1] if i + 1 < len(lines) and lines[i + 1].upper() not in noise \
            and not re.match(r"[A-Z]{3} \d{1,2} \(", lines[i + 1]) else ""
        events.append({"promo": "ONE", "name": title, "date": day, "place": place})
    return events


def fetch_one():
    lines, _ = page_lines_and_links("https://www.onefc.com/events/")
    return parse_one(lines)


OFFICIAL = {"ARES": fetch_ares, "Oktagon": fetch_oktagon, "RAF": fetch_raf, "ONE": fetch_one}
MIN_OFFICIAL = 3  # fewer than this and we assume the page changed; keep Wikipedia data
MIN_BY_PROMO = {"RAF": 1}  # RAF has no Wikipedia fallback and often lists only 1-3 shows


def main():
    all_events = []
    for promo, pages in SOURCES.items():
        found = 0
        for page in pages:
            evs = events_from_page(promo, page)
            found += len(evs)
            all_events += evs
        print(f"{promo} (Wikipedia): {found} upcoming events")
    for promo, fetch in OFFICIAL.items():
        try:
            evs = fetch()
        except Exception as exc:  # network error, layout change, anything
            print(f"{promo} (official site): failed - {exc}")
            evs = []
        if len(evs) >= MIN_BY_PROMO.get(promo, MIN_OFFICIAL):
            all_events = [e for e in all_events if e["promo"] != promo] + evs
            print(f"{promo} (official site): using {len(evs)} events")
        else:
            print(f"{promo} (official site): only {len(evs)} found, keeping any Wikipedia data")
    all_events.sort(key=lambda e: (e["date"], e["promo"]))
    if not all_events:
        print("No events found at all; leaving the existing file alone.")
        sys.exit(1)
    with open("mma.ics", "w", newline="") as f:
        f.write(build_ics(all_events))
    print(f"Wrote mma.ics with {len(all_events)} events")


if __name__ == "__main__":
    main()
