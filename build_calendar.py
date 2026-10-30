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


def main():
    all_events = []
    for promo, pages in SOURCES.items():
        found = 0
        for page in pages:
            evs = events_from_page(promo, page)
            found += len(evs)
            all_events += evs
        print(f"{promo}: {found} upcoming events")
    all_events.sort(key=lambda e: (e["date"], e["promo"]))
    if not all_events:
        print("No events found at all; leaving the existing file alone.")
        sys.exit(1)
    with open("mma.ics", "w", newline="") as f:
        f.write(build_ics(all_events))
    print(f"Wrote mma.ics with {len(all_events)} events")


if __name__ == "__main__":
    main()
