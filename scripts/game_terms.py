"""The game's own vocabulary, from RINGSHIFT_KNOWLEDGE.md, and deterministic checks against it.

The knowledge file lists these mechanics: Mirrors, Portals, Amplifiers, Blocks, Half-Blocks, Prisms, Echo
(plus Locks and Source beams, which are always fine to mention). It says "the exact behavior of mechanics
must never be invented". Two kinds of mistakes slipped past an AI-only critic and are caught here in code:

1. Using a name the game does not use ("black hole", "satellite": internal search tags for Portal and Echo).
2. Naming a mechanic that the attached media does not show.
"""

import re

# Canonical (knowledge-file) name -> regex of its written forms. Order matters: Half-Block is removed
# from the text before Block is looked for.
MECHANICS = {
    "Mirror": r"mirrors?",
    "Portal": r"portals?",
    "Amplifier": r"amplifiers?",
    "Half-Block": r"half[\s-]?blocks?",
    "Block": r"blocks?",
    "Prism": r"prisms?",
    "Echo": r"echo(?:es|ed|ing)?",
}

DISPLAY_ORDER = ["Mirror", "Portal", "Amplifier", "Block", "Half-Block", "Prism", "Echo"]

# Names that are NOT in the knowledge file. They exist only as internal tags, or are removed/unknown features.
UNKNOWN_TERMS = {
    "black hole": r"black[\s-]?holes?",
    "satellite": r"satellites?",
    "wormhole": r"wormholes?",
    "teleporter": r"teleporters?",
    "magnet": r"magnet(?:s|ic)?",
}


def _find(text, patterns):
    found = set()
    remaining = " " + str(text or "").lower() + " "

    for name, pattern in patterns.items():
        regex = re.compile(r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])")
        if regex.search(remaining):
            found.add(name)
            remaining = regex.sub(" ", remaining)       # so "half-block" is not also counted as "block"

    return found


def mechanics_mentioned(text):
    """Canonical mechanic names written in `text`, in DISPLAY_ORDER."""
    found = _find(text, MECHANICS)
    return [name for name in DISPLAY_ORDER if name in found]


def unknown_terms(text):
    """Terms the game does not use that appear in `text`."""
    return sorted(_find(text, UNKNOWN_TERMS))


def mechanics_shown(media):
    """Mechanics a media item verifiably shows: the canonical names in its verified description."""
    return mechanics_mentioned((media or {}).get("description", ""))


def check_mechanics(text, media):
    """Problems with the mechanic names in a post, or []. Deterministic; no AI involved."""
    problems = []

    unknown = unknown_terms(text)
    if unknown:
        problems.append("uses name(s) the game does not use: " + ", ".join(unknown)
                        + " (use the knowledge-file names, e.g. Portal / Echo)")

    shown = set(mechanics_shown(media))
    extra = [name for name in mechanics_mentioned(text) if name not in shown]
    if extra:
        if shown:
            problems.append("names mechanic(s) the media does not show: " + ", ".join(extra)
                            + " (it shows: " + ", ".join(m for m in DISPLAY_ORDER if m in shown) + ")")
        else:
            problems.append("names mechanic(s) (" + ", ".join(extra) + ") but this media has no verified mechanic list")

    return problems
