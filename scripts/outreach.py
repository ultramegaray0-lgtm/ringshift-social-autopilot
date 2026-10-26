"""Human-reviewed outreach tracker.

This tool NEVER contacts anyone. It has no code that sends a message, follows an
account, posts a comment, or opens a connection. It keeps a CSV of creators and
communities that might like Ringshift, writes DRAFT messages for you to read, edit
and send yourself, and remembers who you already contacted so nobody is pestered.

    python scripts/outreach.py add "Channel name" --platform youtube --url https://... --category creator --why "plays logic puzzles"
    python scripts/outreach.py discover "indie puzzle game" --max 10    (YouTube search; needs YOUTUBE_API_KEY, optional)
    python scripts/outreach.py list [--status found]
    python scripts/outreach.py draft ID                  write a draft you can edit (status -> drafted)
    python scripts/outreach.py mark ID approved|contacted|replied|declined|skip [--note "..."]
    python scripts/outreach.py due                       leads you contacted a while ago with no reply logged

Workflow: found -> drafted -> approved (you read it) -> contacted (you sent it yourself)
-> replied / declined / skip. Only you set approved/contacted, by hand.

Respect each platform's rules and the person's wishes: one polite message, no follow-up
spam, no automation, nothing unsolicited in public replies. Subreddits and Discords have
their own self-promotion rules - read them first and use the proper channels (e.g. a
self-promo thread) rather than DMs.
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LEADS_FILE = BASE_DIR / "data" / "outreach.csv"
DRAFTS_DIR = BASE_DIR / "outreach" / "drafts"
PROJECT_FILE = BASE_DIR / "data" / "project.json"

FIELDS = ["id", "name", "platform", "url", "category", "why", "source", "status", "added", "updated", "note"]
STATUSES = ["found", "drafted", "approved", "contacted", "replied", "declined", "skip"]
CATEGORIES = ["creator", "community", "press", "other"]
PLATFORMS = ["youtube", "x", "reddit", "discord", "itch", "twitch", "email", "other"]
REMIND_AFTER_DAYS = 14          # "due" lists contacted leads older than this; it never sends anything
RECONTACT_BLOCK_DAYS = 90       # a draft is refused for someone contacted more recently than this

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


class OutreachError(RuntimeError):
    pass


def _today(today=None):
    return today or date.today()


# ---- storage -------------------------------------------------------------------

def read_leads(path=None):
    path = Path(path or LEADS_FILE)
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return [{f: (row.get(f) or "") for f in FIELDS} for row in csv.DictReader(handle)]
    except OSError:
        return []


def write_leads(leads, path=None):
    path = Path(path or LEADS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(leads)


def normalize_url(url):
    url = str(url or "").strip().lower()
    url = re.sub(r"^https?://(www\.)?", "", url)
    return url.rstrip("/")


def next_id(leads):
    numbers = [int(l["id"][1:]) for l in leads if re.fullmatch(r"L\d+", l["id"])]
    return f"L{(max(numbers) if numbers else 0) + 1:03d}"


def find(leads, lead_id):
    for lead in leads:
        if lead["id"].lower() == str(lead_id).lower():
            return lead
    raise OutreachError(f"no lead with id {lead_id!r}")


def add_lead(name, platform, url, category="creator", why="", source="manual", *, path=None, today=None):
    if not name.strip():
        raise OutreachError("a lead needs a name")
    if not re.match(r"^https://\S+$", str(url or "").strip()):
        raise OutreachError("a lead needs a public https:// URL so you can check them before writing")
    if platform not in PLATFORMS:
        raise OutreachError(f"platform must be one of: {', '.join(PLATFORMS)}")
    if category not in CATEGORIES:
        raise OutreachError(f"category must be one of: {', '.join(CATEGORIES)}")

    leads = read_leads(path)
    existing = next((l for l in leads if normalize_url(l["url"]) == normalize_url(url)), None)
    if existing:
        raise OutreachError(f"already tracked as {existing['id']} ({existing['name']}, {existing['status']})")

    stamp = _today(today).isoformat()
    lead = {"id": next_id(leads), "name": name.strip(), "platform": platform, "url": url.strip(),
            "category": category, "why": why.strip(), "source": source, "status": "found",
            "added": stamp, "updated": stamp, "note": ""}
    leads.append(lead)
    write_leads(leads, path)
    return lead


def set_status(lead_id, status, note="", *, path=None, today=None):
    if status not in STATUSES:
        raise OutreachError(f"status must be one of: {', '.join(STATUSES)}")
    leads = read_leads(path)
    lead = find(leads, lead_id)

    # "contacted" must follow a reviewed draft: no skipping straight to it.
    if status == "contacted" and lead["status"] not in ("approved", "contacted"):
        raise OutreachError(
            f"{lead['id']} is '{lead['status']}'. Read the draft and mark it 'approved' first; "
            "then mark 'contacted' after you have sent it yourself.")
    if status == "approved" and lead["status"] not in ("drafted", "approved"):
        raise OutreachError(f"{lead['id']} has no draft yet. Run: outreach.py draft {lead['id']}")

    lead["status"] = status
    lead["updated"] = _today(today).isoformat()
    if note:
        lead["note"] = note
    write_leads(leads, path)
    return lead


def list_leads(status=None, path=None):
    return [l for l in read_leads(path) if not status or l["status"] == status]


def due_leads(*, path=None, today=None):
    cutoff = _today(today) - timedelta(days=REMIND_AFTER_DAYS)
    due = []
    for lead in read_leads(path):
        if lead["status"] == "contacted":
            try:
                if datetime.strptime(lead["updated"], "%Y-%m-%d").date() <= cutoff:
                    due.append(lead)
            except ValueError:
                due.append(lead)
    return due


# ---- drafts --------------------------------------------------------------------

def project_url():
    try:
        url = json.loads(PROJECT_FILE.read_text(encoding="utf-8-sig")).get("project_url")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return url if isinstance(url, str) and url.startswith("https://") else None


DRAFT_TEMPLATES = {
    "creator": (
        "Hi {name},\n\n"
        "{personal}\n\n"
        "I'm making Ringshift, a small indie puzzle game about rotating concentric rings so beams "
        "line up with locks on the rim. I thought it might suit what you cover{why_clause}.\n\n"
        "{link_line}\n\n"
        "No obligation at all - if it isn't your thing, ignore this and have a good day.\n"
    ),
    "community": (
        "Hi,\n\n"
        "{personal}\n\n"
        "I'm making Ringshift, an indie puzzle game about rotating rings so beams reach the right locks. "
        "Before posting anything about it here, I wanted to check how this community prefers developers "
        "to share their projects - is there a self-promotion thread or a rule I should follow?\n\n"
        "{link_line}\n"
    ),
    "press": (
        "Hello {name},\n\n"
        "{personal}\n\n"
        "I'm making Ringshift, an indie puzzle game where you rotate concentric rings to steer beams "
        "onto locks. {link_line}\n\n"
        "Happy to answer questions or share more footage if useful. Thanks for your time.\n"
    ),
}
DRAFT_TEMPLATES["other"] = DRAFT_TEMPLATES["creator"]


def build_draft(lead, link=None):
    """Text only; it has no way to send itself. Placeholders in [BRACKETS] must be filled by a human."""
    link_line = f"Here is the project page: {link}" if link else "[ADD A LINK TO THE GAME HERE]"
    why = lead["why"].strip().rstrip(".")
    return DRAFT_TEMPLATES.get(lead["category"], DRAFT_TEMPLATES["creator"]).format(
        name=lead["name"],
        personal="[WRITE ONE SPECIFIC SENTENCE ABOUT SOMETHING YOU ACTUALLY WATCHED/READ OF THEIRS]",
        why_clause=f" ({why})" if why else "",
        link_line=link_line,
    )


def write_draft(lead_id, *, path=None, drafts_dir=None, link=None, today=None):
    leads = read_leads(path)
    lead = find(leads, lead_id)
    stamp = _today(today)

    if lead["status"] in ("skip", "declined"):
        raise OutreachError(f"{lead['id']} is '{lead['status']}'; not drafting a message to someone who said no or was skipped")

    if lead["status"] in ("contacted", "replied"):
        try:
            age = (stamp - datetime.strptime(lead["updated"], "%Y-%m-%d").date()).days
        except ValueError:
            age = 0
        if age < RECONTACT_BLOCK_DAYS:
            raise OutreachError(
                f"{lead['id']} was already {lead['status']} {age} days ago; wait {RECONTACT_BLOCK_DAYS} days "
                "before writing again, or write to them yourself")

    drafts = Path(drafts_dir or DRAFTS_DIR)
    drafts.mkdir(parents=True, exist_ok=True)
    out = drafts / f"{lead['id']}_{re.sub(r'[^a-z0-9]+', '-', lead['name'].lower()).strip('-') or 'lead'}.txt"
    header = (f"DRAFT - NOT SENT. Review, edit, and send it yourself on {lead['platform']} ({lead['url']}).\n"
              "Replace every [BRACKET]. Do not mention numbers, awards, or reviews that are not true.\n"
              "-----------------------------------------------\n\n")
    out.write_text(header + build_draft(lead, link if link is not None else project_url()), encoding="utf-8")

    if lead["status"] == "found":
        lead["status"] = "drafted"
        lead["updated"] = stamp.isoformat()
        write_leads(leads, path)

    return out


# ---- optional discovery (YouTube Data API) -----------------------------------

def search_youtube(query, max_results, api_key, *, get=None):
    """Channels matching `query`. Read-only search; the key is sent as a query parameter, never logged."""
    import requests
    get = get or requests.get
    response = get(YOUTUBE_SEARCH_URL, params={
        "part": "snippet", "type": "channel", "q": query,
        "maxResults": max(1, min(int(max_results), 25)), "key": api_key}, timeout=30)
    if response.status_code != 200:
        raise OutreachError(f"YouTube search failed (HTTP {response.status_code}); no leads were added")
    channels = []
    for item in response.json().get("items", []):
        channel_id = (item.get("id") or {}).get("channelId")
        snippet = item.get("snippet") or {}
        if channel_id and snippet.get("title"):
            channels.append({"name": snippet["title"], "url": f"https://www.youtube.com/channel/{channel_id}",
                             "why": (snippet.get("description") or "")[:120].strip()})
    return channels


def discover(query, max_results=10, *, path=None, today=None, get=None):
    from env_check import require_env
    api_key = require_env("YOUTUBE_API_KEY")["YOUTUBE_API_KEY"]
    added, skipped = [], 0
    for channel in search_youtube(query, max_results, api_key, get=get):
        try:
            added.append(add_lead(channel["name"], "youtube", channel["url"], "creator",
                                  channel["why"], f"youtube search: {query}", path=path, today=today))
        except OutreachError:
            skipped += 1
    return added, skipped


# ---- command line --------------------------------------------------------------

def _print_leads(leads):
    for l in leads:
        print(f"{l['id']}  {l['status']:<9} {l['platform']:<8} {l['name'][:32]:<32} {l['url']}")
    print(f"\n{len(leads)} lead(s)")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Human-reviewed outreach tracker (never contacts anyone).")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add")
    p.add_argument("name")
    p.add_argument("--platform", required=True, choices=PLATFORMS)
    p.add_argument("--url", required=True)
    p.add_argument("--category", default="creator", choices=CATEGORIES)
    p.add_argument("--why", default="")
    p = sub.add_parser("discover")
    p.add_argument("query")
    p.add_argument("--max", type=int, default=10)
    p = sub.add_parser("list")
    p.add_argument("--status", choices=STATUSES)
    p = sub.add_parser("draft")
    p.add_argument("id")
    p = sub.add_parser("mark")
    p.add_argument("id")
    p.add_argument("status", choices=STATUSES)
    p.add_argument("--note", default="")
    sub.add_parser("due")

    args = parser.parse_args(argv)

    try:
        if args.command == "add":
            lead = add_lead(args.name, args.platform, args.url, args.category, args.why)
            print(f"Added {lead['id']}: {lead['name']} (found)")
        elif args.command == "discover":
            added, skipped = discover(args.query, args.max)
            _print_leads(added)
            print(f"{skipped} already tracked or skipped. Nobody was contacted; review each channel before drafting.")
        elif args.command == "list":
            _print_leads(list_leads(args.status))
        elif args.command == "draft":
            print(f"Draft written: {write_draft(args.id)}\nEdit it, then send it yourself.")
        elif args.command == "mark":
            lead = set_status(args.id, args.status, args.note)
            print(f"{lead['id']} -> {lead['status']}")
        elif args.command == "due":
            _print_leads(due_leads())
    except (OutreachError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except SystemExit as error:            # missing YOUTUBE_API_KEY
        print(f"error: {error}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
