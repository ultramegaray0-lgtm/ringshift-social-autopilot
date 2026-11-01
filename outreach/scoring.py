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
    researched_relevance = bool((candidate.get("research") or {}).get("similar_coverage"))
    if not puzzle and not indie and candidate.get("category") not in core.ROUTE_CATEGORIES and not researched_relevance:
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

    research = candidate.get("research") or {}
    coverage = research.get("similar_coverage") or []
    if coverage:
        points += min(10, 5 * len(coverage))
        reasons.append("already covered similar puzzle games: "
                       + ", ".join(c.get("game") or c.get("title") for c in coverage[:3]))
    if research.get("accepts_submissions"):
        points += 5
        reasons.append("publicly invites developers to send games")

    stats = candidate.get("stats") or {}
    rate = stats.get("engagement_rate")
    if isinstance(rate, (int, float)) and not isinstance(rate, bool):
        share = f"{rate:.0%}" if rate < 10 else f"{rate:.0f}x"
        if rate >= 0.10:
            points += 5
            reasons.append(f"engaged audience (a typical recent video gets views equal to {share} of subscribers)")
        elif rate >= 0.03:
            points += 2
            reasons.append(f"moderate engagement (typical recent video: {share} of subscribers)")
        else:
            reasons.append(f"low engagement (typical recent video: {share} of subscribers)")
    latest = core.parse_time(stats.get("latest_upload"))
    if latest is not None and core.days_between(latest, now) > 90:
        disqualifiers.append(f"inactive: last upload {int(core.days_between(latest, now))} days ago")

    return max(0, min(points, 100)), reasons, disqualifiers


def tier(points, min_score):
    """'high' / 'medium' / 'low' priority for the report. Only medium and above can be drafted."""
    if points >= 75:
        return "high"
    return "medium" if points >= min_score else "low"
