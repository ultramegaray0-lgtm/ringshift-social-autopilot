"""Relevance score (0-100) with human-readable reasons. Relevance and recency count far more than audience size."""

import re

from . import core


def hits(text, words):
    text = f" {str(text or '').lower()} "
    return sorted({w for w in words if re.search(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])", text)})


def score(candidate, keywords, now, *, max_age_days):
    """candidate: evidence {title, excerpt, date}, channel_text, subscribers (int|None), videos (int|None).

    Returns (score, reasons, disqualifiers). A disqualifier means "never draft", whatever the score."""
    evidence = candidate["evidence"]
    text = f"{evidence.get('title', '')} {evidence.get('excerpt', '')}"
    reasons, disqualifiers = [], []
    points = 0

    excluded = hits(f"{text} {candidate.get('channel_text', '')}", keywords["exclude"])
    if excluded:
        disqualifiers.append(f"off-topic or unsuitable words: {', '.join(excluded)}")

    puzzle = hits(text, keywords["puzzle"])
    indie = hits(text, keywords["indie"])
    if puzzle:
        points += 30 + min(10, 5 * (len(puzzle) - 1))
        reasons.append(f"evidence mentions {', '.join(puzzle)}")
    if indie:
        points += 15
        reasons.append(f"evidence mentions {', '.join(indie)}")
    if not puzzle and not indie and candidate.get("category") not in core.ROUTE_CATEGORIES:
        disqualifiers.append("evidence is not about puzzle or indie games")

    focus = hits(candidate.get("channel_text", ""), keywords["puzzle"] + keywords["indie"])
    if focus:
        points += 10
        reasons.append("their own description mentions " + ", ".join(focus[:4]))

    when = core.parse_time(evidence.get("date"))
    if when is None:
        if candidate.get("category") in core.ROUTE_CATEGORIES:
            points += 15
            reasons.append("open submission route (checked this run)")
        else:
            disqualifiers.append("evidence has no date")
    else:
        age = core.days_between(when, now)
        if age < -2:
            disqualifiers.append("evidence date is in the future")
        elif age <= 30:
            points += 25
            reasons.append(f"evidence is recent ({int(max(age, 0))} days old)")
        elif age <= 90:
            points += 15
            reasons.append(f"evidence is {int(age)} days old")
        elif age <= max_age_days:
            points += 5
            reasons.append(f"evidence is {int(age)} days old")
        else:
            disqualifiers.append(f"evidence is {int(age)} days old (limit {max_age_days})")

    bonus = int(candidate.get("seed_bonus") or 0)
    if bonus:
        points += bonus
        reasons.append("you listed this source in sources.json")

    subs = candidate.get("subscribers")
    if candidate.get("category") == "youtube":
        if subs is None:
            points += 3
            reasons.append("subscriber count hidden")
        elif 1000 <= subs <= 100000:
            points += 10
            reasons.append(f"small-to-mid channel ({subs:,} subscribers)")
        elif subs < 1000:
            points += 5
            reasons.append(f"small channel ({subs:,} subscribers)")
        elif subs <= 500000:
            points += 3
            reasons.append(f"larger channel ({subs:,} subscribers)")
        else:
            reasons.append(f"very large channel ({subs:,} subscribers), unlikely to reply")
        videos = candidate.get("videos")
        if isinstance(videos, int) and videos >= 10:
            points += 5
            reasons.append(f"active channel ({videos} videos)")
        recent = candidate.get("recent_uploads")
        if isinstance(recent, int) and recent >= 3:
            points += 5
            reasons.append(f"uploads regularly ({recent} videos in the last 90 days)")

    return min(points, 100), reasons, disqualifiers
