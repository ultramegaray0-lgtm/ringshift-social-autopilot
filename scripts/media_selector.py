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

import content_matching
import game_terms
import media_manifest
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

SOLVED_STILL_RE = re.compile(r"just been solved|BREACHED", re.IGNORECASE)

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


class NoSuitableMediaError(RuntimeError):
    """No media in the library can honestly carry this kind of post. The caller should try another post type
    or report that nothing can be drafted; it must never fall back to media that does not fit."""


def _manifest_records():
    """{path: record} from media/manifest.json; None when there is no manifest file at all (older checkouts, tests).

    A manifest that EXISTS but cannot be read is an error, not a reason to fall back to unchecked media: the
    manifest is what says which media is safe for which post, so a broken one must stop the draft."""
    path = MEDIA_DIR / "manifest.json"
    if not path.exists():
        return None

    data = read_json(path, None)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise NoSuitableMediaError("media/manifest.json exists but is unreadable or has no 'items' list; "
                                   "run 'python scripts/media_manifest.py validate' (nothing is guessed without it)")
    return {item.get("path"): item for item in data["items"] if isinstance(item, dict) and item.get("path")}


MANIFEST_FIELDS = ("puzzle_state", "shows_action", "mechanisms", "theme", "aspect", "post_categories")


def _with_manifest(result, record):
    """Add the structured facts of a manifest record to a selection result."""
    game = record.get("game") or {}
    result.update({key: record.get(key) for key in MANIFEST_FIELDS})
    result.update({
        "manifest_id": record.get("id"),
        "level": game.get("level"),
        "mode": game.get("mode"),
        "verification": (record.get("verification") or {}).get("status"),
        "review": (record.get("review") or {}).get("status"),
        "game_build": (record.get("capture") or {}).get("game_build"),
        "description": str(record.get("description") or result.get("description") or "").strip(),
    })
    return result


def _select_for_post_type(files, tags, records, post_type_id, rng):
    """Choose among the media whose manifest record supports `post_type_id`; raise if none does."""
    history = read_json(HISTORY_FILE, [])
    history = history if isinstance(history, list) else []
    recent = set(history[-RECENT_LIMIT:])

    fitting, reasons = [], {}
    for item in files:
        record = records.get(item["file"])
        if record is None:
            reasons["not in media/manifest.json (run media_manifest.py sync)"] = reasons.get("not in media/manifest.json (run media_manifest.py sync)", 0) + 1
            continue
        reason = content_matching.why_not(record, post_type_id)
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        fitting.append((item, record))

    if not fitting:
        detail = "; ".join(f"{count} x {reason}" for reason, count in sorted(reasons.items(), key=lambda r: -r[1])[:3])
        raise NoSuitableMediaError(f"no media fits post type '{post_type_id}' ({detail or 'library is empty'})")

    available = [pair for pair in fitting if pair[0]["file"] not in recent] or fitting

    uses = {}
    for name in history:
        uses[name] = uses.get(name, 0) + 1
    least = min(uses.get(item["file"], 0) for item, _ in available)
    pool = [pair for pair in available if uses.get(pair[0]["file"], 0) == least]

    chooser = rng.choice if rng is not None else random.choice
    chosen, record = chooser(pool)
    entry = chosen["entry"]

    result = {
        "file": chosen["file"],
        "url": chosen["url"],
        "type": chosen["type"],
        "match": "post-type match",
        "topics": [],
        "reason": f"{len(fitting)} file(s) fit post type '{post_type_id}'; {len(pool)} least-used",
        "tags": sorted(get_tags(tags, chosen["file"])),
        "description": str(entry.get("description") or "").strip(),
        "notes": str(entry.get("notes") or "").strip(),
        "bytes": chosen.get("bytes"),
        "skipped": 0,
    }
    return _with_manifest(result, record)


def select_media(post="", *, require_description=False, require_tags=False, allow_special_mode=True,
                 avoid_solved_stills=False, post_type=None, rng=None):
    """Pick media for a post.

    post_type (an id such as "challenge", or a post-type dict): when the manifest exists, only media whose record
    supports that kind of post is considered (see content_matching.REQUIREMENTS), and NoSuitableMediaError is
    raised if there is none. Without a manifest the older rules below apply.

    require_description / require_tags narrow the pool to media that has verified
    facts (so the post can be written about what it really shows); if nothing in
    the pool qualifies, the filter is dropped rather than failing.

    allow_special_mode=False keeps clips flagged "special_mode" (game modes the knowledge file does
    not describe, usually low visual interest) out of the pool unless nothing else is available."""
    tags = read_json(TAGS_FILE, {})
    if not isinstance(tags, dict):
        tags = {}

    files, skipped = _list_candidates(tags)

    if not files:
        raise RuntimeError("No supported media files found in media/.")

    if post_type is not None:
        records = _manifest_records()
        if records is not None:
            type_id = post_type.get("id") if isinstance(post_type, dict) else str(post_type)
            return _select_for_post_type(files, tags, records, type_id, rng)

    history = read_json(HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []

    recent = set(history[-RECENT_LIMIT:])

    available = [item for item in files if item["file"] not in recent]

    if not available:
        available = files

    if not allow_special_mode:
        ordinary = [item for item in available if not item["entry"].get("special_mode")]
        available = ordinary or available

    if avoid_solved_stills:
        # A question like "can you see the solution?" must not sit on a screenshot that already shows it solved.
        unsolved = [item for item in available
                    if not (item["type"] == "image" and SOLVED_STILL_RE.search(str(item["entry"].get("description") or "")))]
        available = unsolved or available

    if require_description:
        described = [item for item in available if str(item["entry"].get("description") or "").strip()]
        available = described or available

    if require_tags:
        tagged = [item for item in available if get_tags(tags, item["file"])]
        available = tagged or available

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
    """A short factual block about the chosen media, for grounding the post text and the critic.

    The selector's internal topic tags (e.g. "black hole", "satellite") are deliberately NOT shown:
    they are search aliases, not names the game uses, and showing them made the writer use them."""
    kind = "short gameplay video" if media.get("type") == "video" else "gameplay screenshot"
    lines = [f"Attached media: a {kind}."]

    if media.get("description"):
        lines.append(f"What it shows (verified): {media['description']}")
    else:
        lines.append(
            "What it shows: not described. Write only about Ringshift in general terms "
            "and do not say what happens in the media."
        )

    for fact in (content_matching.media_facts(media) if media.get("puzzle_state") else []):
        lines.append(fact)

    shown = game_terms.mechanics_shown(media)

    if shown:
        lines.append("Mechanics on screen (use exactly these names, and no other mechanic names): " + ", ".join(shown) + ".")
    else:
        lines.append("No mechanic is verified for this media: do not name any mechanic.")

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
