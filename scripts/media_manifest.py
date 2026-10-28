"""Structured media manifest (media/manifest.json) and its validator.

media/media_tags.json stays exactly as it was (search tags + a free-text description). The manifest is an
additive, structured layer on top of it: one record per media file with fields the autopilot can rely on
(level, mode, mechanisms, puzzle state, whether a clip shows an action, provenance, validation result).

Evidence rules
* A field is only called "verified" when it was read from the game itself by scripts/capture_gameplay.py.
* Facts taken from a hand-written description are marked "reviewed", never "verified".
* Anything that cannot be established stays "unknown". It is never guessed, and it never matches a post type.
* Nothing from media_tags.json is discarded: every migrated record keeps the original entry under "legacy".

Commands
    python scripts/media_manifest.py sync       # add/refresh records from media_tags.json and the files on disk
    python scripts/media_manifest.py validate   # check the manifest against the files (exit 1 on errors)
    python scripts/media_manifest.py report     # counts by type / state / category
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import game_terms
import media_tools as tools

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = BASE_DIR / "media"
TAGS_FILE = MEDIA_DIR / "media_tags.json"
MANIFEST_FILE = MEDIA_DIR / "manifest.json"

SCHEMA = "ringshift-media-manifest/1"
HASH_HEX_LENGTH = 64            # perceptual hashes are 256-bit (see dhash)
RAW_REPO = "https://raw.githubusercontent.com/ultramegaray0-lgtm/ringshift-social-autopilot"

MEDIA_TYPES = ("image", "video")
PUZZLE_STATES = ("unsolved", "in_progress", "solved", "failed", "unknown")
MODES = ("easy", "normal", "hard", "special", "unknown")
ASPECT_NAMES = ("9:16", "4:5", "1:1", "16:9", "other")
CATEGORIES = ("puzzle_challenge", "solution_reveal", "mechanism_demo", "gameplay_moment", "visual_showcase")
VERIFICATION_STATUSES = ("verified", "reviewed", "unverified")
REVIEW_STATUSES = ("unreviewed", "approved", "rejected")
VALIDATION_STATUSES = ("ok", "failed", "unchecked")
FIELD_SOURCES = ("game-data", "capture-tool", "description", "filename", "file-probe", "unknown")

_ASPECT_RATIOS = {"9:16": 9 / 16, "4:5": 4 / 5, "1:1": 1.0, "16:9": 16 / 9}
_MEDIA_EXTENSIONS = tools.VIDEO_EXTENSIONS | tools.IMAGE_EXTENSIONS

# Folders under media/ that are NOT Ringshift media for X: media/funny/ holds other people's funny clips for the
# TikTok videos and media/tiktok-output/ holds finished TikTok videos. They must never be indexed, selected or posted
# by the X autopilot (a funny clip is not gameplay), so every scan of media/ skips them.
NOT_X_MEDIA_DIRS = ("media/funny/", "media/tiktok-output/")


def is_x_media_path(relative):
    """False for files in the TikTok-only folders (see NOT_X_MEDIA_DIRS)."""
    relative = str(relative).replace("\\", "/")
    return not any(relative.startswith(prefix) for prefix in NOT_X_MEDIA_DIRS)

SOLVED_RE = re.compile(r"BREACHED|just been solved|level \d+ solved", re.IGNORECASE)
UNSOLVED_RE = re.compile(r"no locks (?:lit|solved) yet|starting position", re.IGNORECASE)
NOT_FINISHED_RE = re.compile(r"does not reach a completion screen", re.IGNORECASE)
FAILED_RE = re.compile(r"LOCK DESTROYED|a failed run", re.IGNORECASE)
ACTION_RE = re.compile(r"\b(solving|rotating rings|solution is entered|real in-game play)\b", re.IGNORECASE)
LEVEL_RE = re.compile(r"\bLevel (\d+)\b")
MODE_IN_TEXT_RE = re.compile(r"\((Easy|Normal|Hard)\)")
FILENAME_MODE_RE = re.compile(r"(?<![a-z])(easy|normal|hard)(\d+)(?!\d)", re.IGNORECASE)
THEME_RE = re.compile(r"in the '([a-z0-9 -]+)' visual theme|in the ([a-z]+) theme", re.IGNORECASE)
CAPTURE_NAME_RE = re.compile(r"^rs_(easy|normal|hard)_L(\d+)_.*?_(clip|puzzle|solved|teaser)_(\d{8})", re.IGNORECASE)


# ---------------------------------------------------------------- small helpers

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def raw_url(relative, ref="main"):
    return f"{RAW_REPO}/{quote(str(ref), safe='/')}/{quote(relative, safe='/')}"


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def make_id(relative):
    return slug(Path(relative).stem)


def aspect_name(width, height):
    if not width or not height:
        return "other"
    ratio = width / height
    for name, target in _ASPECT_RATIOS.items():
        if abs(ratio - target) <= 0.02:
            return name
    return "other"


def kind_of(path):
    suffix = Path(path).suffix.lower()
    if suffix in tools.VIDEO_EXTENSIONS:
        return "video"
    if suffix in tools.IMAGE_EXTENSIONS:
        return "image"
    return None


def dhash(path, at=0.0):
    """256-bit difference hash of one frame as a 64-character hex string, or None if undecodable.
    Two frames of the same picture differ in only a few bits; different puzzles differ in many more
    (a coarser 64-bit hash was tried and rejected: different levels came out within 3-4 bits of each other)."""
    try:
        data = tools.frame_gray(path, at, size=17)
    except tools.MediaToolError:
        return None        # no FFmpeg on this machine: no fingerprint (duplicate detection is simply unavailable)
    if not data:
        return None
    bits = 0
    for row in range(16):
        for col in range(16):
            bits = (bits << 1) | (1 if data[row * 17 + col] > data[row * 17 + col + 1] else 0)
    return f"{bits:064x}"


def hamming(hash_a, hash_b):
    try:
        return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")
    except (TypeError, ValueError):
        return 10 ** 6


def fingerprints(path, kind, duration=0.0):
    """Perceptual hashes used to spot near-identical captures: one for an image, three for a video."""
    times = [0.0] if kind == "image" else [max(duration * f, 0.0) for f in (0.2, 0.5, 0.8)]
    hashes = [dhash(path, t) for t in times]
    return [h for h in hashes if h]


def looks_duplicate(hashes_a, hashes_b, max_fraction=0.06):
    """True when two fingerprint lists have the same length and every pair differs in at most `max_fraction`
    of its bits (default 6%, about 15 of 256)."""
    if not hashes_a or len(hashes_a) != len(hashes_b):
        return False
    return all(hamming(a, b) <= max_fraction * 4 * len(a) for a, b in zip(hashes_a, hashes_b))


# ------------------------------------------------------------------- categories

def categories_for(item):
    """Which kinds of post this media can honestly support. A pure function of the record, so it can
    always be recomputed and checked. Media that failed validation, is excluded, belongs to a special mode
    the knowledge file does not describe, or has no description supports nothing."""
    game = item.get("game") or {}
    validation = (item.get("validation") or {}).get("status")

    if (item.get("exclude") or game.get("special_mode") or validation == "failed"
            or not str(item.get("description") or "").strip()
            or (item.get("review") or {}).get("status") == "rejected"):
        return []

    state = item.get("puzzle_state", "unknown")
    is_video = item.get("type") == "video"
    action = bool(item.get("shows_action"))
    categories = []

    # Never offer a challenge on media that already shows the answer, and never on media of unknown state.
    if state in ("unsolved", "in_progress") and (not is_video or action):
        categories.append("puzzle_challenge")

    if state == "solved" and (not is_video or action):
        categories.append("solution_reveal")

    if is_video and action and state != "failed":
        categories.append("gameplay_moment")

    if item.get("mechanisms"):
        categories.append("mechanism_demo")

    if state != "failed":
        categories.append("visual_showcase")

    return categories


# ------------------------------------------------------------------ record building

def probe_file(path):
    """Technical facts read from the file itself."""
    info = tools.probe(path)
    kind = kind_of(path)
    return {
        "width": info["width"], "height": info["height"],
        "aspect": aspect_name(info["width"], info["height"]),
        "bytes": info["bytes"],
        "duration": round(info["duration"], 1) if kind == "video" else None,
        "sha256": sha256_file(path),
    }


def check_decodes(path):
    """('ok'|'failed', [problems]) using the same validator that gates uploads."""
    try:
        problems = tools.validate_media(path)
    except Exception as error:  # a corrupt file must be reported, not crash the run
        problems = [f"validator error: {type(error).__name__}: {error}"]
    return ("failed" if problems else "ok"), problems


def make_item(relative, *, base_dir=BASE_DIR, media_type=None, description="", source="unknown",
              captured_at=None, game_build=None, mode="unknown", level=None, scene=None, theme=None,
              special_mode=False, mechanisms=None, mechanisms_source="unknown", puzzle_state="unknown",
              puzzle_state_source="unknown", shows_action=False, tags=None, notes=None, exclude=False,
              verification="unverified", sources=None, legacy=None, url=None, url_ref="main",
              technical=None, validation=None, hashes=None, review="unreviewed", method=None):
    """Build one manifest record. `technical` / `validation` / `hashes` are filled from the file when it exists."""
    path = Path(base_dir) / relative
    exists = path.is_file()
    kind = media_type or kind_of(relative) or "image"
    technical = dict(technical or {})

    if exists and not technical:
        try:
            technical = probe_file(path)
        except tools.MediaToolError as error:
            validation = validation or {"status": "failed", "checked_at": utc_now(), "problems": [str(error)]}

    if exists and validation is None:
        status, problems = check_decodes(path)
        validation = {"status": status, "checked_at": utc_now(), "problems": problems}

    if not exists and validation is None:
        validation = {"status": "unchecked", "checked_at": None, "problems": []}

    if exists and hashes is None and validation["status"] == "ok":
        hashes = fingerprints(path, kind, technical.get("duration") or 0.0)

    mechanisms = list(mechanisms if mechanisms is not None else game_terms.mechanics_mentioned(description))

    item = {
        "id": make_id(relative),
        "path": relative,
        "url": url or raw_url(relative, url_ref),
        "url_ref": url_ref,
        "url_verified": None,
        "hosted_only": not exists and bool(url),
        "type": kind,
        "width": technical.get("width"),
        "height": technical.get("height"),
        "aspect": technical.get("aspect") or "other",
        "duration": technical.get("duration"),
        "bytes": technical.get("bytes"),
        "sha256": technical.get("sha256"),
        "perceptual_hashes": hashes or [],
        "capture": {"source": source, "captured_at": captured_at, "game_build": game_build, "method": method},
        "game": {"mode": mode, "level": level, "scene": scene, "theme": theme, "special_mode": bool(special_mode)},
        "mechanisms": mechanisms,
        "puzzle_state": puzzle_state,
        "shows_action": bool(shows_action),
        "description": str(description or "").strip(),
        "selector_tags": list(tags or []),
        "notes": notes or None,
        "exclude": bool(exclude),
        "verification": {
            "status": verification,
            "fields": {"mechanisms": mechanisms_source, "puzzle_state": puzzle_state_source, **(sources or {})},
        },
        "validation": validation,
        "review": {"status": review},
        "legacy": legacy,
    }
    item["post_categories"] = categories_for(item)
    return item


# ------------------------------------------------------------- migration from media_tags.json

def _entry(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {"tags": raw}
    if isinstance(raw, str):
        return {"tags": [raw]}
    return {}


def describe_state(description, kind):
    """(puzzle_state, shows_action) read ONLY from the wording of a verified description. Unknown stays unknown."""
    text = str(description or "")

    if FAILED_RE.search(text):
        return "failed", kind == "video"
    if NOT_FINISHED_RE.search(text):
        return "in_progress", kind == "video"
    if SOLVED_RE.search(text):
        return "solved", kind == "video" and bool(ACTION_RE.search(text))
    if UNSOLVED_RE.search(text):
        return "unsolved", False
    return "unknown", kind == "video" and bool(ACTION_RE.search(text))


def migrate_entry(relative, raw, *, base_dir=BASE_DIR, validate=True):
    """A manifest record for one media_tags.json entry (list, string or object form)."""
    entry = _entry(raw)
    path = Path(base_dir) / relative
    kind = entry.get("type") or kind_of(relative) or "image"
    description = str(entry.get("description") or "").strip()
    source = entry.get("source") or "legacy"

    mode, level, scene, theme = "unknown", None, entry.get("scene"), entry.get("theme")
    sources = {}
    state, action = describe_state(description, kind)
    state_source = "description" if state != "unknown" else "unknown"

    if source == "capture_gameplay.py":
        name = CAPTURE_NAME_RE.search(Path(relative).name)
        if scene and ":" in scene:
            mode, number = scene.split(":", 1)
            level = int(number) if number.isdigit() else None
        elif name:
            mode, level = name.group(1).lower(), int(name.group(2))
        if name:
            part = name.group(3).lower()
            state = {"puzzle": "unsolved", "solved": "solved", "clip": "solved", "teaser": "in_progress"}[part]
            action = part in ("clip", "teaser")
            state_source = "capture-tool"
        mechanisms_source, status = "game-data", "verified"
        sources["mode"] = sources["level"] = "game-data"
    else:
        number = LEVEL_RE.search(description)
        level = int(number.group(1)) if number else None
        in_text = MODE_IN_TEXT_RE.search(description)
        if in_text:
            mode = in_text.group(1).lower()
            sources["mode"] = "description"
        else:
            from_name = FILENAME_MODE_RE.search(Path(relative).stem)
            # A number in a file name is only trusted when it agrees with the level the description names.
            if from_name and level is not None and int(from_name.group(2)) == level:
                mode = from_name.group(1).lower()
                sources["mode"] = "filename"
        if level is not None:
            sources["level"] = "description"
        mechanisms_source = "description" if game_terms.mechanics_mentioned(description) else "unknown"
        status = "reviewed" if description else "unverified"

    special = bool(entry.get("special_mode"))
    if special:
        mode = "special"

    if not theme:
        theme_match = THEME_RE.search(description)
        theme = (theme_match.group(1) or theme_match.group(2)).strip() if theme_match else None
        if theme:
            sources["theme"] = "description"

    hosted_url = entry.get("url")
    technical = None
    validation = None
    if not path.is_file():
        technical = {k: entry.get(k) for k in ("width", "height", "bytes", "duration")}
        technical["aspect"] = aspect_name(technical.get("width"), technical.get("height"))
        validation = {"status": "unchecked", "checked_at": None, "problems": []}
    elif not validate:
        validation = {"status": "unchecked", "checked_at": None, "problems": []}

    return make_item(
        relative, base_dir=base_dir, media_type=kind, description=description, source=source,
        captured_at=entry.get("captured_at"), game_build=entry.get("game_build"),
        mode=mode, level=level, scene=scene, theme=theme, special_mode=special,
        mechanisms_source=mechanisms_source, puzzle_state=state, puzzle_state_source=state_source,
        shows_action=action, tags=_entry(raw).get("tags") if isinstance(_entry(raw).get("tags"), list) else [],
        notes=entry.get("notes"), exclude=bool(entry.get("exclude")), verification=status, sources=sources,
        legacy=raw, url=hosted_url, url_ref=entry.get("ref") or "main", technical=technical, validation=validation,
        method="capture_gameplay.py (real game, real key presses)" if source == "capture_gameplay.py"
        else "hand-written description migrated from media_tags.json")


# ------------------------------------------------------------------------- manifest

def empty_manifest():
    return {"schema": SCHEMA, "generated_at": None, "items": []}


def load_manifest(path=MANIFEST_FILE):
    data = read_json(path, None)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return empty_manifest()
    return data


def save_manifest(manifest, path=MANIFEST_FILE):
    manifest["generated_at"] = utc_now()
    manifest["items"] = sorted(manifest["items"], key=lambda item: item["path"])
    write_json(path, manifest)


def by_path(manifest):
    return {item.get("path"): item for item in manifest.get("items", [])}


def upsert(manifest, item):
    """Add `item`, or replace the record with the same path while keeping human decisions
    (description, review status, exclude flag and notes) that were already made."""
    existing = by_path(manifest).get(item["path"])
    if existing is not None:
        for key in ("review", "notes"):
            if existing.get(key):
                item[key] = existing[key]
        if existing.get("exclude"):
            item["exclude"] = True
        if str(existing.get("description") or "").strip() and existing.get("capture", {}).get("source") != "capture_gameplay.py":
            item["description"] = existing["description"]
        item["post_categories"] = categories_for(item)
        manifest["items"] = [i for i in manifest["items"] if i.get("path") != item["path"]]
    manifest["items"].append(item)
    return item


def sync(base_dir=BASE_DIR, tags_file=None, manifest_file=None, validate=True, log=print):
    """Bring the manifest up to date with media_tags.json and the files on disk. Nothing is deleted:
    records whose file disappeared are kept so the validator reports them."""
    base_dir = Path(base_dir)
    tags_file = Path(tags_file or base_dir / "media" / "media_tags.json")
    manifest_file = Path(manifest_file or base_dir / "media" / "manifest.json")
    tags = read_json(tags_file, {})
    tags = tags if isinstance(tags, dict) else {}
    manifest = load_manifest(manifest_file)
    known = by_path(manifest)
    added = refreshed = 0

    paths = set(tags)
    media_dir = base_dir / "media"
    if media_dir.is_dir():
        for found in sorted(media_dir.rglob("*")):
            if found.is_file() and found.suffix.lower() in _MEDIA_EXTENSIONS \
                    and is_x_media_path(found.relative_to(base_dir).as_posix()):
                paths.add(found.relative_to(base_dir).as_posix())

    for relative in sorted(paths):
        raw = tags.get(relative, {})
        existing = known.get(relative)
        if existing is not None and existing.get("capture", {}).get("source") == "capture_gameplay.py" \
                and (base_dir / relative).is_file() and existing.get("sha256") == sha256_file(base_dir / relative) \
                and existing.get("perceptual_hashes") and all(len(h) == HASH_HEX_LENGTH for h in existing["perceptual_hashes"]):
            refreshed += 1
            continue          # a real capture whose bytes are unchanged is already authoritative
        upsert(manifest, migrate_entry(relative, raw, base_dir=base_dir, validate=validate))
        if existing is None:
            added += 1
        else:
            refreshed += 1

    save_manifest(manifest, manifest_file)
    log(f"manifest: {len(manifest['items'])} record(s) ({added} added, {refreshed} refreshed) -> {manifest_file}")
    return manifest


# ------------------------------------------------------------------------ validation

class Problem(dict):
    def __init__(self, level, item_id, message):
        super().__init__(level=level, id=item_id, message=message)

    def __str__(self):
        return f"{self['level'].upper():7s} {self['id']}: {self['message']}"


def validate_manifest(manifest, base_dir=BASE_DIR, *, tags=None, decode=True, check_hashes=True):
    """Check a manifest against the files on disk. Returns a list of Problem (level 'error' or 'warning')."""
    base_dir = Path(base_dir)
    problems = []

    def error(item_id, message):
        problems.append(Problem("error", item_id, message))

    def warning(item_id, message):
        problems.append(Problem("warning", item_id, message))

    if manifest.get("schema") != SCHEMA:
        error("(manifest)", f"unknown schema {manifest.get('schema')!r}; expected {SCHEMA!r}")

    items = manifest.get("items")
    if not isinstance(items, list):
        error("(manifest)", "'items' is missing or not a list")
        return problems

    seen_ids, seen_paths = {}, {}
    for item in items:
        item_id = str(item.get("id") or "(no id)")
        path = item.get("path")

        if not item.get("id"):
            error(item_id, "record has no id")
        elif item_id in seen_ids:
            error(item_id, f"duplicate id (also used by {seen_ids[item_id]})")
        seen_ids[item_id] = path

        if not path:
            error(item_id, "record has no path")
            continue
        if path in seen_paths:
            error(item_id, f"duplicate path {path} (also {seen_paths[path]})")
        seen_paths[path] = item_id

        for field, allowed in (("type", MEDIA_TYPES), ("puzzle_state", PUZZLE_STATES), ("aspect", ASPECT_NAMES)):
            if item.get(field) not in allowed:
                error(item_id, f"{field} {item.get(field)!r} is not one of {list(allowed)}")
        if (item.get("game") or {}).get("mode") not in MODES:
            error(item_id, f"game.mode {(item.get('game') or {}).get('mode')!r} is not one of {list(MODES)}")
        if (item.get("verification") or {}).get("status") not in VERIFICATION_STATUSES:
            error(item_id, "verification.status is invalid")
        if (item.get("review") or {}).get("status") not in REVIEW_STATUSES:
            error(item_id, "review.status is invalid")
        if (item.get("validation") or {}).get("status") not in VALIDATION_STATUSES:
            error(item_id, "validation.status is invalid")
        for source_field, source in ((item.get("verification") or {}).get("fields") or {}).items():
            if source not in FIELD_SOURCES:
                error(item_id, f"verification.fields.{source_field} has unknown source {source!r}")

        for name in item.get("mechanisms") or []:
            if name not in game_terms.DISPLAY_ORDER:
                error(item_id, f"mechanism {name!r} is not a canonical game name ({', '.join(game_terms.DISPLAY_ORDER)})")
        description = str(item.get("description") or "")
        unknown = game_terms.unknown_terms(description)
        if unknown:
            error(item_id, "description uses name(s) the game does not use: " + ", ".join(unknown))
        listed = set(item.get("mechanisms") or [])
        mentioned = set(game_terms.mechanics_mentioned(description))
        if (item.get("verification") or {}).get("fields", {}).get("mechanisms") == "description" and listed != mentioned:
            error(item_id, "mechanisms do not match the mechanisms named in the description")

        verification = item.get("verification") or {}
        fields = verification.get("fields") or {}
        if verification.get("status") == "verified":
            if (item.get("capture") or {}).get("source") != "capture_gameplay.py":
                error(item_id, "marked verified but was not produced by the capture tool")
            if fields.get("mechanisms") != "game-data" or fields.get("puzzle_state") not in ("capture-tool", "game-data"):
                error(item_id, "marked verified but mechanisms/puzzle state are not from game data")
            if not (item.get("capture") or {}).get("game_build"):
                error(item_id, "marked verified but has no game build id")

        captured_at = (item.get("capture") or {}).get("captured_at")
        if captured_at:
            try:
                datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
            except ValueError:
                error(item_id, f"capture.captured_at {captured_at!r} is not an ISO timestamp")

        if not description.strip() and not item.get("exclude"):
            warning(item_id, "no description: this media supports no post type")

        expected = categories_for(item)
        if sorted(item.get("post_categories") or []) != sorted(expected):
            error(item_id, f"post_categories {item.get('post_categories')} do not match the record's evidence {expected}")
        for category in item.get("post_categories") or []:
            if category not in CATEGORIES:
                error(item_id, f"unknown post category {category!r}")

        local = base_dir / path
        if item.get("hosted_only"):
            if not str(item.get("url") or "").startswith("https://"):
                error(item_id, "hosted-only media needs an https url")
            continue

        if not local.is_file():
            error(item_id, f"file is missing: {path}")
            continue

        expected_url = raw_url(path, item.get("url_ref") or "main")
        if item.get("url") != expected_url:
            error(item_id, f"stale url: {item.get('url')} (expected {expected_url})")

        if check_hashes and item.get("sha256"):
            actual = sha256_file(local)
            if actual != item["sha256"]:
                error(item_id, "file changed since it was indexed (sha256 mismatch); run 'media_manifest.py sync'")
                continue

        inspected = tools.inspect_file(local)
        if not inspected["ok"] and (item.get("validation") or {}).get("status") != "failed":
            error(item_id, "not a valid media file: " + "; ".join(inspected["problems"]))
            continue

        if decode:
            status, problems_found = check_decodes(local)
            if status == "failed":
                if (item.get("validation") or {}).get("status") == "failed":
                    warning(item_id, "known not postable: " + "; ".join(problems_found))
                else:
                    error(item_id, "media cannot be used: " + "; ".join(problems_found))

    indexed = set(seen_paths)
    media_dir = base_dir / "media"
    if media_dir.is_dir():
        for found in sorted(media_dir.rglob("*")):
            if found.is_file() and found.suffix.lower() in _MEDIA_EXTENSIONS:
                relative = found.relative_to(base_dir).as_posix()
                if relative not in indexed and is_x_media_path(relative):
                    warning(relative, "media file is not in the manifest (run 'media_manifest.py sync'); it will not be auto-selected")

    if tags is not None:
        legacy_paths = {item.get("path") for item in items}
        for key in tags:
            if key not in legacy_paths:
                error(key, "media_tags.json entry has no manifest record (metadata would be lost)")
        for item in items:
            if item.get("path") in tags and item.get("legacy") != tags[item["path"]]:
                error(str(item.get("id")), "the original media_tags.json entry was altered or dropped (item.legacy differs)")

    return problems


def check_urls(manifest, head, *, only_ids=None):
    """Optional online check: HEAD each record's url. `head(url)` returns an HTTP status code.
    Returns problems; offline tests pass a stub."""
    problems = []
    for item in manifest.get("items", []):
        if only_ids and item.get("id") not in only_ids:
            continue
        if item.get("exclude") or not item.get("post_categories"):
            continue
        try:
            status = head(item["url"])
        except Exception as error:  # network problems are reported, not raised
            problems.append(Problem("warning", item["id"], f"could not check url: {type(error).__name__}"))
            continue
        if status != 200:
            problems.append(Problem("error", item["id"], f"url answers HTTP {status}: {item['url']}"))
    return problems


# ----------------------------------------------------------------------------- report

def summary(manifest):
    from collections import Counter
    items = manifest.get("items", [])
    return {
        "records": len(items),
        "by_type": dict(Counter(i.get("type") for i in items)),
        "by_state": dict(Counter(i.get("puzzle_state") for i in items)),
        "by_verification": dict(Counter((i.get("verification") or {}).get("status") for i in items)),
        "by_category": dict(Counter(c for i in items for c in i.get("post_categories") or [])),
        "unusable": sorted(i["id"] for i in items if not i.get("post_categories")),
    }


# ------------------------------------------------------------------------------ CLI

def _cmd_sync(args):
    sync(validate=not args.fast)
    return 0


def _cmd_validate(args):
    manifest = load_manifest()
    tags = read_json(TAGS_FILE, {})
    problems = validate_manifest(manifest, tags=tags if isinstance(tags, dict) else None, decode=not args.fast)

    if args.check_urls:
        import requests

        def head(url):
            return requests.head(url, allow_redirects=False, timeout=20).status_code

        problems += check_urls(manifest, head)

    for problem in problems:
        print(problem)
    errors = [p for p in problems if p["level"] == "error"]
    print(f"\n{len(manifest['items'])} record(s): {len(errors)} error(s), {len(problems) - len(errors)} warning(s)")
    return 1 if errors else 0


def _cmd_report(args):
    print(json.dumps(summary(load_manifest()), indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Structured media manifest: sync, validate, report.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="add/refresh records from media_tags.json and the files on disk")
    p_sync.add_argument("--fast", action="store_true", help="skip decode validation (records are marked unchecked)")
    p_sync.set_defaults(func=_cmd_sync)

    p_validate = sub.add_parser("validate", help="check the manifest against the files (exit 1 on errors)")
    p_validate.add_argument("--fast", action="store_true", help="skip decoding every file")
    p_validate.add_argument("--check-urls", action="store_true", help="also HEAD every public URL (needs network)")
    p_validate.set_defaults(func=_cmd_validate)

    p_report = sub.add_parser("report", help="counts by type, state, category")
    p_report.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except tools.MediaToolError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
