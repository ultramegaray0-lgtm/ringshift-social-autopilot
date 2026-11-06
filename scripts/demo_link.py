"""Put the demo link on a steady share of X posts.

Why: 47 X posts produced 1,268 measured impressions and 0 link clicks (data/report.md, 2026-11-06). Until
data/project.json had a project_url, no post was allowed to carry a link at all; and once it does, only the "invite"
post type asks for one, which the rotation reaches roughly once a month. A post without a link cannot send anyone
to the demo, so this module appends a short, true call to action with the configured project URL to every Nth post
(data/project.json "link_every_n_posts", default 2).

Rules it keeps:
* The only link it ever adds is project["project_url"] (the same one screen_candidate allows).
* It never adds a second link, never touches a post that already has one (e.g. an invite post).
* The link goes before the trailing hashtag block, so the caption still ends with its tags.
* It never pushes the caption past X's 280 weighted characters (a URL counts 23); if it doesn't fit, the post goes
  out without it.
* Every phrase is a true statement from RINGSHIFT_KNOWLEDGE.md: the demo is free and plays in the browser.
* Like hashtags.complete, it runs inside consider() BEFORE screening, so the caption that is screened, critiqued and
  queued is exactly the one that goes out.

Kill switch: repository variable RINGSHIFT_DEMO_LINK=off. The offline test harness turns it off by default because
many pipeline tests pin exact caption text; tests/test_demo_link.py turns it on.
"""
import os
import re

import post_rules

ENABLED = os.environ.get("RINGSHIFT_DEMO_LINK", "on").strip().lower() != "off"
DEFAULT_EVERY = 2

CALLS_TO_ACTION = [
    "Free demo, plays in your browser: {url}",
    "Play the free demo: {url}",
    "The demo is free in the browser: {url}",
]

_TRAILING_TAGS_RE = re.compile(r"(\s+#\w+)+\s*$")


def every_n(project):
    try:
        n = int((project or {}).get("link_every_n_posts") or DEFAULT_EVERY)
    except (TypeError, ValueError):
        n = DEFAULT_EVERY
    return max(1, n)


def is_due(project, counter):
    """True when the post after `counter` successful posts should carry the link."""
    return bool((project or {}).get("project_url")) and int(counter) % every_n(project) == 0


def append(text, project, counter, *, enabled=None):
    """`text` with the demo link added when it is due and fits; otherwise `text` unchanged."""
    enabled = ENABLED if enabled is None else enabled
    url = (project or {}).get("project_url")
    text = str(text).rstrip()
    if not enabled or not url or not is_due(project, counter):
        return text
    if post_rules.URL_RE.search(text):
        return text
    phrase = CALLS_TO_ACTION[int(counter) % len(CALLS_TO_ACTION)].format(url=url)
    match = _TRAILING_TAGS_RE.search(text)
    body, tags = (text[:match.start()].rstrip(), match.group(0).strip()) if match else (text, "")
    out = f"{body}\n\n{phrase}" + (f"\n\n{tags}" if tags else "")
    if post_rules.weighted_length(out) > post_rules.X_MAX_WEIGHTED_LENGTH:
        return text
    return out
