"""Reddit-native draft helper for Ringshift. Offline only: never accesses Reddit or posts anything.

Examples:
  python scripts/reddit_repurpose.py draft --post-id 24 --subreddit IndieGaming
  python scripts/reddit_repurpose.py opportunity --url https://reddit.com/r/example/comments/... --score 1200 --comments 8 --topic-fit 0.9 --age-hours 12

Opportunity inputs are manually supplied aggregate metrics. This tool never fetches Reddit pages or stores Reddit usernames.
"""
import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POST_LOG = ROOT / "data" / "post_log.json"
MANIFEST = ROOT / "media" / "manifest.json"
COMMUNITIES = ROOT / "reddit" / "communities.json"


class RedditDraftError(ValueError):
    pass


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RedditDraftError(f"Cannot read {path}: {exc}") from exc


def load_community(name, path=COMMUNITIES):
    data = _read_json(path)
    key = str(name or "").strip().lower().removeprefix("r/")
    for item in data.get("communities", []):
        if item.get("name", "").lower().removeprefix("r/") == key:
            return item
    raise RedditDraftError(f"r/{key} is not in the reviewed community registry; add it and review its rules first")


def load_source(post_id, post_log=POST_LOG, manifest_path=MANIFEST):
    posts = _read_json(post_log)
    entry = next((p for p in posts if str(p.get("number")) == str(post_id)), None)
    if not entry:
        raise RedditDraftError(f"No X post #{post_id} found in data/post_log.json")
    media_file = entry.get("media_file")
    if not media_file:
        raise RedditDraftError("Source X post has no media_file")
    manifest = _read_json(manifest_path)
    media = next((m for m in manifest.get("items", []) if m.get("path") == media_file), None)
    if not media:
        raise RedditDraftError("Source media is missing from media/manifest.json")
    if media.get("exclude") or media.get("verification", {}).get("status") != "verified":
        raise RedditDraftError("Source media is excluded or lacks verified provenance")
    if media.get("validation", {}).get("status") != "ok":
        raise RedditDraftError("Source media has not passed manifest validation")
    if not media.get("sha256") or not media.get("url"):
        raise RedditDraftError("Source media is missing its SHA-256 or public URL")
    return entry, media


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _title(media, post_type):
    game = media.get("game") or {}
    mode = _clean(game.get("mode")).title()
    level = game.get("level")
    mechanisms = media.get("mechanisms") or []
    mech = ", ".join(_clean(x) for x in mechanisms[:2])
    state = media.get("puzzle_state")
    if post_type == "challenge" or state == "unsolved":
        return f"Could you work out this {mode or 'Ringshift'} puzzle?"
    if post_type == "mechanic" and mech:
        return f"How would you use {mech} in this puzzle?"
    if post_type == "dev_story":
        return "A puzzle mechanism I'm building for Ringshift"
    if level is not None:
        return f"A Ringshift puzzle from {mode or 'the game'} — Level {level}"
    return "A small puzzle-game experiment I'm working on"


def build_draft(post, media, subreddit, community):
    """Build a Reddit-native, disclosure-first draft; status is HOLD until rules are verified."""
    game = media.get("game") or {}
    mechanisms = [_clean(x) for x in media.get("mechanisms") or [] if _clean(x)]
    level = game.get("level")
    mode = _clean(game.get("mode")).title()
    theme = _clean(game.get("theme"))
    state = _clean(media.get("puzzle_state"))
    post_type = _clean(post.get("post_type"))
    description = _clean(media.get("description"))
    visible = []
    if mode:
        visible.append(mode)
    if level is not None:
        visible.append(f"Level {level}")
    if theme:
        visible.append(f"{theme} theme")
    context = " · ".join(visible)

    if state == "unsolved":
        opening = "I made this Ringshift puzzle as a small beam-routing challenge."
        question = "What would you try first?"
    elif post_type == "dev_story":
        opening = "I'm building Ringshift, an indie puzzle game about rotating concentric rings to steer beams toward the locks."
        question = "Which part of the board would you inspect first?"
    elif mechanisms:
        opening = f"I'm working on Ringshift, where this board brings {', '.join(mechanisms[:3])} into the same puzzle."
        question = "Which of these mechanisms would you plan around first?"
    else:
        opening = "I'm making Ringshift, an indie puzzle game built around rotating rings and steering beams."
        question = "What stands out to you in this layout?"

    lines = [opening]
    if context:
        lines.append(f"This capture shows {context}.")
    if mechanisms:
        lines.append("Mechanisms visible: " + ", ".join(mechanisms) + ".")
    if description and len(description) < 240:
        lines.append(description)
    lines.append(question)
    lines.append("Disclosure: I'm the developer of Ringshift.")
    if community.get("self_promo_allowed") is True and community.get("allow_links") is True:
        lines.append("Free browser demo: https://ultramegaray0-lgtm.github.io/ringshift-game/")
    elif community.get("self_promo_allowed") is not True:
        lines.append("[Do not submit until the community's self-promotion rules have been checked.]")

    reviewed = bool(community.get("rules_reviewed")) and community.get("self_promo_allowed") is not None
    status = "READY_FOR_HUMAN_REVIEW" if reviewed else "HOLD_RULES_UNVERIFIED"
    return {
        "source_x_post_number": post.get("number"),
        "source_x_post_type": post_type,
        "source_x_text": post.get("text"),
        "media_file": media.get("path"),
        "media_url": media.get("url"),
        "media_sha256": media.get("sha256"),
        "game_commit": (media.get("capture") or {}).get("game_commit") or (media.get("capture") or {}).get("game_build"),
        "level": level,
        "mode": game.get("mode"),
        "theme": theme or None,
        "mechanisms": mechanisms,
        "target_subreddit": "r/" + _clean(community.get("name")).removeprefix("r/"),
        "title": _title(media, post_type),
        "body": "\n\n".join(lines),
        "rules_url": community.get("rules_url"),
        "rules_reviewed": reviewed,
        "status": status,
        "must_be_submitted_manually": True,
    }


def score_opportunity(score, comments, topic_fit, age_hours):
    """Score a user-supplied post snapshot; this does not fetch Reddit or identify voters."""
    score = max(0, int(score))
    comments = max(0, int(comments))
    topic_fit = min(1.0, max(0.0, float(topic_fit)))
    age_hours = max(0.0, float(age_hours))
    engagement = min(1.0, math.log1p(score) / math.log1p(1000))
    low_comment_signal = 1.0 / (1.0 + comments / 20.0)
    freshness = math.exp(-age_hours / 96.0)
    total = round(100 * (0.45 * engagement + 0.25 * low_comment_signal + 0.20 * topic_fit + 0.10 * freshness))
    return {
        "score": total,
        "inputs": {"upvotes_or_score": score, "comments": comments, "topic_fit": topic_fit, "age_hours": age_hours},
        "method": "Aggregate-only heuristic using manually supplied metrics; not a Reddit API result and not a list of voters.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create approval-first Reddit drafts from Ringshift X posts.")
    sub = parser.add_subparsers(dest="command", required=True)
    draft = sub.add_parser("draft", help="Create a Reddit-native draft from a verified X post.")
    draft.add_argument("--post-id", required=True)
    draft.add_argument("--subreddit", required=True)
    draft.add_argument("--output")
    opportunity = sub.add_parser("opportunity", help="Score manually supplied aggregate Reddit post metrics.")
    opportunity.add_argument("--url", required=True)
    opportunity.add_argument("--score", required=True, type=int)
    opportunity.add_argument("--comments", required=True, type=int)
    opportunity.add_argument("--topic-fit", required=True, type=float)
    opportunity.add_argument("--age-hours", required=True, type=float)
    args = parser.parse_args(argv)

    if args.command == "draft":
        post, media = load_source(args.post_id)
        community = load_community(args.subreddit)
        result = build_draft(post, media, args.subreddit, community)
    else:
        if not re.match(r"^https://(?:www\.)?reddit\.com/r/[A-Za-z0-9_]+/comments/[A-Za-z0-9]+", args.url):
            raise RedditDraftError("Provide a full Reddit post URL (https://www.reddit.com/r/.../comments/...).")
        result = {"url": args.url, **score_opportunity(args.score, args.comments, args.topic_fit, args.age_hours)}

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if getattr(args, "output", None):
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    try:
        main()
    except RedditDraftError as exc:
        raise SystemExit(f"ERROR: {exc}")
