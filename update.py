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
HTTP.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

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


# --- Swiss regional dates (search.ch) ----------------------------------------

REGIONS = {
    "romandie": r"Romandie|Suisse romande|Westschweiz",
    "deutschschweiz": r"Deutschschweiz|Suisse allemande",
    "ticino": r"Italienische Schweiz|Suisse italienne|Svizzera italiana",
}
SWISS_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s*\(([^)]+)\)")


def searchch_date(title, director, region):
    """Regional Swiss release date from search.ch's cinema guide, or None.

    A search that matches a single film redirects straight to its page; the page
    must name the director, so a homonym is never picked up.
    """
    try:
        r = HTTP.get("https://search.ch/cine/", params={"q": title}, timeout=30)
        if r.status_code == 404:  # no film by that name (yet)
            return None
        r.raise_for_status()
    except requests.RequestException as e:
        log(f"    ! search.ch {title}: {e}")
        return None
    text = BeautifulSoup(r.text, "html.parser").get_text("\n", strip=True)
    if fold(director.split()[-1]) not in fold(text):
        return None
    for d, m, y, where in SWISS_DATE.findall(text):
        if re.search(REGIONS[region], where, re.I):
            return dt.date(int(y), int(m), int(d))
    return None


def film_events(cfg):
    films = cfg.get("films", {})
    country = films.get("country", "CH")
    region = films.get("region")
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
            # Skip old films early to save requests. Undated = upcoming.
            if m.get("release_date") and dt.date.fromisoformat(m["release_date"]) < cutoff - dt.timedelta(days=365):
                continue

            title = m.get("original_title") or m["title"]
            when, where = None, None
            if region:
                for q in dict.fromkeys([title, m["title"]]):
                    if when := searchch_date(q, d["name"], region):
                        where = region
                        break
            if not when:
                rd = tmdb(f"/movie/{m['id']}/release_dates")
                for c in [country, *fallbacks]:
                    if when := release_date(rd, c):
                        where = c
                        break
            if not when or when < cutoff:
                continue

            confirmed = where in (region, country)
            suffix = "" if confirmed else f" ({where} – CH TBC)"
            label = {"romandie": "Suisse romande", "deutschschweiz": "Deutschschweiz",
                     "ticino": "Svizzera italiana"}.get(where, where)
            events.append(all_day(
                f"🎬 {title} — {d['name']}{suffix}",
                when,
                key=f"film|{m['id']}",
                description=f"Sortie {label}. {m.get('overview', '')}".strip(),
                url=f"https://www.themoviedb.org/movie/{m['id']}",
                tentative=not confirmed,
            ))
            log(f"    {when} {title} [{where}]")
    return events


# --- Cinémathèque suisse -------------------------------------------------------

CS = "https://live.cinematheque.ch/"
MONTHS = {m: i for i, m in enumerate(
    "janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split(), 1)}
MONTH_RE = "|".join(MONTHS)
PRESENCE = re.compile(r"[^.\n]*\ben présence\b[^.\n]*", re.I)
SESSION = re.compile(rf"(\d{{1,2}})(?:er)?\s+({MONTH_RE})\s+(\d{{4}})\s+(\d{{1,2}}):(\d{{2}})", re.I)
DAY_MONTH = re.compile(rf"(\d{{1,2}})(?:er)?\s+({MONTH_RE})", re.I)


def fold(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def get_soup(url):
    r = HTTP.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def cinematheque_events(cfg):
    """Screenings at the Cinémathèque announced 'en présence' of one of the directors.

    Crawls the programme's cycles, then each film page, which carries lines like
    "Séance du 30 septembre en présence du réalisateur" and a list of sessions.
    """
    if not cfg.get("cinematheque", {}).get("enabled"):
        return []
    tz = cfg["calendar"]["timezone"]
    directors = [d["name"] for d in cfg.get("directors", [])]

    index = get_soup(CS + "?interval=events")
    lists = {CS} | {requests.compat.urljoin(CS, a["href"]) for a in index.find_all("a", href=True)
                    if "category=" in a["href"]}
    films = set()
    for url in lists:
        try:
            soup = index if url == CS else get_soup(url)
        except requests.RequestException as e:
            log(f"  ! {url}: {e}")
            continue
        films |= {requests.compat.urljoin(CS, a["href"]).split("?")[0]
                  for a in soup.find_all("a", href=True) if "/films/" in a["href"]}
    log(f"  {len(lists)} listings, {len(films)} films")

    events = []
    for url in sorted(films):
        try:
            text = get_soup(url).get_text("\n", strip=True)
        except requests.RequestException as e:
            log(f"  ! {url}: {e}")
            continue
        made_by = re.search(r"\nDe\n(.+)", text)
        made_by = made_by[1] if made_by else ""
        title = text.split("|")[0].strip()
        sessions = [(dt.datetime(int(y), MONTHS[mo.lower()], int(d), int(h), int(mi)))
                    for d, mo, y, h, mi in SESSION.findall(text.split("Séance(s)")[-1].replace("\n", " "))]

        # Which of our directors is present, and on which days ("Séance du 30 septembre")
        who, days, note = None, set(), ""
        for sentence in {s.strip() for s in PRESENCE.findall(text)}:
            named = next((n for n in directors if fold(n) in fold(sentence)), None)
            if not named and re.search(r"réalisat|cinéaste", sentence, re.I):
                named = next((n for n in directors if fold(n) in fold(made_by)), None)
            if not named:
                continue
            who = named
            if day := DAY_MONTH.search(sentence):
                days.add((int(day[1]), MONTHS[day[2].lower()]))
                note = sentence
            note = note or sentence
        if not who:
            continue
        # No day given: the guest could be at any of the sessions, so list them all
        for start in sorted({s for s in sessions if not days or (s.day, s.month) in days}):
            if start.date() < TODAY:
                continue
            ev = Event()
            ev.add("uid", uid(f"cs|{url}|{start:%Y%m%d%H%M}"))
            ev.add("summary", f"🎥 {title} — {who} en personne (Cinémathèque)")
            ev.add("dtstart", start, parameters={"TZID": tz})
            ev.add("dtend", start + dt.timedelta(hours=2, minutes=30), parameters={"TZID": tz})
            ev.add("dtstamp", dt.datetime.now(dt.timezone.utc))
            ev.add("location", "Cinémathèque suisse – Capitole, Av. du Théâtre 6, Lausanne")
            ev.add("description", note)
            ev.add("url", url)
            events.append(ev)
            log(f"    {start} {title}: {who}")
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
