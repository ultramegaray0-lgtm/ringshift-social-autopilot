"""Deterministic hashtag top-up for X captions.

The writer prompt asks for 3-5 relevant hashtags, but nothing enforced a minimum and the developer-voice prompt
capped them at two, so every post from Oct 31 to Nov 3 went out with only #Ringshift + one more. Hashtags are how
X shows a small account's post to people who don't follow it yet, so this tops each caption up to TARGET tags from a
short list of tags that genuinely describe Ringshift posts. It never removes or reorders the writer's own tags, never
adds a tag already present (case-insensitive), never exceeds post_rules.MAX_HASHTAGS, and never pushes the caption
past X's 280 weighted characters (it simply adds fewer). Nothing here calls the network or an AI model.

Every topped-up caption still goes through the same screening, rules and critic as before.
"""
import os
from datetime import datetime, timezone, timedelta

import post_rules

# On in production. Repository variable RINGSHIFT_HASHTAG_TOPUP=off turns it off without a code change; the offline
# test harness turns it off by default because many pipeline tests pin exact caption text (tests/test_hashtags.py
# turns it back on to prove the wiring).
ENABLED = os.environ.get("RINGSHIFT_HASHTAG_TOPUP", "on").strip().lower() != "off"

TARGET = 3                       # #Ringshift + at least two discovery tags
BRAND = "#Ringshift"
RIYADH = timezone(timedelta(hours=3))

# Tags that describe every Ringshift post truthfully (it is an indie puzzle game, in development, playable on the
# web). Ordered by how well each one reaches puzzle players and indie-game followers; rotated by `seed` so
# consecutive posts don't carry an identical tail.
PLAYER_TAGS = ["#puzzlegame", "#indiegame", "#puzzle", "#webgames", "#brainteaser"]
DEVELOPER_TAGS = ["#indiedev", "#gamedev", "#puzzlegame", "#indiegame", "#solodev"]

# Weekly community tags with large audiences. Only used on their day (Riyadh time) and only when the post carries
# real gameplay media, which is what those threads are for.
DAY_TAGS = {5: "#ScreenshotSaturday", 2: "#WIPWednesday"}     # Monday == 0


def _present(text):
    return {tag.lower() for tag in post_rules.HASHTAG_RE.findall(str(text))}


def pool(developer_led, *, now=None, has_media=True, seed=0):
    """The tags to try, in order: today's community tag first (when it applies), then the rotated base list."""
    base = DEVELOPER_TAGS if developer_led else PLAYER_TAGS
    shift = seed % len(base)
    ordered = base[shift:] + base[:shift]
    now = (now or datetime.now(timezone.utc)).astimezone(RIYADH)
    day = DAY_TAGS.get(now.weekday())
    if day and has_media and (day != "#WIPWednesday" or developer_led):
        ordered = [day] + ordered
    return ordered


def complete(text, *, developer_led=False, now=None, has_media=True, seed=0, target=TARGET):
    """`text` with hashtags appended until it carries `target` of them. Returns the text unchanged when it already
    has enough, or when no tag fits within X's length limit."""
    if not ENABLED:
        return text
    text = str(text).rstrip()
    present = _present(text)
    if len(present) >= target:
        return text
    wanted = ([] if BRAND.lower() in present else [BRAND]) + pool(developer_led, now=now, has_media=has_media, seed=seed)
    out = text
    for tag in wanted:
        if len(_present(out)) >= min(target, post_rules.MAX_HASHTAGS):
            break
        if tag.lower() in _present(out):
            continue
        trial = f"{out} {tag}"
        if post_rules.weighted_length(trial) <= post_rules.X_MAX_WEIGHTED_LENGTH:
            out = trial
    return out
