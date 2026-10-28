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
    return sources


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
