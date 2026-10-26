"""Which media can honestly go with which kind of post, and checks of a post's text against its media.

Everything here is deterministic (no AI, no network):
* REQUIREMENTS says which media categories (see media_manifest.categories_for) each post type may use.
* eligible() / why_not() apply that to one manifest record.
* grounding_block() turns a record into the facts the writer and the critic are given.
* text_problems() rejects text that contradicts the media (e.g. asks the viewer to solve a puzzle the media shows
  solved, says "solved" over an unsolved puzzle, says "watch" over a still image).
* load_project_facts() reads data/project_facts.json: the only source for developer-progress style posts.
"""

import json
import re
from pathlib import Path

import game_terms

# post type id -> what its media must support. "categories": any one is enough.
REQUIREMENTS = {
    "challenge":     {"categories": ("puzzle_challenge",)},
    "hook":          {"categories": ("puzzle_challenge", "gameplay_moment"), "media_type": "video"},
    "mechanic":      {"categories": ("mechanism_demo",)},
    "observation":   {"categories": ("visual_showcase",)},
    "visual":        {"categories": ("visual_showcase",)},
    "solution":      {"categories": ("solution_reveal",)},
    "moment":        {"categories": ("gameplay_moment",), "media_type": "video"},
    "feedback":      {"categories": ("visual_showcase",)},
    "discovery":     {"categories": ("visual_showcase",)},
    "progress":      {"categories": ("visual_showcase",), "needs_facts": "progress"},
    "behind_scenes": {"categories": ("visual_showcase",), "needs_facts": "behind_the_scenes"},
    "invite":        {"categories": ("visual_showcase",)},
}

FACT_KINDS = ("progress", "behind_the_scenes")


def requirement(post_type_id):
    return REQUIREMENTS.get(post_type_id) or {"categories": ("visual_showcase",)}


def why_not(item, post_type_id):
    """None when `item` (a manifest record) can carry a post of this type; otherwise the reason it cannot."""
    need = requirement(post_type_id)
    categories = item.get("post_categories") or []

    if not categories:
        return "media supports no post type (no description, failed validation, excluded or special mode)"
    if need.get("media_type") and item.get("type") != need["media_type"]:
        return f"post type '{post_type_id}' needs a {need['media_type']}, this is a {item.get('type')}"
    if not any(category in categories for category in need["categories"]):
        state = item.get("puzzle_state", "unknown")
        return (f"post type '{post_type_id}' needs {' or '.join(need['categories'])}; "
                f"this media offers {', '.join(categories)} (puzzle state: {state})")
    return None


def eligible(item, post_type_id):
    return why_not(item, post_type_id) is None


# ----------------------------------------------------------------------- project facts

def load_project_facts(path):
    """Verified project facts from data/project_facts.json: a list of
    {"id", "kind": "progress"|"behind_the_scenes", "statement", "source"}. Malformed entries are ignored
    (never repaired or guessed), so a typo can only remove a fact, never add one."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []

    facts = data.get("facts") if isinstance(data, dict) else data
    if not isinstance(facts, list):
        return []

    valid = []
    for fact in facts:
        if (isinstance(fact, dict) and fact.get("kind") in FACT_KINDS
                and str(fact.get("statement") or "").strip() and str(fact.get("source") or "").strip()
                and str(fact.get("id") or "").strip()):
            valid.append({key: fact.get(key) for key in ("id", "kind", "statement", "source", "date")})
    return valid


def facts_of_kind(facts, kind):
    return [fact for fact in facts or [] if fact.get("kind") == kind]


def post_type_available(post_type, project, facts):
    """Whether a post type can be written at all right now (needs a project URL or verified facts)."""
    if post_type.get("needs_url") and not (project or {}).get("project_url"):
        return False
    needed = requirement(post_type["id"]).get("needs_facts")
    if needed and not facts_of_kind(facts, needed):
        return False
    return True


# -------------------------------------------------------------------------- grounding

def media_facts(media):
    """The verified, human-readable facts about one media item, as a list of short lines."""
    lines = []
    level, mode = media.get("level"), media.get("mode")
    if level and mode in ("easy", "normal", "hard"):
        lines.append(f"It is Level {level} ({mode.title()}).")
    elif level:
        lines.append(f"It is Level {level}.")

    state = media.get("puzzle_state")
    is_video = media.get("type") == "video"
    if state == "unsolved":
        lines.append("The puzzle is NOT solved in this media.")
    elif state == "in_progress":
        lines.append("The puzzle is NOT solved in this media: the clip stops partway through an attempt.")
    elif state == "solved":
        lines.append("The level IS solved by the end of this media (the BREACHED screen)." if is_video
                     else "The level IS solved in this media (the BREACHED screen is showing).")

    if is_video:
        lines.append("It is a video showing rotations being entered." if media.get("shows_action")
                     else "It is a video; what happens in it is only what the description says.")
    else:
        lines.append("It is a still image: nothing moves.")

    if media.get("theme"):
        lines.append(f"Visual theme: {media['theme']}.")
    return lines


def grounding_block(media, post_type, facts=None):
    """Audience, objective, what the media shows, what is safe to say and what is not."""
    lines = ["--- POST BRIEF ---"]
    if post_type.get("audience"):
        lines.append(f"Audience: {post_type['audience']}")
    if post_type.get("objective"):
        lines.append(f"Objective: {post_type['objective']}")
    lines.append("--- END BRIEF ---")

    shown = media_facts(media or {})
    state = (media or {}).get("puzzle_state")
    is_image = (media or {}).get("type") == "image"

    lines.append("--- WHAT THE MEDIA VISIBLY SHOWS (verified) ---")
    lines.extend(f"- {line}" for line in shown) if shown else lines.append("- (no structured facts; use only the description)")
    lines.append("--- END ---")

    do_not = []
    if state in ("unsolved", "in_progress"):
        do_not.append("Do not say or imply the puzzle is solved, and do not reveal or hint at the solution.")
    if state == "solved":
        do_not.append("Do not ask the viewer to solve this puzzle or find its solution: the media already shows it solved.")
    if is_image:
        do_not.append("Do not tell the viewer to watch, press play or follow motion: it is a still image.")
    do_not.append("Do not state move counts, difficulty, or that a solution is unique unless the verified facts say so.")
    lines.append("Do NOT: " + " ".join(do_not))

    usable = facts_of_kind(facts, post_type.get("fact_kind") or "")
    if usable:
        lines.append("--- VERIFIED PROJECT FACTS (the only developer-progress claims you may make) ---")
        lines.extend(f"- {fact['statement']} (source: {fact['source']})" for fact in usable)
        lines.append("--- END FACTS ---")

    return "\n".join(lines)


def evidence_sources(media, knowledge, facts=None):
    """The text a critic's evidence quotes must be found in: knowledge file, media description and facts."""
    parts = [knowledge or "", (media or {}).get("description") or ""]
    parts.extend(media_facts(media or {}))
    parts.extend(fact.get("statement", "") for fact in facts or [])
    return "\n".join(parts)


# ----------------------------------------------------------------- text versus media

ASK_TO_SOLVE_RE = re.compile(
    r"\b(?:can|could|would|will|do)\s+(?:you|u)\s+(?:solve|crack|find|figure|work\s+out|spot|see|beat|get)\b"
    r"|\b(?:solve|crack|beat)\s+(?:this|it|the\s+(?:puzzle|level))\b"
    r"|\bfind\s+the\s+(?:solution|answer)\b|\b(?:see|spot)\s+the\s+(?:solution|answer)\b"
    r"|\bhow\s+would\s+you\s+solve\b|\bfigure\s+(?:it|this)\s+out\b|\byour\s+(?:solution|answer)\b",
    re.IGNORECASE)
SOLVED_CLAIM_RE = re.compile(r"\b(?:solved|cracked|cleared|breached|finally\s+beat|beat\s+it)\b", re.IGNORECASE)
MOTION_WORDS_RE = re.compile(
    r"\b(?:watch|watching|press\s+play|in\s+this\s+(?:clip|video)|this\s+clip|this\s+video|as\s+it\s+(?:moves|plays))\b",
    re.IGNORECASE)


def text_problems(text, media, post_type=None):
    """Reasons `text` contradicts what `media` shows. [] means no contradiction was found.
    Uses the structured fields when the media has them; otherwise only what is certain from type/description."""
    problems = []
    media = media or {}
    state = media.get("puzzle_state")
    kind = media.get("type")

    if state == "solved" and ASK_TO_SOLVE_RE.search(text):
        problems.append("asks the viewer to solve or find the solution, but the media already shows the level solved")
    if state in ("unsolved", "in_progress") and SOLVED_CLAIM_RE.search(text):
        problems.append("says the level is solved, but the media shows it unsolved")
    if kind == "image" and MOTION_WORDS_RE.search(text):
        problems.append("refers to watching or a clip, but the media is a still image")

    if post_type:
        need = requirement(post_type.get("id"))
        if need.get("media_type") and kind and kind != need["media_type"]:
            problems.append(f"post type '{post_type['id']}' needs a {need['media_type']}")

    return problems


def opening_words(text, count=3):
    """The first few words of a post, lower-cased without punctuation: a hook's identity."""
    words = re.sub(r"[^a-z0-9' ]+", " ", str(text).lower()).split()
    return " ".join(words[:count])


def repeats_opening(candidate, recent_posts, count=3):
    """The earlier post that starts with the same first words as `candidate`, or None."""
    key = opening_words(candidate, count)
    if len(key.split()) < count:
        return None
    for other in recent_posts or []:
        if opening_words(other, count) == key:
            return other
    return None


def canonical_mechanics(media):
    return game_terms.mechanics_shown(media)
