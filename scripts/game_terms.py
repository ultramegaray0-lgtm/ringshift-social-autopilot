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
    """Mechanics a media item verifiably shows. A manifest record carries an explicit canonical list; older
    entries fall back to the canonical names written in their verified description."""
    media = media or {}
    listed = media.get("mechanisms")

    if isinstance(listed, list):
        return [name for name in DISPLAY_ORDER if name in listed]

    return mechanics_mentioned(media.get("description", ""))


# Some mechanic names are also everyday English words ("a block of time", "an echo of last week", "mirror image").
# For those, a use only counts as naming the mechanic when it is capitalised mid-sentence ("That Prism bounce")
# or sits in a sentence that is clearly about the puzzle. Portal, Amplifier and Half-Block are never ordinary words.
ORDINARY_WORDS = {"Block", "Mirror", "Echo", "Prism"}
GAME_CONTEXT_RE = re.compile(
    r"\b(?:beams?|rings?|locks?|puzzles?|levels?|gimmicks?|mechanics?|sources?|rim|spokes?|portals?|amplifiers?|"
    r"half[\s-]?blocks?|blocks?|mirrors?|prisms?|echoe?s?|solution|solves?|rotat\w*)\b", re.IGNORECASE)


def _used_as_game_term(text, name, pattern):
    """True if `name` appears in `text` in a game sense. Names that are not ordinary words always count."""
    regex = re.compile(r"(?<![A-Za-z0-9])" + pattern + r"(?![A-Za-z0-9])", re.IGNORECASE)

    if name not in ORDINARY_WORDS:
        return bool(regex.search(text))

    for match in regex.finditer(text):
        before = text[:match.start()].rstrip()
        mid_sentence = bool(before) and before[-1] not in ".!?\n"

        if match.group(0)[0].isupper() and mid_sentence:
            return True

        start = max(text.rfind(mark, 0, match.start()) for mark in ".!?\n") + 1
        ends = [i for i in (text.find(mark, match.end()) for mark in ".!?\n") if i != -1]
        sentence = text[start:min(ends) if ends else len(text)]
        others = GAME_CONTEXT_RE.sub(lambda m: " " if m.group(0).lower() == match.group(0).lower() else m.group(0), sentence)

        if GAME_CONTEXT_RE.search(others):
            return True

    return False


def mechanics_used_as_names(text):
    """Canonical mechanic names `text` uses as game terms (see ORDINARY_WORDS), in DISPLAY_ORDER."""
    mentioned = set(mechanics_mentioned(text))
    return [name for name in DISPLAY_ORDER
            if name in mentioned and _used_as_game_term(str(text or ""), name, MECHANICS[name])]


def check_mechanics(text, media):
    """Problems with the mechanic names in a post, or []. Deterministic; no AI involved."""
    problems = []

    unknown = unknown_terms(text)
    if unknown:
        problems.append("uses name(s) the game does not use: " + ", ".join(unknown)
                        + " (use the knowledge-file names, e.g. Portal / Echo)")

    shown = set(mechanics_shown(media))
    extra = [name for name in mechanics_used_as_names(text) if name not in shown]
    if extra:
        if shown:
            problems.append("names mechanic(s) the media does not show: " + ", ".join(extra)
                            + " (it shows: " + ", ".join(m for m in DISPLAY_ORDER if m in shown) + ")")
        else:
            problems.append("names mechanic(s) (" + ", ".join(extra) + ") but this media has no verified mechanic list")

    return problems
