"""Make ONE fresh, genuine piece of Ringshift gameplay media for today's post. Runs unattended in GitHub Actions.

    python scripts/daily_media.py --game-dir game-src            # plan, capture, validate, host, record
    python scripts/daily_media.py --game-dir game-src --dry-run  # plan, capture and validate; upload and record nothing

What happens, in order
1. The game is the real one: the newest `ringshift vX.Y.html` in a checkout of the public game repository
   (branch main, the stable build). Its commit and build id go into the record. No game -> clear failure, no media.
2. The plan: what kind of media (a still most days, a real gameplay clip about every third post, see KIND_CYCLE),
   which level (one the game's own solver can solve, chosen for gimmick variety and not shown recently) and which theme
   (one of the game's own themes, least used so far; a theme that is new in the game is shown first).
3. The capture: capture_gameplay.py plays the level in headless Chromium with real key presses, equips the theme the
   way the Shop does, checks the screen (puzzle screen, whole board visible, no menu/pop-up/toast), and records.
   A clip is only kept if the game itself reports the win; a teaser is only kept if it stopped unsolved.
4. The checks: FFmpeg/ffprobe validation (format, size, duration, decodes, contrast, motion), a still-usability check
   (not blank/flat), and a near-duplicate check against every earlier capture. A failure tries another level (and, for
   a video, finally a still), never a fake or an old file.
5. Hosting: the file goes to the `media-store` branch (scripts/media_store.py, newest 60 kept), and the run waits until
   the public URL serves exactly these bytes.
6. Records: a manifest record (path store/<file>, sha256, level, mechanisms, theme, game commit, capture time),
   the matching media_tags.json entry, data/capture_history.json and data/daily_media.json. Store files that were
   pruned are marked excluded so they are never selected again.

The output for the workflow is the store path of the new file (GITHUB_OUTPUT media_file=...), which the autopilot
pins the post to.
"""

import argparse
import hashlib
import json
import os
import random
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import capture_gameplay as cg
import game_source
import game_terms
import media_manifest as manifest_tools
import media_store
import media_tools as tools

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "daily_media.json"
TAGS_FILE = BASE_DIR / "media" / "media_tags.json"
MANIFEST_FILE = BASE_DIR / "media" / "manifest.json"
HISTORY_FILE = BASE_DIR / "data" / "capture_history.json"
STATE_SCHEMA = "ringshift-daily-media/1"
STATE_LIMIT = 400

# Media kinds over consecutive days: stills most days, a real gameplay video every third post (a full solution or a
# teaser that stops before the solution, alternating). "solved" is a still of the BREACHED screen.
KIND_CYCLE = ("puzzle", "puzzle", "clip", "puzzle", "solved", "teaser")
VIDEO_KINDS = ("clip", "teaser")
KINDS = {
    #          capture pass        stills kept      solution length   pacing
    "puzzle": {"pass": ("screenshot",), "stills": ("puzzle",), "moves": (3, 16), "pace": 1.0},
    "solved": {"pass": ("screenshot",), "stills": ("solved",), "moves": (3, 16), "pace": 1.0},
    "clip":   {"pass": ("video",), "stills": (), "moves": (9, 16), "pace": cg.SOCIAL_PACE},
    "teaser": {"pass": ("teaser",), "stills": (), "moves": (11, 16), "pace": cg.SOCIAL_PACE, "fraction": 0.7},
}
MANIFEST_KIND = {"puzzle": "puzzle", "solved": "solved", "clip": "video", "teaser": "teaser"}
ASPECT = "4:5"                      # X shows 4:5 at full size in the feed; vertical and phone friendly
SCENES_TO_TRY = 4                   # levels tried before giving up on a kind
RECENT_THEMES = 6                   # a theme is not repeated within this many runs when others are available
URL_WAIT_SECONDS = (5, 10, 20, 30, 45, 60)


class DailyMediaError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


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


def load_state(path=STATE_FILE):
    state = read_json(path, None)
    if not isinstance(state, dict) or not isinstance(state.get("runs"), list):
        state = {"schema": STATE_SCHEMA, "known_themes": [], "runs": []}
    state.setdefault("known_themes", [])
    return state


# ------------------------------------------------------------------------------------------- planning (pure)

def captured_runs(state):
    return [run for run in state.get("runs", []) if run.get("status") == "captured"]


def next_kind(state):
    """The media kind for the next post, from how many fresh files were made so far (see KIND_CYCLE)."""
    return KIND_CYCLE[len(captured_runs(state)) % len(KIND_CYCLE)]


def choose_theme(themes, state, rng):
    """(theme dict, reason). Only themes the game lists and a player can get. A theme the game did not have on the
    previous run (for example a theme experiment that has been merged) is shown first; otherwise the least used theme
    that was not shown in the last few runs."""
    usable = [t for t in themes if t.get("assigned", True) and t.get("id")]
    if not usable:
        raise DailyMediaError("the game lists no themes")

    known = set(state.get("known_themes") or [])
    if known:
        new = [t for t in usable if t["id"] not in known]
        if new:
            return new[0], f"new in the game since the last run ({len(new)} new)"

    runs = captured_runs(state)
    counts = Counter(run.get("theme") for run in runs)
    recent = {run.get("theme") for run in runs[-RECENT_THEMES:]}
    pool = [t for t in usable if t["id"] not in recent] or usable
    least = min(counts.get(t["id"], 0) for t in pool)
    pool = [t for t in pool if counts.get(t["id"], 0) == least]
    chosen = rng.choice(pool)
    return chosen, f"least shown theme ({least} earlier post(s)), not in the last {RECENT_THEMES}"


def scene_history(state, capture_history):
    """Earlier scenes, oldest first, from both the capture history and the daily runs (for choose_scenes)."""
    history = [entry for entry in capture_history if isinstance(entry, dict) and entry.get("scene")]
    history += [{"scene": run["scene"], "labels": run.get("labels") or []} for run in captured_runs(state) if run.get("scene")]
    return history


def postable(scene):
    """A level is only usable if it shows at least one mechanic under its knowledge-file name (the caption is written
    from that list, and media without one cannot be matched to a post)."""
    return any(label in game_terms.DISPLAY_ORDER for label in scene.get("labels") or [])


def choose_scenes(scenes, kind, history, rng, count=SCENES_TO_TRY):
    """Up to `count` levels to try for `kind`, best first. Levels never captured come first; once every suitable level
    has been shown, the least recently shown ones are used again (with a different theme they look different)."""
    low, high = KINDS[kind]["moves"]
    eligible = [s for s in scenes if postable(s)]
    fresh = cg.choose_scenes(eligible, history, count, rng=rng, min_moves=low, max_moves=high)
    if len(fresh) >= count:
        return fresh
    last_seen = {}
    for position, entry in enumerate(history):
        last_seen[entry["scene"]] = position
    again = [s for s in eligible if s.get("solution_cost") is not None and low <= s["solution_cost"] <= high
             and s not in fresh]
    again.sort(key=lambda s: (last_seen.get(s["id"], -1), rng.random()))
    return (fresh + again)[:count]


def plan_attempts(kind):
    """The kinds to try, in order. A video that cannot be made falls back to a still (never to a fake video)."""
    return [kind] + (["puzzle"] if kind in VIDEO_KINDS else [])


# ------------------------------------------------------------------------------------------- checks

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def freshness_problems(path, media_type, manifest, duration=0.0):
    """Why a new file is not new: identical bytes or a near-identical picture already in the manifest."""
    digest = sha256_file(path)
    for item in manifest.get("items", []):
        if item.get("sha256") == digest:
            return [f"identical bytes to existing {item.get('path')}"]
    twin = cg.find_near_duplicate(manifest, path, media_type, ASPECT, duration)
    if twin:
        return [f"looks the same as existing {twin}"]
    return []


def media_problems(result):
    """Everything that disqualifies a captured file. Uses the same validator as the upload gate."""
    path = Path(result["path"])
    problems = list(tools.validate_media(path))
    if result["type"] == "image":
        problems += cg.still_problems(path)
    else:
        info = tools.probe(path)
        if info["duration"] < 4.0:
            problems.append(f"clip is only {info['duration']:.1f}s long")
        if not (info["width"] and info["height"]) or info["height"] < info["width"]:
            problems.append(f"clip is not vertical ({info['width']}x{info['height']})")
    return problems


# ------------------------------------------------------------------------------------------- capture

def capture_one(game_path, scene, kind, theme_id, out_dir, *, session_factory=cg.GameSession, log=print):
    """Capture one file of `kind` for `scene` in `theme_id`. Returns the capture_scene result dict or raises."""
    spec = KINDS[kind]
    last_error = None
    for attempt in (1, 2):
        try:
            with session_factory(game_path, aspect=ASPECT) as session:
                results = cg.capture_scene(
                    session, scene, out_dir, kinds=spec["pass"], aspect=ASPECT, theme=theme_id, frame_board=True,
                    pace=spec["pace"], stills=spec["stills"] or ("puzzle", "solved"), teaser_fraction=spec.get("fraction"),
                    log=log)
                wanted = MANIFEST_KIND[kind]
                matching = [r for r in results if r["kind"] == wanted]
                for other in results:
                    if other not in matching:
                        Path(other["path"]).unlink(missing_ok=True)
                if not matching:
                    raise cg.CaptureError(f"{scene['id']}: the capture produced no {wanted} file")
                if session.errors:
                    for item in matching:
                        Path(item["path"]).unlink(missing_ok=True)
                    raise cg.CaptureError(f"{scene['id']}: the game logged {len(session.errors)} page error(s) while "
                                          f"capturing (first: {session.errors[0][:120]}); nothing kept")
                matching[0]["page_errors"] = 0
                return matching[0]
        except cg.CaptureError:
            raise
        except Exception as error:      # browser/driver crash: one retry in a fresh browser
            last_error = error
            log(f"  browser attempt {attempt}/2 failed: {type(error).__name__}: {str(error)[:160]}")
    raise cg.CaptureError(f"{scene['id']}: the browser failed twice ({last_error})")


def make_media(game_path, state, *, rng, kind=None, out_dir, session_factory=cg.GameSession, manifest=None,
               capture_history=None, log=print):
    """Plan and capture one fresh, validated file. Returns a dict describing it plus every attempt; raises
    DailyMediaError (with the attempts) when nothing passed."""
    manifest = manifest if manifest is not None else manifest_tools.load_manifest(MANIFEST_FILE)
    capture_history = capture_history if capture_history is not None else read_json(HISTORY_FILE, [])
    requested = kind or next_kind(state)

    with session_factory(game_path, aspect=ASPECT) as planner:
        scenes = planner.scenes()
        for scene in scenes:
            solution = planner.solve(scene["mode"], scene["index"])
            scene["solution_cost"] = solution["cost"] if solution else None
        themes = planner.themes()

    theme, theme_reason = choose_theme(themes, state, rng)
    history = scene_history(state, capture_history if isinstance(capture_history, list) else [])
    attempts = []

    for attempt_kind in plan_attempts(requested):
        for scene in choose_scenes(scenes, attempt_kind, history, rng):
            label = f"{attempt_kind} {scene['id']} ({', '.join(scene['labels'])}) theme {theme['id']}"
            log(f"Capturing {label} ...")
            try:
                result = capture_one(game_path, scene, attempt_kind, theme["id"], out_dir, session_factory=session_factory, log=log)
            except cg.CaptureError as error:
                attempts.append({"kind": attempt_kind, "scene": scene["id"], "outcome": "capture failed", "why": str(error)})
                log(f"  FAILED: {error}")
                continue

            duration = (result.get("info") or {}).get("duration") or 0.0
            problems = media_problems(result) + freshness_problems(result["path"], result["type"], manifest, duration)
            if problems:
                Path(result["path"]).unlink(missing_ok=True)
                attempts.append({"kind": attempt_kind, "scene": scene["id"], "outcome": "rejected", "why": "; ".join(problems)})
                log("  REJECTED: " + "; ".join(problems))
                continue

            attempts.append({"kind": attempt_kind, "scene": scene["id"], "outcome": "kept"})
            return {"result": result, "kind": attempt_kind, "requested_kind": requested, "scene": scene,
                    "theme": theme, "theme_reason": theme_reason, "themes": [t["id"] for t in themes], "attempts": attempts}

    error = DailyMediaError(f"no fresh {requested} media passed every check ({len(attempts)} attempt(s))")
    error.attempts = attempts
    error.themes = [t["id"] for t in themes]
    raise error


# ------------------------------------------------------------------------------------------- hosting

def wait_for_url(url, sha256, *, get=None, sleep=time.sleep, waits=URL_WAIT_SECONDS):
    """Wait until the public URL serves exactly these bytes (HTTP 200, no redirect). Returns True/False."""
    if get is None:
        import requests
        get = requests.get
    for wait in (0,) + tuple(waits):
        if wait:
            sleep(wait)
        try:
            response = get(url, stream=True, allow_redirects=False, timeout=30)
        except Exception:
            continue
        try:
            if response.status_code != 200:
                continue
            digest = hashlib.sha256()
            for block in response.iter_content(1 << 20):
                digest.update(block)
            if digest.hexdigest() == sha256:
                return True
        finally:
            response.close()
    return False


def prune_hosted(manifest, tags, kept_names):
    """Mark store/ records whose file is no longer on the media-store branch as excluded (manifest AND media_tags
    entry, so the manifest's copy of the entry stays identical). Returns the paths marked."""
    marked = []
    for item in manifest.get("items", []):
        path = str(item.get("path") or "")
        if not path.startswith(media_store.STORE_DIR + "/") or item.get("exclude"):
            continue
        if Path(path).name in kept_names:
            continue
        entry = dict(tags.get(path) or item.get("legacy") or {})
        entry["exclude"] = True
        entry["notes"] = "removed from the media-store branch by retention; not postable"
        tags[path] = entry
        item["legacy"] = entry
        item["exclude"] = True
        item["post_categories"] = manifest_tools.categories_for(item)
        marked.append(path)
    return marked


def host_and_record(made, game, *, now, run_id=None, upload=media_store.publish, list_store=media_store.list_store,
                    wait=wait_for_url, state, base_dir=BASE_DIR, tags_file=TAGS_FILE, manifest_file=MANIFEST_FILE,
                    history_file=HISTORY_FILE, log=print):
    """Upload the file to the media-store branch, wait until it is served, and write every record. Returns the run
    entry (also appended to `state`)."""
    result = made["result"]
    local = Path(result["path"])
    digest = sha256_file(local)

    published = upload([local])
    if len(published) != 1:
        raise DailyMediaError(f"media-store returned {len(published)} file(s) for one upload")
    hosted = published[0]
    relative = f"{media_store.STORE_DIR}/{hosted['name']}"
    log(f"Hosted at {hosted['url']}")

    if not wait(hosted["url"], digest):
        raise DailyMediaError(f"the public URL never served the uploaded bytes: {hosted['url']}")

    entry, record = cg.build_entry_and_record(
        result, relative, game["build"], now=now, base_dir=base_dir, hosted_url=hosted["url"],
        game_commit=game.get("commit"), game_repo=game_source.GAME_REPO, game_file=game.get("file"))
    if record.get("sha256") != digest:
        raise DailyMediaError("the record does not describe the uploaded bytes")

    tags = read_json(tags_file, {})
    tags = tags if isinstance(tags, dict) else {}
    manifest = manifest_tools.load_manifest(manifest_file)
    tags[relative] = entry
    manifest_tools.upsert(manifest, record)

    try:
        kept = {item.get("name") for item in list_store()}
    except Exception as error:      # pruning is housekeeping; a listing failure must not lose today's media
        kept = None
        log(f"note: could not list the media-store branch ({type(error).__name__}); nothing pruned")
    pruned = prune_hosted(manifest, tags, kept) if kept else []

    write_json(tags_file, tags)
    manifest_tools.save_manifest(manifest, manifest_file)

    history = read_json(history_file, [])
    history = history if isinstance(history, list) else []
    scene = made["scene"]
    history.append({"scene": scene["id"], "labels": scene["labels"], "files": [relative], "game_build": game["build"],
                    "game_commit": game.get("commit"), "captured_at": now.isoformat(timespec="seconds")})
    write_json(history_file, history)

    run = {
        "at": now.isoformat(timespec="seconds"), "run_id": run_id, "status": "captured",
        "kind": made["kind"], "requested_kind": made["requested_kind"],
        "scene": scene["id"], "labels": scene["labels"], "theme": made["theme"]["id"], "theme_name": made["theme"].get("name"),
        "file": relative, "url": hosted["url"], "sha256": digest, "type": result["type"],
        "bytes": local.stat().st_size, "duration": (result.get("info") or {}).get("duration"),
        "game_commit": game.get("commit"), "game_build": game["build"], "game_file": game.get("file"),
        "attempts": made["attempts"], "pruned": pruned,
    }
    return run


def append_run(state, run, themes=None):
    state["runs"] = (state.get("runs") or []) + [run]
    state["runs"] = state["runs"][-STATE_LIMIT:]
    if themes:
        state["known_themes"] = sorted(set(state.get("known_themes") or []) | set(themes))
    state["schema"] = STATE_SCHEMA
    return state


# ------------------------------------------------------------------------------------------- reporting

def summary_markdown(run, game):
    if run.get("status") != "captured":
        lines = ["### Fresh gameplay media: NONE", "",
                 f"No new media passed every check, so nothing will be posted from this run. Game `{game.get('file')}` "
                 f"at `{str(game.get('commit') or '?')[:10]}`.", ""]
        for attempt in run.get("attempts") or []:
            lines.append(f"- {attempt['kind']} `{attempt['scene']}`: {attempt['outcome']} - {attempt.get('why', '')}")
        return "\n".join(lines) + "\n"
    seconds = f", {run['duration']:.1f}s" if run.get("duration") else ""
    return "\n".join([
        "### Fresh gameplay media",
        "",
        f"- **File:** `{run['file']}` ({run['type']}, {run['kind']}{seconds}, {run['bytes'] / 1048576:.2f} MB) - [open]({run['url']})",
        f"- **Level:** `{run['scene']}` ({', '.join(run['labels'])}) - **Theme:** {run.get('theme_name')} (`{run['theme']}`)",
        f"- **Game:** `{run['game_file']}` at commit `{str(run.get('game_commit') or '?')[:10]}` (build `{run['game_build']}`)",
        f"- **SHA-256:** `{run['sha256'][:16]}...`",
        f"- **Checks:** FFmpeg decode/size/duration, contrast{', motion' if run['type'] == 'video' else ', still usability'}, "
        "no near-duplicate, public URL serves these bytes",
        *([f"- Requested {run['requested_kind']}, fell back to {run['kind']}"] if run['kind'] != run['requested_kind'] else []),
        "",
    ])


def write_outputs(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")


# ------------------------------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description="Capture one fresh, genuine Ringshift gameplay file for today's post.")
    parser.add_argument("--game-dir", default="game-src", help="checkout of the game repository (default game-src)")
    parser.add_argument("--kind", choices=sorted(KINDS), help="force a media kind (default: the rotation's)")
    parser.add_argument("--seed", type=int, help="make the level/theme choice repeatable")
    parser.add_argument("--dry-run", action="store_true", help="capture and validate only; upload and record nothing")
    parser.add_argument("--out-dir", help="where to keep the captured file (default: a temporary folder)")
    args = parser.parse_args(argv)

    try:
        game = game_source.describe(args.game_dir)
    except game_source.GameSourceError as error:
        print(f"error: {error}", file=sys.stderr)
        write_summary(f"### Fresh gameplay media: NONE\n\nThe game could not be loaded: {error}\n")
        return 2
    print(f"Game: {game['file']} commit {game['commit'] or '(not a git checkout)'} build {game['build']}")

    state = load_state()
    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    now = utc_now()

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(args.out_dir) if args.out_dir else Path(tmp)
        try:
            made = make_media(game["path"], state, rng=rng, kind=args.kind, out_dir=out_dir)
        except (DailyMediaError, cg.CaptureError, tools.MediaToolError) as error:
            run = {"at": now.isoformat(timespec="seconds"), "run_id": os.environ.get("GITHUB_RUN_ID"), "status": "failed",
                   "requested_kind": args.kind or next_kind(state), "why": str(error),
                   "attempts": getattr(error, "attempts", []), "game_commit": game["commit"], "game_build": game["build"]}
            print(f"\nNO FRESH MEDIA: {error}")
            write_summary(summary_markdown(run, game))
            if not args.dry_run:
                write_json(STATE_FILE, append_run(state, run, getattr(error, "themes", None)))
            write_outputs({"media_file": "", "status": "failed"})
            return 3

        result = made["result"]
        print(f"\nKept {Path(result['path']).name}: {made['kind']} of {made['scene']['id']} in {made['theme']['id']} "
              f"({made['theme_reason']})")

        if args.dry_run:
            print("DRY RUN: nothing was uploaded or recorded.")
            write_outputs({"media_file": "", "status": "dry-run"})
            return 0

        try:
            run = host_and_record(made, game, now=now, run_id=os.environ.get("GITHUB_RUN_ID"), state=state)
        except (DailyMediaError, media_store.StoreError, cg.CaptureError) as error:
            print(f"\nNO FRESH MEDIA: hosting failed: {error}")
            run = {"at": now.isoformat(timespec="seconds"), "run_id": os.environ.get("GITHUB_RUN_ID"), "status": "failed",
                   "requested_kind": made["requested_kind"], "why": f"hosting failed: {error}", "attempts": made["attempts"],
                   "game_commit": game["commit"], "game_build": game["build"]}
            write_summary(summary_markdown(run, game))
            write_json(STATE_FILE, append_run(state, run, made["themes"]))
            write_outputs({"media_file": "", "status": "failed"})
            return 3

    write_json(STATE_FILE, append_run(state, run, made["themes"]))
    write_summary(summary_markdown(run, game))
    write_outputs({"media_file": run["file"], "status": "captured", "media_kind": run["kind"], "media_type": run["type"]})
    print(f"FRESH MEDIA READY: {run['file']} -> {run['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
