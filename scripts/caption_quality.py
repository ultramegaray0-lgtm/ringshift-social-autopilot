"""Deterministic caption quality checks: repeats, generic filler, and "does this caption belong to this media?".

No AI and no network. These run on every candidate BEFORE the critic is asked anything, so an empty caption such as
"Brand new idea about rings." or the same question reworded three times never costs a critic call and can never
become a draft with an approval code.

* same_idea / distinct_candidates: repeated captions are found by what they SAY (their content words), so changing
  the punctuation, a hashtag or one synonym does not hide a repeat, and a different content hash does not make a
  caption different.
* filler_problems: stock phrases that carry no information, and captions with almost no specific words.
* anchor_problems: the caption must refer to something verified about THIS media (a mechanic it shows, its level,
  its theme) or, for progress-style posts, to a verified project fact.
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


def anchor_problems(text, media, post_type_id=None, facts=None):
    """A caption must refer to something verified about this media. [] when it does (or needs no anchor)."""
    if post_type_id in ANCHORLESS_TYPES:
        return []
    anchors = anchors_for(media, post_type_id, facts if post_type_id in FACT_TYPES else None)
    if not anchors:
        return ["the media has no verified facts to refer to, so a caption cannot be matched to it"]
    if post_type_id in FACT_TYPES:
        if not [a for a in mentions_anchor(text, anchors) if a.startswith("fact word")]:
            return ["does not state one of the verified project facts it is supposed to be about"]
        return []
    if not mentions_anchor(text, anchors):
        shown = ", ".join(sorted(anchors)[:4])
        return [f"does not refer to anything verified about this media (it could be: {shown})"]
    return []


def quality_problems(text, media, post_type_id=None, facts=None):
    """Everything this module rejects a caption for."""
    return filler_problems(text) + anchor_problems(text, media, post_type_id, facts)
