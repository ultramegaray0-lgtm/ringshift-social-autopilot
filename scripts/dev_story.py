"""Developer-led posts: the indie developer sharing what they built, in the first person. Deterministic: no AI, no network.

The feed used to describe screenshots ("The Terracotta Courtyard visual theme changes how the puzzle looks ...").
This module makes about two thirds of the posts developer-led ("I built this level", "here is what makes this puzzle
interesting", "a small development update") and rotates how each one invites a reply or a connection, without
letting the writer invent anything:

* rotation_order: which post type a run tries first. Two of every three rotation positions are developer-led
  (DEVELOPER_LED_TYPES); the third goes to the other types, least recently published first, so they keep their variety.
* choose_connection_style: one of four endings per run (an invitation to fellow indie developers, a specific question
  about the visible design, a maker's note inviting people to follow the development, or a showcase with no ask),
  rotated so consecutive developer posts do not end the same way.
* angle_slots: three different developer subjects per writing round (the mechanic I built, the level I built, what makes
  the puzzle interesting, a small development update), all anchored on verified media facts.
* voice_problems: the deterministic screen for a developer-story caption (first person, a verified detail of THIS media,
  the run's ending style, no invented development history or anecdote).
* closing_problems / bait_problems: for every post type, no repeated closing line and no engagement bait.
* fact_captions: developer-voice captions built ONLY from verified media fields and the knowledge file's DEVELOPER VOICE
  sentences, the floor of the second writing round. They are still screened and must pass the critic like any caption.
"""

import math
import re

import caption_quality
import game_terms

DEV_STORY = "dev_story"
DEVELOPER_LED_TYPES = ("dev_story", "progress", "behind_scenes")
ROTATION_PERIOD = 3                # rotation positions per cycle ...
OTHER_SLOT = 0                     # ... of which position 0 goes to the other types and 1, 2 to developer-led types
TARGET_DEVELOPER_SHARE = (ROTATION_PERIOD - 1) / ROTATION_PERIOD


def is_developer_slot(position):
    """True for two of every three rotation positions. Any run of three consecutive positions, or positions that
    advance by two (a daily post moves both the post counter and the day), holds exactly two developer slots."""
    return position % ROTATION_PERIOD != OTHER_SLOT


def _last_used(post_log, key):
    """{value: index of its latest entry in the post log} for `key` (post_type or connection_style)."""
    seen = {}
    for index, entry in enumerate(post_log or []):
        value = (entry or {}).get(key) if isinstance(entry, dict) else None
        if value:
            seen[value] = index
    return seen


def _rotation_start(position, count):
    """Where the tie-break rotation starts. It is the old plain rotation (position modulo the number of types) plus one
    extra step each time the positions have gone round a whole common cycle, so even when only every third position
    reaches these types (positions 0, 3, 6, ...) every type gets its turn in draft-only runs, which log nothing."""
    cycle = math.lcm(ROTATION_PERIOD, count)
    return (position + position // cycle) % count


def _least_recent(ids, position, post_log, key):
    """`ids` ordered least recently used first (never used before anything used), ties broken by the rotation."""
    if not ids:
        return []
    last = _last_used(post_log, key)
    start = _rotation_start(position, len(ids))
    return sorted(ids, key=lambda i: (last.get(i, -1), (ids.index(i) - start) % len(ids)))


def rotation_order(types, position, post_log=()):
    """The post types (dicts, already filtered to the eligible ones) in the order a run should try them.

    Developer slot: developer-led types first (least recently published first), then the rest. Other slot: the other
    types first, least recently published first, then the developer-led ones. Every eligible type stays in the list,
    so media that fits none of the preferred types still falls back to one it does fit."""
    developer = [t for t in types if t["id"] in DEVELOPER_LED_TYPES]
    others = [t for t in types if t["id"] not in DEVELOPER_LED_TYPES]
    by_id = {t["id"]: t for t in types}
    dev_ids = _least_recent([t["id"] for t in developer], position, post_log, "post_type")
    other_ids = _least_recent([t["id"] for t in others], position, post_log, "post_type")
    order = dev_ids + other_ids if is_developer_slot(position) else other_ids + dev_ids
    if not dev_ids or not other_ids:
        order = dev_ids or other_ids
    return [by_id[i] for i in order]


# ------------------------------------------------------------------------------------------- connection styles

CONNECTION_STYLES = {
    "design_question": {
        "label": "a specific question about the visible design",
        "question": True,
        "instruction": ("End with ONE short, specific question about something visible in the media (a mechanic on "
                        "screen, the theme, the level) that a viewer can answer by looking. Not a request to solve it, "
                        "not 'what do you think?', not 'what are you working on?'."),
    },
    "maker_note": {
        "label": "a short maker's note with an invitation to follow the development",
        "question": False,
        "instruction": ("No question. End with one natural line inviting people to follow along as Ringshift is built "
                        "(for example that I share the build here as it comes together). No 'stay tuned', no "
                        "'more soon', no promise of what comes next or when."),
    },
    "indie_invite": {
        "label": "an invitation to fellow indie developers to share what they are building",
        "question": True,
        "instruction": ("End by inviting fellow indie developers to share what they are making, in your own words "
                        "(not 'Let's connect' and not 'What are you working on?', which were used before)."),
    },
    "showcase": {
        "label": "a showcase that stands on its own",
        "question": False,
        "instruction": ("No question and no call to action: let the mechanic or level stand on its own. End on the "
                        "concrete detail, not on an invitation."),
    },
}
STYLE_ORDER = ("design_question", "maker_note", "indie_invite", "showcase")
QUESTION_SHARE_LIMIT = 0.5          # the same limit screen_candidate applies to questions


def choose_connection_style(position, post_log=(), question_share=0.0, recent_count=0):
    """The ending style for this run's developer-story post. Least recently used first (from the post log), never the
    style the last developer-story post used, and no question style while most recent posts were questions (the
    caption would only be rejected for that)."""
    styles = list(STYLE_ORDER)
    if question_share >= QUESTION_SHARE_LIMIT and recent_count >= 4:
        styles = [s for s in styles if not CONNECTION_STYLES[s]["question"]]
    dev_entries = [e for e in post_log or [] if isinstance(e, dict) and e.get("connection_style")]
    last = dev_entries[-1]["connection_style"] if dev_entries else None
    ordered = _least_recent(styles, position, dev_entries, "connection_style")
    fresh = [s for s in ordered if s != last]
    return (fresh or ordered)[0]


# ------------------------------------------------------------------------------------------- writing angles

def _where(media):
    level, mode = media.get("level"), media.get("mode")
    if not level:
        return None
    return f"Level {level}" + (f" on {mode.title()}" if mode in ("easy", "normal", "hard") else "")


def _join(names):
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def angle_slots(media, style, seed=0, count=3):
    """`count` developer subjects {id, focus, instruction, shape}, each different, each anchored on verified facts.
    The shape of every slot is the run's connection style, so the batch differs in subject, not in its ending."""
    media = media or {}
    shown = game_terms.mechanics_shown(media)
    where = _where(media)
    theme = media.get("theme")
    state = media.get("puzzle_state")
    slots = []
    if shown:
        name = shown[seed % len(shown)]
        slots.append({"id": "built_mechanic", "focus": f"the {name}, a mechanic I built",
                      "instruction": (f"In the first person, talk about the {name} as something I built for Ringshift. "
                                      f"You may say what it does ONLY in the words of the knowledge's VERIFIED MECHANIC "
                                      f"BEHAVIOUR line for {name}, as a general fact, never as something happening at a "
                                      "spot in this media. Name no mechanic that is not on screen.")})
    if where:
        state_note = {"solved": "solved here (the BREACHED screen)", "unsolved": "at its starting position here",
                      "in_progress": "not solved yet in this clip"}.get(state)
        slots.append({"id": "level_built", "focus": f"{where}, a level I built",
                      "instruction": (f"In the first person: {where} is one of the levels I hand-author"
                                      + (f"; it is {state_note}" if state_note else "")
                                      + ". Say nothing about how hard it is, how long it took, or how it is solved.")})
    if len(shown) >= 2 or (shown and theme):
        mix = _join(shown) if len(shown) >= 2 else f"the {shown[0]} in the {theme} theme"
        slots.append({"id": "puzzle_interest", "focus": f"what makes this puzzle interesting: {mix}",
                      "instruction": (f"In the first person, say what interests me about this puzzle as its maker: "
                                      f"{mix} on one circular puzzle. An honest opinion is fine; no claim about "
                                      "positions, what a beam does here, difficulty or the number of solutions.")})
    slots.append({"id": "dev_update", "focus": "a small development update",
                  "instruction": ("A small development update in the first person, using ONLY a sentence of the "
                                  "knowledge's DEVELOPER VOICE section (for example that I check every level against the "
                                  "game's own engine before it becomes playable), tied to this media's "
                                  + (where or "level") + ". No dates, no durations, no plans.")})
    if len(slots) > count:
        offset = seed % len(slots)
        slots = (slots[offset:] + slots[:offset])[:count]
    while len(slots) < count:
        slots.append(dict(slots[-1], id=f"{slots[-1]['id']}_{len(slots)}"))
    shape = f"developer voice, first person; ending: {CONNECTION_STYLES[style]['label']}"
    for slot in slots:
        slot["shape"] = shape
    return slots


def brief(style):
    """The developer-voice block for the writer's prompt."""
    return "\n".join([
        "--- DEVELOPER VOICE (this post type) ---",
        "Write as the indie developer who is building Ringshift, in the first person (I / my). Share the mechanic, level "
        "or puzzle shown as something you made, the way a developer posts progress, not as a description of a picture.",
        "Every caption must name a verified detail of THIS media: a mechanic listed under 'Mechanics on screen', the "
        "level number, or the theme.",
        "Only the knowledge's DEVELOPER VOICE sentences and verified facts describe the development. Never invent how "
        "long anything took, attempts or versions, what changed or was cut, what testers, friends or players said or "
        "felt, plans, dates, or anything that happened on a particular day.",
        f"ENDING FOR THIS POST ({CONNECTION_STYLES[style]['label']}): {CONNECTION_STYLES[style]['instruction']}",
        "Use #Ringshift and at most one more hashtag (for example #indiedev).",
        "--- END DEVELOPER VOICE ---",
    ])


# ------------------------------------------------------------------------------------------- screening

_I_RE = re.compile(r"(?:^|[^A-Za-z'’])I(?:['’](?:m|ve|d|ll))?(?![A-Za-z])")
_MY_RE = re.compile(r"\b(?:my|me|mine|myself)\b", re.IGNORECASE)

# Development history, anecdotes and other claims no verified fact makes. Checked on developer-story captions only:
# the other post types never speak as the developer about the development.
DEV_HISTORY_PATTERNS = [
    (r"\b(?:took|takes|taken|spent|spend|spending|cost)\b[^.?!]{0,40}\b(?:hours?|days?|weeks?|months?|years?|nights?|"
     r"ages|forever|weekend|evenings?|all\s+(?:day|night))\b", "says how long something took"),
    (r"\b(?:for|over|after)\s+(?:\d+|a\s+few|several|many|two|three|four|five|six|ten)\s+(?:hours|days|weeks|months|years|nights|tries|attempts)\b",
     "says how long or how many tries"),
    (r"\b(?:last|this|that)\s+(?:night|week|weekend|month|morning|evening|afternoon)\b|\byesterday\b|\btonight\b|\btoday\b",
     "refers to a particular day"),
    (r"\b(?:tomorrow|next\s+(?:week|month|year|update|build|patch)|soon|upcoming|coming\s+(?:up|next))\b",
     "promises something later"),
    (r"\b(?:rewr(?:o|i)te|rewritten|rebuil(?:t|d)|redesign\w*|rework\w*|scrapp\w*|threw\s+(?:it\s+)?out|refactor\w*|"
     r"overhaul\w*|tweak\w*|iterat\w*|prototyp\w*|originally|used\s+to\s+be|first\s+version|old\s+version|"
     r"went\s+through|(?:cut|removed|deleted|changed|replaced|swapped)\s+(?:it|the|a|that|this|my|an?)\b)",
     "describes development history (what changed, was cut or rewritten)"),
    (r"\b(?:almost|nearly)\s+gave\s+up\b|\bgave\s+up\b|\b(?:drove|driving|drives)\s+me\b|\bbroke\s+my\s+brain\b|"
     r"\bkept\s+me\s+up\b|\blost\s+sleep\b|\bheadaches?\b|\bnightmare\b|\bI\s+(?:cried|screamed|panicked)\b|"
     r"\bI\s+(?:was|got)\s+stuck\b|\bI\s+couldn'?t\s+(?:solve|beat|figure|sleep)\b",
     "tells a personal anecdote"),
    (r"\b(?:play)?testers?\b|\bbeta\b|\bmy\s+(?:friends?|wife|husband|partner|kids?|son|daughter|brother|sister|mum|mom|"
     r"dad|family|cat|dog|team)\b|\ba\s+friend\b|\b(?:someone|people|players|everyone|nobody|a\s+player)\s+"
     r"(?:told|asked|said|kept|found|struggled|loved|hated|missed|complained)\b",
     "reports what other people said or did"),
    (r"\b\d+(?:st|nd|rd|th)?\s+(?:attempts?|tries|iterations?|versions?|drafts?|revisions?|prototypes?|playthroughs?|levels?)\b|"
     r"\b(?:hundreds|thousands|dozens)\s+of\b",
     "states a count the verified facts do not give"),
]
_DEV_HISTORY_RES = [(re.compile(p, re.IGNORECASE), why) for p, why in DEV_HISTORY_PATTERNS]

# Engagement bait and stock calls to action: rejected on every post type.
BAIT_PATTERNS = [
    r"\blike\s+(?:and|&|\+)\s+(?:retweet|rt|share|follow)\b", r"\b(?:rt|retweet|repost)\s+if\b", r"\bfollow\s+for\s+more\b",
    r"\bfollow\s+back\b", r"\bf4f\b", r"\bsmash\s+(?:that|the)\b", r"\btag\s+(?:a|your)\s+(?:friend|buddy|mate)\b",
    r"\bcomment\s+below\b", r"\blet\s+me\s+know\s+in\s+the\s+comments\b", r"\bwho'?s\s+with\s+me\b",
    r"\bdon'?t\s+forget\s+to\s+(?:follow|like|share)\b", r"\bdrop\s+a\s+(?:like|follow|comment|❤|heart)\b",
]
_BAIT_RES = [re.compile(p, re.IGNORECASE) for p in BAIT_PATTERNS]

# Endings that ask fellow developers to connect, and endings that invite people to follow the development.
INDIE_AUDIENCE_RE = re.compile(r"\b(?:indie\s*dev(?:eloper)?s?|fellow\s+(?:indie\s+)?(?:dev(?:eloper)?s?|makers?|creators?)|"
                               r"other\s+(?:indie\s+)?(?:dev(?:eloper)?s?|makers?)|game\s*devs?|(?:making|building)\s+a\s+game"
                               r"|#indiedev|#gamedev)\b", re.IGNORECASE)
INVITE_RE = re.compile(r"\?|\b(?:share|show\s+me|tell\s+me|reply|i'?d\s+(?:love|like)\s+to\s+(?:see|hear|know)|"
                       r"(?:love|like)\s+to\s+(?:see|hear))\b", re.IGNORECASE)
FOLLOW_RE = re.compile(r"\b(?:follow(?:ing)?\s+(?:along|the\s+(?:build|journey|progress|development|devlog)|ringshift|me)|"
                       r"i'?(?:ll|m)\s+(?:keep\s+)?(?:shar|post)\w*|(?:sharing|posting)\s+(?:the\s+)?(?:build|progress|"
                       r"development|devlog|ringshift|more|it)\b|as\s+(?:it|the\s+game|ringshift)\s+(?:grows|comes\s+together|"
                       r"takes\s+shape)|devlog|in\s+public)", re.IGNORECASE)
CONNECT_RE = re.compile(r"\blet'?s\s+connect\b|\bconnect\s+with\b", re.IGNORECASE)

_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]*")


def closing_sentence(text):
    """The last sentence of a post, without hashtags, links or mentions ("" when there is none)."""
    body = re.sub(r"https?://\S+|[#@]\w+", " ", str(text or "")).strip()
    sentences = [s.strip() for s in _SENTENCE_RE.findall(body) if caption_quality.normalize(s)]
    return sentences[-1] if sentences else ""


def first_person(text):
    return bool(_I_RE.search(str(text or "")) or _MY_RE.search(str(text or "")))


def history_problems(text):
    problems = []
    for pattern, why in _DEV_HISTORY_RES:
        found = pattern.search(str(text or ""))
        if found:
            problems.append(f"{why} ({found.group(0)!r}); only a verified project fact can say that")
    return problems


def bait_problems(text):
    for pattern in _BAIT_RES:
        found = pattern.search(str(text or ""))
        if found:
            return [f"engagement bait ({found.group(0)!r})"]
    return []


CLOSING_WINDOW = 12        # the newest posts of each source (our own log, Buffer's export) a closing line is compared with


def closing_problems(text, recent):
    """The closing line must not repeat the closing line of a recent post (same words, or the same idea), and a
    stock 'Let's connect' may not come back while a recent post still has it. `recent`: the posts to compare with
    (autopilot passes the newest CLOSING_WINDOW of its own posts and of Buffer's, see autopilot.recent_closings)."""
    closing = closing_sentence(text)
    if not closing:
        return []
    key = caption_quality.normalize(closing)
    for other in list(recent or []):
        other_closing = closing_sentence(other)
        if not other_closing:
            continue
        if caption_quality.normalize(other_closing) == key:
            return [f"ends with the same line as an earlier post ({other_closing!r})"]
        if len(key.split()) >= 4 and caption_quality.same_idea(closing, other_closing)[0]:
            return [f"ends the same way as an earlier post ({other_closing!r})"]
    if CONNECT_RE.search(text) and any(CONNECT_RE.search(str(other)) for other in recent or []):
        return ["uses 'Let's connect' again; a recent post already ended that way"]
    return []


def style_problems(text, media, style):
    """Reasons `text` does not end the way the run's connection style asks."""
    if style not in CONNECTION_STYLES:
        return []
    closing = closing_sentence(text)
    asks = str(text).strip().rstrip("#").count("?") > 0
    if style == "design_question":
        if not closing.endswith("?"):
            return ["ending should be a specific question about the visible design"]
        anchors = caption_quality.anchors_for(media)
        visual = re.search(r"\b(?:eye|read|reads|look|looks|colou?rs?|theme|first|stand\s+out|notice|catch)\w*\b",
                           closing, re.IGNORECASE)
        if not caption_quality.mentions_anchor(closing, anchors) and not visual:
            return ["the closing question is not about anything visible in this media"]
        return []
    if style == "indie_invite":
        if not INDIE_AUDIENCE_RE.search(text) or not INVITE_RE.search(closing):
            return ["ending should invite fellow indie developers to share what they are making"]
        return []
    if style == "maker_note":
        if asks:
            return ["a maker's note ends without a question"]
        if not FOLLOW_RE.search(text):
            return ["a maker's note ends with a natural invitation to follow the development"]
        return []
    if style == "showcase":
        if asks or FOLLOW_RE.search(text) or CONNECT_RE.search(text) or INDIE_AUDIENCE_RE.search(closing):
            return ["a showcase stands on its own: no question, no call to follow or connect"]
        return []
    return []


def specific_anchor_problems(text, media):
    """A developer story must name something verified about THIS media (mechanic, level or theme); the game's general
    identity alone is not enough."""
    anchors = {label: spellings for label, spellings in caption_quality.anchors_for(media).items()
               if not label.endswith("difficulty")}
    if not anchors:
        return ["the media has no verified level, mechanic or theme for a developer story to be about"]
    if not caption_quality.mentions_anchor(text, anchors):
        return ["does not name the level, a mechanic on screen or the theme of this media"]
    return []


def voice_problems(text, media, style=None):
    """Everything a developer-story caption is rejected for, beyond the rules every caption has to pass."""
    problems = []
    if not first_person(text):
        problems.append("not in the developer's first-person voice (I / my)")
    problems.extend(specific_anchor_problems(text, media))
    problems.extend(history_problems(text))
    problems.extend(style_problems(text, media, style))
    return problems


# ------------------------------------------------------------------------------------------- verified-fact captions

TAGS = " #Ringshift #indiedev"


def _theme_tail(media):
    return f" in the {media['theme']} theme" if media.get("theme") else ""


def _bodies(media):
    """Opening sentences built from verified fields only. Most of them start with this media's own level or mechanic
    names, so consecutive developer posts open differently even when they come from the floor."""
    where = _where(media)
    shown = game_terms.mechanics_shown(media)
    state = media.get("puzzle_state")
    tail = _theme_tail(media)
    # The mechanic list names kinds, not counts, so one kind is "the Mirror mechanic", never "one Mirror".
    mix = _join(shown) if len(shown) != 1 else f"the {shown[0]} mechanic"
    bodies = []
    if where and shown:
        bodies.append(f"{where} is one of the levels I hand-author for Ringshift: {mix}{tail}.")
        bodies.append(f"{mix[0].upper() + mix[1:]} on one circular puzzle: {where}, a level I built for Ringshift{tail}.")
        bodies.append(f"The {shown[0]} is part of {where}, a Ringshift level I built{tail}.")
    if where and state == "unsolved":
        bodies.append(f"{where}{tail}, at its starting position. I check every level against the game's own engine "
                      f"before it becomes playable.")
    if where and state == "solved":
        bodies.append(f"{where}{tail}, solved: the BREACHED screen. I check every level against the game's own engine "
                      f"before it becomes playable.")
    if where and shown:
        bodies.append(f"A level I built for Ringshift: {where}, with {mix}{tail}.")
        bodies.append(f"I hand-author Ringshift's Easy, Normal and Hard levels. This is {where}: {mix} on one circular puzzle.")
    if where:
        bodies.append(f"I'm an indie developer building Ringshift. Here is {where}{tail}.")
    elif shown:
        bodies.append(f"I'm an indie developer building Ringshift. Here: {mix}{tail}.")
    return bodies


def _endings(media, style):
    shown = game_terms.mechanics_shown(media)
    theme = media.get("theme")
    if style == "design_question":
        endings = []
        if len(shown) >= 2:
            endings.append(f"Which catches your eye first here, the {shown[0]} or the {shown[-1]}?")
            endings.append(f"Is the {shown[1]} easy to tell apart from the {shown[0]} at this size?")
        if shown and theme:
            endings.append(f"Does the {shown[0]} read clearly in the {theme} theme?")
        if theme:
            endings.append(f"Does the {theme} theme keep the rings easy to read?")
        if shown:
            endings.append(f"Does the {shown[-1]} stand out enough on a phone screen?")
            endings.append(f"Is the {shown[0]} the first thing you notice here?")
            endings.append(f"How does the {shown[0]} read to you at this size?")
        return endings
    if style == "maker_note":
        return ["I'm sharing the build here as Ringshift comes together.",
                "Follow along if you like seeing a puzzle game take shape.",
                "I'll keep sharing levels here as the game grows.",
                "I'm posting Ringshift's development as I go, if you want to follow along.",
                "Follow the build if you like seeing how a puzzle game gets made."]
    if style == "indie_invite":
        return ["Fellow indie devs, I'd love to see what you're building.",
                "If you're making a game too, tell me what you're building.",
                "Indie devs: share what you're making, I'd like to see it.",
                "Fellow devs, reply with a screenshot of your game; I'd like to see it.",
                "Other indie devs: what mechanic are you proudest of in your game?"]
    return [""]


def fact_captions(media, style, seed=0):
    """Developer-voice captions built only from verified media fields and DEVELOPER VOICE sentences of the knowledge
    file, ending in the run's connection style. Candidates only: they are screened and reviewed like any other."""
    media = media or {}
    bodies, endings = _bodies(media), _endings(media, style)
    if not bodies or not endings:
        return []
    captions = []
    for index, body in enumerate(bodies):
        ending = endings[(index + seed) % len(endings)]
        text = (body + (" " + ending if ending else "")).strip() + TAGS
        if text not in captions:
            captions.append(text)
    offset = seed % len(captions)
    return captions[offset:] + captions[:offset]


# ------------------------------------------------------------------------------------------- safe claims

def _knowledge_lines(knowledge):
    text = re.sub(r"\\(?=[#*_\-.])", "", str(knowledge or ""))
    return [line.strip() for line in text.splitlines() if line.strip()]


def _section(knowledge, title):
    lines, inside = [], False
    for line in _knowledge_lines(knowledge):
        if line.startswith("#"):
            inside = line.lstrip("# ").strip().upper() == title
            continue
        if inside:
            lines.append(line.lstrip("-* ").strip())
    return lines


def verified_claims(media, knowledge):
    """Knowledge sentences a developer story may restate, copied as written: the short DEVELOPER VOICE statements and
    the VERIFIED MECHANIC BEHAVIOUR line of each mechanic the media shows. Never composed."""
    shown = game_terms.mechanics_shown(media or {})
    claims = [line for line in _section(knowledge, "DEVELOPER VOICE")
              if len(line) <= 140 and not line.lower().startswith(("a developer post", "endings rotate"))]
    for line in _section(knowledge, "VERIFIED MECHANIC BEHAVIOUR"):
        name = line.split(":", 1)[0].strip()
        if ":" in line and name in shown:
            claims.append(line)
    return claims
