"""Choose the image or video that goes with a post, and remember what was used.

Sources of media
- Files under media/ (served to Buffer through raw.githubusercontent.com).
- Entries in media/media_tags.json that carry their own "url" (clips hosted on
  the media-store branch; see scripts/media_store.py). They need no local file.

media/media_tags.json maps a path to either a plain list of topic tags (the
original format, still supported) or an object:

    {
      "tags": ["prism", "mirror"],        # topics the clip really shows
      "description": "What is visible",   # facts only; used to ground the post text
      "type": "video",                     # needed only for hosted entries
      "url": "https://...",               # hosted entries only
      "ref": "main",                       # git ref the file is served from
      "duration": 9.3,                     # seconds (videos)
      "exclude": false,                    # true = never auto-select
      "notes": "free text for humans"
    }
"""

import json
import random
import re
from pathlib import Path
from urllib.parse import quote

from media_tools import MAX_VIDEO_BYTES, MAX_VIDEO_SECONDS

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = BASE_DIR / "media"
TAGS_FILE = MEDIA_DIR / "media_tags.json"
HISTORY_FILE = BASE_DIR / "data" / "media_history.json"

RAW_REPO = (
    "https://raw.githubusercontent.com/"
    "ultramegaray0-lgtm/ringshift-social-autopilot"
)
RAW_BASE = RAW_REPO + "/main"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
RECENT_LIMIT = 5
HISTORY_LIMIT = 100

ALIASES = {
    "black hole": ["black hole", "black holes", "portal", "portals"],
    "prism": ["prism", "prisms"],
    "mirror": ["mirror", "mirrors"],
    "satellite": ["satellite", "satellites", "echo", "echoes"],
    "block": ["block", "blocks", "half-block", "half block"],
    "half-block": ["half-block", "half-blocks", "half block", "half blocks"],
    "amplifier": ["amplifier", "amplifiers"],
    "lock": ["lock", "locks"],
    "coin": ["coin", "coins"],
    "mechanism": ["mechanism", "mechanisms", "gimmick", "gimmicks"],
}


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return fallback


def post_topics(post):
    text = normalize(post)
    found = set()

    for topic, aliases in ALIASES.items():
        for alias in aliases:
            # Whole-word match on the normalized text, so "lock" does not
            # match inside "block" and "coin" does not match "coincidence".
            pattern = r"\b" + re.escape(normalize(alias)) + r"\b"

            if re.search(pattern, text):
                found.add(topic)
                break

    return found


def entry_for(tags, relative):
    """The catalog entry for `relative` as a dict (legacy list/str forms are upgraded)."""
    raw = tags.get(relative, [])

    if isinstance(raw, dict):
        return raw

    if isinstance(raw, str):
        return {"tags": [raw]}

    if isinstance(raw, list):
        return {"tags": raw}

    return {}


def get_tags(tags, relative):
    raw_tags = entry_for(tags, relative).get("tags", [])

    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]

    if not isinstance(raw_tags, list):
        return set()

    return {
        normalize(tag)
        for tag in raw_tags
        if normalize(tag)
    }


def raw_url(relative, ref="main"):
    return f"{RAW_REPO}/{quote(str(ref), safe='/')}/{quote(relative, safe='/')}"


def _kind_of(suffix):
    return "video" if suffix.lower() in VIDEO_EXTENSIONS else "image"


def _list_candidates(tags):
    """Every media item that could be posted, as dicts, with the reason for each exclusion."""
    items = []
    skipped = []

    seen = set()

    for path in sorted(MEDIA_DIR.rglob("*")):
        if not (path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS):
            continue

        relative = path.relative_to(BASE_DIR).as_posix()
        seen.add(relative)
        entry = entry_for(tags, relative)
        kind = _kind_of(path.suffix)
        size = path.stat().st_size

        if entry.get("exclude"):
            skipped.append((relative, "marked exclude in media_tags.json"))
            continue

        if kind == "video":
            if size > MAX_VIDEO_BYTES:
                skipped.append((relative, f"video is {size / 1048576:.1f} MB (limit {MAX_VIDEO_BYTES // 1048576} MB)"))
                continue

            duration = entry.get("duration")
            if isinstance(duration, (int, float)) and duration > MAX_VIDEO_SECONDS:
                skipped.append((relative, f"video is {duration:.0f}s (limit {MAX_VIDEO_SECONDS:.0f}s)"))
                continue

        items.append({
            "file": relative,
            "url": entry.get("url") or raw_url(relative, entry.get("ref") or "main"),
            "type": kind,
            "bytes": size,
            "entry": entry,
        })

    # Hosted entries: clips served from another branch/URL with no local file.
    for relative, raw in tags.items():
        if relative in seen or not isinstance(raw, dict):
            continue

        url = raw.get("url")
        kind = raw.get("type")

        if not url or kind not in ("image", "video") or raw.get("exclude"):
            continue

        items.append({"file": relative, "url": url, "type": kind, "bytes": raw.get("bytes"), "entry": raw})

    return items, skipped


def select_media(post=""):
    tags = read_json(TAGS_FILE, {})
    if not isinstance(tags, dict):
        tags = {}

    files, skipped = _list_candidates(tags)

    if not files:
        raise RuntimeError("No supported media files found in media/.")

    history = read_json(HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []

    recent = set(history[-RECENT_LIMIT:])

    available = [item for item in files if item["file"] not in recent]

    if not available:
        available = files

    topics = post_topics(post)
    matched = []

    for item in available:
        media_tags = get_tags(tags, item["file"])

        if topics.intersection(media_tags):
            matched.append(item)

    if matched:
        pool = matched
        match = "topic match"
        reason = f"{len(matched)} verified tagged file(s) matched"
    else:
        pool = available
        match = "random fallback"

        if not topics:
            reason = "no recognized topic in the post"
        else:
            reason = (
                "no available media has verified matching tags; "
                "tag the media before expecting topic matching"
            )

    # Rotate: among the candidates, prefer the ones used least often recently so
    # the same handful of clips are not recycled while others sit unused.
    uses = {}
    for name in history:
        uses[name] = uses.get(name, 0) + 1

    least = min(uses.get(item["file"], 0) for item in pool)
    pool = [item for item in pool if uses.get(item["file"], 0) == least]
    reason += f"; {len(pool)} least-used"

    chosen = random.choice(pool)
    entry = chosen["entry"]

    return {
        "file": chosen["file"],
        "url": chosen["url"],
        "type": chosen["type"],
        "match": match,
        "topics": sorted(topics),
        "reason": reason,
        "tags": sorted(get_tags(tags, chosen["file"])),
        "description": str(entry.get("description") or "").strip(),
        "notes": str(entry.get("notes") or "").strip(),
        "bytes": chosen.get("bytes"),
        "skipped": len(skipped),
    }


def describe_media(media):
    """One short paragraph about the chosen media, for grounding the post text."""
    kind = "short gameplay video" if media.get("type") == "video" else "gameplay screenshot"
    lines = [f"Attached media: a {kind}."]

    if media.get("description"):
        lines.append(f"What it shows (verified): {media['description']}")
    else:
        lines.append(
            "What it shows: not described. Write only about Ringshift in general terms "
            "and do not say what happens in the media."
        )

    if media.get("tags"):
        lines.append("Mechanics on screen: " + ", ".join(media["tags"]) + ".")

    return " ".join(lines)


def record_media(media):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

    history = read_json(HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []

    history.append(media["file"])

    HISTORY_FILE.write_text(
        json.dumps(history[-HISTORY_LIMIT:], indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    import sys

    result = select_media(" ".join(sys.argv[1:]))

    print("Selected media:")
    print("File:", result["file"])
    print("Type:", result["type"])
    print("Match:", result["match"])
    print("Topics:", result["topics"])
    print("Reason:", result["reason"])
    print("URL:", result["url"])
    print("Description:", result["description"] or "(none)")
