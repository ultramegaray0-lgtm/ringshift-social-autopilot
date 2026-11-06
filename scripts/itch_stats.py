"""Daily snapshot of the Ringshift itch.io page's own counters (views, downloads/plays, purchases).

Source: itch.io's server-side API, GET https://api.itch.io/profile/games with `Authorization: Bearer <key>`
(https://itch.io/docs/api/serverside). It returns, per game, "views_count", "downloads_count" and
"purchases_count". These are itch's own lifetime totals; this script stores one snapshot per UTC day in
data/itch_stats.json, so day-over-day differences are the measured visits for each day.

What it does NOT give (and this script never pretends to): which site a visitor came from. itch shows referrers only
in its web dashboard (Analytics -> Referrers); there is no API for them. Record them by hand in
data/campaign/ledger.json "referrers" if you want them in the report; otherwise the report says "unavailable".

Secret: ITCH_API_KEY (itch.io -> Settings -> API keys -> Generate new API key). Without it the script prints
"unavailable", writes nothing and exits 0, so a missing key never breaks the schedule. The key is never printed.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
STATS_FILE = BASE_DIR / "data" / "itch_stats.json"
PROJECT_FILE = BASE_DIR / "data" / "project.json"
API_URL = "https://api.itch.io/profile/games"
FIELDS = ("views_count", "downloads_count", "purchases_count")


def project_url(path=PROJECT_FILE):
    try:
        return (json.loads(Path(path).read_text(encoding="utf-8")).get("project_url") or "").rstrip("/")
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def pick_game(games, url):
    """The game whose page is `url` (case-insensitive, trailing slash ignored); else the only game; else None."""
    url = (url or "").rstrip("/").lower()
    for game in games or []:
        if str(game.get("url") or "").rstrip("/").lower() == url and url:
            return game
    return games[0] if games and len(games) == 1 else None


def fetch_games(key, session=None, timeout=30):
    session = session or requests
    response = session.get(API_URL, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"itch.io API answered HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict) or "games" not in data:
        raise RuntimeError(f"itch.io API returned no games list: {str(data.get('errors') if isinstance(data, dict) else data)[:200]}")
    return data["games"]


def snapshot(game, now=None):
    now = now or datetime.now(timezone.utc)
    record = {"date": now.strftime("%Y-%m-%d"), "at": now.isoformat(timespec="seconds"),
              "game_id": game.get("id"), "title": game.get("title"), "url": game.get("url")}
    for field in FIELDS:
        value = game.get(field)
        record[field] = value if isinstance(value, int) else None
    return record


def load(path=STATS_FILE):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def upsert(history, record):
    """One snapshot per UTC day: a later run the same day replaces that day's snapshot."""
    kept = [r for r in history if r.get("date") != record["date"]]
    kept.append(record)
    return sorted(kept, key=lambda r: r.get("date") or "")


def daily_deltas(history):
    """[(date, {field: delta or None})] between consecutive snapshots. None when either side is unknown."""
    rows = []
    for prev, cur in zip(history, history[1:]):
        rows.append((cur["date"], {f: (cur.get(f) - prev.get(f)) if isinstance(cur.get(f), int) and isinstance(prev.get(f), int) else None
                                   for f in FIELDS}))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stats-file", default=str(STATS_FILE))
    args = parser.parse_args(argv)
    key = os.environ.get("ITCH_API_KEY", "").strip()
    if not key:
        print("itch.io stats: unavailable (no ITCH_API_KEY secret). Nothing written.")
        return 0
    try:
        games = fetch_games(key)
    except (requests.RequestException, RuntimeError, ValueError) as error:
        print(f"itch.io stats: FAILED to read the API: {error}", file=sys.stderr)
        return 1
    game = pick_game(games, project_url())
    if not game:
        print(f"itch.io stats: no game matching {project_url() or 'data/project.json project_url'} among "
              f"{len(games)} game(s): {[g.get('url') for g in games]}", file=sys.stderr)
        return 1
    record = snapshot(game)
    history = upsert(load(args.stats_file), record)
    Path(args.stats_file).write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    print(f"itch.io stats {record['date']}: views={record['views_count']} downloads/plays={record['downloads_count']} "
          f"purchases={record['purchases_count']} ({len(history)} day(s) recorded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
