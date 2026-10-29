"""Make a vertical TikTok video: a funny clip you supply on top, real Ringshift gameplay (muted) underneath; or, when
no funny clip is due, a gameplay-only video of the board.

    python scripts/tiktok_video.py status                              # what the next run will make, and why
    python scripts/tiktok_video.py make --game-dir game-src            # one video, live gameplay preferred
    python scripts/tiktok_video.py make --gameplay curated             # one video from media/gameplay/ recordings
    python scripts/tiktok_video.py make --format gameplay              # a gameplay-only video, even if a clip is new

Two formats (both 1080x1920, board-focused)
- funny:    the funny clip in the top panel with its own sound, the board below, the demo card between them.
- gameplay: no funny clip; a larger board in the middle with a "ROTATE THE RINGS / LIGHT EVERY LOCK" card above and
            the demo card below. Silent (gameplay sound is never used and no music is added; add a sound in TikTok).

Which format (--format auto, the scheduled default), in order
1. a funny clip that was never used: funny format;
2. else an old clip that may be reused: last used at least REUSE_AFTER_DAYS ago, used fewer than MAX_FUNNY_USES
   times, and paired with gameplay it was not shown with before: funny format again;
3. else: gameplay format. So running out of funny clips never stops the output.
--format funny never falls back to gameplay (it skips, and says why); --format gameplay always makes gameplay.
Any video whose content fingerprint (format, clip, gameplay levels/themes or recordings) matches an earlier video is
refused, so a reuse can never repeat an earlier video.

What `make` does, in order
1. Picks the format and, for the funny format, ONE funny clip from media/funny/ (MP4/MOV/WebM) that is valid (real
   header, decodes, 2-180 s). "Used" is tracked by SHA-256, so renaming a clip does not make it new and editing it does.
2. Gets genuine gameplay. Preferred: plays real levels of the stable game (repository ringshift-game, branch main)
   in headless Chromium with the existing capture tool (capture_gameplay.py: real key presses, the game must report
   the win). Otherwise, or if that fails: curated recordings from media/gameplay/.
3. Measures where the puzzle board is in every gameplay piece: from the game's own SVG geometry for live captures
   (tiktok_board.BOARD_JS) and from the pixels for recordings (tiktok_board.detect_board). Each piece is cropped to
   a square around the board's outer rim (plus a small margin) and scaled to the board area of the layout.
4. Composes 1080x1920 (9:16): the funny clip in the top panel (fitted, blurred fill behind it), the board in the
   lower panel, and a "PLAY THE DEMO / LINK IN BIO" card between them, inside TikTok's safe area and clear of the
   board. Audio: only the funny clip's own audio track. Gameplay audio is never mapped; no music is added.
5. Validates the result (decode, size, codec, duration, audio is exactly the funny clip's audio, the whole board
   circle is visible and large in its area, the card is readable and away from the board) and writes a metadata JSON.
   A video that fails any check is deleted and the run fails; nothing is reported as made.

Uploading to TikTok is manual (see docs/TIKTOK.md). This script never contacts TikTok.
"""

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import media_tools as tools
import tiktok_board as tb
import tiktok_state as ts

BASE_DIR = Path(__file__).resolve().parent.parent
FUNNY_DIR = BASE_DIR / "media" / "funny"
GAMEPLAY_DIR = BASE_DIR / "media" / "gameplay"
OUTPUT_DIR = BASE_DIR / "media" / "tiktok-output"

VIDEO_EXTENSIONS = tools.VIDEO_EXTENSIONS          # .mp4 .mov .webm
MIN_FUNNY_SECONDS = 2.0
MAX_FUNNY_SECONDS = 180.0
MAX_FUNNY_BYTES = 500 * 1024 * 1024
MIN_FUNNY_SIDE = 240
MIN_SEGMENT_SECONDS = 3.0
MAX_SEGMENTS = 4
MAX_OUTPUT_BYTES = 287 * 1024 * 1024              # TikTok's web upload limit for a video file
FPS = 30
BACKGROUND = "0x060a14"                            # close to the game's own deep-space background

CTA_LINES = ("PLAY THE DEMO", "LINK IN BIO")
HOOK_LINES = ("ROTATE THE RINGS", "LIGHT EVERY LOCK")     # gameplay format only: the game's own goal, nothing invented

FORMATS = ("auto", "funny", "gameplay")
REUSE_AFTER_DAYS = 7               # an old funny clip may come back after this many days...
MAX_FUNNY_USES = 3                 # ...at most this many times in total (first use included)
GAMEPLAY_TARGET_SECONDS = 20.0     # gameplay format: aim for this much real gameplay
MIN_GAMEPLAY_SECONDS = 8.0         # gameplay format: less than this is not a video worth uploading
MAX_GAMEPLAY_SECONDS = 60.0

# TikTok draws its own interface over the video: tabs at the top, the like/comment/share rail on the right and the
# caption, user name and sound line at the bottom. Text and the board are kept inside this box (1080x1920 pixels;
# scaled for other sizes). Bottom 400 px and right 130 px are left free, as TikTok's safe-zone templates show.
SAFE_AREA = {"left": 60, "top": 150, "right": 950, "bottom": 1520}

SCHEMA = "ringshift-tiktok-video/1"


class TikTokError(RuntimeError):
    pass


class DuplicateVideo(TikTokError):
    """The planned video has the same content fingerprint as one made before."""


class GameplayUnavailable(TikTokError):
    """Neither live capture nor any curated recording gave usable gameplay (not the funny clip's fault)."""


def even(value):
    return int(round(value / 2.0)) * 2


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now():
    return datetime.now(timezone.utc)


def rel(path):
    try:
        return Path(path).resolve().relative_to(BASE_DIR).as_posix()
    except ValueError:
        return Path(path).name


# ================================================================================================ layout (pure)

def plan_layout(clip_width, clip_height, width=1080, height=1920):
    """Where everything goes in a width x height (9:16) frame, for a funny clip of clip_width x clip_height.

    Returns {"width", "height", "scale", "safe", "top": {"x","y","w","h"}, "panel": {...}, "cta": {...},
    "board": {"x","y","size"}}. The top panel is as tall as a full-width 16:9 clip needs (608 px at 1080 wide) and at
    most 660 px for squarer or vertical clips (those get a blurred fill at the sides), so the board keeps most of
    the safe area."""
    if abs(width / height - 9 / 16) > 0.01:
        raise TikTokError(f"the output must be 9:16, not {width}x{height}")
    if clip_width <= 0 or clip_height <= 0:
        raise TikTokError("the funny clip has no picture size")
    s = width / 1080.0
    safe = {key: even(value * s) for key, value in SAFE_AREA.items()}
    top_h = even(min(max(width * clip_height / clip_width, 608 * s), 660 * s))
    gap = even(12 * s)
    cta_h = even(112 * s)
    cta_w = even(760 * s)
    cta = {"x": even((width - cta_w) / 2), "y": top_h + gap, "w": cta_w, "h": cta_h}
    board_top = cta["y"] + cta_h + gap
    half_room = min(width / 2 - safe["left"], safe["right"] - width / 2)
    size = even(min(safe["bottom"] - board_top, 2 * half_room))
    if size < 0.5 * width:
        raise TikTokError(f"the board would only be {size}px wide; the layout does not fit")
    board = {"x": even((width - size) / 2), "y": board_top, "size": size}
    return {"format": "funny", "width": width, "height": height, "scale": s, "safe": safe,
            "top": {"x": 0, "y": 0, "w": width, "h": top_h},
            "panel": {"x": 0, "y": top_h, "w": width, "h": height - top_h},
            "cta": cta, "board": board}


def plan_gameplay_layout(width=1080, height=1920):
    """The gameplay-only layout: no funny panel, so the board is as large as TikTok's safe area allows (820 px at
    1080 wide, centred), with the hook card above it and the demo card below, the three stacked in the middle of the
    safe area. Same keys as plan_layout plus "hook"; "top" has height 0 and "panel" is the whole frame."""
    if abs(width / height - 9 / 16) > 0.01:
        raise TikTokError(f"the output must be 9:16, not {width}x{height}")
    s = width / 1080.0
    safe = {key: even(value * s) for key, value in SAFE_AREA.items()}
    card_w, card_h, gap = even(760 * s), even(112 * s), even(24 * s)
    half_room = min(width / 2 - safe["left"], safe["right"] - width / 2)
    size = even(min(2 * half_room, safe["bottom"] - safe["top"] - 2 * card_h - 2 * gap))
    if size < 0.5 * width:
        raise TikTokError(f"the board would only be {size}px wide; the layout does not fit")
    stack = card_h + gap + size + gap + card_h
    y0 = even(safe["top"] + (safe["bottom"] - safe["top"] - stack) / 2)
    card_x = even((width - card_w) / 2)
    hook = {"x": card_x, "y": y0, "w": card_w, "h": card_h}
    board = {"x": even((width - size) / 2), "y": y0 + card_h + gap, "size": size}
    cta = {"x": card_x, "y": board["y"] + size + gap, "w": card_w, "h": card_h}
    return {"format": "gameplay", "width": width, "height": height, "scale": s, "safe": safe,
            "top": {"x": 0, "y": 0, "w": width, "h": 0},
            "panel": {"x": 0, "y": 0, "w": width, "h": height},
            "hook": hook, "cta": cta, "board": board}


def _inside(box, outer):
    return (box["x"] >= outer["x"] and box["y"] >= outer["y"] and box["x"] + box["w"] <= outer["x"] + outer["w"]
            and box["y"] + box["h"] <= outer["y"] + outer["h"])


def _overlap(a, b):
    return not (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"] or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])


def layout_problems(layout):
    """[] when the layout keeps the card inside the gameplay panel and TikTok's safe area, away from the board, and
    the board large and fully inside the safe area."""
    safe = layout["safe"]
    safe_box = {"x": safe["left"], "y": safe["top"], "w": safe["right"] - safe["left"], "h": safe["bottom"] - safe["top"]}
    board = layout["board"]
    board_box = {"x": board["x"], "y": board["y"], "w": board["size"], "h": board["size"]}
    problems = []
    if not _inside(layout["cta"], safe_box):
        problems.append("the demo card is outside TikTok's safe area")
    if not _inside(layout["cta"], layout["panel"]):
        problems.append("the demo card is not in the gameplay panel")
    if _overlap(layout["cta"], board_box):
        problems.append("the demo card overlaps the board area")
    if not _inside(board_box, safe_box):
        problems.append("the board area is outside TikTok's safe area")
    if not _inside(board_box, layout["panel"]):
        problems.append("the board area is not in the gameplay panel")
    if board["size"] < 0.6 * layout["width"]:
        problems.append(f"the board area is only {board['size']}px wide")
    hook = layout.get("hook")
    if hook is not None:
        if not _inside(hook, safe_box):
            problems.append("the hook card is outside TikTok's safe area")
        if _overlap(hook, board_box):
            problems.append("the hook card overlaps the board area")
        if _overlap(hook, layout["cta"]):
            problems.append("the hook card overlaps the demo card")
    elif layout.get("format") == "gameplay":
        problems.append("the gameplay layout has no hook card")
    return problems


# ================================================================================================ demo card

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


def _font(size):
    from PIL import ImageFont
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size), Path(candidate).name
    try:
        return ImageFont.load_default(size=size), "Pillow default"
    except TypeError as error:  # Pillow < 10.1 has no scalable default font
        raise TikTokError("no TrueType font found for the demo card; install fonts-dejavu-core") from error


def render_cta(width, height, destination, scale=1.0, lines=CTA_LINES):
    """Draw the demo card (transparent PNG, width x height): a dark rounded panel with the two lines centred.
    Returns {"path", "font", "text_boxes": [(x0, y0, x1, y1), ...]} (text boxes in card pixels)."""
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = int(28 * scale)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=(6, 12, 26, 228),
                           outline=(95, 212, 255, 255), width=max(2, int(4 * scale)))
    big, font_name = _font(int(50 * scale))
    small, _ = _font(int(36 * scale))
    styles = [(lines[0], big, (255, 255, 255, 255), int(6 * scale)), (lines[1], small, (95, 212, 255, 255), int(8 * scale))]

    def measure(text, font, spacing):
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0] + spacing * (len(text) - 1), box[3] - box[1], box

    sizes = [measure(text, font, spacing) for text, font, _, spacing in styles]
    gap = int(10 * scale)
    total = sum(h for _, h, _ in sizes) + gap
    y = (height - total) / 2
    boxes = []
    for (text, font, colour, spacing), (w, h, box) in zip(styles, sizes):
        x = (width - w) / 2
        cursor = x
        for char in text:                         # letter spacing, drawn char by char
            draw.text((cursor, y - box[1]), char, font=font, fill=colour)
            cb = draw.textbbox((0, 0), char, font=font)
            cursor += (cb[2] - cb[0]) + spacing if char != " " else draw.textlength(" ", font=font) + spacing
        boxes.append((round(x), round(y), round(x + w), round(y + h)))
        y += h + gap
    destination = Path(destination)
    image.save(destination)
    return {"path": destination, "font": font_name, "text_boxes": boxes}


def cta_problems(cta_render, width, height, lines=CTA_LINES):
    problems = []
    for x0, y0, x1, y1 in cta_render["text_boxes"]:
        if x0 < 8 or y0 < 4 or x1 > width - 8 or y1 > height - 4:
            problems.append("a line of the card does not fit inside the card")
    if len(cta_render["text_boxes"]) != len(lines):
        problems.append("the card does not have all its lines")
    return problems


# ================================================================================================ funny clips

def probe_clip(path):
    """Facts about a clip, including its display size after rotation metadata and every audio stream."""
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, check=False)
    if result.returncode != 0:
        raise TikTokError(f"ffprobe cannot read {Path(path).name}: {result.stderr.decode(errors='replace').strip()[:200]}")
    data = json.loads(result.stdout or b"{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise TikTokError(f"{Path(path).name} has no video stream")
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = int(float(side["rotation"]))
    if not rotation and (video.get("tags") or {}).get("rotate"):
        rotation = int(float(video["tags"]["rotate"]))
    sar = video.get("sample_aspect_ratio") or "1:1"
    try:
        num, den = (int(v) for v in sar.split(":"))
        if num > 0 and den > 0 and num != den:
            width = int(round(width * num / den))
    except ValueError:
        pass
    if abs(rotation) % 180 == 90:
        width, height = height, width
    try:
        duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    except ValueError:
        duration = 0.0
    return {"width": width, "height": height, "rotation": rotation, "duration": round(duration, 3),
            "video_codec": video.get("codec_name"), "audio_streams": len(audio), "has_audio": bool(audio),
            "audio_codec": audio[0].get("codec_name") if audio else None,
            "audio_channels": int(audio[0].get("channels") or 0) if audio else 0,
            "audio_rate": int(audio[0].get("sample_rate") or 0) if audio else 0,
            "bytes": Path(path).stat().st_size}


def decode_problems(path, limit_seconds=None):
    """[] if FFmpeg decodes the whole file (video and audio) without an error."""
    command = ["ffmpeg", "-v", "error", "-xerror", "-i", str(path)]
    if limit_seconds:
        command += ["-t", f"{limit_seconds:.3f}"]
    result = subprocess.run(command + ["-f", "null", "-"], capture_output=True, check=False, timeout=900)
    text = result.stderr.decode(errors="replace").strip()
    if result.returncode != 0:
        return [f"does not decode cleanly: {text[:200] or 'ffmpeg failed'}"]
    return []


def check_funny(path):
    """(info or None, problems) for one funny clip."""
    path = Path(path)
    inspected = tools.inspect_file(path)
    if not inspected["ok"]:
        return None, inspected["problems"]
    if inspected["bytes"] > MAX_FUNNY_BYTES:
        return None, [f"file is {inspected['bytes'] / 1048576:.0f} MB (limit {MAX_FUNNY_BYTES // 1048576} MB)"]
    try:
        info = probe_clip(path)
    except TikTokError as error:
        return None, [str(error)]
    problems = []
    if info["duration"] < MIN_FUNNY_SECONDS:
        problems.append(f"too short ({info['duration']:.1f}s; minimum {MIN_FUNNY_SECONDS:.0f}s)")
    if info["duration"] > MAX_FUNNY_SECONDS:
        problems.append(f"too long ({info['duration']:.0f}s; maximum {MAX_FUNNY_SECONDS:.0f}s): trim it first")
    if min(info["width"], info["height"]) < MIN_FUNNY_SIDE:
        problems.append(f"picture is too small ({info['width']}x{info['height']})")
    if not problems:
        problems += decode_problems(path)
    return info, problems


def discover_funny(folder=FUNNY_DIR):
    """Every file in the funny folder that looks like a video (by extension), sorted by name. Other files (README,
    .gitkeep) are ignored; videos with another extension are listed so the report can explain them."""
    folder = Path(folder)
    if not folder.is_dir():
        return [], []
    videos, other = [], []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            videos.append(path)
        elif path.suffix.lower() in (".m4v", ".avi", ".mkv", ".gif", ".wmv"):
            other.append(path)
    return videos, other


def review_funny(state, folder=FUNNY_DIR, wanted=None, allow_reuse=False, check=None):
    """Every funny clip with its verdict. Returns (chosen or None, report). Each report row:
    {"file", "sha256", "status": eligible|used|invalid|duplicate|failed-before|not-requested, "why", "info"}."""
    check = check or check_funny
    videos, other = discover_funny(folder)
    report = [{"file": rel(p), "sha256": None, "status": "invalid", "why": f"unsupported format {p.suffix} (use MP4, MOV or WebM)",
               "info": None} for p in other]
    if wanted is not None:
        wanted = Path(wanted).resolve()
        if wanted not in [p.resolve() for p in videos]:
            raise TikTokError(f"{wanted} is not a supported video in {rel(folder)}/")
    used = ts.used_hashes(state)
    seen = {}
    chosen = None
    for path in videos:
        row = {"file": rel(path), "sha256": None, "status": "eligible", "why": "", "info": None}
        report.append(row)
        if wanted is not None and path.resolve() != wanted:
            row.update(status="not-requested", why="another clip was asked for")
            continue
        try:
            row["sha256"] = sha256_file(path)
        except OSError as error:
            row.update(status="invalid", why=f"cannot be read ({type(error).__name__})")
            continue
        if row["sha256"] in seen:
            row.update(status="duplicate", why=f"same bytes as {seen[row['sha256']]}")
            continue
        seen[row["sha256"]] = row["file"]
        if row["sha256"] in used and not allow_reuse:
            earlier = ts.video_for_hash(state, row["sha256"]) or {}
            row.update(status="used", why=f"already made into {earlier.get('output_name', 'a video')} on {str(earlier.get('at', '?'))[:10]}")
            continue
        if ts.failure_count(state, row["sha256"]) >= ts.MAX_FAILURES and wanted is None:
            failure = state["failures"][row["sha256"]]
            row.update(status="failed-before", why=f"failed {failure['count']} times (last: {failure.get('last_reason', '?')[:120]}); "
                                                   "pick it by name to try again")
            continue
        if chosen is not None:
            row.update(status="eligible", why="waiting for a later run")
            continue
        info, problems = check(path)
        row["info"] = info
        if problems:
            row.update(status="invalid", why="; ".join(problems))
            continue
        chosen = {"path": path, "file": row["file"], "sha256": row["sha256"], "info": info,
                  "reuse": None}
        if row["sha256"] in used:
            chosen["reuse"] = reuse_details(state, row["sha256"])
        row["why"] = "chosen for this run"
    return chosen, report


# ------------------------------------------------------------------------------------------------ reusing old clips

def parse_at(value):
    """A state timestamp as an aware datetime, or None."""
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def reuse_details(state, sha256):
    """What earlier videos of this clip showed, so a reuse can show something else: {"use": the number this use will
    be, "previous": earlier artifact names, "avoid_scenes": live levels, "avoid_gameplay": curated recording hashes}."""
    earlier = ts.videos_for_hash(state, sha256)
    return {"use": len(earlier) + 1, "previous": [v.get("output_name") for v in earlier],
            "avoid_scenes": sorted({s.get("scene") for v in earlier for s in v.get("scenes") or [] if s.get("scene")}),
            "avoid_gameplay": sorted({sha for v in earlier for sha in v.get("gameplay_sha256") or []})}


def choose_reuse(state, report, folder=FUNNY_DIR, now=None, check=None):
    """When no clip is new: the old clip that may come back now, or None. A clip qualifies when its last video is at
    least REUSE_AFTER_DAYS old, it has been used fewer than MAX_FUNNY_USES times and it has not failed MAX_FAILURES
    times; the least used, longest-rested one is taken. Each "used" row in `report` gets a note saying when (or why
    not) it can come back. Returns the chosen clip dict (as review_funny) with "reuse" filled in."""
    check = check or check_funny
    now = now or utc_now()
    candidates = []
    for row in report:
        if row["status"] != "used" or not row.get("sha256"):
            continue
        earlier = ts.videos_for_hash(state, row["sha256"])
        dates = [d for d in (parse_at(v.get("at")) for v in earlier) if d is not None]
        last = max(dates) if dates else None
        if len(earlier) >= MAX_FUNNY_USES:
            row["reuse"] = f"used {len(earlier)} times (limit {MAX_FUNNY_USES}); not reused again"
            continue
        if ts.failure_count(state, row["sha256"]) >= ts.MAX_FAILURES:
            row["reuse"] = "failed too often to be reused automatically"
            continue
        if last is None:
            row["reuse"] = "no date for its last use; not reused automatically"
            continue
        rested = (now - last).total_seconds() / 86400
        if rested < REUSE_AFTER_DAYS:
            ready = last.timestamp() + REUSE_AFTER_DAYS * 86400
            row["reuse"] = (f"can be reused from {datetime.fromtimestamp(ready, timezone.utc):%Y-%m-%d} "
                            f"(used {len(earlier)}x, last {rested:.1f} days ago)")
            continue
        candidates.append((len(earlier), last, row))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]["file"]))
    for _, _, row in candidates:
        path = Path(folder) / Path(row["file"]).name          # clips sit directly in the funny folder
        info, problems = check(path)
        row["info"] = info
        if problems:
            row["reuse"] = "cannot be reused: " + "; ".join(problems)
            continue
        row["reuse"] = "reused in this run"
        return {"path": path, "file": row["file"], "sha256": row["sha256"], "info": info,
                "reuse": reuse_details(state, row["sha256"])}
    return None


def plan_format(state, fmt="auto", folder=FUNNY_DIR, wanted=None, allow_reuse=False, now=None, check=None):
    """Decide what this run makes. Returns {"format": "funny" | "gameplay" | None, "funny": clip or None,
    "why": reason, "report": funny report}. None only when a funny video was explicitly asked for (--format funny or
    --funny-file) and no clip can be used; auto always has the gameplay format to fall back on."""
    if fmt not in FORMATS:
        raise TikTokError(f"unknown format {fmt!r} (use {', '.join(FORMATS)})")
    if fmt == "gameplay":
        if wanted is not None:
            raise TikTokError("--funny-file makes a funny video; it cannot be combined with --format gameplay")
        return {"format": "gameplay", "funny": None, "why": "gameplay format asked for", "report": []}
    funny, report = review_funny(state, folder, wanted=wanted, allow_reuse=allow_reuse, check=check)
    if funny is not None:
        why = "the clip that was asked for" if wanted is not None else "a funny clip that was never used"
        if funny.get("reuse"):
            why += f" (reuse {funny['reuse']['use']}, allowed by allow_reuse)"
        return {"format": "funny", "funny": funny, "why": why, "report": report}
    if wanted is not None:
        return {"format": None, "funny": None, "why": skip_reason(report), "report": report}
    funny = choose_reuse(state, report, folder, now=now, check=check)
    if funny is not None:
        return {"format": "funny", "funny": funny, "report": report,
                "why": (f"no new funny clip; reusing {Path(funny['file']).name} (use {funny['reuse']['use']} of at most "
                        f"{MAX_FUNNY_USES}, at least {REUSE_AFTER_DAYS} days after its last video, with different gameplay)")}
    if fmt == "funny":
        return {"format": None, "funny": None, "why": skip_reason(report) + "; none can be reused yet", "report": report}
    return {"format": "gameplay", "funny": None, "report": report,
            "why": skip_reason(report) + "; none can be reused yet, so this run makes a gameplay-only video"}


# ================================================================================================ gameplay

def segment_from_detection(path, sha, start, duration, info, circle, method, extra=None):
    crop = tb.crop_for_circle(circle["cx"], circle["cy"], circle["r"], info["width"], info["height"])
    segment = {"file": rel(path), "path": str(path), "sha256": sha, "start": round(start, 3), "duration": round(duration, 3),
               "source_size": [info["width"], info["height"]],
               "circle": {k: round(float(circle[k]), 2) for k in ("cx", "cy", "r")}, "crop": crop, "bounds_method": method}
    segment.update(extra or {})
    return segment


def plan_curated(need_seconds, state, folder=GAMEPLAY_DIR, log=print, detect=tb.detect_board, avoid=()):
    """Curated recordings that show a whole, measurable board, least used first, until `need_seconds` is covered
    (or MAX_SEGMENTS pieces). Recordings whose hash is in `avoid` (what a reused clip was shown with before) come
    last. Returns (segments, report rows)."""
    folder = Path(folder)
    avoid = set(avoid or ())
    files = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
                   key=lambda p: p.name.lower()) if folder.is_dir() else []
    candidates = []
    for path in files:
        try:
            sha = sha256_file(path)
        except OSError:
            continue
        candidates.append((sha in avoid, state["gameplay_uses"].get(sha, 0), path.name.lower(), path, sha))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))

    segments, report, total, seen = [], [], 0.0, set()
    for _, _, _, path, sha in candidates:
        if total >= need_seconds or len(segments) >= MAX_SEGMENTS:
            break
        if sha in seen:
            continue
        seen.add(sha)
        inspected = tools.inspect_file(path)
        if not inspected["ok"]:
            report.append({"file": rel(path), "outcome": "skipped", "why": "; ".join(inspected["problems"])})
            continue
        try:
            info = tools.probe(path)
        except tools.MediaToolError as error:
            report.append({"file": rel(path), "outcome": "skipped", "why": str(error)[:200]})
            continue
        if info["duration"] < MIN_SEGMENT_SECONDS:
            report.append({"file": rel(path), "outcome": "skipped", "why": f"only {info['duration']:.1f}s long"})
            continue
        duration = min(info["duration"] - 0.1, max(need_seconds - total, MIN_SEGMENT_SECONDS))
        try:
            found = detect(path, 0.0, duration, info=info)
        except (tb.BoardError, tools.MediaToolError) as error:
            report.append({"file": rel(path), "outcome": "skipped", "why": f"board not found: {error}"})
            continue
        problems = tb.check_board_detection(found)
        if problems:
            report.append({"file": rel(path), "outcome": "skipped", "why": "; ".join(problems)})
            continue
        segments.append(segment_from_detection(path, sha, 0.0, duration, info, found, "pixels",
                                               {"source": "curated", "detection": {k: found[k] for k in ("support", "frames_showing", "frames_sampled")}}))
        total += duration
        report.append({"file": rel(path), "outcome": "used", "why": f"{duration:.1f}s, board r={found['r']:.0f}px"})
        log(f"  curated gameplay: {path.name} ({duration:.1f}s, board found with {found['support']:.0%} rim support)")
    return segments, report


def _measure_board(session, settle_ms=2500):
    """BOARD_JS once the board's own CSS animations (level intro, win surge) have finished."""
    waited = 0
    while True:
        board = session.page.evaluate(tb.BOARD_JS)
        if not board.get("animating") or waited >= settle_ms:
            return board
        session.page.wait_for_timeout(150)
        waited += 150


def capture_live_segment(game_path, scene, theme_id, out_dir, *, session_factory=None, log=print):
    """Play one level for real (capture_gameplay.capture_scene, 9:16 phone window) and measure the board in the same
    browser before and after. Returns (capture result, dom board dict). Raises capture_gameplay.CaptureError."""
    import capture_gameplay as cg
    session_factory = session_factory or cg.GameSession
    last_error = None
    for attempt in (1, 2):
        try:
            with session_factory(game_path, aspect="9:16") as session:
                if theme_id:
                    session.set_theme(theme_id)
                session.start_level(scene["mode"], scene["index"])
                before = _measure_board(session)
                results = cg.capture_scene(session, scene, out_dir, kinds=("video",), aspect="9:16", theme=theme_id,
                                           frame_board=False, pace=cg.SOCIAL_PACE, log=log)
                after = _measure_board(session)
                videos = [r for r in results if r["type"] == "video"]
                if not videos:
                    raise cg.CaptureError(f"{scene['id']}: the capture produced no video")
                if session.errors:
                    for item in results:
                        Path(item["path"]).unlink(missing_ok=True)
                    raise cg.CaptureError(f"{scene['id']}: the game logged {len(session.errors)} page error(s); nothing kept")
                if not before.get("box") or not after.get("box"):
                    raise cg.CaptureError(f"{scene['id']}: the game did not report the board geometry")
                moved = max(abs(before["box"][k] - after["box"][k]) for k in ("left", "top", "right", "bottom"))
                if moved > 0.02 * (before["box"]["right"] - before["box"]["left"]):
                    raise cg.CaptureError(f"{scene['id']}: the board moved by {moved:.0f}px during the level")
                return videos[0], before
        except cg.CaptureError:
            raise
        except Exception as error:      # browser/driver crash: one retry in a fresh browser
            last_error = error
            log(f"  browser attempt {attempt}/2 failed: {type(error).__name__}: {str(error)[:160]}")
    raise cg.CaptureError(f"{scene['id']}: the browser failed twice ({last_error})")


def plan_live(need_seconds, state, game_dir, work_dir, *, rng, log=print, session_factory=None, max_levels=3,
              detect=tb.detect_board, avoid_scenes=()):
    """Real gameplay captured now from the stable game. Levels in `avoid_scenes` (what a reused clip was shown with
    before) are only played when nothing else is left. Returns (segments, report, game). Raises TikTokError if no
    level could be captured."""
    import capture_gameplay as cg
    import daily_media as dm
    import game_source

    try:
        game = game_source.describe(game_dir)
    except game_source.GameSourceError as error:
        raise TikTokError(f"the game is not available: {error}") from error
    session_factory = session_factory or cg.GameSession

    with session_factory(game["path"], aspect="9:16") as planner:
        scenes = planner.scenes()
        for scene in scenes:
            solution = planner.solve(scene["mode"], scene["index"])
            scene["solution_cost"] = solution["cost"] if solution else None
        themes = planner.themes()

    pseudo = {"known_themes": [], "runs": [{"status": "captured", "theme": s.get("theme")} for s in state["scenes"]]}
    theme, _ = dm.choose_theme(themes, pseudo, rng)
    history = [{"scene": s["scene"], "labels": s.get("labels") or []} for s in state["scenes"]]
    avoid_scenes = set(avoid_scenes or ())
    picks = dm.choose_scenes(scenes, "clip", history, rng, count=max_levels + 3 + len(avoid_scenes))
    picks = [s for s in picks if s["id"] not in avoid_scenes] + [s for s in picks if s["id"] in avoid_scenes]

    segments, report, total = [], [], 0.0
    for scene in picks:
        if total >= need_seconds or len(segments) >= max_levels:
            break
        log(f"  live gameplay: playing {scene['id']} ({', '.join(scene['labels'])}) in theme {theme['id']} ...")
        try:
            result, dom = capture_live_segment(game["path"], scene, theme["id"], work_dir, session_factory=session_factory, log=log)
        except cg.CaptureError as error:
            report.append({"scene": scene["id"], "outcome": "capture failed", "why": str(error)[:300]})
            continue
        path = Path(result["path"])
        info = tools.probe(path)
        try:
            circle = dict(zip(("cx", "cy", "r"), tb.circle_from_dom(dom["box"], dom["viewport"]["width"], info["width"])))
        except tb.BoardError as error:
            report.append({"scene": scene["id"], "outcome": "rejected", "why": str(error)})
            continue
        try:
            pixels = detect(path, 0.0, info["duration"], info=info)
            cross = tb.agreement(circle, pixels)
        except (tb.BoardError, tools.MediaToolError) as error:
            pixels, cross = None, {"error": str(error)[:200]}
        segments.append(segment_from_detection(path, sha256_file(path), 0.0, info["duration"], info, circle, "game-dom", {
            "source": "live", "scene": scene["id"], "labels": scene["labels"], "theme": theme["id"],
            "theme_name": theme.get("name"), "moves": result.get("moves"), "game_file": game["file"],
            "game_commit": game.get("commit"), "game_build": game["build"], "dom_box_css": dom["box"],
            "viewport": dom["viewport"], "pixel_cross_check": cross}))
        total += info["duration"]
        report.append({"scene": scene["id"], "outcome": "captured", "why": f"{info['duration']:.1f}s, won in-game"})
    if not segments:
        raise TikTokError("no level could be captured from the game: " + "; ".join(f"{r['scene']}: {r['why']}" for r in report)[:600])
    return segments, report, game


# ================================================================================================ FFmpeg

def _ffmpeg(command, what):
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        raise TikTokError(f"FFmpeg failed while {what}: {result.stderr.decode(errors='replace').strip()[-500:]}")
    return result


def render_board_track(segments, size, destination, work_dir):
    """One silent video of just the board (size x size): every gameplay piece cropped around ITS board and joined."""
    parts = []
    for index, segment in enumerate(segments):
        part = Path(work_dir) / f"board_{index:02d}.mp4"
        _ffmpeg(["ffmpeg", "-v", "error", "-y", "-ss", f"{segment['start']:.3f}", "-i", segment["path"],
                 "-t", f"{segment['duration']:.3f}", "-map", "0:v:0",
                 "-vf", tb.crop_filter(segment["crop"], size, BACKGROUND) + f",fps={FPS},setsar=1,format=yuv420p",
                 "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", str(part)], "cropping the board")
        parts.append(part)
    listing = Path(work_dir) / "board_parts.txt"
    listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    _ffmpeg(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(destination)],
            "joining the gameplay pieces")
    return destination


def render_board_mask(size, destination, margin=tb.CROP_MARGIN):
    """Grayscale mask for the board square: fully opaque over the whole board (the rim sits at 0.5/(1+2*margin) of
    the square) and fading to transparent at the square's edge, so the square's corners (bits of the game's HUD, the
    recording's own background) blend into the panel instead of showing a hard box."""
    import numpy as np
    from PIL import Image
    rim = 0.5 / (1 + 2 * margin)
    solid, clear = rim + 0.35 * (0.5 - rim), 0.5
    ys, xs = np.mgrid[0:size, 0:size]
    distance = np.hypot(xs + 0.5 - size / 2, ys + 0.5 - size / 2) / size
    alpha = np.clip((clear - distance) / (clear - solid), 0.0, 1.0)
    Image.fromarray((alpha * 255).astype(np.uint8), "L").save(destination)
    return {"path": Path(destination), "opaque_radius": solid, "rim_radius": rim}


def compose_command(funny, funny_info, board_track, cta_png, mask_png, layout, duration, destination):
    """The single FFmpeg command that builds the final video. Only the funny clip's audio is mapped."""
    W, H = layout["width"], layout["height"]
    top = layout["top"]["h"]
    board, cta = layout["board"], layout["cta"]
    line = max(2, even(3 * layout["scale"]))
    graph = ";".join([
        f"[0:v]fps={FPS},split=2[fa][fb]",
        f"[fa]scale={W}:{top}:force_original_aspect_ratio=increase,crop={W}:{top},gblur=sigma={28 * layout['scale']:.1f},"
        f"eq=brightness=-0.20:saturation=0.75,setsar=1[bg]",
        f"[fb]scale={W}:{top}:force_original_aspect_ratio=decrease,setsar=1[fg]",
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[top]",
        f"[1:v]fps={FPS},setsar=1,split=2[bsrc][bbg]",
        # The panel behind the board: the gameplay itself, enlarged, blurred and darkened (no hard edges, no music).
        f"[bbg]scale={W}:{H - top}:force_original_aspect_ratio=increase,crop={W}:{H - top},"
        f"gblur=sigma={36 * layout['scale']:.1f},eq=brightness=-0.30:saturation=0.6,"
        f"drawbox=x=0:y=0:w={W}:h={line}:color=0x5fd4ff@0.55:t=fill,setsar=1[panel]",
        "[bsrc]format=yuva420p[bsq]",
        "[3:v]format=gray[mask]",
        "[bsq][mask]alphamerge[board]",
        f"[panel][board]overlay=x={board['x']}:y={board['y'] - top}[p1]",
        f"[p1][2:v]overlay=x={cta['x']}:y={cta['y'] - top}[p2]",
        "[top][p2]vstack=inputs=2,format=yuv420p[v]",
    ])
    command = ["ffmpeg", "-v", "error", "-y", "-i", str(funny), "-stream_loop", "-1", "-i", str(board_track),
               "-loop", "1", "-framerate", str(FPS), "-i", str(cta_png),
               "-loop", "1", "-framerate", str(FPS), "-i", str(mask_png),
               "-filter_complex", graph, "-map", "[v]"]
    if funny_info["has_audio"]:
        command += ["-map", "0:a:0"]
        if funny_info["audio_codec"] == "aac":
            command += ["-c:a", "copy"]
        else:
            command += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    else:
        command += ["-an"]
    command += ["-t", f"{duration:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination)]
    return command


def compose_gameplay_command(board_track, hook_png, cta_png, mask_png, layout, duration, destination):
    """The FFmpeg command for the gameplay-only format: the board track (already cropped to the board) over a blurred,
    darkened enlargement of itself filling the frame, the hook card above the board and the demo card below it. No
    audio at all: gameplay sound is never mapped and no music is added."""
    W, H = layout["width"], layout["height"]
    board, cta, hook = layout["board"], layout["cta"], layout["hook"]
    graph = ";".join([
        f"[0:v]fps={FPS},setsar=1,split=2[bsrc][bbg]",
        f"[bbg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"gblur=sigma={36 * layout['scale']:.1f},eq=brightness=-0.30:saturation=0.6,setsar=1[bg]",
        "[bsrc]format=yuva420p[bsq]",
        "[3:v]format=gray[mask]",
        "[bsq][mask]alphamerge[board]",
        f"[bg][board]overlay=x={board['x']}:y={board['y']}[p1]",
        f"[p1][1:v]overlay=x={hook['x']}:y={hook['y']}[p2]",
        f"[p2][2:v]overlay=x={cta['x']}:y={cta['y']},format=yuv420p[v]",
    ])
    return ["ffmpeg", "-v", "error", "-y", "-stream_loop", "-1", "-i", str(board_track),
            "-loop", "1", "-framerate", str(FPS), "-i", str(hook_png),
            "-loop", "1", "-framerate", str(FPS), "-i", str(cta_png),
            "-loop", "1", "-framerate", str(FPS), "-i", str(mask_png),
            "-filter_complex", graph, "-map", "[v]", "-an",
            "-t", f"{duration:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-profile:v", "high", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination)]


# ================================================================================================ validation

def _pcm(path, seconds, rate=8000):
    import numpy as np
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-t", f"{seconds:.3f}", "-map", "0:a:0",
                             "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"], capture_output=True, check=False)
    if result.returncode != 0:
        return None
    return np.frombuffer(result.stdout, dtype=np.int16).astype(np.float64)


def audio_match(output, funny, seconds, rate=8000, max_lag=0.12):
    """How exactly the output's audio is the funny clip's audio: best correlation over small lags, and the residual
    energy left after removing the funny clip (gameplay sound or music mixed in would leave a large residual)."""
    import numpy as np
    seconds = min(seconds, 60.0)
    a, b = _pcm(output, seconds, rate), _pcm(funny, seconds, rate)
    if a is None or b is None:
        return {"ok": False, "detail": "audio could not be decoded"}
    n = min(len(a), len(b))
    if n < rate:
        return {"ok": False, "detail": f"only {n / rate:.2f}s of audio"}
    a, b = a[:n], b[:n]
    if np.sqrt(np.mean(b ** 2)) < 30:          # a silent source track: the output must be silent too
        loud = float(np.sqrt(np.mean(a ** 2)))
        return {"ok": loud < 60, "correlation": None, "residual": None,
                "detail": f"source audio is silent; output RMS {loud:.0f}"}
    best = (-2.0, 0)
    window = slice(int(0.15 * n), int(0.85 * n))
    for lag in range(-int(max_lag * rate), int(max_lag * rate) + 1, 4):
        shifted = np.roll(b, lag)[window]
        corr = float(np.dot(a[window], shifted) / (np.linalg.norm(a[window]) * np.linalg.norm(shifted) + 1e-9))
        if corr > best[0]:
            best = (corr, lag)
    for lag in range(best[1] - 4, best[1] + 5):
        shifted = np.roll(b, lag)[window]
        corr = float(np.dot(a[window], shifted) / (np.linalg.norm(a[window]) * np.linalg.norm(shifted) + 1e-9))
        if corr > best[0]:
            best = (corr, lag)
    shifted = np.roll(b, best[1])[window]
    gain = float(np.dot(a[window], shifted) / (np.dot(shifted, shifted) + 1e-9))
    residual = float(np.linalg.norm(a[window] - gain * shifted) / (np.linalg.norm(a[window]) + 1e-9))
    ok = best[0] >= 0.95 and residual <= 0.30 and 0.7 <= gain <= 1.4
    return {"ok": ok, "correlation": round(best[0], 4), "residual": round(residual, 4), "gain": round(gain, 3),
            "lag_ms": round(best[1] * 1000 / rate, 1),
            "detail": f"correlation {best[0]:.3f}, residual {residual:.3f}, gain {gain:.2f}"}


def board_visibility(output, layout, duration, samples=6):
    """Find the board circle inside the board area of the FINISHED video: it must be there, whole and large."""
    board = layout["board"]
    size = (tb.ANALYSIS_SIZE, tb.ANALYSIS_SIZE)
    frames = tb.read_gray_frames(output, tb.sample_times(0.0, duration, samples), size,
                                 crop=(board["x"], board["y"], board["size"], board["size"]))
    try:
        found = tb.detect_in_frames(frames)
    except tb.BoardError as error:
        return {"ok": False, "detail": f"no board circle found in the board area: {error}"}
    diameter_share = 2 * found["r"] / size[0]
    showing = sum(1 for v in found["frame_support"] if v >= tb.FRAME_SUPPORT)
    margin_px = min(found["cx"] - found["r"], found["cy"] - found["r"], size[0] - found["cx"] - found["r"],
                    size[1] - found["cy"] - found["r"]) * board["size"] / size[0]
    ok = (found["outside_share"] == 0.0 and diameter_share >= 0.82 and showing >= math.ceil(0.75 * len(frames))
          and margin_px >= 2)
    return {"ok": ok, "diameter_px": round(2 * found["r"] * board["size"] / size[0]),
            "diameter_share_of_width": round(2 * found["r"] * board["size"] / size[0] / layout["width"], 3),
            "fills_board_area": round(diameter_share, 3), "edge_margin_px": round(margin_px, 1),
            "frames_showing": showing, "frames_sampled": len(frames), "rim_support": found["support"],
            "detail": (f"board circle {2 * found['r'] * board['size'] / size[0]:.0f}px wide "
                       f"({diameter_share:.0%} of its area), {showing}/{len(frames)} frames, "
                       f"{'fully visible' if found['outside_share'] == 0 else 'CUT OFF'}, margin {margin_px:.0f}px")}


def cta_visibility(output, layout, at, card_png, box=None):
    """A card (the demo card, or the hook card with box=layout["hook"]) is really in the finished video, where the
    layout says, and readable: the pixels where the card's letters are drawn must be bright and the card's own panel
    around them dark (text-to-panel contrast)."""
    import numpy as np
    from PIL import Image
    cta = box or layout["cta"]
    result = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", str(output), "-frames:v", "1",
                             "-vf", f"crop={cta['w']}:{cta['h']}:{cta['x']}:{cta['y']},format=gray",
                             "-f", "rawvideo", "-"], capture_output=True, check=False)
    if len(result.stdout) != cta["w"] * cta["h"]:
        return {"ok": False, "detail": "could not read the demo card area"}
    shown = np.frombuffer(result.stdout, dtype=np.uint8).astype(np.float64).reshape(cta["h"], cta["w"])
    with Image.open(card_png) as opened:
        card = np.asarray(opened.convert("RGBA"), dtype=np.float64)
    luma = card[..., :3] @ np.array([0.299, 0.587, 0.114])
    opaque = card[..., 3] > 200
    # Letters (white and cyan) vs the card's dark panel, away from the border and the letters' anti-aliased edges.
    text = opaque & (luma > 150)
    edge = max(6, cta["h"] // 10)
    inner = np.zeros_like(opaque)
    inner[edge:-edge, edge:-edge] = True
    panel = opaque & (luma < 40) & inner
    if text.sum() < 200 or panel.sum() < 1000:
        return {"ok": False, "detail": "the demo card has no letters to check"}
    text_level, panel_level = float(shown[text].mean()), float(shown[panel].mean())
    contrast = text_level - panel_level
    ok = text_level >= 150 and panel_level <= 70 and contrast >= 110
    return {"ok": ok, "text_level": round(text_level, 1), "panel_level": round(panel_level, 1),
            "contrast": round(contrast, 1),
            "detail": f"letters {text_level:.0f}/255 on a {panel_level:.0f}/255 panel (contrast {contrast:.0f})"}


def validate_output(output, funny, funny_info, layout, duration, cta_render, hook_render=None):
    """Every check on the finished file. Returns {"ok": bool, "checks": {name: {"ok", "detail", ...}}}.
    For the gameplay format pass funny=None, funny_info=None and the hook card's render: the video must then be
    silent and both cards must be readable. Nothing is relaxed for either format."""
    checks = {}
    gameplay = funny_info is None

    def add(name, ok, detail, **extra):
        checks[name] = {"ok": bool(ok), "detail": detail, **extra}

    max_seconds = (MAX_GAMEPLAY_SECONDS if gameplay else MAX_FUNNY_SECONDS) + 1
    problems = tools.validate_media(output, max_video_bytes=MAX_OUTPUT_BYTES, max_seconds=max_seconds)
    add("media", not problems, "; ".join(problems) or "H.264 yuv420p, decodes, picture has contrast and motion")
    try:
        info = probe_clip(output)
    except TikTokError as error:
        add("probe", False, str(error))
        return {"ok": False, "checks": checks}
    add("resolution", (info["width"], info["height"]) == (layout["width"], layout["height"]),
        f"{info['width']}x{info['height']} (expected {layout['width']}x{layout['height']})")
    add("duration", abs(info["duration"] - duration) <= 0.25,
        f"{info['duration']:.2f}s ({'planned gameplay' if gameplay else 'funny clip'} {duration:.2f}s)")
    decoded = decode_problems(output)
    add("full_decode", not decoded, "; ".join(decoded) or "every frame and audio packet decodes")

    if gameplay:
        add("audio_tracks", info["audio_streams"] == 0,
            f"{info['audio_streams']} audio track(s); expected 0 (gameplay-only video: no gameplay sound, no music)")
    else:
        expected_audio = 1 if funny_info["has_audio"] else 0
        add("audio_tracks", info["audio_streams"] == expected_audio,
            f"{info['audio_streams']} audio track(s); expected {expected_audio} (the funny clip's own, nothing else)")
        if funny_info["has_audio"] and info["audio_streams"] == 1:
            match = audio_match(output, funny, duration)
            add("audio_is_funny_clip", match.pop("ok"), match.pop("detail"), **match)
        elif not funny_info["has_audio"]:
            add("audio_is_funny_clip", info["audio_streams"] == 0, "the funny clip has no audio, so the video is silent (no music added)")

    expected_layout = "gameplay" if gameplay else "funny"
    problems = layout_problems(layout)
    if layout.get("format", "funny") != expected_layout:
        problems.append(f"a {layout.get('format')} layout was used for a {expected_layout} video")
    add("layout", not problems, "; ".join(problems) or "cards and board inside TikTok's safe area, cards clear of the board")
    card = cta_problems(cta_render, layout["cta"]["w"], layout["cta"]["h"])
    add("cta_text_fits", not card, "; ".join(card) or "both lines fit inside the card")
    shown = cta_visibility(output, layout, min(1.0, duration / 2), cta_render["path"])
    add("cta_visible", shown.pop("ok"), shown.pop("detail"), **shown)
    if gameplay:
        if hook_render is None or "hook" not in layout:
            add("hook_visible", False, "the gameplay video has no hook card")
        else:
            hook = cta_problems(hook_render, layout["hook"]["w"], layout["hook"]["h"], HOOK_LINES)
            add("hook_text_fits", not hook, "; ".join(hook) or "both lines fit inside the hook card")
            shown = cta_visibility(output, layout, min(1.0, duration / 2), hook_render["path"], box=layout["hook"])
            add("hook_visible", shown.pop("ok"), shown.pop("detail"), **shown)
    board = board_visibility(output, layout, duration)
    add("board_visible", board.pop("ok"), board.pop("detail"), **board)
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks}


# ================================================================================================ metadata

HASHTAGS = "#ringshift #puzzlegame #indiegame #gamedev #brainteaser"


def caption_suggestion(segments, video_format="funny"):
    """A short caption to paste into TikTok. Only facts from this video: the mechanics come from the game's own level
    data for live captures; no URL (the link lives in the profile bio)."""
    labels = []
    for segment in segments:
        for label in segment.get("labels") or []:
            if label not in labels:
                labels.append(label)
    mechanics = f" This one has {', '.join(labels[:3])}." if labels else ""
    lead = "Ringshift:" if video_format == "gameplay" else "Meanwhile, in Ringshift:"
    return (f"{lead} rotate the rings until every beam lights its lock.{mechanics} "
            f"Play the demo (link in bio). {HASHTAGS}")


def gameplay_ids(segments):
    """What each gameplay piece shows, comparable across runs: a live level in a theme, or a curated recording."""
    ids = []
    for segment in segments:
        if segment.get("source") == "live":
            ids.append(f"live:{segment.get('scene')}:{segment.get('theme')}")
        else:
            ids.append(f"curated:{segment.get('sha256')}")
    return sorted(set(ids))


def fingerprint(video_format, funny_sha, segments):
    """The content of a video, as one string: two videos with the same fingerprint show the same thing."""
    return f"{video_format}|{funny_sha or '-'}|{','.join(gameplay_ids(segments))}"


def output_stem(funny, now, video_format="funny", content=None):
    if video_format == "gameplay":
        digest = hashlib.sha256((content or "").encode()).hexdigest()[:8]
        return f"ringshift-tiktok-{now:%Y%m%d}-gameplay-{digest}"
    slug = re.sub(r"[^a-z0-9]+", "-", Path(funny["file"]).stem.lower()).strip("-")[:32] or "clip"
    reuse = f"-reuse{funny['reuse']['use']}" if funny.get("reuse") else ""
    return f"ringshift-tiktok-{now:%Y%m%d}-{slug}-{funny['sha256'][:8]}{reuse}"


FUNNY_FACTS = ("duration", "width", "height", "rotation", "bytes", "video_codec", "has_audio", "audio_codec",
               "audio_channels", "audio_rate")


def build_metadata(*, status, now, funny, segments, gameplay_source, fallback, looped, layout, cta_render, output=None,
                   output_info=None, validation=None, caption=None, error=None, gameplay_report=None,
                   video_format="funny", format_reason=None, content=None):
    run_id, attempt = os.environ.get("GITHUB_RUN_ID"), os.environ.get("GITHUB_RUN_ATTEMPT")
    data = {
        "schema": SCHEMA, "status": status, "format": video_format, "format_reason": format_reason,
        "fingerprint": content, "created_at": now.isoformat(timespec="seconds"),
        "run_id": run_id, "run_attempt": attempt,
        "funny": ({"file": funny["file"], "sha256": funny["sha256"], "reuse": funny.get("reuse"),
                   **{k: funny["info"][k] for k in FUNNY_FACTS}} if funny else None),
        "gameplay": {
            "source": gameplay_source, "fallback_reason": fallback, "muted": True, "looped": looped,
            "segments": [{k: v for k, v in s.items() if k != "path"} for s in segments],
            "report": gameplay_report or [],
        },
        "audio_policy": ("silent: gameplay audio is never mapped and no music is added (add a sound in the TikTok app)"
                         if video_format == "gameplay" else
                         "only the funny clip's own audio track is mapped; gameplay audio is never mapped; no music"),
        "layout": layout,
        "cta": {"lines": list(CTA_LINES), "box": layout["cta"], "font": cta_render.get("font") if cta_render else None,
                "note": "no URL in the video; the demo link is whatever is in the profile bio"},
        "hook": {"lines": list(HOOK_LINES), "box": layout["hook"]} if layout.get("hook") else None,
        "caption_suggestion": caption,
        "publishing": {"mode": "manual", "note": "Download the artifact and upload it in the TikTok app. Nothing was posted."},
    }
    if output is not None and output_info is not None:
        data["output"] = {"file": Path(output).name, "sha256": sha256_file(output), "bytes": output_info["bytes"],
                          "width": output_info["width"], "height": output_info["height"], "duration": output_info["duration"],
                          "fps": FPS, "video_codec": output_info["video_codec"], "audio_tracks": output_info["audio_streams"],
                          "audio_codec": output_info["audio_codec"]}
    if validation is not None:
        data["validation"] = validation
    if error:
        data["error"] = error
    return data


# ================================================================================================ the run

def make_video(*, state, out_dir, funny_dir=FUNNY_DIR, gameplay_dir=GAMEPLAY_DIR, game_dir=None, gameplay="auto",
               wanted=None, allow_reuse=False, width=1080, rng=None, now=None, log=print, live_planner=None,
               curated_planner=None, video_format="auto", check=None):
    """Make one video. Returns (status, metadata, paths) with status 'produced', 'skipped', 'already-done' or
    'failed'. Never leaves a video file behind unless every check passed.

    video_format: "auto" (a new funny clip, else a reusable one, else gameplay-only), "funny" (never gameplay-only)
    or "gameplay" (always gameplay-only). See plan_format."""
    now = now or utc_now()
    rng = rng or random.Random()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    live_planner = live_planner or plan_live
    curated_planner = curated_planner or plan_curated

    done = ts.video_for_run(state, os.environ.get("GITHUB_RUN_ID"))
    if done and wanted is None:
        return "already-done", {"status": "already-done", "previous": done,
                                "why": f"this workflow run already made {done.get('output_name')} (a re-run makes no second video)"}, {}

    plan = plan_format(state, video_format, funny_dir, wanted=wanted, allow_reuse=allow_reuse, now=now, check=check)
    if plan["format"] is None:
        return "skipped", {"status": "skipped", "format": None, "why": plan["why"], "funny_report": plan["report"]}, {}
    fmt, funny = plan["format"], plan["funny"]
    log(f"Format: {fmt} - {plan['why']}")

    if fmt == "funny":
        log(f"Funny clip: {funny['file']} ({funny['info']['duration']:.1f}s, {funny['info']['width']}x{funny['info']['height']}, "
            f"{'audio ' + str(funny['info']['audio_codec']) if funny['info']['has_audio'] else 'no audio'})")
        need = funny["info"]["duration"]
        layout = plan_layout(funny["info"]["width"], funny["info"]["height"], width, even(width * 16 / 9))
    else:
        need = GAMEPLAY_TARGET_SECONDS
        layout = plan_gameplay_layout(width, even(width * 16 / 9))
    problems = layout_problems(layout)
    if problems:
        raise TikTokError("layout: " + "; ".join(problems))
    reuse = (funny or {}).get("reuse") or {}

    segments, gameplay_report, fallback, source = [], [], None, None
    cta_render = hook_render = None
    duration, looped, content, paths = need, False, None, {}

    def failed(error, extra=None):
        stem = output_stem(funny, now, fmt, content or f"{fmt}|{now.isoformat()}")
        paths_ = {"metadata": out_dir / f"{stem}.json"}
        (out_dir / f"{stem}.mp4").unlink(missing_ok=True)
        metadata = build_metadata(status="failed", now=now, funny=funny, segments=segments, gameplay_source=source,
                                  fallback=fallback, looped=False, layout=layout, cta_render=cta_render or {},
                                  error=f"{type(error).__name__}: {error}"[:800], gameplay_report=gameplay_report,
                                  video_format=fmt, format_reason=plan["why"], content=content)
        metadata.update(extra or {})
        write_json(paths_["metadata"], metadata)
        return "failed", metadata, paths_

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        try:
            if gameplay in ("auto", "live"):
                if game_dir and Path(game_dir).is_dir():
                    try:
                        segments, gameplay_report, _ = live_planner(need, state, game_dir, work, rng=rng, log=log,
                                                                    avoid_scenes=reuse.get("avoid_scenes") or ())
                        source = "live"
                    except Exception as error:      # any capture problem: fall back (auto) or fail (live)
                        fallback = f"live capture failed: {str(error)[:300]}"
                        gameplay_report = [{"outcome": "live capture failed", "why": str(error)[:300]}]
                        if gameplay == "live":
                            raise TikTokError(fallback) from error
                        log(f"  {fallback}; using curated recordings")
                else:
                    fallback = "the game repository is not checked out here"
                    if gameplay == "live":
                        raise TikTokError(fallback)
            if not segments and gameplay in ("auto", "curated"):
                segments, curated_report = curated_planner(need, state, gameplay_dir, log=log,
                                                           avoid=reuse.get("avoid_gameplay") or ())
                gameplay_report += curated_report
                source = "curated"
            if not segments:                # no gameplay at all is a fault to fix, never a quiet green day
                raise GameplayUnavailable("no valid gameplay: no live capture and no curated recording with a whole, "
                                          "measurable board")

            total = sum(s["duration"] for s in segments)
            if fmt == "gameplay":
                if total < MIN_GAMEPLAY_SECONDS:
                    raise TikTokError(f"only {total:.1f}s of usable gameplay (a gameplay video needs {MIN_GAMEPLAY_SECONDS:.0f}s)")
                duration = round(min(total, MAX_GAMEPLAY_SECONDS), 3)
            looped = total + 0.05 < duration

            content = fingerprint(fmt, (funny or {}).get("sha256"), segments)
            earlier = ts.fingerprints(state).get(content)
            if earlier:
                raise DuplicateVideo(f"this video would show exactly what {earlier.get('output_name')} showed "
                                     f"({str(earlier.get('at', '?'))[:10]}); nothing was made")

            stem = output_stem(funny, now, fmt, content)
            paths = {"video": out_dir / f"{stem}.mp4", "metadata": out_dir / f"{stem}.json",
                     "preview": out_dir / f"{stem}_preview.jpg"}
            board_track = work / "board.mp4"
            render_board_track(segments, layout["board"]["size"], board_track, work)
            cta_render = render_cta(layout["cta"]["w"], layout["cta"]["h"], work / "cta.png", layout["scale"])
            card = cta_problems(cta_render, layout["cta"]["w"], layout["cta"]["h"])
            if card:
                raise TikTokError("demo card: " + "; ".join(card))
            if fmt == "gameplay":
                hook_render = render_cta(layout["hook"]["w"], layout["hook"]["h"], work / "hook.png", layout["scale"],
                                         lines=HOOK_LINES)
                card = cta_problems(hook_render, layout["hook"]["w"], layout["hook"]["h"], HOOK_LINES)
                if card:
                    raise TikTokError("hook card: " + "; ".join(card))
            log(f"Composing {fmt} video {layout['width']}x{layout['height']}, {duration:.1f}s, board {layout['board']['size']}px, "
                f"gameplay {source}{' (looped)' if looped else ''} ...")
            mask = render_board_mask(layout["board"]["size"], work / "board_mask.png")
            if fmt == "gameplay":
                command = compose_gameplay_command(board_track, hook_render["path"], cta_render["path"], mask["path"],
                                                   layout, duration, paths["video"])
            else:
                command = compose_command(funny["path"], funny["info"], board_track, cta_render["path"], mask["path"],
                                          layout, duration, paths["video"])
            _ffmpeg(command, "composing the video")
            validation = validate_output(paths["video"], funny["path"] if funny else None, funny["info"] if funny else None,
                                         layout, duration, cta_render, hook_render=hook_render)
            output_info = probe_clip(paths["video"])
        except DuplicateVideo as error:
            return failed(error, {"duplicate": True})
        except GameplayUnavailable as error:
            return failed(error, {"gameplay_unavailable": True})
        except Exception as error:
            if paths.get("video"):
                paths["video"].unlink(missing_ok=True)
            return failed(error)

    caption = caption_suggestion(segments, fmt)
    status = "produced" if validation["ok"] else "failed"
    if status == "produced":
        _ffmpeg(["ffmpeg", "-v", "error", "-y", "-ss", f"{min(duration / 2, 2.0):.2f}", "-i", str(paths["video"]),
                 "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "3", str(paths["preview"])], "saving a preview frame")
    metadata = build_metadata(status=status, now=now, funny=funny, segments=segments, gameplay_source=source,
                              fallback=fallback, looped=looped, layout=layout, cta_render=cta_render,
                              output=paths["video"], output_info=output_info, validation=validation, caption=caption,
                              gameplay_report=gameplay_report, video_format=fmt, format_reason=plan["why"],
                              content=content)
    if status != "produced":
        failed_checks = [f"{name}: {item['detail']}" for name, item in validation["checks"].items() if not item["ok"]]
        metadata["error"] = "validation failed: " + "; ".join(failed_checks)
        paths["video"].unlink(missing_ok=True)       # a video that failed a check is never handed out
        write_json(paths["metadata"], metadata)
        return "failed", metadata, {"metadata": paths["metadata"]}
    write_json(paths["metadata"], metadata)
    return "produced", metadata, paths


def skip_reason(report):
    if not report:
        return "no funny clips in media/funny/ yet: upload an MP4 or MOV there"
    counts = {}
    for row in report:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    parts = [f"{count} {status}" for status, count in sorted(counts.items())]
    return "no eligible funny clip (" + ", ".join(parts) + ")"


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


# ================================================================================================ reporting + CLI

def summary_markdown(status, metadata, artifact=None, state_saved=None):
    if status == "produced":
        out, funny, checks = metadata["output"], metadata.get("funny"), metadata["validation"]["checks"]
        board = checks["board_visible"]
        fmt = metadata.get("format", "funny")
        lines = [
            "### TikTok video: READY (upload it by hand; nothing was posted)", "",
            f"- **Download:** artifact `{artifact}`" if artifact else f"- **File:** `{out['file']}`",
            f"- **Format:** {fmt} - {metadata.get('format_reason') or ''}",
            f"- **Video:** {out['width']}x{out['height']}, {out['duration']:.1f}s, {out['bytes'] / 1048576:.1f} MB",
        ]
        if funny:
            reuse = funny.get("reuse")
            lines.append(f"- **Funny clip:** `{funny['file']}` (audio: "
                         f"{'kept, ' + str(funny.get('audio_codec')) if funny.get('has_audio') else 'none in the clip'})"
                         + (f" - reuse {reuse['use']} of at most {MAX_FUNNY_USES}" if reuse else ""))
        else:
            lines.append("- **Sound:** none (gameplay-only video; add a sound in the TikTok app)")
        lines += [
            f"- **Gameplay:** {metadata['gameplay']['source']} ({len(metadata['gameplay']['segments'])} piece(s), muted"
            f"{', looped' if metadata['gameplay']['looped'] else ''})"
            + (f" - fallback: {metadata['gameplay']['fallback_reason']}" if metadata['gameplay'].get('fallback_reason') else ""),
            f"- **Board:** {board['detail']}",
            f"- **Caption suggestion:** {metadata['caption_suggestion']}",
            "", "| Check | Result |", "|---|---|",
        ]
        lines += [f"| {name} | {'pass' if c['ok'] else 'FAIL'}: {c['detail']} |" for name, c in checks.items()]
        if state_saved is False:
            lines += ["", "State was NOT saved (run was not on main), so this clip stays eligible."]
        return "\n".join(lines) + "\n"
    if status == "skipped":
        lines = ["### TikTok video: SKIPPED", "", metadata.get("why", ""), ""]
        lines += _funny_report_lines(metadata.get("funny_report") or [])
        for row in metadata.get("gameplay_report") or []:
            lines.append(f"- gameplay `{row.get('file') or row.get('scene', '?')}`: {row['outcome']} - {row.get('why', '')}")
        return "\n".join(lines) + "\n"
    if status == "already-done":
        return f"### TikTok video: already made by this run\n\n{metadata.get('why')}\n"
    lines = ["### TikTok video: FAILED (no video was produced)", "", f"`{metadata.get('error', '?')}`", ""]
    if metadata.get("format"):
        lines.append(f"- format: {metadata['format']} - {metadata.get('format_reason') or ''}")
    for name, check in ((metadata.get("validation") or {}).get("checks") or {}).items():
        lines.append(f"- {name}: {'pass' if check['ok'] else 'FAIL'} - {check['detail']}")
    for row in (metadata.get("gameplay") or {}).get("report") or metadata.get("gameplay_report") or []:
        lines.append(f"- gameplay `{row.get('file') or row.get('scene', '?')}`: {row['outcome']} - {row.get('why', '')}")
    return "\n".join(lines) + "\n"


def _funny_report_lines(report):
    lines = []
    for row in report:
        line = f"- `{row['file']}`: {row['status']}" + (f" - {row['why']}" if row.get("why") else "")
        if row.get("reuse"):
            line += f"; {row['reuse']}"
        lines.append(line)
    return lines


def write_outputs(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def _resolve_funny_arg(value, funny_dir):
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = (BASE_DIR / path) if (BASE_DIR / path).exists() else (Path(funny_dir) / path)
    if path.resolve().parent != Path(funny_dir).resolve():
        raise TikTokError(f"--funny-file must be a file directly in {rel(funny_dir)}/ (got {value!r})")
    return path


def _cmd_status(args):
    """What the next `make` with the same --format would make, and why. Outputs eligible=true when it would make a
    video (with auto that is always, because the gameplay format needs no funny clip) and plan=<format>."""
    state = ts.load(args.state)
    plan = plan_format(state, args.format, args.funny_dir)
    report = plan["report"]
    if not report and args.format != "gameplay":
        print("No funny clips in media/funny/ yet.")
    for line in _funny_report_lines(report):
        print(line[2:])
    print(f"\n{len(ts.produced(state))} video(s) made so far.")
    print(f"Next video: {plan['format'] or 'none'} - {plan['why']}")
    write_summary(f"### TikTok plan: {plan['format'] or 'nothing to make'}\n\n{plan['why']}\n\n"
                  + "\n".join(_funny_report_lines(report)) + "\n")
    write_outputs({"eligible": "true" if plan["format"] else "false", "plan": plan["format"] or "none"})
    return 0


def _cmd_make(args):
    state = ts.load(args.state)
    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    try:
        wanted = _resolve_funny_arg(args.funny_file, args.funny_dir)
        status, metadata, paths = make_video(
            state=state, out_dir=args.out_dir, funny_dir=args.funny_dir, gameplay_dir=args.gameplay_dir,
            game_dir=args.game_dir, gameplay=args.gameplay, wanted=wanted, allow_reuse=args.allow_reuse,
            width=args.width, rng=rng, video_format=args.format)
    except TikTokError as error:
        status, metadata, paths = "failed", {"status": "failed", "error": str(error)}, {}
    except Exception as error:      # never end without a status: an unexpected error is a failed run, not a success
        status, metadata, paths = "failed", {"status": "failed", "error": f"{type(error).__name__}: {error}"}, {}

    artifact = Path(paths["video"]).stem if status == "produced" else ""
    funny = metadata.get("funny") or {}
    if status == "produced":
        segments = metadata["gameplay"]["segments"]
        ts.record_video(state, {
            "format": metadata.get("format", "funny"), "funny_file": funny.get("file"), "funny_sha256": funny.get("sha256"),
            "reuse": bool(funny.get("reuse")), "fingerprint": metadata.get("fingerprint"),
            "output_name": artifact, "output_sha256": metadata["output"]["sha256"], "run_id": metadata["run_id"],
            "gameplay_source": metadata["gameplay"]["source"],
            "gameplay_sha256": [s["sha256"] for s in segments if s.get("source") == "curated"],
            "scenes": [{"scene": s["scene"], "labels": s.get("labels"), "theme": s.get("theme")} for s in segments if s.get("scene")],
            "at": metadata["created_at"]})
        ts.save(state, args.state)
    elif status == "failed" and funny.get("sha256") and not (metadata.get("duplicate") or metadata.get("gameplay_unavailable")):
        # Only a failure that may be the clip's fault counts against it (3 strikes and it is no longer picked).
        ts.record_failure(state, funny["sha256"], funny["file"], metadata.get("error", "?"), os.environ.get("GITHUB_RUN_ID"))
        ts.save(state, args.state)

    write_summary(summary_markdown(status, metadata, artifact or None))
    write_outputs({"status": status, "format": metadata.get("format") or "", "artifact_name": artifact,
                   "video_file": str(paths.get("video", "")), "metadata_file": str(paths.get("metadata", "")),
                   "preview_file": str(paths.get("preview", ""))})
    if status == "produced":
        print(f"\nVIDEO READY ({metadata.get('format')}): {paths['video']}\nMetadata: {paths['metadata']}\n"
              f"Caption: {metadata['caption_suggestion']}")
        return 0
    if status in ("skipped", "already-done"):
        print(f"\n{status.upper()}: {metadata.get('why')}")
        return 0
    print(f"\nFAILED, no video produced: {metadata.get('error')}", file=sys.stderr)
    for name, check in ((metadata.get("validation") or {}).get("checks") or {}).items():
        if not check["ok"]:
            print(f"  {name}: {check['detail']}", file=sys.stderr)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Make a TikTok video: a funny clip on top, muted Ringshift gameplay "
                                                 "below; or a gameplay-only video when no funny clip is due.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--state", default=str(ts.STATE_FILE), help="state file (default data/tiktok_state.json)")
        p.add_argument("--funny-dir", default=str(FUNNY_DIR))
        p.add_argument("--format", choices=FORMATS, default="auto",
                       help="auto = new funny clip, else a reusable one, else gameplay-only; funny = never "
                            "gameplay-only; gameplay = always gameplay-only")

    p_status = sub.add_parser("status", help="say what the next video will be and why")
    common(p_status)
    p_status.set_defaults(func=_cmd_status)

    p_make = sub.add_parser("make", help="make one video")
    common(p_make)
    p_make.add_argument("--gameplay-dir", default=str(GAMEPLAY_DIR))
    p_make.add_argument("--game-dir", default=None, help="checkout of the ringshift-game repository (enables live gameplay)")
    p_make.add_argument("--gameplay", choices=("auto", "live", "curated"), default="auto",
                        help="auto = live capture if possible, else curated recordings")
    p_make.add_argument("--funny-file", default=None, help="use this clip from media/funny/ instead of the next one")
    p_make.add_argument("--allow-reuse", action="store_true", help="allow a clip that was already made into a video")
    p_make.add_argument("--out-dir", default=str(OUTPUT_DIR))
    p_make.add_argument("--width", type=int, default=1080, choices=(720, 1080), help="output width (9:16)")
    p_make.add_argument("--seed", type=int, default=None)
    p_make.set_defaults(func=_cmd_make)

    args = parser.parse_args(argv)
    if not tools.have_ffmpeg():
        print("error: ffmpeg and ffprobe are required", file=sys.stderr)
        write_outputs({"status": "failed", "eligible": "false"})
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
