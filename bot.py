#!/usr/bin/env python3
"""
Telegram-driven multi-movie BookMyShow watcher (serverless).

Designed to run ONCE per invocation on the every-~10-min GitHub Actions cron.
Each run does three phases:

  1. Drain pending Telegram commands (/request, /list, /delete) via getUpdates.
  2. Check every active request; alert + delete any whose booking has opened.
  3. Persist requests.json (the workflow commits it back to the repo).

Commands are only accepted from the owner chat (TELEGRAM_CHAT_ID) so a stranger
who finds the bot can't queue watches and burn ScraperAPI credits.

Because the workflow serializes runs (concurrency group, cancel-in-progress:false),
Telegram's single-consumer getUpdates and the git push never race.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# Reuse the proven helpers from the single-target watcher.
from poller import fetch, send_telegram, load_json, save_json

ROOT = Path(__file__).resolve().parent
STORE_PATH = Path(os.environ.get("STORE_PATH", ROOT / "requests.json"))

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OWNER_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

REQUEST_FORMAT = (
    "Send it like this (Time is optional):\n\n"
    "/request\n"
    "Movie Name: Spider-Man: Brand New Day (IMAX 2D)\n"
    "Date: 2026-08-01\n"
    "Venue: Broadway Cinemas Coimbatore\n"
    "Time: 7:00 PM\n"
    "URL: https://in.bookmyshow.com/movies/coimbatore/spiderman-brand-new-day/buytickets/ET00447840/20260801"
)

HELP_TEXT = (
    "Commands:\n"
    "/request — add a watch (labeled block, see below)\n"
    "/list — show active watches\n"
    "/delete <id> — remove a watch (or /delete all)\n\n"
    + REQUEST_FORMAT
)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def normalize(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def slug_tokens(name):
    """Distinctive lowercase word tokens, for matching a venue name to a slug."""
    return [t for t in re.split(r"[^a-z0-9]+", normalize(name)) if t]


LABELS = {
    "movie name": "movie", "movie": "movie",
    "date": "date",
    "venue": "venue_name", "theatre": "venue_name", "theater": "venue_name",
    "time": "time",
    "url": "url", "link": "url",
}

FIELD_LABEL = {"movie": "Movie Name", "date": "Date", "venue_name": "Venue", "url": "URL"}


def parse_request_block(text):
    """Parse 'Label: value' lines into a fields dict. First ':' splits the label,
    so values containing ':' (URLs, times) survive intact."""
    fields = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, val = line.partition(":")
        key = LABELS.get(normalize(label))
        if key and val.strip():
            fields[key] = val.strip()
    return fields


BMS_BUY = re.compile(
    r"bookmyshow\.com/movies/([^/]+)/([^/]+)/buytickets/(ET\d+)", re.I)
BMS_MICRO = re.compile(
    r"bookmyshow\.com/movies/([^/]+)/([^/]+)/(ET\d+)", re.I)


def parse_bms_url(url):
    """Return (region, slug, event_code) from a BMS movie URL, or None."""
    m = BMS_BUY.search(url) or BMS_MICRO.search(url)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3).upper()


def parse_date(s):
    """Accept YYYY-MM-DD, YYYYMMDD, or DD-MM-YYYY -> normalize to YYYYMMDD."""
    s = s.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    if re.match(r"^\d{8}$", s):
        return s
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", s)
    if m:
        return m.group(3) + m.group(2) + m.group(1)
    return None


def pretty_date(d):
    return f"{d[6:8]}-{d[4:6]}-{d[0:4]}"


# --------------------------------------------------------------------------- #
# Venue resolution + availability detection
# --------------------------------------------------------------------------- #

# BMS venue anchor, e.g.
#   /cinemas/coimbatore/broadway-cinemas-coimbatore/BWCB" class="...">Broadway Cinemas: Coimbatore</a>
VENUE_ANCHOR = re.compile(
    r'/cinemas/[^/]+/([^/"\']+)/([A-Z0-9]{3,6})"[^>]*>([^<]+)</a>')


def list_venues(region, slug, event, date):
    """Fetch the buytickets page and return ([(code, slug, label), ...], error)."""
    url = f"https://in.bookmyshow.com/movies/{region}/{slug}/buytickets/{event}/{date}"
    try:
        page = fetch({"target_url": url})
    except requests.RequestException as exc:
        return None, str(exc)
    venues = {}
    for m in VENUE_ANCHOR.finditer(page):
        vslug, code, label = m.group(1), m.group(2), m.group(3).strip()
        venues.setdefault(code, (vslug, label))
    return [(code, v[0], v[1]) for code, v in venues.items()], None


def match_venues(venues, venue_name):
    """Venues whose slug+label contain every token of the requested venue name."""
    tokens = slug_tokens(venue_name)
    if not tokens:
        return []
    out = []
    for code, vslug, label in venues:
        hay = normalize(label) + " " + vslug.replace("-", " ")
        if all(t in hay for t in tokens):
            out.append((code, label))
    return out


def request_is_open(page_text, req):
    """A request is OPEN when the page has a live booking link for the EXACT date
    at the requested venue. The date is baked into the link, so BMS's silent
    fallback to the nearest open date can't cause a false positive.

    Match by venue_code when known; otherwise fall back to venue-slug tokens.
    """
    date = req["date"]
    code = req.get("venue_code")
    if code:
        return f"/{code}/{date}" in page_text
    tokens = slug_tokens(req.get("venue_name", ""))
    if not tokens:
        return False
    pattern = re.compile(r"/cinemas/[^/]+/([^/\"']+)/buytickets/[A-Z0-9]+/" + re.escape(date))
    for m in pattern.finditer(page_text):
        vslug = m.group(1).lower()
        if all(t in vslug for t in tokens):
            return True
    return False


# --------------------------------------------------------------------------- #
# Telegram command handling (Phase 1)
# --------------------------------------------------------------------------- #

def reply(chat_id, text):
    try:
        send_telegram(TOKEN, chat_id, text)
    except requests.RequestException as exc:
        print(f"[reply] failed: {exc}")


def get_updates(offset):
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    resp = requests.get(url, params={"timeout": 0, "offset": offset}, timeout=35)
    resp.raise_for_status()
    return resp.json().get("result", [])


def drain_commands(store):
    """Consume all pending updates, advancing the persisted offset so Telegram
    doesn't re-deliver them next run."""
    highest = store.get("offset", 0)
    while True:
        updates = get_updates(highest + 1 if highest else 0)
        if not updates:
            break
        for upd in updates:
            highest = max(highest, upd["update_id"])
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            chat_id = msg.get("chat", {}).get("id")
            if str(chat_id) != str(OWNER_CHAT_ID):
                continue  # ignore strangers; offset still advances past them
            handle_message(store, chat_id, msg.get("text", "") or "")
    store["offset"] = highest


def handle_message(store, chat_id, text):
    stripped = text.strip()
    if not stripped.startswith("/"):
        return  # ignore non-command chatter silently
    low = stripped.lower()
    if low.startswith("/request"):
        cmd_request(store, chat_id, stripped)
    elif low.startswith("/list"):
        cmd_list(store, chat_id)
    elif low.startswith("/delete"):
        cmd_delete(store, chat_id, stripped)
    elif low.startswith(("/start", "/help")):
        reply(chat_id, HELP_TEXT)
    else:
        reply(chat_id, "Unknown command.\n\n" + HELP_TEXT)


def cmd_request(store, chat_id, text):
    fields = parse_request_block(text)
    missing = [k for k in ("movie", "date", "venue_name", "url") if not fields.get(k)]
    if missing:
        reply(chat_id, "Missing: " + ", ".join(FIELD_LABEL[m] for m in missing)
              + "\n\n" + REQUEST_FORMAT)
        return

    parsed = parse_bms_url(fields["url"])
    if not parsed:
        reply(chat_id, "That doesn't look like a BookMyShow movie URL. Expected "
              "something like .../movies/<city>/<movie>/buytickets/ET.../<date>")
        return
    region, slug, event = parsed

    date = parse_date(fields["date"])
    if not date:
        reply(chat_id, "Couldn't read the Date. Use YYYY-MM-DD (e.g. 2026-08-01).")
        return

    venue_code = venue_label = None
    venues, err = list_venues(region, slug, event, date)
    if venues is None:
        note = "(couldn't reach BMS to resolve the venue just now — will match by name)"
    elif not venues:
        note = "(no venues listed yet — will match by name when booking opens)"
    else:
        matched = match_venues(venues, fields["venue_name"])
        if len(matched) == 1:
            venue_code, venue_label = matched[0]
            note = f"resolved to {venue_label} ({venue_code})"
        elif len(matched) > 1:
            lines = "\n".join(f"  • {lbl} ({c})" for c, lbl in matched[:10])
            reply(chat_id, "Several venues match that name — resend with a more "
                  "specific Venue:\n" + lines)
            return
        else:
            sample = "\n".join(f"  • {lbl}" for _, _, lbl in venues[:12])
            reply(chat_id, f"No venue matched '{fields['venue_name']}'. Venues listed "
                  f"for this movie/date:\n{sample}\n\n(If it's not open yet that's "
                  "expected — you can resend and it'll match by name.)")
            return

    req = {
        "id": store["next_id"],
        "chat_id": chat_id,
        "movie": fields["movie"],
        "date": date,
        "time": fields.get("time"),
        "venue_name": fields["venue_name"],
        "venue_code": venue_code,
        "venue_label": venue_label,
        "region": region,
        "slug": slug,
        "event_code": event,
        "url_template": f"https://in.bookmyshow.com/movies/{region}/{slug}/buytickets/{event}/{{date}}",
        "created_at": int(time.time()),
        "last_checked": None,
    }
    store["requests"].append(req)
    store["next_id"] += 1

    reply(chat_id,
          f"✅ Watching #{req['id']}\n"
          f"{fields['movie']}\n"
          f"Venue: {venue_label or fields['venue_name']}\n"
          f"Date: {pretty_date(date)}\n"
          f"{note}\n\n"
          "I'll ping you here the moment booking opens.")


def cmd_list(store, chat_id):
    reqs = store["requests"]
    if not reqs:
        reply(chat_id, "No active watches. Use /request to add one.")
        return
    lines = []
    for r in reqs:
        venue = r.get("venue_label") or r.get("venue_name")
        t = f" @ {r['time']}" if r.get("time") else ""
        lines.append(f"#{r['id']}  {r['movie']}\n     {venue} — {pretty_date(r['date'])}{t}")
    reply(chat_id, "Active watches:\n\n" + "\n".join(lines))


def cmd_delete(store, chat_id, text):
    parts = text.split()
    if len(parts) < 2:
        reply(chat_id, "Usage: /delete <id>  (or /delete all). Use /list to see ids.")
        return
    arg = parts[1].lower().lstrip("#")
    if arg == "all":
        n = len(store["requests"])
        store["requests"] = []
        reply(chat_id, f"Deleted all {n} watch(es).")
        return
    if not arg.isdigit():
        reply(chat_id, "Give a numeric id, e.g. /delete 3")
        return
    rid = int(arg)
    before = len(store["requests"])
    store["requests"] = [r for r in store["requests"] if r["id"] != rid]
    if len(store["requests"]) < before:
        reply(chat_id, f"Deleted #{rid}.")
    else:
        reply(chat_id, f"No watch with id #{rid}. Use /list to see them.")


# --------------------------------------------------------------------------- #
# Checker (Phase 2)
# --------------------------------------------------------------------------- #

def notify_open(r):
    d = r["date"]
    venue = r.get("venue_label") or r.get("venue_name")
    t = f"Time: {r['time']}\n" if r.get("time") else ""
    url = r["url_template"].format(date=d)
    msg = (f"🎬 Booking just OPENED!\n\n"
           f"{r['movie']}\n"
           f"Theatre: {venue}\n"
           f"Date: {pretty_date(d)}\n"
           f"{t}\n"
           f"Book here: {url}")
    try:
        send_telegram(TOKEN, r["chat_id"], msg)
        print(f"[check] notified #{r['id']}")
        return True
    except requests.RequestException as exc:
        print(f"[check] notify failed #{r['id']}: {exc}")
        return False


def check_requests(store):
    reqs = store["requests"]
    if not reqs:
        return
    # One fetch per unique movie+date; evaluate every venue against that page.
    groups = {}
    for r in reqs:
        groups.setdefault((r["region"], r["slug"], r["event_code"], r["date"]), []).append(r)

    fired = set()
    now = int(time.time())
    for (region, slug, event, date), group in groups.items():
        url = group[0]["url_template"].format(date=date)
        try:
            page = fetch({"target_url": url})
        except requests.RequestException as exc:
            print(f"[check] fetch failed for {slug}/{date}: {exc}")
            continue
        for r in group:
            r["last_checked"] = now
            label = f"{r['movie']} @ {r.get('venue_label') or r['venue_name']}"
            is_open = request_is_open(page, r)
            print(f"[check] #{r['id']} {label} open={is_open}")
            if is_open and notify_open(r):
                fired.add(r["id"])

    if fired:
        store["requests"] = [r for r in reqs if r["id"] not in fired]


# --------------------------------------------------------------------------- #

def main():
    if not TOKEN or not OWNER_CHAT_ID:
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

    store = load_json(STORE_PATH, default=None) or {}
    store.setdefault("next_id", 1)
    store.setdefault("offset", 0)
    store.setdefault("requests", [])

    before = json.dumps(store, sort_keys=True)

    try:
        drain_commands(store)
    except requests.RequestException as exc:
        print(f"[drain] getUpdates failed: {exc}")

    check_requests(store)

    if json.dumps(store, sort_keys=True) != before:
        save_json(STORE_PATH, store)
        print("[store] saved")
    else:
        print("[store] no change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
