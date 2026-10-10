#!/usr/bin/env python3
"""Build docs/calendar.ics from config.yaml.

Sources:
  - TMDB: Swiss release dates of films by the configured directors
  - Cinémathèque suisse agenda: screenings with a configured director present
  - config.yaml: festival dates and ticketing milestones (hand-maintained)

Each source fails independently: if one is down, the others still publish.
"""
import datetime as dt
import hashlib
import os
import re
import sys
import unicodedata

import requests
import yaml
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

TMDB = "https://api.themoviedb.org/3"
TODAY = dt.date.today()
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "cultural-calendar (personal iCal feed)"

MILESTONE_LABELS = {
    "tickets": "🎟 Billetterie ouverte",
    "presale": "🎟 Prévente",
    "program": "📖 Programme",
    "lineup": "📣 Premiers noms",
}


def log(msg):
    print(msg, file=sys.stderr)


def uid(*parts):
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16] + "@cultural-calendar"


def all_day(summary, start, end=None, *, key, description="", url=None, tentative=False):
    """All-day event; `end` is inclusive."""
    ev = Event()
    ev.add("uid", uid(key))
    ev.add("summary", summary)
    ev.add("dtstart", start)
    ev.add("dtend", (end or start) + dt.timedelta(days=1))
    ev.add("dtstamp", dt.datetime.now(dt.timezone.utc))
    ev.add("transp", "TRANSPARENT")
    if description:
        ev.add("description", description)
    if url:
        ev.add("url", url)
    if tentative:
        ev.add("status", "TENTATIVE")
    return ev


# --- Films (TMDB) ------------------------------------------------------------

def tmdb(path, **params):
    params["api_key"] = os.environ["TMDB_API_KEY"]
    r = HTTP.get(f"{TMDB}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def find_director(d):
    if d.get("tmdb_id"):
        return d["tmdb_id"]
    results = tmdb("/search/person", query=d["name"])["results"]
    directing = [p for p in results if p.get("known_for_department") == "Directing"]
    pick = (directing or results or [None])[0]
    if not pick:
        log(f"  ! no TMDB match for {d['name']}")
        return None
    log(f"  {d['name']} -> TMDB person {pick['id']} ({pick['name']})")
    return pick["id"]


def release_date(release_dates, country):
    """Earliest theatrical (type 3) or limited (type 2) date in a country."""
    for c in release_dates["results"]:
        if c["iso_3166_1"] != country:
            continue
        dates = [r["release_date"][:10] for r in c["release_dates"] if r["type"] in (2, 3)]
        if dates:
            return dt.date.fromisoformat(min(dates))
    return None


def film_events(cfg):
    films = cfg.get("films", {})
    country = films.get("country", "CH")
    fallbacks = films.get("fallback_countries", [])
    cutoff = TODAY - dt.timedelta(days=films.get("lookback_days", 60))
    events = []

    for d in cfg.get("directors", []):
        pid = find_director(d)
        if not pid:
            continue
        credits = tmdb(f"/person/{pid}/movie_credits")["crew"]
        seen = set()
        for m in credits:
            if m.get("job") != "Director" or m["id"] in seen:
                continue
            seen.add(m["id"])
            # Skip old films early to save API calls. Undated = upcoming.
            if m.get("release_date") and dt.date.fromisoformat(m["release_date"]) < cutoff - dt.timedelta(days=365):
                continue

            rd = tmdb(f"/movie/{m['id']}/release_dates")
            when, where = release_date(rd, country), country
            for fb in fallbacks:
                if when:
                    break
                when, where = release_date(rd, fb), fb
            if not when or when < cutoff:
                continue

            title = m.get("original_title") or m["title"]
            suffix = "" if where == country else f" ({where} – {country} TBC)"
            events.append(all_day(
                f"🎬 {title} — {d['name']}{suffix}",
                when,
                key=f"film|{m['id']}",
                description=f"Sortie {where}. {m.get('overview', '')}".strip(),
                url=f"https://www.themoviedb.org/movie/{m['id']}",
                tentative=where != country,
            ))
            log(f"    {when} {title} [{where}]")
    return events


# --- Cinémathèque suisse -------------------------------------------------------

PRESENCE = re.compile(r"en présence|en presence|in anwesenheit|in person|rencontre avec", re.I)
MONTHS = {m: i for i, m in enumerate(
    "janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split(), 1)}
DATE_NUM = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b")
DATE_TXT = re.compile(r"\b(\d{1,2})(?:er)?\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?", re.I)
TIME = re.compile(r"\b(\d{1,2})[h:](\d{2})\b")


def fold(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def parse_date(text):
    if m := DATE_NUM.search(text):
        d, mo, y = map(int, m.groups())
        return dt.date(y + 2000 if y < 100 else y, mo, d)
    if m := DATE_TXT.search(text):
        d, mo, y = int(m[1]), MONTHS[m[2].lower()], m[3]
        year = int(y) if y else TODAY.year + (1 if mo < TODAY.month - 1 else 0)
        return dt.date(year, mo, d)
    return None


def cinematheque_events(cfg):
    cc = cfg.get("cinematheque", {})
    if not cc.get("enabled"):
        return []
    names = {d["name"]: fold(d["name"]) for d in cfg.get("directors", [])}
    events, found = [], set()

    for page in cc.get("pages", []):
        try:
            r = HTTP.get(page, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            log(f"  ! {page}: {e}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        # Smallest blocks that mention both a director and a presence keyword
        for el in soup.find_all(["article", "li", "div", "section", "tr"]):
            text = el.get_text(" ", strip=True)
            if len(text) > 1500 or not PRESENCE.search(text):
                continue
            who = next((n for n, f in names.items() if f in fold(text)), None)
            if not who or any(who in fold(c.get_text(" ")) and PRESENCE.search(c.get_text(" "))
                              for c in el.find_all(["article", "li", "div", "section", "tr"])):
                continue  # a child block matches too; use the innermost one
            day = parse_date(text)
            if not day or day < TODAY or (who, day) in found:
                continue
            found.add((who, day))
            link = el.find("a", href=True)
            url = requests.compat.urljoin(page, link["href"]) if link else page
            summary = f"🎥 Cinémathèque: {who} en personne"
            ev = all_day(summary, day, key=f"cs|{who}|{day}", description=text[:500], url=url)
            if t := TIME.search(text):
                start = dt.datetime(day.year, day.month, day.day, int(t[1]), int(t[2]))
                ev = Event()
                ev.add("uid", uid(f"cs|{who}|{day}"))
                ev.add("summary", summary)
                ev.add("dtstart", start, parameters={"TZID": cfg["calendar"]["timezone"]})
                ev.add("dtend", start + dt.timedelta(hours=2, minutes=30),
                       parameters={"TZID": cfg["calendar"]["timezone"]})
                ev.add("dtstamp", dt.datetime.now(dt.timezone.utc))
                ev.add("description", text[:500])
                ev.add("url", url)
            events.append(ev)
            log(f"    {day} {who} @ Cinémathèque")
    return events


# --- Festivals (config) --------------------------------------------------------

def festival_events(cfg):
    events = []
    for f in cfg.get("festivals", []):
        for ed in f.get("editions", []):
            tent = ed.get("tentative", False)
            events.append(all_day(
                f"🎪 {f['name']}{' (?)' if tent else ''}",
                ed["start"], ed.get("end"),
                key=f"fest|{f['name']}|{ed['start'].year}",
                description="Dates estimées, pas encore annoncées." if tent else "",
                url=f.get("url"), tentative=tent,
            ))
            for ms in ed.get("milestones", []):
                mt = ms.get("tentative", False)
                label = MILESTONE_LABELS.get(ms["type"], ms["type"])
                events.append(all_day(
                    f"{label}: {f['name']}{' (?)' if mt else ''}",
                    ms["date"],
                    key=f"fest|{f['name']}|{ed['start'].year}|{ms['type']}",
                    description=ms.get("note", "Date estimée." if mt else ""),
                    url=f.get("url"), tentative=mt,
                ))
    return events


def main():
    with open("config.yaml") as fh:
        cfg = yaml.safe_load(fh)

    cal = Calendar()
    cal.add("prodid", "-//cultural-calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", cfg["calendar"]["name"])
    cal.add("x-wr-timezone", cfg["calendar"]["timezone"])
    cal.add("refresh-interval", dt.timedelta(days=1), parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", "P1D")

    sources = [("festivals", festival_events), ("cinematheque", cinematheque_events)]
    if os.environ.get("TMDB_API_KEY"):
        sources.insert(0, ("films", film_events))
    else:
        log("TMDB_API_KEY not set, skipping films")

    failed = []
    for name, fn in sources:
        log(f"[{name}]")
        try:
            for ev in fn(cfg):
                cal.add_component(ev)
        except Exception as e:  # keep publishing the other sources
            log(f"  ! {name} failed: {e!r}")
            failed.append(name)

    out = cfg["calendar"]["output"]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(cal.to_ical())
    log(f"wrote {out} ({len(cal.subcomponents)} events)")
    if failed:
        sys.exit(f"sources failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
