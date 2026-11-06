"""Campaign links: one tagged demo URL per channel and post, so every visit can be attributed.

    https://ringshift.itch.io/ringshift?utm_source=x&utm_medium=social&utm_campaign=autopilot&utm_content=post26

What each layer can measure (and what it cannot):
* Buffer counts clicks on every X link it posts, per post (data/metrics.json, "Clicks"). That works with or
  without UTM tags and is the most reliable click count we have.
* itch.io's own dashboard shows referrers by site (t.co, reddit.com, ...), not UTM tags.
* UTM tags become per-channel, per-post visit counts only in Google Analytics 4, which itch.io supports on a
  project page (itch user settings -> Analytics for the whole account, or per project in the dashboard's Interact section) with a free
  measurement ID. Until the owner adds one, the tags are harmless: itch ignores unknown query parameters.

Rules: tags carry only campaign facts (channel, placement, angle id, post number), never personal data. Values are
lower-case ASCII slugs. The base URL is always data/project.json "project_url".
"""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content")
_SLUG_RE = re.compile(r"[^a-z0-9_.-]+")

# source -> medium. Every channel the campaign uses has exactly one entry, so reports group consistently.
CHANNELS = {
    "x": "social",
    "bluesky": "social",
    "youtube": "video",
    "reddit": "community",
    "itch_community": "community",
    "itch_devlog": "community",
    "hackernews": "community",
    "newgrounds": "portal",
    "gamejolt": "portal",
    "email": "outreach",
    "press": "outreach",
}


def slug(value):
    value = _SLUG_RE.sub("-", str(value).strip().lower()).strip("-")
    if not value:
        raise ValueError("empty tracking value")
    return value[:60]


def base_url(url):
    """`url` without query string or fragment (the page itself)."""
    parts = urlsplit(str(url).strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")).rstrip("/")


def tagged(base, source, campaign, content=None):
    """`base` with utm_source/medium/campaign[/content]. Unknown sources are refused so reports stay consistent."""
    source = slug(source)
    if source not in CHANNELS:
        raise ValueError(f"unknown tracking source {source!r}; add it to tracking.CHANNELS")
    params = [("utm_source", source), ("utm_medium", CHANNELS[source]), ("utm_campaign", slug(campaign))]
    if content is not None:
        params.append(("utm_content", slug(content)))
    return f"{base_url(base)}?{urlencode(params)}"


def tags(url):
    """The utm_* values of `url` as a dict (empty when untagged)."""
    return {k: v for k, v in parse_qsl(urlsplit(str(url)).query) if k in UTM_KEYS}


def is_project_link(url, project_url):
    """True when `url` is the project page, tagged or not, and carries no query parameters other than utm_*."""
    if not project_url:
        return False
    url = str(url).rstrip(".,!?)")
    if base_url(url).lower() != base_url(project_url).lower():
        return False
    return all(k in UTM_KEYS for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True))
