"""Capture fresh, genuine Ringshift gameplay with a real browser.

What it does
    1. Opens YOUR copy of the game (a local .html file) in headless Chromium.
    2. Finds a solution for a level using the game's own beam tracer.
    3. Plays that solution through the game's real keyboard controls
       (arrow keys select and rotate rings) while recording the screen.
    4. Only keeps the clip if the game itself reports the level as won.
    5. Encodes an X-ready MP4 plus screenshots, validates them, and records
       tags/descriptions in media/media_tags.json and the scene in
       data/capture_history.json so the same level is not captured twice.

The game file is NOT part of this repository. Pass it with --game.

    python scripts/capture_gameplay.py list    --game "C:\\path\\to\\ringshift.html"
    python scripts/capture_gameplay.py plan    --game "C:\\path\\to\\ringshift.html" --count 3
    python scripts/capture_gameplay.py capture --game "C:\\path\\to\\ringshift.html" --count 3

Needs: pip install playwright && playwright install chromium, and FFmpeg on PATH.
"""

import argparse
import base64
import hashlib
import json
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import media_tools as tools

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = BASE_DIR / "media"
TAGS_FILE = MEDIA_DIR / "media_tags.json"
HISTORY_FILE = BASE_DIR / "data" / "capture_history.json"
DEFAULT_OUT_DIR = MEDIA_DIR / "captured"

# Modes with the standard "light every required lock" win rule. The special
# modes (meteor, rocket, orchestra) have timers and random hazards, so a
# solution found from the level data alone would not be reliable.
STANDARD_MODES = ("easy", "normal", "hard")

VIEWPORT = {"width": 540, "height": 960}   # a phone-shaped window, the game's own layout
STILL_SCALE = 2                            # screenshots are 1080x1920
MIN_MOVES = 3                              # skip trivial levels (nothing to watch)
MAX_MOVES = 16                             # keep clips short

# Pacing in milliseconds: slow enough that a viewer can follow each rotation.
INTRO_HOLD_MS = 1600
SELECT_MS = 380
ROTATE_MS = 430
RING_PAUSE_MS = 250
WIN_SHOT_MS = 600                          # BREACHED banner is up; take the 'solved' screenshot
WIN_HOLD_MS = 1300                         # total time the BREACHED banner stays in the clip

# Game gimmick labels (as shown in the game's HUD chips) -> topic tags used by the media selector.
LABEL_TO_TAGS = {
    "mirror": ["mirror"],
    "portal": ["black hole"],
    "echo": ["satellite"],
    "block": ["block"],
    "half-block": ["half-block", "block"],
    "prism": ["prism"],
    "amplifier": ["amplifier"],
}

THEME_CLASS_RE = re.compile(r"\btheme-([a-z0-9]+)\b")

REQUIRED_GLOBALS = (
    "LEVELS", "gstate", "goGame", "traceAllBeams", "gimmickKindsInLevel",
    "GIMMICK_LABELS", "rewardForLevel", "ensureRewardShape", "save", "persistSave",
)


class CaptureError(RuntimeError):
    pass


# ---------------------------------------------------------------- pure helpers

def scene_id(mode, index):
    return f"{mode}:{index + 1}"


def tags_for_labels(labels):
    tags = []
    for label in labels:
        for tag in LABEL_TO_TAGS.get(str(label).lower(), []):
            if tag not in tags:
                tags.append(tag)
    if len(labels) >= 4:
        tags.append("mechanism")
    return tags


def theme_from_classes(class_names):
    """The world theme the game applied (e.g. 'ice'), or None for the default space look."""
    for theme in THEME_CLASS_RE.findall(class_names or ""):
        if theme not in ("space",):
            return theme
    return None


def describe_scene(scene, solution, theme, kind):
    """A factual description generated from real level data, never from imagination."""
    labels = scene.get("labels") or []
    parts = [f"Level {scene['index'] + 1} ({scene['mode'].title()})"]
    if labels:
        parts.append("with " + ", ".join(labels) + (" gimmicks" if len(labels) > 1 else " gimmick"))
    if theme:
        parts.append(f"in the '{theme}' visual theme")
    sentence = " ".join(parts) + "."

    if kind == "video":
        sentence += (
            f" Real in-game play: the solution is entered with the keyboard in {solution['cost']} moves"
            " and the clip ends on the BREACHED level-complete screen."
        )
    elif kind == "solved":
        sentence += " The level has just been solved and the BREACHED screen is showing."
    else:
        sentence += " The puzzle at its starting position; no locks solved yet."

    return sentence


def choose_scenes(scenes, history, count, rng=None, min_moves=MIN_MOVES, max_moves=MAX_MOVES, force=False):
    """Pick `count` scenes that have not been captured, favouring gimmicks not recently shown.

    `scenes` need: id, labels, solution_cost (None when unsolvable).
    `history` is the list stored in data/capture_history.json."""
    rng = rng or random.Random()
    captured = {entry["scene"] for entry in history if entry.get("scene")}
    recent_labels = []
    for entry in history[-12:]:
        recent_labels.extend(entry.get("labels", []))

    eligible = []
    for scene in scenes:
        cost = scene.get("solution_cost")
        if cost is None or cost < min_moves or cost > max_moves:
            continue
        if scene["id"] in captured and not force:
            continue
        eligible.append(scene)

    chosen = []
    seen_labels = list(recent_labels)

    while eligible and len(chosen) < count:
        def score(scene):
            fresh = sum(1 for label in set(scene["labels"]) if label not in seen_labels)
            variety = len(set(scene["labels"]))
            return (fresh * 10 + variety + rng.random())

        best = max(eligible, key=score)
        chosen.append(best)
        seen_labels.extend(best["labels"])
        eligible.remove(best)

    return chosen


def clip_filename(scene, kind, when=None):
    when = when or datetime.now(timezone.utc)
    slug = "-".join(re.sub(r"[^a-z0-9]+", "", label.lower()) for label in scene["labels"][:3]) or "plain"
    return f"rs_{scene['mode']}_L{scene['index'] + 1:02d}_{slug}_{kind}_{when:%Y%m%d}"


def game_fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ----------------------------------------------------------------- browser side

SOLVE_JS = """([mode, idx, withBonus])=>{
  const lvl = LEVELS[mode][idx];
  const n = lvl.slots, R = lvl.rings.length;
  const bonus = lvl.bonusLockSlots || [];
  const need = withBonus ? lvl.lockSlots.concat(bonus) : lvl.lockSlots;
  const like = {slots:n, rings:lvl.rings, lockSlots:[...new Set(lvl.lockSlots.concat(bonus))]};
  const init = lvl.rings.map(r=>r.initialRotation||0);
  const start = traceAllBeams(like, init, undefined, undefined).litLocks;
  const sols = [];
  const rots = new Array(R).fill(0);
  (function rec(r){
    if(r===R){
      const lit = traceAllBeams(like, rots.slice(), undefined, undefined).litLocks;
      if(need.every(s=>lit.has(s))){
        const deltas = []; let cost = 0;
        for(let i=0;i<R;i++){ let d=((rots[i]-init[i])%n+n)%n; if(d>n/2) d-=n; deltas.push(d); cost+=Math.abs(d); }
        sols.push({rots:rots.slice(), deltas, cost});
      }
      return;
    }
    for(let v=0; v<n; v++){ rots[r]=v; rec(r+1); }
  })(0);
  sols.sort((a,b)=>a.cost-b.cost);
  return {count: sols.length, best: sols[0] || null, startSolved: need.every(s=>start.has(s)), hasBonus: bonus.length>0};
}"""

SCENES_JS = """()=>{
  const out = [];
  for(const mode of %s){
    (LEVELS[mode]||[]).forEach((lvl, index)=>{
      let kinds = [];
      try{ kinds = gimmickKindsInLevel(lvl); }catch(e){}
      out.push({mode, index, name: lvl.name, slots: lvl.slots, rings: lvl.rings.length,
                locks: lvl.lockSlots.length, bonus: (lvl.bonusLockSlots||[]).length,
                labels: kinds.map(k=>GIMMICK_LABELS[k]||String(k))});
    });
  }
  return out;
}"""

PREPARE_PROFILE_JS = """()=>{
  // A throwaway capture profile (fresh browser context): no tutorial, every gimmick already
  // "seen" and every gift box already claimed, so no toast or reward pop-up covers the puzzle.
  ensureRewardShape();
  for(const mode of %s){
    LEVELS[mode].forEach((lvl, i)=>{ const r = rewardForLevel(mode, i); if(r) save.rewards.claimed[r.lvKey] = r.id || 'capture-profile'; });
  }
  save.seenGimmicks = Object.keys(GIMMICK_LABELS);
  save.tutorialSeen = true;
  persistSave();
  return true;
}"""

HIDE_DEV_UI_CSS = "#dgBtn,.dgBtn{display:none !important}"

# While recording ONE level the game must not wander off by itself. The game calls goGame() from a
# timer 1.5s after a win ("auto-advance"); this lets only the capture tool's own calls through.
AUTO_ADVANCE_GUARD_JS = """()=>{
  if(window.__origGoGame) return true;
  window.__origGoGame = goGame;
  window.goGame = function(mode, idx){ if(window.__allowGo) return window.__origGoGame(mode, idx); };
  return true;
}"""


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise CaptureError(
            "Playwright is not installed. Run:\n"
            "    pip install playwright\n    playwright install chromium"
        ) from error
    return sync_playwright


class GameSession:
    """One headless browser with the game loaded. Use as a context manager."""

    def __init__(self, game_path, headed=False):
        self.game_path = Path(game_path).resolve()
        self.headed = headed
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.cdp = None

    def __enter__(self):
        if not self.game_path.is_file():
            raise CaptureError(f"game file not found: {self.game_path}")

        self._pw = _import_playwright()().start()
        self.browser = self._pw.chromium.launch(headless=not self.headed,
                                                 args=["--autoplay-policy=no-user-gesture-required"])
        self.context = self.browser.new_context(
            viewport=VIEWPORT, device_scale_factor=STILL_SCALE, is_mobile=False)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.game_path.as_uri())
        self.page.wait_for_timeout(900)
        self.page.add_style_tag(content=HIDE_DEV_UI_CSS)

        missing = self.page.evaluate(
            "(names)=>names.filter(n=>{try{return typeof eval(n)==='undefined'}catch(e){return true}})",
            list(REQUIRED_GLOBALS))
        if missing:
            raise CaptureError(
                "This game build does not expose what the capture needs: " + ", ".join(missing)
                + ". Use a Ringshift build that has LEVELS, gstate, goGame and traceAllBeams "
                "(v0.26 does), or update REQUIRED_GLOBALS in capture_gameplay.py.")

        self.page.evaluate(PREPARE_PROFILE_JS % json.dumps(list(STANDARD_MODES)))
        self.page.evaluate(AUTO_ADVANCE_GUARD_JS)
        return self

    def __exit__(self, *exc):
        for closer in (lambda: self.context and self.context.close(),
                       lambda: self.browser and self.browser.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass

    # -- queries
    def scenes(self):
        scenes = self.page.evaluate(SCENES_JS % json.dumps(list(STANDARD_MODES)))
        for scene in scenes:
            scene["id"] = scene_id(scene["mode"], scene["index"])
        return scenes

    def solve(self, mode, index):
        """Best solution that also lights the bonus lock, else required locks only."""
        full = self.page.evaluate(SOLVE_JS, [mode, index, True])
        if full["best"] and not full["startSolved"]:
            return {**full["best"], "bonus": True, "solutions": full["count"]}
        basic = self.page.evaluate(SOLVE_JS, [mode, index, False])
        if basic["best"] and not basic["startSolved"]:
            return {**basic["best"], "bonus": False, "solutions": basic["count"]}
        return None

    # -- playing
    def start_level(self, mode, index):
        self.page.evaluate(
            "([m,i])=>{ window.__allowGo = true; try{ window.__origGoGame(m, i); } finally { window.__allowGo = false; } }",
            [mode, index])
        self.page.wait_for_timeout(300)

    def press(self, key, wait_ms):
        self.page.keyboard.press(key)
        self.page.wait_for_timeout(wait_ms)

    def play(self, solution, frames_sink=None):
        """Enter the solution with the real arrow-key controls. Returns True if the game says it was won."""
        selected = self.page.evaluate("gstate.selectedRing")

        for ring, delta in enumerate(solution["deltas"]):
            if delta == 0:
                continue
            while selected < ring:
                self.press("ArrowDown", SELECT_MS)
                selected += 1
            while selected > ring:
                self.press("ArrowUp", SELECT_MS)
                selected -= 1
            for _ in range(abs(delta)):
                self.press("ArrowRight" if delta > 0 else "ArrowLeft", ROTATE_MS)
            self.page.wait_for_timeout(RING_PAUSE_MS)

        if not self.page.evaluate("gstate.won"):
            if self.page.evaluate("gstate.nextUnlocked") and not solution["bonus"]:
                # Required locks are lit but this level has no reachable bonus: use the game's NEXT button.
                self.page.wait_for_timeout(700)
                self.page.click("#nextBtn")
            self.page.wait_for_timeout(250)

        return bool(self.page.evaluate("gstate.won"))

    def state(self):
        return self.page.evaluate("""()=>({
            won: !!gstate.won, moves: gstate.moves, classes: document.body.className,
            lit: [...gstate.litLocks], required: gstate.level.lockSlots })""")

    # -- recording
    def start_recording(self):
        cdp = self.cdp = self.context.new_cdp_session(self.page)
        frames = self.frames = []

        def on_frame(event):
            # Frames can still arrive after stop_recording() has cleared self.cdp, so this handler
            # only uses its own captured session and list, and ignores a session that is gone.
            if self.cdp is not cdp:
                return
            frames.append((float(event["metadata"]["timestamp"]), base64.b64decode(event["data"])))
            try:
                cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
            except Exception:
                pass

        cdp.on("Page.screencastFrame", on_frame)
        cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 92,
            "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"], "everyNthFrame": 1})

    def stop_recording(self):
        try:
            self.cdp.send("Page.stopScreencast")
        finally:
            self.cdp = None
        frames, self.frames = self.frames, []
        return frames

    def screenshot(self, path):
        self.page.screenshot(path=str(path))


# ------------------------------------------------------------------- orchestration

def capture_scene(session, scene, out_dir, *, kinds=("video", "screenshot"), aspect="4:5", zoom=None,
                  now=None, log=print):
    """Capture one scene. Returns a list of result dicts (one per file kept); raises CaptureError on failure."""
    now = now or datetime.now(timezone.utc)
    solution = session.solve(scene["mode"], scene["index"])

    if not solution:
        raise CaptureError(f"{scene['id']}: no solution found with the game's tracer")

    session.start_level(scene["mode"], scene["index"])
    session.page.wait_for_timeout(500)
    classes_at_start = session.state()["classes"]
    theme = theme_from_classes(classes_at_start)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = clip_filename(scene, "clip", now)
    results = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        want_video = "video" in kinds
        if want_video:
            session.start_recording()

        session.page.wait_for_timeout(INTRO_HOLD_MS)

        start_still = tmp / "start.png"
        if "screenshot" in kinds:
            session.screenshot(start_still)

        won = session.play(solution)

        frames = []
        solved_still = tmp / "solved.png"
        if won:
            session.page.wait_for_timeout(WIN_SHOT_MS)
            if "screenshot" in kinds:
                session.screenshot(solved_still)
            session.page.wait_for_timeout(max(WIN_HOLD_MS - WIN_SHOT_MS, 0))

        if want_video:
            frames = session.stop_recording()

        final = session.state()

        if not won or not final["won"]:
            raise CaptureError(f"{scene['id']}: the game did not report a win after playing the solution; clip discarded")

        # Make the game idle again so the next scene starts clean.
        session.page.evaluate("()=>{ if(typeof stopGameTimers==='function') stopGameTimers(); }")

        if want_video:
            if len(frames) < 10:
                raise CaptureError(f"{scene['id']}: the browser produced only {len(frames)} frames")
            frame_files = []
            t0 = frames[0][0]
            for index, (stamp, data) in enumerate(frames):
                path = tmp / f"frame_{index:05d}.jpg"
                path.write_bytes(data)
                frame_files.append((stamp - t0, path))

            video_path = out_dir / (stem + ".mp4")
            info = tools.encode_frames(frame_files, video_path, aspect=aspect, zoom=zoom, hold_last=0.4)
            problems = tools.validate_media(video_path)

            if problems:
                video_path.unlink(missing_ok=True)
                raise CaptureError(f"{scene['id']}: video failed validation: " + "; ".join(problems))

            results.append({"path": video_path, "type": "video", "kind": "video", "info": info})
            log(f"  video  {video_path.name}  {info['width']}x{info['height']}  {info['duration']:.1f}s  {info['bytes'] / 1048576:.1f} MB")

        for label, still, kind_name in (("start", start_still, "puzzle"), ("solved", solved_still, "solved")):
            if "screenshot" not in kinds or not still.exists():
                continue
            image_path = out_dir / (clip_filename(scene, kind_name, now) + ".jpg")
            info = tools.convert_image(still, image_path, aspect=aspect)
            problems = tools.validate_media(image_path)
            if problems:
                image_path.unlink(missing_ok=True)
                log(f"  skipped {kind_name} screenshot: " + "; ".join(problems))
                continue
            results.append({"path": image_path, "type": "image", "kind": kind_name, "info": info})
            log(f"  image  {image_path.name}  {info['width']}x{info['height']}  {info['bytes'] / 1048576:.1f} MB")

    for item in results:
        item.update({"scene": scene, "solution": solution, "theme": theme, "moves": final["moves"]})

    return results


def record_results(results, game_path, tags_file=TAGS_FILE, history_file=HISTORY_FILE, base_dir=BASE_DIR, now=None):
    """Write metadata for kept files into media_tags.json and the scene into capture_history.json."""
    now = now or datetime.now(timezone.utc)
    tags = read_json(tags_file, {})
    if not isinstance(tags, dict):
        tags = {}
    history = read_json(history_file, [])
    if not isinstance(history, list):
        history = []

    build = game_fingerprint(game_path)
    files = []

    for item in results:
        scene = item["scene"]
        try:
            relative = item["path"].resolve().relative_to(Path(base_dir).resolve()).as_posix()
        except ValueError:
            relative = item["path"].name

        description_kind = "video" if item["type"] == "video" else item["kind"]
        entry = {
            "tags": tags_for_labels(scene["labels"]),
            "description": describe_scene(scene, item["solution"], item["theme"], description_kind),
            "type": item["type"],
            "width": item["info"]["width"],
            "height": item["info"]["height"],
            "source": "capture_gameplay.py",
            "scene": scene["id"],
            "game_build": build,
            "captured_at": now.isoformat(timespec="seconds"),
        }
        if item["type"] == "video":
            entry["duration"] = round(item["info"]["duration"], 1)
        if item["theme"]:
            entry["theme"] = item["theme"]

        tags[relative] = entry
        files.append(relative)

    if results:
        scene = results[0]["scene"]
        history.append({
            "scene": scene["id"], "labels": scene["labels"], "files": files,
            "game_build": build, "captured_at": now.isoformat(timespec="seconds"),
        })

    write_json(tags_file, tags)
    write_json(history_file, history)
    return files


# ------------------------------------------------------------------------- CLI

def _annotate(session, scenes):
    for scene in scenes:
        solution = session.solve(scene["mode"], scene["index"])
        scene["solution_cost"] = solution["cost"] if solution else None
        scene["solution_bonus"] = bool(solution and solution["bonus"])
    return scenes


def _cmd_list(args):
    history = read_json(HISTORY_FILE, [])
    captured = {entry["scene"] for entry in history}
    with GameSession(args.game, headed=args.headed) as session:
        scenes = _annotate(session, session.scenes())
    print(f"{'scene':10s} {'rings':>5s} {'moves':>5s}  {'captured':8s} gimmicks")
    for scene in scenes:
        cost = scene["solution_cost"]
        print(f"{scene['id']:10s} {scene['rings']:>5d} {str(cost) if cost is not None else '-':>5s}  "
              f"{'yes' if scene['id'] in captured else '':8s} {', '.join(scene['labels'])}")
    usable = [s for s in scenes if s["solution_cost"] and MIN_MOVES <= s["solution_cost"] <= MAX_MOVES]
    print(f"\n{len(scenes)} levels, {len(usable)} suitable, {len([s for s in usable if s['id'] in captured])} already captured")
    return 0


def _pick(session, args):
    history = read_json(HISTORY_FILE, [])
    scenes = _annotate(session, session.scenes())

    if args.scene:
        wanted = {s.strip() for s in args.scene}
        chosen = [s for s in scenes if s["id"] in wanted]
        missing = wanted - {s["id"] for s in chosen}
        if missing:
            raise CaptureError("unknown scene(s): " + ", ".join(sorted(missing)))
        return chosen

    return choose_scenes(scenes, history, args.count, force=args.force)


def _cmd_plan(args):
    with GameSession(args.game, headed=args.headed) as session:
        chosen = _pick(session, args)
    if not chosen:
        print("Nothing left to capture: every suitable level has been captured (use --force to repeat).")
        return 0
    for scene in chosen:
        print(f"{scene['id']:10s} {scene['solution_cost']:>2d} moves  {', '.join(scene['labels'])}")
    return 0


def _capture_with_retry(args, scene, kinds, attempts=2, log=print):
    """Capture one scene in its own fresh browser. An unexpected browser/driver error
    (the browser can be killed on a busy machine) is retried once; a game-reported
    failure (no win) is final."""
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            with GameSession(args.game, headed=args.headed) as session:
                results = capture_scene(session, scene, Path(args.out), kinds=kinds,
                                        aspect=args.aspect, zoom=args.zoom, log=log)
                return results, list(session.errors)
        except CaptureError:
            raise
        except Exception as error:  # Playwright/driver/browser failures
            last_error = error
            log(f"  attempt {attempt}/{attempts} hit an unexpected error: {type(error).__name__}: {str(error)[:160]}")

    raise CaptureError(f"{scene['id']}: browser failed {attempts} times ({last_error})")


def _cmd_capture(args):
    kinds = {"video": ("video",), "screenshot": ("screenshot",), "both": ("video", "screenshot")}[args.kind]
    kept, failed, page_errors = [], [], []

    # One short session just to list levels, solve them and choose scenes.
    with GameSession(args.game, headed=args.headed) as planner:
        chosen = _pick(planner, args)

    if not chosen:
        print("Nothing left to capture: every suitable level has been captured (use --force to repeat).")
        return 0

    for scene in chosen:
        print(f"Capturing {scene['id']} ({', '.join(scene['labels'])}) ...")
        try:
            results, errors = _capture_with_retry(args, scene, kinds)
        except CaptureError as error:
            print(f"  FAILED: {error}")
            failed.append(scene["id"])
            continue

        page_errors.extend(errors)

        if args.dry_run:
            for item in results:
                item["path"].unlink(missing_ok=True)
            print("  dry run: files discarded, nothing recorded")
            continue

        kept.extend(record_results(results, args.game))

    if page_errors:
        print(f"Note: the game logged {len(page_errors)} page error(s); first: {page_errors[0][:160]}")

    print(f"\nKept {len(kept)} file(s); {len(failed)} scene(s) failed." + (" Failed: " + ", ".join(failed) if failed else ""))
    if kept:
        print("Review the files, then commit them (or publish them with scripts/media_store.py).")
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Capture genuine Ringshift gameplay from your local game file.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--game", required=True, help="path to your Ringshift .html file")
        p.add_argument("--headed", action="store_true", help="show the browser window")

    p_list = sub.add_parser("list", help="list levels, their move counts and capture status")
    common(p_list)
    p_list.set_defaults(func=_cmd_list)

    for name, helptext, func in (("plan", "show which scenes would be captured", _cmd_plan),
                                 ("capture", "capture clips and screenshots", _cmd_capture)):
        p = sub.add_parser(name, help=helptext)
        common(p)
        p.add_argument("--count", type=int, default=3, help="how many levels (default 3)")
        p.add_argument("--scene", action="append", help="capture a specific level, e.g. hard:11 (repeatable)")
        p.add_argument("--force", action="store_true", help="allow levels that were captured before")
        if name == "capture":
            p.add_argument("--kind", choices=("video", "screenshot", "both"), default="both")
            p.add_argument("--aspect", choices=sorted(tools.ASPECTS), default="4:5")
            p.add_argument("--zoom", type=float, default=None, help="optional slow push-in, e.g. 1.05")
            p.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="output folder (default media/captured)")
            p.add_argument("--dry-run", action="store_true", help="capture and validate, then discard everything")
        p.set_defaults(func=func)

    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except (CaptureError, tools.MediaToolError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
