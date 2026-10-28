"""Deterministic caption quality checks: repeats, generic filler, and "does this caption belong to this media?".

No AI and no network. These run on every candidate BEFORE the critic is asked anything, so an empty caption such as
"Brand new idea about rings." or the same question reworded three times never costs a critic call and can never
become a draft with an approval code.

* same_idea / distinct_candidates: repeated captions are found by what they SAY (their content words), so changing
  the punctuation, a hashtag or one synonym does not hide a repeat, and a different content hash does not make a
  caption different.
* filler_problems: stock phrases that carry no information, and captions with almost no specific words.
* anchor_problems: the caption must refer to something verified about THIS media (a mechanic it shows, its level,
  its theme), or, for progress-style posts, to a verified project fact, or, when nothing more specific applies, to the
  game's verified identity (IDENTITY_ANCHORS, quoted word for word from the knowledge file).
* invented_detail_problems: positions of game objects relative to each other and what they do or how they move are in
  no verified fact, so a caption stating them is rejected without asking the critic.
"""

import re

import game_terms

# Words that carry no information about this particular picture or idea.
STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have how i if in into is it its just like
me my no not of on one or our out over so than that the their them then there these they this those to up us was we
were what when where which who why will with would you your yours about every each any all some more most very really
actually genuinely totally
""".split())

# Game words so common in our posts that using them says nothing specific.
GENERIC_GAME_WORDS = frozenset("""
ring rings beam beams lock locks puzzle puzzles level levels game games ringshift turn turns turning rotate rotates
rotating spin spins first second next move moves light lights lit idea ideas thought thoughts new fresh different
thing things something look looks looking see sees way ways try tries play plays playing think thinks
""".split())

FILLER_PATTERNS = [
    r"\bbrand[- ]new\b", r"\bgenuinely\b", r"\b(?:new|fresh|different) (?:idea|thought|take|angle)\b",
    r"\b(?:idea|thought)s? about\b", r"\bsomething (?:new|different|fresh|special)\b",
    r"\bcheck (?:it|this|that) out\b", r"\byou(?:'ll| will| won't| wont| will not) (?:believe|love|want)\b",
    r"\bgame[- ]?chang\w+", r"\bmind[- ]?blow\w+", r"\bmust[- ](?:see|play|try|watch)\b", r"\bso (?:good|cool|satisfying|addictive)\b",
    r"\bjust (?:a )?(?:thought|post|vibes?)\b", r"\b(?:good|great|nice|chill) vibes?\b", r"\bstay tuned\b", r"\bmore (?:to come|soon)\b",
    r"\bobsessed\b", r"\bbanger\b", r"\bhype\b", r"\bamazing\b", r"\bincredible\b", r"\bcan(?:'|no)?t wait\b",
    r"\bwhat do you think\??$", r"\bthoughts\?$",
]
FILLER_RES = [re.compile(p, re.IGNORECASE) for p in FILLER_PATTERNS]

MIN_SPECIFIC_WORDS = 2
SAME_IDEA_JACCARD = 0.6          # content-word overlap at or above this = the same idea
SAME_OPENING_JACCARD = 0.4       # ...or this much overlap when the first words are identical

_CLEAN_RE = re.compile(r"https?://\S+|[#@]\w+")
_WORD_RE = re.compile(r"[a-z0-9']+")

# Light stemming so "rings"/"ring", "turning"/"turn" count as the same word.
_SUFFIXES = ("ing", "ed", "es", "s")


def _stem(word):
    word = word.strip("'")
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def normalize(text):
    """Lower-case words only: no URLs, hashtags, mentions or punctuation."""
    return " ".join(_WORD_RE.findall(_CLEAN_RE.sub(" ", str(text).lower())))


def words(text):
    return [_stem(w) for w in normalize(text).split()]


def content_words(text, drop_generic=False):
    """The informative words of a caption (stems), without stopwords; optionally also without common game words."""
    skip = {_stem(w) for w in STOPWORDS}
    if drop_generic:
        skip |= {_stem(w) for w in GENERIC_GAME_WORDS}
    return {w for w in words(text) if w not in skip and len(w) > 1}


def _jaccard(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0


def same_idea(a, b):
    """(True, reason) when two captions are effectively the same post, however they are worded or hashed."""
    if normalize(a) == normalize(b):
        return True, "identical wording"
    words_a, words_b = content_words(a), content_words(b)
    if not words_a or not words_b:
        return False, ""
    overlap = _jaccard(words_a, words_b)
    if overlap >= SAME_IDEA_JACCARD:
        return True, f"{overlap:.0%} of the same words"
    first_a, first_b = normalize(a).split()[:3], normalize(b).split()[:3]
    if first_a and first_a == first_b and overlap >= SAME_OPENING_JACCARD:
        return True, "same opening and the same words after it"
    # The same question shape with one word swapped ("Which ring would you turn first?" / "...rotate first?")
    shape_a, shape_b = words(a), words(b)
    if len(shape_a) >= 4 and len(shape_a) == len(shape_b):
        same_positions = sum(1 for x, y in zip(shape_a, shape_b) if x == y)
        if same_positions / len(shape_a) >= 0.7:
            return True, "same sentence with a word or two changed"
    return False, ""


def distinct_candidates(candidates, earlier=(), limit=None):
    """(kept, dropped). `kept` are captions that differ in substance from each other AND from `earlier` posts;
    `dropped` is [(text, reason)]. Order is preserved: the first of two repeats wins. `limit` caps how many are kept."""
    kept, dropped = [], []
    for text in candidates:
        if not normalize(text):
            dropped.append((text, "empty"))
            continue
        clash = None
        for other in list(earlier) + kept:
            same, why = same_idea(text, other)
            if same:
                clash = (other, why)
                break
        if clash:
            origin = "an earlier post" if clash[0] in earlier else "another candidate in this batch"
            dropped.append((text, f"repeats {origin} ({clash[1]})"))
        elif limit is not None and len(kept) >= limit:
            dropped.append((text, "over the per-run candidate limit"))
        else:
            kept.append(text)
    return kept, dropped


def filler_problems(text):
    """Reasons a caption is generic filler, or []."""
    problems = []
    cleaned = _CLEAN_RE.sub(" ", str(text)).strip()
    for pattern in FILLER_RES:
        match = pattern.search(cleaned)
        if match:
            problems.append(f"generic filler ({match.group(0)!r} says nothing about this media)")
            break
    if len(content_words(text, drop_generic=True)) < MIN_SPECIFIC_WORDS:
        problems.append("too generic: after removing everyday and game words almost nothing specific is left")
    return problems


# ---------------------------------------------------------------------------------------------- anchors

ANCHORLESS_TYPES = ("invite",)           # a link post needs no media detail
FACT_TYPES = ("progress", "behind_scenes")

# The game's verified identity: phrases copied word for word from RINGSHIFT_KNOWLEDGE.md (a test proves every spelling
# still occurs there, so this list can never drift into an unverified claim). A caption that names one of them is about
# the game itself, which is always true of a Ringshift gameplay picture. This is what lets a truthful caption about the
# puzzle-solving experience pass when nothing more specific about the media can be verified, instead of every caption
# being refused. Repetition, filler, the critic's evidence check and its minimum score still apply to it in full.
IDENTITY_ANCHORS = {
    "identity circular puzzle": ["circular puzzle"],
    "identity rings and beams": ["rings and beams", "beams, rings"],
    "identity source beams": ["source beams"],
    "identity indie puzzle game": ["indie puzzle game"],
    "identity visual themes": ["visual themes"],
}


def anchors_for(media, post_type_id=None, facts=None):
    """The words/phrases that tie a caption to THIS media (or to a verified project fact): {label: [spellings]}."""
    media = media or {}
    anchors = {}
    for name in media.get("mechanisms") or game_terms.mechanics_shown(media):
        anchors[f"mechanic {name}"] = [name.lower()]
    level = media.get("level")
    if level:
        anchors[f"level {level}"] = [f"level {level}", f"l{level}", f"lvl {level}"]
    if media.get("theme"):
        anchors[f"theme {media['theme']}"] = [str(media["theme"]).lower()]
    mode = media.get("mode")
    if mode in ("easy", "normal", "hard"):
        anchors[f"{mode} difficulty"] = [f"{mode} level", f"{mode} mode", f"on {mode}", f"{mode} puzzle"]
    for fact in facts or []:
        for word in sorted(content_words(fact.get("statement", ""), drop_generic=True))[:6]:
            anchors[f"fact word {word}"] = [word]
    return anchors


def mentions_anchor(text, anchors):
    """The labels of the anchors `text` refers to."""
    lowered = " " + normalize(text) + " "
    stems = set(words(text))
    found = []
    for label, spellings in anchors.items():
        for spelling in spellings:
            token = normalize(spelling)
            if (" " + token + " " in lowered) or (" " not in token and _stem(token) in stems):
                found.append(label)
                break
    return found


def identity_mentions(text):
    """The verified-identity anchors (see IDENTITY_ANCHORS) that `text` names."""
    return mentions_anchor(text, IDENTITY_ANCHORS)


def anchor_problems(text, media, post_type_id=None, facts=None):
    """A caption must refer to something verified: about this media (a mechanic it shows, its level, theme or
    difficulty) or, failing that, the game's verified identity from the knowledge file. [] when it does (or needs no
    anchor). Progress-style posts must state their verified project fact; identity does not replace that."""
    if post_type_id in ANCHORLESS_TYPES:
        return []
    anchors = anchors_for(media, post_type_id, facts if post_type_id in FACT_TYPES else None)
    if post_type_id in FACT_TYPES:
        if not [a for a in mentions_anchor(text, anchors) if a.startswith("fact word")]:
            return ["does not state one of the verified project facts it is supposed to be about"]
        return []
    if anchors and mentions_anchor(text, anchors):
        return []
    if identity_mentions(text):
        return []
    if not anchors:
        return ["the media has no verified facts to refer to, so only a caption about the game's verified identity "
                "(for example its circular puzzle of rings and beams) can go with it"]
    shown = ", ".join(sorted(anchors)[:4])
    return [f"does not refer to anything verified about this media (it could be: {shown}) or to the game's verified "
            "identity"]


# ---------------------------------------------------------------------------------------------- invented details

# Nothing verified ever says where a game object sits relative to another, how it moves, or what it does: the knowledge
# file states that mechanic behaviour must never be invented, and the media facts hold only level, difficulty, state,
# still/video, theme and the mechanic list. Captions that state such things anyway were the commonest real failure
# (run #47: "a block sitting right beside the portal", "the block shift positions right before the portal catches the
# beam"). The critic's evidence check only proves a quote EXISTS, so a critic that quotes the mechanic list as
# "evidence" for a position would pass it; this check does not depend on the critic. Rings are left out on purpose:
# turning rings is the verified core of the game ("the player manipulates rings and beams").
_OBJECTS = r"(?:mirrors?|portals?|amplifiers?|half[\s-]?blocks?|blocks?|prisms?|echo(?:es)?|locks?|beams?|sources?)"
_RELATIONS = (r"(?:right\s+|just\s+|directly\s+)?(?:beside|next\s+to|behind|in\s+front\s+of|between|adjacent\s+to|"
              r"on\s+top\s+of|underneath|across\s+from|opposite|lined?\s+up\s+with|lines\s+up\s+with|line\s+up\s+with|"
              r"pointing\s+at|aimed\s+at|facing)")
_BEHAVIOURS = (r"(?:shift(?:s|ed|ing)?|slid(?:e|es|ing)|mov(?:e|es|ed|ing)|catch(?:es|ing)?|caught|bounc(?:e|es|ed|ing)|"
               r"bend(?:s|ing)?|bent|split(?:s|ting)?|teleport(?:s|ed|ing)?|redirect(?:s|ed|ing)?|reflect(?:s|ed|ing)?|"
               r"swallow(?:s|ed|ing)?|send(?:s|ing)?|sent|cop(?:y|ies|ied|ying)|duplicat(?:e|es|ed|ing)|"
               r"hit(?:s|ting)?|light(?:s|ed)?\s+up|lit\s+up|loop(?:s|ed|ing)?|swap(?:s|ped|ping)?)")
_RELATION_RE = re.compile(rf"\b{_OBJECTS}\b(?:\s+\w+){{0,2}}?\s+{_RELATIONS}\s+(?:the\s+|a\s+|an\s+|that\s+|this\s+)?{_OBJECTS}\b",
                          re.IGNORECASE)
_BEHAVIOUR_RE = re.compile(rf"\b{_OBJECTS}\s+(?:\w+ly\s+)?{_BEHAVIOURS}\b", re.IGNORECASE)


def invented_detail_problems(text):
    """Reasons a caption states a position between game objects or what an object does, neither of which any verified
    fact supports. [] when it states neither."""
    problems = []
    found = _RELATION_RE.search(str(text))
    if found:
        problems.append(f"states where game objects sit relative to each other ({found.group(0)!r}); positions are not "
                        "in the verified facts")
    found = _BEHAVIOUR_RE.search(str(text))
    if found:
        problems.append(f"states what a game object does or how it moves ({found.group(0)!r}); behaviour and movement "
                        "are not in the verified facts")
    return problems


LEVEL_RE = re.compile(r"\b(?:level|lvl|l)\s*(\d{1,3})\b", re.IGNORECASE)


def level_problems(text, media):
    """A caption that names a level number must name the level the media really shows."""
    shown = (media or {}).get("level")
    if not shown:
        return []
    wrong = sorted({int(n) for n in LEVEL_RE.findall(str(text))} - {int(shown)})
    return [f"refers to level {n} but the media shows level {shown}" for n in wrong]


def quality_problems(text, media, post_type_id=None, facts=None):
    """Everything this module rejects a caption for."""
    return (filler_problems(text) + level_problems(text, media) + invented_detail_problems(text)
            + anchor_problems(text, media, post_type_id, facts))
