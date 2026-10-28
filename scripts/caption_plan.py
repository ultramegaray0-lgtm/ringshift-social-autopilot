"""How captions are planned, retried and diagnosed. Deterministic: no AI, no network.

Scheduled runs #46 and #47 (2026-10-28) failed the same way: every caption was rejected and the run ended. The causes:

* The writer was asked for three "angles" that all had to name the same verified facts (level, mechanics, theme), so
  the three captions converged on one sentence (run #46: two candidates were 62% the same words).
* With nothing more to say, the writer invented positions and movement ("a block sitting right beside the portal",
  "the block shift positions"), which the critic rightly refused.
* There was one round plus one critic rewrite. The rewrite in run #47 ("Level 20 in the Autumn Millstone theme.") was
  true but plain and scored 29/60 against a minimum of 30, and the run simply stopped there, every day.

This module supplies what the pipeline in autopilot.generate_draft uses to do better without lowering any standard:

* angle_slots: each candidate gets a DIFFERENT verified subject (one mechanic / the look / the level and its state /
  the puzzle-solving experience), so the batch is varied by construction.
* verified_claims: the short list of statements a caption may restate (media facts, the mechanics line, the verified
  description, and identity sentences quoted from the knowledge file).
* retry_feedback: what went wrong in the first round, by category, for a second, better-aimed writing round, including
  the near-threshold case (a grounded caption a few points under the minimum).
* fact_captions: captions assembled ONLY from verified fields, as the floor of the second round. They are candidates,
  never winners by default: they go through the same repetition check, post rules and evidence-checked critic.
* diagnose: a plain account of why no caption passed, for the run report. Nothing is published in that case.
"""

import re

import caption_quality
import content_matching
import game_terms

NEAR_MISS_MARGIN = 6     # a critic-rejected caption scoring within this many points of the minimum is a near miss

# Sentence shapes, rotated across the slots so the batch also differs in form, not only in subject.
SHAPES = [
    "a calm, concrete statement (no question mark)",
    "a short line in the developer's own voice; an opinion is fine, an anecdote or a feeling attributed to players is not",
    "a short direct question ONLY if it can be answered by looking at the media; otherwise a statement",
]

_MODES = ("easy", "normal", "hard")


def _where(media):
    level, mode = media.get("level"), media.get("mode")
    if not level:
        return None
    return f"Level {level}" + (f" on {mode.title()}" if mode in _MODES else "")


def _join(names):
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def angle_slots(media, post_type_id=None, seed=0, count=3):
    """`count` distinct writing slots {id, focus, instruction, shape}, each about a different verified subject.

    Only subjects the media's verified facts support are offered; the last slot is always the puzzle-solving
    experience in the knowledge file's own words, which needs nothing about the media beyond what is verified."""
    media = media or {}
    shown = game_terms.mechanics_shown(media)
    theme = media.get("theme")
    where = _where(media)
    state = media.get("puzzle_state")

    specific = []
    if shown:
        name = shown[seed % len(shown)]
        specific.append({
            "id": "one_mechanic", "focus": f"the {name}",
            "instruction": (f"Make the {name} the subject; it is listed as on screen. Name no other mechanic. Do not say "
                            f"where it is, what it is next to, or what it does; react to it or give an opinion instead.")})
    if theme:
        specific.append({
            "id": "look", "focus": f"the {theme} theme",
            "instruction": (f"Make the look the subject: the {theme} visual theme. Visual only (themes never change "
                            "gameplay). Name no mechanic in this one.")})
    if where:
        state_note = {"solved": "it is solved by the end (the BREACHED screen)",
                      "unsolved": "it is not solved in this media", "in_progress": "it stops before it is solved"}.get(state)
        specific.append({
            "id": "level", "focus": where,
            "instruction": (f"Make {where} the subject" + (f", and that {state_note}" if state_note else "")
                            + ". Say nothing about how hard it is or how it is solved.")})

    preferred = {"visual": "look", "observation": "look", "mechanic": "one_mechanic", "solution": "level",
                 "moment": "level", "challenge": "level"}.get(post_type_id)
    specific.sort(key=lambda slot: slot["id"] != preferred)        # stable: the post type's natural subject first

    experience = {
        "id": "experience", "focus": "the puzzle-solving experience",
        "instruction": ("Make the puzzle-solving experience the subject, anchored on the game's verified identity in the "
                        "knowledge's own words (for example: rings and beams around a circular puzzle). No anecdote, no "
                        "claim about this level's difficulty, nothing a player supposedly felt.")}

    slots = specific[:max(count - 1, 0)] + [experience]
    while len(slots) < count:                       # very little is verified: vary the shape of the identity angle
        slots.insert(0, dict(experience, id=f"experience_{len(slots)}"))
    shapes = SHAPES[seed % len(SHAPES):] + SHAPES[:seed % len(SHAPES)]
    for index, slot in enumerate(slots[:count]):
        slot["shape"] = shapes[index % len(shapes)]
    return slots[:count]


# ----------------------------------------------------------------------------------------- verified claims

IDENTITY_SENTENCE_KEYS = ("indie puzzle game", "circular puzzle", "multiple visual themes", "keeping the gameplay intact")


def knowledge_identity_sentences(knowledge):
    """Sentences from the knowledge file that state what the game is, copied as written (markdown escapes removed).
    Never composed: if the knowledge does not contain one, it is not offered."""
    text = re.sub(r"\\(?=[#*_\-.])", "", str(knowledge or ""))
    sentences = []
    for line in text.splitlines():
        line = line.strip().lstrip("#-* ").strip()
        if any(key in line.lower() for key in IDENTITY_SENTENCE_KEYS) and line not in sentences:
            sentences.append(line)
    return sentences


def verified_claims(media, knowledge):
    """The statements a caption may restate, as short lines. Everything here is also in the critic's evidence sources."""
    media = media or {}
    claims = list(content_matching.media_facts(media))
    line = content_matching.mechanics_line(media)
    if line:
        claims.append(line)
    if media.get("description"):
        claims.append(f"What it shows (verified): {media['description']}")
    claims.extend(knowledge_identity_sentences(knowledge))
    return claims


# ----------------------------------------------------------------------------------------- rejection categories

CATEGORY_LABELS = {
    "repeat": "repeated an earlier post or another caption",
    "unsupported_claim": "claimed something the verified facts do not say",
    "evidence_missing": "cited evidence that is not in the knowledge or media facts",
    "low_score": "was below the critic's minimum score",
    "generic": "was generic filler or not about this media",
    "rules": "broke a post rule",
    "critic_no": "the critic said KEEP: NO",
    "not_reviewed": "was not reviewed (critic call limit)",
}


def categorize(reason):
    """The rejection categories in one reason string (several can apply)."""
    text = str(reason or "").lower()
    found = []
    if any(key in text for key in ("repeat", "duplicate", "too similar", "same words as an earlier", "already tried")):
        found.append("repeat")
    if "unsupported claim" in text:
        found.append("unsupported_claim")
    if "was not found in the knowledge" in text or "has no evidence quote" in text:
        found.append("evidence_missing")
    if "below the minimum" in text:
        found.append("low_score")
    if "generic" in text or "does not refer to anything verified" in text:
        found.append("generic")
    if "critic call limit" in text:
        found.append("not_reviewed")
    if "failed post rules" in text and not set(found) & {"repeat", "generic"}:
        found.append("rules")
    if not found and "keep: no" in text:
        found.append("critic_no")
    return found or (["rules"] if text else [])


def attempt_reason(attempt):
    return attempt.get("reason") or "; ".join(attempt.get("reasons") or [])


def near_miss(attempts, minimum, margin=NEAR_MISS_MARGIN):
    """The best critic-reviewed, rejected caption whose score is within `margin` below `minimum`, or None.
    This is the 'true but plain' case: the second round is told to keep it that grounded and give it a hook."""
    reviewed = [a for a in attempts if a.get("outcome") == "critic rejected" and isinstance(a.get("score"), int)]
    close = [a for a in reviewed if minimum - margin <= a["score"] < minimum]
    return max(close, key=lambda a: a["score"]) if close else None


def retry_feedback(attempts, minimum):
    """Text for the second writing round: every caption already tried (do not reuse), why each failed, and guidance
    per failure category. Contains no new facts."""
    tried = [a for a in attempts if a.get("text")]
    if not tried:
        return ""
    lines = ["--- FIRST ROUND (every caption below was rejected; do not reuse its wording, opening or idea) ---"]
    seen = set()
    for attempt in tried:
        categories = categorize(attempt_reason(attempt))
        why = ", ".join(CATEGORY_LABELS[c] for c in categories) or "rejected"
        score = f" (score {attempt['score']}/60)" if isinstance(attempt.get("score"), int) else ""
        key = (attempt["text"], why)
        if key not in seen:
            seen.add(key)
            lines.append(f'- "{attempt["text"]}" -> {why}{score}')
    lines.append("--- END FIRST ROUND ---")

    all_categories = {c for a in tried for c in categorize(attempt_reason(a))}
    guidance = []
    if "repeat" in all_categories:
        guidance.append("Pick subjects and openings that none of the recent posts or first-round captions used.")
    if all_categories & {"unsupported_claim", "evidence_missing"}:
        guidance.append("Say nothing about where anything sits or what it is next to, what moves or shifts, what a beam "
                        "does or hits, or how a mechanic behaves: none of that is verified. Restate only SAFE CLAIMS.")
    if "generic" in all_categories:
        guidance.append("Tie each caption to one verified detail or to the game's verified identity, not to nothing.")
    close = near_miss(attempts, minimum)
    if close:
        guidance.append(f'The closest caption was grounded but too plain (score {close["score"]}/60, minimum {minimum}): '
                        f'"{close["text"]}". Keep it that truthful and give it a hook instead: a contrast, a short '
                        "question answerable from the media, or an opinion. Do not add facts to make it interesting.")
    elif "low_score" in all_categories:
        guidance.append(f"Captions scored below the minimum ({minimum}/60): make each one specific and a little "
                        "curious, still using only SAFE CLAIMS.")
    if guidance:
        lines.append("What to do differently in this round:")
        lines.extend(f"- {g}" for g in guidance)
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------- verified-fact captions

MOVES_RE = re.compile(r"\bin (\d{1,3}) moves\b", re.IGNORECASE)
TAG = " #Ringshift"


def fact_captions(media, seed=0):
    """Captions built ONLY from verified fields of `media` and sentences of the knowledge file, in a few different
    forms. They are the floor of the second round: each one is still screened (repeats, post rules, mechanic names,
    media contradictions) and must pass the evidence-checked critic and its minimum score like any other caption.
    When nothing about the media is verified, only the identity line (quoted from the knowledge file) is offered."""
    media = media or {}
    where = _where(media)
    theme = media.get("theme")
    shown = game_terms.mechanics_shown(media)
    state = media.get("puzzle_state")
    is_video = media.get("type") == "video"
    moves = MOVES_RE.search(str(media.get("description") or ""))
    theme_tail = f", {theme} theme" if theme else ""

    captions = []
    if where and theme:
        captions.append(f"Same rules, different coat of paint: {where} in the {theme} theme. "
                        f"Ringshift's visual themes only change the look.{TAG}")
    if where and is_video and state == "solved" and moves and media.get("shows_action"):
        captions.append(f"{moves.group(1)} moves, then BREACHED. {where}{theme_tail}.{TAG}")
    if where and shown:
        captions.append(f"{_join(shown)} on one circular puzzle. {where}{theme_tail}.{TAG}")
    if where and not is_video and state == "solved":
        captions.append(f"The BREACHED screen on {where}{theme_tail}.{TAG}")
    if where and state in ("unsolved", "in_progress"):
        captions.append(f"{where}, still unsolved here. Rings and beams around a circular puzzle: where would you look first?{TAG}")
    if where:
        captions.append(f"Ringshift in one line: you manipulate rings and beams around a circular puzzle. "
                        f"This one is {where}{theme_tail}.{TAG}")
    else:
        shown_in = f", shown here in the {theme} theme" if theme else ""
        captions.append(f"An indie puzzle game where you manipulate rings and beams around a circular puzzle{shown_in}.{TAG}")

    if captions:
        offset = seed % len(captions)
        captions = captions[offset:] + captions[:offset]
    return captions


# ----------------------------------------------------------------------------------------- diagnosis

def diagnose(attempts, minimum, rounds=1, critic_calls=0):
    """Why no caption passed, as data plus one readable line. Used in the no-winner report and the run summary."""
    counts = {}
    for attempt in attempts:
        for category in categorize(attempt_reason(attempt)):
            counts[category] = counts.get(category, 0) + 1
    reviewed = [a for a in attempts if isinstance(a.get("score"), int)]
    best = max(reviewed, key=lambda a: a["score"]) if reviewed else None
    close = near_miss(attempts, minimum)

    parts = [f"{n} {CATEGORY_LABELS.get(c, c)}" for c, n in sorted(counts.items(), key=lambda item: -item[1])]
    summary = (f"No caption passed after {rounds} writing round(s): {len([a for a in attempts if a.get('text')])} "
               f"caption(s) tried, {critic_calls} critic review(s). " + ("; ".join(parts) + "." if parts else ""))
    if best:
        summary += f" Best critic score {best['score']}/60 (minimum {minimum})."
    summary += " Nothing was published."
    return {
        "summary": summary,
        "rounds": rounds,
        "critic_calls": critic_calls,
        "captions_tried": len([a for a in attempts if a.get("text")]),
        "by_reason": counts,
        "best": ({"text": best.get("text"), "score": best.get("score"), "reasons": best.get("reasons")} if best else None),
        "near_miss": bool(close),
    }


def is_identity_only(text, media):
    """True when a caption's only verified anchor is the game's identity (useful in logs and reports)."""
    anchors = caption_quality.anchors_for(media)
    return not caption_quality.mentions_anchor(text, anchors) and bool(caption_quality.identity_mentions(text))
