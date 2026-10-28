"""Loads config.json and sources.json, and checks the game facts against the repository's knowledge file."""

import json
import os
import re
from pathlib import Path

from . import core

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
CONFIG_FILE = PACKAGE_DIR / "config.json"
SOURCES_FILE = PACKAGE_DIR / "sources.json"
KNOWLEDGE_FILE = REPO_DIR / "RINGSHIFT_KNOWLEDGE.md"

# The mechanic words a draft may use, each mapped to the name in RINGSHIFT_KNOWLEDGE.md that makes it true.
MECHANIC_WORDS = {
    "mirror": "Mirrors", "mirrors": "Mirrors", "portal": "Portals", "portals": "Portals",
    "amplifier": "Amplifiers", "amplifiers": "Amplifiers", "block": "Blocks", "blocks": "Blocks",
    "half-block": "Half-Blocks", "half-blocks": "Half-Blocks", "prism": "Prisms", "prisms": "Prisms",
    "echo": "Echo", "lock": "Locks", "locks": "Locks", "beam": "Source beams", "beams": "Source beams",
}


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise core.OutreachError(f"cannot read {Path(path).name}: {error}") from None


def load_config(path=None, env=None):
    env = env if env is not None else os.environ
    config = load_json(path or CONFIG_FILE)
    for section in ("demo", "game", "sender", "limits", "youtube", "gemini", "keywords"):
        if not isinstance(config.get(section), dict):
            raise core.OutreachError(f"config.json is missing the '{section}' section")
    config["demo"]["url"] = core.validate_url(config["demo"]["url"])
    name = core.clean_text(config["sender"].get("name") or env.get("OUTREACH_SENDER_NAME") or "", 60)
    config["sender"]["name"] = name
    return config


def load_sources(path=None):
    sources = load_json(path or SOURCES_FILE)
    for field in ("feeds", "youtube_channels", "curators"):
        if not isinstance(sources.get(field), list):
            raise core.OutreachError(f"sources.json: '{field}' must be a list")
    if not isinstance(sources.setdefault("excluded", []), list):
        raise core.OutreachError("sources.json: 'excluded' must be a list")
    return sources


# ---- source records: what was checked, when, and what is still uncertain ---------------------------------------

SOURCE_TYPES = {"puzzle-curator", "indie-curator", "browser-game-directory", "youtube-creator", "blog", "newsletter"}
ROUTE_KINDS = {"form", "upload", "email"}
# meets: every stated requirement is met by the current demo. likely: route verified, no stated requirement is known to
# fail, but something could not be confirmed. uncertain: a requirement may not be met; kept on hold (never drafted).
# not-eligible: a stated requirement is not met; belongs in "excluded", never in a source list.
ELIGIBILITY = ("meets", "likely", "uncertain", "not-eligible")
DRAFTABLE = ("meets", "likely")
STALE_AFTER_DAYS = 120


def _seed_label(kind, seed):
    return f"{kind} '{seed.get('name', '?')}'" if isinstance(seed, dict) else f"{kind} entry"


def _seed_keys(kind, seed):
    """The canonical keys a seed claims (its own site/channel plus aliases for the same organisation)."""
    keys = []
    if kind == "youtube_channels":
        keys.append(f"youtube:channel:{seed.get('channel_id')}")
    elif seed.get("site_url"):
        keys.append(core.canonical_url(seed["site_url"]))
    keys += [core.canonical_url(alias) for alias in seed.get("aliases", [])]
    return keys


def source_problems(sources, today=None):
    """{label: [problem, ...]} for every seed whose record is incomplete or contradictory. Empty dict = all good.

    Discovery skips a seed that has problems (and says why); tests require the committed file to have none."""
    today = today or core.utcnow().date()
    problems, owners = {}, {}
    for kind in ("feeds", "youtube_channels", "curators"):
        for seed in sources.get(kind, []):
            label, found = _seed_label(kind, seed), []
            if not isinstance(seed, dict):
                problems[label] = ["not an object"]
                continue
            if not core.clean_text(seed.get("name")):
                found.append("missing name")
            try:
                if kind == "youtube_channels":
                    core.youtube_channel_url(seed.get("channel_id"))
                else:
                    core.validate_url(seed.get("site_url"))
                for field in ("submission_url", "contact_source_url", "feed_url"):
                    if seed.get(field):
                        core.validate_url(seed[field])
                keys = _seed_keys(kind, seed)
            except core.OutreachError as error:
                found.append(str(error))
                keys = []
            for key in keys:
                if key in owners and owners[key] != label:
                    found.append(f"same site/channel as {owners[key]} ({key}): one organisation, one prospect")
                owners.setdefault(key, label)
            if seed.get("source_type") not in SOURCE_TYPES:
                found.append(f"source_type must be one of {sorted(SOURCE_TYPES)}")
            checked = core.parse_time(seed.get("last_checked"))
            if checked is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(seed.get("last_checked"))):
                found.append("last_checked must be a date (YYYY-MM-DD)")
            elif (checked.date() - today).days > 1:
                found.append("last_checked is in the future")
            if not core.clean_text(seed.get("fit")):
                found.append("missing 'fit' (why Ringshift fits this source)")
            eligibility = seed.get("eligibility") if isinstance(seed.get("eligibility"), dict) else {}
            status = eligibility.get("status")
            if status not in ELIGIBILITY:
                found.append(f"eligibility.status must be one of {list(ELIGIBILITY)}")
            if status == "not-eligible":
                found.append("not-eligible sources belong in 'excluded'")
            verified = eligibility.get("verified")
            if not isinstance(verified, list) or not verified or not all(core.clean_text(v) for v in verified):
                found.append("eligibility.verified must list the requirements read on the source's own page")
            if not isinstance(eligibility.get("uncertain", []), list):
                found.append("eligibility.uncertain must be a list")
            if status in ("likely", "uncertain") and not eligibility.get("uncertain"):
                found.append(f"status '{status}' needs eligibility.uncertain to say what is not confirmed")
            if not isinstance(seed.get("enabled"), bool):
                found.append("enabled must be true or false")
            elif seed["enabled"] and status not in DRAFTABLE:
                found.append(f"enabled, but eligibility is '{status}': only {list(DRAFTABLE)} may be enabled")
            if seed.get("contact_email"):
                try:
                    core.validate_email(seed["contact_email"])
                except core.OutreachError as error:
                    found.append(f"contact_email: {error}")
                if not seed.get("contact_source_url"):
                    found.append("contact_email needs contact_source_url (the public page it is written on)")
            if kind == "curators":
                if not (seed.get("submission_url") or seed.get("contact_email")):
                    found.append("a curator needs submission_url or contact_email")
                if seed.get("submission_url") and not core.clean_text(seed.get("must_contain")):
                    found.append("a submission_url needs must_contain (a phrase proving the route is still open)")
                if seed.get("route_kind") not in ROUTE_KINDS:
                    found.append(f"route_kind must be one of {sorted(ROUTE_KINDS)}")
            if found:
                problems[label] = found
    for entry in sources.get("excluded", []):
        label = _seed_label("excluded", entry)
        found = []
        if not isinstance(entry, dict):
            problems[label] = ["not an object"]
            continue
        if not core.clean_text(entry.get("name")) or not core.clean_text(entry.get("reason")):
            found.append("excluded entries need a name and a reason")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(entry.get("last_checked", ""))):
            found.append("last_checked must be a date (YYYY-MM-DD)")
        try:
            key = core.canonical_url(entry.get("url"))
        except core.OutreachError as error:
            found.append(str(error))
        else:
            if key in owners:
                found.append(f"excluded, but also listed as {owners[key]}")
        if found:
            problems[label] = found
    return problems


def stale_sources(sources, today=None):
    """Labels of enabled seeds whose eligibility was last checked by a person more than STALE_AFTER_DAYS ago."""
    today = today or core.utcnow().date()
    stale = []
    for kind in ("feeds", "youtube_channels", "curators"):
        for seed in sources.get(kind, []):
            checked = core.parse_time(seed.get("last_checked")) if isinstance(seed, dict) else None
            if isinstance(seed, dict) and seed.get("enabled") and checked and \
                    (today - checked.date()).days > STALE_AFTER_DAYS:
                stale.append(_seed_label(kind, seed))
    return stale


def knowledge_mechanics(path=None):
    """The mechanic names listed in the knowledge file (the '- Name' bullets under 'Important mechanics')."""
    text = Path(path or KNOWLEDGE_FILE).read_text(encoding="utf-8").replace("\\", "")
    section = text.split("Important mechanics include:", 1)
    if len(section) < 2:
        return set()
    body = section[1].split("##", 1)[0]
    return {m.strip() for m in re.findall(r"(?m)^\s*-\s*([A-Za-z][A-Za-z -]+?)\s*$", body)}


def check_game_facts(config, knowledge_path=None):
    """Problems with the fixed game sentences: every mechanic word must be backed by the knowledge file."""
    known = knowledge_mechanics(knowledge_path)
    problems = []
    if not known:
        problems.append("could not read the mechanics list from RINGSHIFT_KNOWLEDGE.md")
    text = " ".join(str(config["game"].get(k, "")) for k in ("pitch", "mechanics_line", "short", "demo_line"))
    for word in re.findall(r"[a-z]+(?:-[a-z]+)?", text.lower()):
        if word in MECHANIC_WORDS and MECHANIC_WORDS[word] not in known:
            problems.append(f"'{word}' is not a mechanic listed in RINGSHIFT_KNOWLEDGE.md")
    return problems
