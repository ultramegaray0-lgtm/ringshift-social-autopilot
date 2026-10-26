
import json
import random
import re
from pathlib import Path
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = BASE_DIR / "media"
TAGS_FILE = MEDIA_DIR / "media_tags.json"
HISTORY_FILE = BASE_DIR / "data" / "media_history.json"

RAW_BASE = (
    "https://raw.githubusercontent.com/"
    "ultramegaray0-lgtm/ringshift-social-autopilot/main"
)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
RECENT_LIMIT = 5

ALIASES = {
    "black hole": ["black hole", "black holes", "portal", "portals"],
    "prism": ["prism", "prisms"],
    "mirror": ["mirror", "mirrors"],
    "satellite": ["satellite", "satellites", "echo", "echoes"],
    "block": ["block", "blocks", "half-block", "half block"],
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


def get_tags(tags, relative):
    raw_tags = tags.get(relative, [])

    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]

    if not isinstance(raw_tags, list):
        return set()

    return {
        normalize(tag)
        for tag in raw_tags
        if normalize(tag)
    }


def select_media(post=""):
    files = sorted(
        path
        for path in MEDIA_DIR.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not files:
        raise RuntimeError("No supported media files found in media/.")

    tags = read_json(TAGS_FILE, {})
    if not isinstance(tags, dict):
        tags = {}

    history = read_json(HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []

    recent = set(history[-RECENT_LIMIT:])

    available = [
        path for path in files
        if path.relative_to(BASE_DIR).as_posix() not in recent
    ]

    if not available:
        available = files

    topics = post_topics(post)
    matched = []

    for path in available:
        relative = path.relative_to(BASE_DIR).as_posix()
        media_tags = get_tags(tags, relative)

        if topics.intersection(media_tags):
            matched.append(path)

    if matched:
        chosen = random.choice(matched)
        match = "topic match"
        reason = f"{len(matched)} verified tagged file(s) matched"
    else:
        chosen = random.choice(available)
        match = "random fallback"

        if not topics:
            reason = "no recognized topic in the post"
        else:
            reason = (
                "no available media has verified matching tags; "
                "tag the media before expecting topic matching"
            )

    relative = chosen.relative_to(BASE_DIR).as_posix()

    return {
        "file": relative,
        "url": RAW_BASE + "/" + quote(relative, safe="/"),
        "type": (
            "video"
            if chosen.suffix.lower() in VIDEO_EXTENSIONS
            else "image"
        ),
        "match": match,
        "topics": sorted(topics),
        "reason": reason,
    }


def record_media(media):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

    history = read_json(HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []

    history.append(media["file"])

    HISTORY_FILE.write_text(
        json.dumps(history[-100:], indent=2),
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
