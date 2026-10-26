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

    python scripts/capture_gameplay.py doctor  --game "C:\\path\\to\\ringshift.html"
    python scripts/capture_gameplay.py list    --game "C:\\path\\to\\ringshift.html"
    python scripts/capture_gameplay.py plan    --game "C:\\path\\to\\ringshift.html" --count 3
    python scripts/capture_gameplay.py capture --game "C:\\path\\to\\ringshift.html" --count 3

Needs: pip install playwright && playwright install chromium, and FFmpeg on PATH.
"""

import argparse
import base64
import hashlib
import json
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import media_manifest as manifest_tools
import media_tools as tools

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = BASE_DIR / "media"
TAGS_FILE = MEDIA_DIR / "media_tags.json"
HISTORY_FILE = BASE_DIR / "data" / "capture_history.json"
DEFAULT_OUT_DIR = MEDIA_DIR / "captured"
PREVIEW_DIR = BASE_DIR / "data" / "capture_preview"      # contact sheet + report; NOT under media/ so it is never posted

# Modes with the standard "light every required lock" win rule. The special
# modes (meteor, rocket, orchestra) have timers and random hazards, so a
# solution found from the level data alone would not be reliable.
STANDARD_MODES = ("easy", "normal", "hard")

VIEWPORT = {"width": 540, "height": 960}   # a phone-shaped window, the game's own layout
STILL_SCALE = 2                            # screenshots are 1080x1920

# Measured here (Chromium 153): the screencast always delivers CSS-pixel frames (540x960 for this window) however large
# maxWidth is, and a native 1080x1920 window makes the game switch to a different, desktop-style layout. So clips are
# recorded at 540 wide and upscaled 2x, while screenshots are rendered at 2x and stay sharp.
# The browser window used for each output shape. Portrait shapes use the phone-shaped window (4:5 and 1:1 are
# cropped from it); 16:9 uses a landscape window so the game's own responsive layout is recorded, not a thin strip.
LAYOUTS = {
    "4:5":  {"viewport": VIEWPORT, "scale": STILL_SCALE, "cast_width": VIEWPORT["width"]},
    "1:1":  {"viewport": VIEWPORT, "scale": STILL_SCALE, "cast_width": VIEWPORT["width"]},
    "9:16": {"viewport": VIEWPORT, "scale": STILL_SCALE, "cast_width": VIEWPORT["width"]},
    "16:9": {"viewport": {"width": 1280, "height": 720}, "scale": 2, "cast_width": 1280},
}
DEFAULT_ASPECT = "4:5"

MIN_MOVES = 3                              # skip trivial levels (nothing to watch)
MAX_MOVES = 16                             # keep clips short
SCENE_TIMEOUT_S = 150                      # wall-clock limit for playing one level (encoding is separate)
PAGE_TIMEOUT_MS = 15000                    # any single browser operation
TEASER_FRACTION = 0.5                      # a teaser plays about this share of the solution, then stops
DUPLICATE_FRACTION = 0.06                  # two captures whose perceptual hashes differ in <= 6% of bits are the same picture
TEASER_HOLD_MS = 1200                      # how long a teaser holds on its last (unsolved) position

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
    elif kind == "teaser":
        sentence += (
            f" Real in-game play: the first {solution.get('played', '?')} of {solution['cost']} rotation moves of a solution"
            " are entered with the keyboard. The clip stops before the level is solved, so no BREACHED screen is shown."
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


def clip_filename(scene, kind, when=None, aspect=DEFAULT_ASPECT):
    """File stem. The default 4:5 shape keeps the original name; other shapes add a suffix (e.g. _9x16) so one
    scene captured in two shapes on the same day never overwrites itself."""
    when = when or datetime.now(timezone.utc)
    slug = "-".join(re.sub(r"[^a-z0-9]+", "", label.lower()) for label in scene["labels"][:3]) or "plain"
    shape = "" if aspect == DEFAULT_ASPECT else "_" + aspect.replace(":", "x")
    return f"rs_{scene['mode']}_L{scene['index'] + 1:02d}_{slug}_{kind}_{when:%Y%m%d}{shape}"


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

    def __init__(self, game_path, headed=False, aspect=DEFAULT_ASPECT):
        if aspect not in LAYOUTS:
            raise CaptureError(f"unknown aspect {aspect!r}; choose from {sorted(LAYOUTS)}")
        self.game_path = Path(game_path).resolve()
        self.headed = headed
        self.aspect = aspect
        self.layout = LAYOUTS[aspect]
        self.deadline = None
        self.errors = []
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.cdp = None

    def __enter__(self):
        if not self.game_path.is_file():
            raise CaptureError(f"game file not found: {self.game_path}")

        self._pw = _import_playwright()().start()
        try:
            self.browser = self._pw.chromium.launch(headless=not self.headed,
                                                     args=["--autoplay-policy=no-user-gesture-required"])
        except Exception as error:
            self.__exit__()
            raise CaptureError(
                "Chromium could not be started (" + str(error).strip().splitlines()[0][:160] + "). "
                "Install it with:  python -m playwright install chromium") from error
        self.context = self.browser.new_context(
            viewport=self.layout["viewport"], device_scale_factor=self.layout["scale"], is_mobile=False)
        self.page = self.context.new_page()
        self.page.set_default_timeout(PAGE_TIMEOUT_MS)
        self.page.set_default_navigation_timeout(PAGE_TIMEOUT_MS * 2)
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

    def begin_scene(self, limit_seconds=SCENE_TIMEOUT_S):
        """Start the wall-clock limit for one level. Playing past it raises CaptureError instead of hanging."""
        self.deadline = time.monotonic() + limit_seconds

    def _check_deadline(self):
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise CaptureError("timed out while playing the level (scene time limit reached)")

    def press(self, key, wait_ms):
        self._check_deadline()
        self.page.keyboard.press(key)
        self.page.wait_for_timeout(wait_ms)

    def play(self, solution, stop_after=None):
        """Enter the solution with the real arrow-key controls.

        Returns True if the game says it was won. With `stop_after` (a number of rotation moves) it enters only
        that many rotations and stops; the level must then still be unsolved (it never fakes or forces a win)."""
        selected = self.page.evaluate("gstate.selectedRing")
        rotations = 0

        for ring, delta in enumerate(solution["deltas"]):
            if delta == 0:
                continue
            if stop_after is not None and rotations >= stop_after:
                break
            while selected < ring:
                self.press("ArrowDown", SELECT_MS)
                selected += 1
            while selected > ring:
                self.press("ArrowUp", SELECT_MS)
                selected -= 1
            for _ in range(abs(delta)):
                if stop_after is not None and rotations >= stop_after:
                    break
                self.press("ArrowRight" if delta > 0 else "ArrowLeft", ROTATE_MS)
                rotations += 1
            self.page.wait_for_timeout(RING_PAUSE_MS)

        self.rotations_played = rotations

        if stop_after is not None:
            return bool(self.page.evaluate("gstate.won"))

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
        viewport = self.layout["viewport"]
        factor = self.layout["cast_width"] / viewport["width"]
        cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 92,
            "maxWidth": int(viewport["width"] * factor), "maxHeight": int(viewport["height"] * factor), "everyNthFrame": 1})

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

def capture_scene(session, scene, out_dir, *, kinds=("video", "screenshot"), aspect=None, zoom=None,
                  now=None, log=print, scene_timeout=SCENE_TIMEOUT_S):
    """Capture one scene. Returns a list of result dicts (one per file kept); raises CaptureError on failure.

    kinds: "video" (full solution ending on the BREACHED screen), "screenshot" (puzzle + solved stills) or
    "teaser" (real play of only part of the solution, ending before the level is solved). A teaser is captured in
    its own pass, so a clip never mixes a partial and a complete attempt."""
    now = now or datetime.now(timezone.utc)
    aspect = aspect or session.aspect

    if aspect != session.aspect:
        raise CaptureError(f"this browser session was opened for {session.aspect}, not {aspect}")

    teaser = "teaser" in kinds
    if teaser and set(kinds) != {"teaser"}:
        raise CaptureError("capture 'teaser' in its own pass, not together with 'video' or 'screenshot'")

    solution = session.solve(scene["mode"], scene["index"])

    if not solution:
        raise CaptureError(f"{scene['id']}: no solution found with the game's tracer")

    if teaser and solution["cost"] < 2:
        raise CaptureError(f"{scene['id']}: too few moves for a teaser")

    session.start_level(scene["mode"], scene["index"])
    session.page.wait_for_timeout(500)
    session.begin_scene(scene_timeout)
    classes_at_start = session.state()["classes"]
    theme = theme_from_classes(classes_at_start)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = clip_filename(scene, "teaser" if teaser else "clip", now, aspect)
    results = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        want_video = "video" in kinds or teaser
        if want_video:
            session.start_recording()

        session.page.wait_for_timeout(INTRO_HOLD_MS)

        start_still = tmp / "start.png"
        if "screenshot" in kinds:
            session.screenshot(start_still)

        frames = []
        solved_still = tmp / "solved.png"

        if teaser:
            target = max(1, min(int(solution["cost"] * TEASER_FRACTION), solution["cost"] - 1))
            won = session.play(solution, stop_after=target)
            session.page.wait_for_timeout(TEASER_HOLD_MS)
            frames = session.stop_recording()
            final = session.state()

            if won or final["won"]:
                raise CaptureError(f"{scene['id']}: the level was solved before the teaser stopped; clip discarded")

            solution = {**solution, "played": session.rotations_played}
        else:
            won = session.play(solution)

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

            results.append({"path": video_path, "type": "video", "kind": "teaser" if teaser else "video", "info": info})
            log(f"  {'teaser' if teaser else 'video '} {video_path.name}  {info['width']}x{info['height']}  {info['duration']:.1f}s  {info['bytes'] / 1048576:.1f} MB")

        for label, still, kind_name in (("start", start_still, "puzzle"), ("solved", solved_still, "solved")):
            if "screenshot" not in kinds or not still.exists():
                continue
            image_path = out_dir / (clip_filename(scene, kind_name, now, aspect) + ".jpg")
            info = tools.convert_image(still, image_path, aspect=aspect)
            problems = tools.validate_media(image_path)
            if problems:
                image_path.unlink(missing_ok=True)
                log(f"  skipped {kind_name} screenshot: " + "; ".join(problems))
                continue
            results.append({"path": image_path, "type": "image", "kind": kind_name, "info": info})
            log(f"  image  {image_path.name}  {info['width']}x{info['height']}  {info['bytes'] / 1048576:.1f} MB")

    for item in results:
        item.update({"scene": scene, "solution": solution, "theme": theme, "moves": final["moves"], "aspect": aspect})

    return results


# What each kind of captured file shows. These come from how the file was made, not from guessing.
KIND_FACTS = {
    "video":  {"state": "solved",      "action": True},
    "teaser": {"state": "in_progress", "action": True},
    "puzzle": {"state": "unsolved",    "action": False},
    "solved": {"state": "solved",      "action": False},
}


def find_near_duplicate(manifest, path, media_type, aspect, duration=0.0):
    """The id of an already indexed record that is visually the same picture as `path`, else None."""
    hashes = manifest_tools.fingerprints(path, media_type, duration)
    if not hashes:
        return None
    for existing in manifest.get("items", []):
        if existing.get("type") == media_type and existing.get("aspect") == aspect \
                and manifest_tools.looks_duplicate(existing.get("perceptual_hashes"), hashes, DUPLICATE_FRACTION):
            return existing.get("id")
    return None


def record_results(results, game_path, tags_file=TAGS_FILE, history_file=HISTORY_FILE, base_dir=BASE_DIR, now=None,
                   manifest_file=None):
    """Write metadata for kept files into media_tags.json (compatible with the old selector), the structured
    manifest next to it, and the scene into capture_history.json."""
    now = now or datetime.now(timezone.utc)
    manifest_file = Path(manifest_file) if manifest_file else Path(tags_file).with_name("manifest.json")
    tags = read_json(tags_file, {})
    if not isinstance(tags, dict):
        tags = {}
    history = read_json(history_file, [])
    if not isinstance(history, list):
        history = []
    manifest = manifest_tools.load_manifest(manifest_file)

    build = game_fingerprint(game_path)
    files = []

    for item in results:
        scene = item["scene"]
        try:
            relative = item["path"].resolve().relative_to(Path(base_dir).resolve()).as_posix()
        except ValueError:
            relative = item["path"].name

        facts = KIND_FACTS[item["kind"]]
        description_kind = item["kind"] if item["kind"] == "teaser" else ("video" if item["type"] == "video" else item["kind"])
        description = describe_scene(scene, item["solution"], item["theme"], description_kind)
        entry = {
            "tags": tags_for_labels(scene["labels"]),
            "description": description,
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

        canonical = [label for label in scene["labels"] if label in manifest_tools.game_terms.DISPLAY_ORDER]
        record = manifest_tools.make_item(
            relative, base_dir=base_dir, media_type=item["type"], description=description,
            source="capture_gameplay.py", captured_at=entry["captured_at"], game_build=build,
            mode=scene["mode"], level=scene["index"] + 1, scene=scene["id"], theme=item["theme"],
            mechanisms=canonical, mechanisms_source="game-data",
            puzzle_state=facts["state"], puzzle_state_source="capture-tool", shows_action=facts["action"],
            tags=entry["tags"], verification="verified",
            sources={"mode": "game-data", "level": "game-data", **({"theme": "game-data"} if item["theme"] else {})},
            legacy=entry, method="capture_gameplay.py (real game, real key presses)",
            validation={"status": "ok", "checked_at": now.isoformat(timespec="seconds"), "problems": []})
        record["capture"]["kind"] = item["kind"]
        record["capture"]["page_errors"] = item.get("page_errors")
        record["capture"]["aspect_requested"] = item.get("aspect")
        if item["kind"] == "teaser":
            record["capture"]["rotations_played"] = item["solution"].get("played")
            record["capture"]["rotations_in_solution"] = item["solution"].get("cost")
        manifest_tools.upsert(manifest, record)

    if results:
        scene = results[0]["scene"]
        history.append({
            "scene": scene["id"], "labels": scene["labels"], "files": files,
            "game_build": build, "captured_at": now.isoformat(timespec="seconds"),
        })

    write_json(tags_file, tags)
    write_json(history_file, history)
    manifest_tools.save_manifest(manifest, manifest_file)
    return files


# ------------------------------------------------------------------ dependency check

def _ok(name, ok, detail, required=True):
    return {"name": name, "ok": bool(ok), "detail": detail, "required": required}


def check_dependencies(game=None, out_dir=DEFAULT_OUT_DIR, *, which=shutil.which, run=subprocess.run,
                       import_playwright=_import_playwright, disk_usage=shutil.disk_usage):
    """Everything the capture needs, as a list of {name, ok, detail, required}. Never raises."""
    checks = []

    version = sys.version_info
    checks.append(_ok("Python 3.9+", version >= (3, 9), platform.python_version()))

    playwright_ok = False
    try:
        import_playwright()
        playwright_ok = True
        checks.append(_ok("Playwright (Python package)", True, "importable"))
    except CaptureError:
        checks.append(_ok("Playwright (Python package)", False, "not installed: pip install -r requirements-capture.txt"))

    if playwright_ok:
        try:
            with import_playwright()() as playwright:
                browser = playwright.chromium.launch(headless=True)
                detail = f"Chromium {browser.version} starts"
                browser.close()
            checks.append(_ok("Chromium (headless)", True, detail))
        except Exception as error:
            first = (str(error).strip().splitlines() or [type(error).__name__])[0][:140]
            checks.append(_ok("Chromium (headless)", False, f"{first} -> python -m playwright install chromium"))
    else:
        checks.append(_ok("Chromium (headless)", False, "needs Playwright first"))

    for tool in ("ffmpeg", "ffprobe"):
        location = which(tool)
        checks.append(_ok(tool, location, location or "not on PATH (Windows: winget install Gyan.FFmpeg, then reopen the terminal)"))

    if which("ffmpeg"):
        try:
            encoders = run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, timeout=30).stdout.decode(errors="replace")
            checks.append(_ok("FFmpeg H.264 encoder (libx264)", "libx264" in encoders,
                              "present" if "libx264" in encoders else "this FFmpeg build has no libx264"))
        except Exception as error:
            checks.append(_ok("FFmpeg H.264 encoder (libx264)", False, f"could not ask FFmpeg: {type(error).__name__}"))

    node = which("node")
    checks.append(_ok("Node.js (optional; only for checking level data by hand)", node, node or "not installed", required=False))

    try:
        import PIL  # noqa: F401
        checks.append(_ok("Pillow (optional; contact sheet)", True, "importable", required=False))
    except ImportError:
        checks.append(_ok("Pillow (optional; contact sheet)", False, "not installed; captures still work, no contact sheet (pip install pillow)", required=False))

    try:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".write-test"
        probe.write_text("x")
        probe.unlink()
        free_mb = disk_usage(out).free / 1048576
        checks.append(_ok("Output folder writable", True, f"{out} ({free_mb:,.0f} MB free)"))
        checks.append(_ok("Free disk space (>= 500 MB)", free_mb >= 500, f"{free_mb:,.0f} MB", required=False))
    except OSError as error:
        checks.append(_ok("Output folder writable", False, f"{out_dir}: {error}"))

    if game:
        game_path = Path(game)
        if not game_path.is_file():
            checks.append(_ok("Game file", False, f"not found: {game_path}"))
        else:
            checks.append(_ok("Game file", True, f"{game_path.name}, build id {game_fingerprint(game_path)}"))
            if playwright_ok:
                try:
                    with GameSession(game_path) as session:
                        count = len(session.scenes())
                    checks.append(_ok("Game exposes what capture needs", True, f"loads and lists {count} levels"))
                except Exception as error:
                    checks.append(_ok("Game exposes what capture needs", False, str(error).splitlines()[0][:200]))

    return checks


def _cmd_doctor(args):
    checks = check_dependencies(args.game, args.out)
    for check in checks:
        mark = "ok  " if check["ok"] else ("FAIL" if check["required"] else "note")
        print(f"[{mark}] {check['name']}: {check['detail']}")
    failed = [c for c in checks if c["required"] and not c["ok"]]
    print("\nReady to capture." if not failed else f"\n{len(failed)} required check(s) failed; fix them before capturing.")
    return 1 if failed else 0


# ------------------------------------------------------------------- report and contact sheet

def make_contact_sheet(paths, destination, columns=4, cell=(270, 480)):
    """A grid of thumbnails (a frame from each video) so a whole batch can be reviewed at a glance, e.g. on a
    phone. Returns the path, or None when Pillow is not installed or there is nothing to show."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    paths = [Path(p) for p in paths]
    if not paths:
        return None

    label_height = 18
    thumbnails = []

    with tempfile.TemporaryDirectory() as tmp:
        for index, path in enumerate(paths):
            try:
                source = path
                if path.suffix.lower() in tools.VIDEO_EXTENSIONS:
                    source = tools.make_poster(path, Path(tmp) / f"poster_{index}.png")
                image = Image.open(source).convert("RGB")
                image.thumbnail(cell)
            except Exception:
                continue          # an unreadable file is left out of the sheet, never fatal
            thumbnails.append((path, image))

        if not thumbnails:
            return None

        # Each row is only as tall as its tallest thumbnail, so wide 16:9 pictures do not leave big empty cells.
        rows = [thumbnails[i:i + columns] for i in range(0, len(thumbnails), columns)]
        heights = [max(image.height for _, image in row) + label_height for row in rows]
        sheet = Image.new("RGB", (columns * cell[0], sum(heights)), (16, 18, 24))
        draw = ImageDraw.Draw(sheet)

        y = 0
        for row, height in zip(rows, heights):
            for column, (path, image) in enumerate(row):
                x = column * cell[0] + (cell[0] - image.width) // 2
                sheet.paste(image, (x, y))
                draw.text((column * cell[0] + 4, y + height - label_height + 3), path.stem[-34:], fill=(190, 200, 215))
            y += height

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, quality=85)
    return destination


def write_report(destination, *, game, build, argv, kept_records, failed, duplicates, page_errors, sheet=None, now=None):
    """A short Markdown report of one capture run. States only what happened in this run."""
    now = now or datetime.now(timezone.utc)
    lines = [
        "# Capture report",
        "",
        f"- Run: {now.isoformat(timespec='seconds')}",
        f"- Game build id: `{build}` (`{Path(game).name}`)",
        f"- Command: `capture_gameplay.py {' '.join(argv)}`",
        f"- Files kept: {len(kept_records)}; scenes failed: {len(failed)}; near-duplicates skipped: {len(duplicates)}",
        "",
    ]
    if kept_records:
        lines += ["| File | Type | Size | Seconds | MB | State | Validation |", "|---|---|---|---|---|---|---|"]
        for record in kept_records:
            seconds = record.get("duration")
            lines.append(
                f"| `{record['path']}` | {record['type']} ({record['aspect']}) | {record['width']}x{record['height']} | "
                f"{seconds if seconds is not None else '-'} | {(record.get('bytes') or 0) / 1048576:.2f} | "
                f"{record['puzzle_state']} | {record['validation']['status']} |")
        lines.append("")
    if failed:
        lines += ["## Failed (nothing kept for these)", ""] + [f"- {item}" for item in failed] + [""]
    if duplicates:
        lines += ["## Skipped as near-duplicates of existing media", ""] + [f"- {item}" for item in duplicates] + [""]
    if page_errors:
        lines += [f"## The game logged {len(page_errors)} page error(s)", "", f"First: `{page_errors[0][:200]}`", ""]
    if sheet:
        lines += [f"Contact sheet: `{Path(sheet).name}` (same folder).", ""]
    lines += [
        "## Still unverified",
        "",
        "- A person has not looked at these files yet. Files, win state and mechanics come from the real game, but "
        "framing, readability and the overlapping top-bar buttons at phone size need a human glance.",
        "- Nothing here was posted or queued.",
        "",
    ]
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination


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

    if getattr(args, "mode", None):
        scenes = [s for s in scenes if s["mode"] in args.mode]

    rng = random.Random(args.seed) if getattr(args, "seed", None) is not None else random.Random()
    return choose_scenes(scenes, history, args.count, rng=rng, force=args.force)


def _cmd_plan(args):
    with GameSession(args.game, headed=args.headed) as session:
        chosen = _pick(session, args)
    if not chosen:
        print("Nothing left to capture: every suitable level has been captured (use --force to repeat).")
        return 0
    for scene in chosen:
        print(f"{scene['id']:10s} {scene['solution_cost']:>2d} moves  {', '.join(scene['labels'])}")
    return 0


def _capture_with_retry(args, scene, kinds, aspect=DEFAULT_ASPECT, attempts=2, log=print):
    """Capture one scene in its own fresh browser. An unexpected browser/driver error
    (the browser can be killed on a busy machine) is retried once; a game-reported
    failure (no win, timeout) is final."""
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            with GameSession(args.game, headed=args.headed, aspect=aspect) as session:
                results = capture_scene(session, scene, Path(args.out), kinds=kinds, aspect=aspect, zoom=args.zoom,
                                        log=log, scene_timeout=args.scene_timeout)
                for item in results:
                    item["page_errors"] = len(session.errors)
                return results, list(session.errors)
        except CaptureError:
            raise
        except Exception as error:  # Playwright/driver/browser failures
            last_error = error
            log(f"  attempt {attempt}/{attempts} hit an unexpected error: {type(error).__name__}: {str(error)[:160]}")

    raise CaptureError(f"{scene['id']}: browser failed {attempts} times ({last_error})")


def passes_for(kind):
    """The capture passes (each a tuple of kinds) that `--kind` asks for."""
    full = {"video": ("video",), "screenshot": ("screenshot",), "both": ("video", "screenshot"), "all": ("video", "screenshot")}
    passes = []
    if kind in full:
        passes.append(full[kind])
    if kind in ("teaser", "all"):
        passes.append(("teaser",))
    return passes


def _cmd_capture(args):
    kept_files, kept_records, failed, duplicates, page_errors = [], [], [], [], []
    manifest_file = Path(args.manifest) if args.manifest else Path(TAGS_FILE).with_name("manifest.json")

    # One short session just to list levels, solve them and choose scenes.
    with GameSession(args.game, headed=args.headed) as planner:
        chosen = _pick(planner, args)

    if not chosen:
        print("Nothing left to capture: every suitable level has been captured (use --force to repeat).")
        return 0

    for scene in chosen:
        for aspect in args.aspect or [DEFAULT_ASPECT]:
            for kinds in passes_for(args.kind):
                label = f"{scene['id']} {aspect} {'+'.join(kinds)}"
                print(f"Capturing {label} ({', '.join(scene['labels'])}) ...")
                try:
                    results, errors = _capture_with_retry(args, scene, kinds, aspect)
                except CaptureError as error:
                    print(f"  FAILED: {error}")
                    failed.append(f"{label}: {error}")
                    continue

                page_errors.extend(errors)

                if args.dry_run:
                    for item in results:
                        item["path"].unlink(missing_ok=True)
                    print("  dry run: files discarded, nothing recorded")
                    continue

                manifest = manifest_tools.load_manifest(manifest_file)
                fresh = []
                for item in results:
                    twin = find_near_duplicate(manifest, item["path"], item["type"], aspect, item["info"].get("duration") or 0.0)
                    if twin:
                        item["path"].unlink(missing_ok=True)
                        duplicates.append(f"{item['path'].name} looks the same as existing {twin}")
                        print(f"  skipped near-duplicate of {twin}")
                    else:
                        fresh.append(item)

                if fresh:
                    written = record_results(fresh, args.game, manifest_file=manifest_file)
                    kept_files.extend(written)
                    by_path = manifest_tools.by_path(manifest_tools.load_manifest(manifest_file))
                    kept_records.extend(by_path[w] for w in written if w in by_path)

    if page_errors:
        print(f"Note: the game logged {len(page_errors)} page error(s); first: {page_errors[0][:160]}")

    print(f"\nKept {len(kept_files)} file(s); {len(failed)} pass(es) failed; {len(duplicates)} near-duplicate(s) skipped."
          + (" Failed: " + "; ".join(failed) if failed else ""))

    if kept_files and not args.dry_run:
        sheet = make_contact_sheet([BASE_DIR / f for f in kept_files], Path(args.report_dir) / "contact_sheet.jpg")
        report = write_report(Path(args.report_dir) / "capture_report.md", game=args.game, build=game_fingerprint(args.game),
                              argv=sys.argv[1:], kept_records=kept_records, failed=failed, duplicates=duplicates,
                              page_errors=page_errors, sheet=sheet)
        print(f"Report: {report}" + (f"\nContact sheet: {sheet}" if sheet else "\n(Install Pillow for a contact sheet.)"))
        print("Review the files, then commit them (or publish them with scripts/media_store.py).")

    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Capture genuine Ringshift gameplay from your local game file.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--game", required=True, help="path to your Ringshift .html file")
        p.add_argument("--headed", action="store_true", help="show the browser window")

    p_doctor = sub.add_parser("doctor", help="check Python, Playwright, Chromium, FFmpeg and (optionally) the game file")
    p_doctor.add_argument("--game", help="also check this game file")
    p_doctor.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="output folder to test (default media/captured)")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_list = sub.add_parser("list", help="list levels, their move counts and capture status")
    common(p_list)
    p_list.set_defaults(func=_cmd_list)

    for name, helptext, func in (("plan", "show which scenes would be captured", _cmd_plan),
                                 ("capture", "capture clips and screenshots", _cmd_capture)):
        p = sub.add_parser(name, help=helptext)
        common(p)
        p.add_argument("--count", type=int, default=3, help="how many levels (default 3)")
        p.add_argument("--scene", action="append", help="capture a specific level, e.g. hard:11 (repeatable)")
        p.add_argument("--mode", action="append", choices=STANDARD_MODES, help="only these difficulties (repeatable)")
        p.add_argument("--seed", type=int, default=None, help="make the choice of levels repeatable")
        p.add_argument("--force", action="store_true", help="allow levels that were captured before")
        if name == "capture":
            p.add_argument("--kind", choices=("video", "screenshot", "both", "teaser", "all"), default="both",
                           help="both = full clip + two stills; teaser = real play that stops before the solution; "
                                "all = both + teaser")
            p.add_argument("--aspect", action="append", choices=sorted(tools.ASPECTS),
                           help="output shape, repeatable (default 4:5). 9:16 = vertical, 16:9 = landscape")
            p.add_argument("--zoom", type=float, default=None, help="optional slow push-in, e.g. 1.05")
            p.add_argument("--scene-timeout", type=int, default=SCENE_TIMEOUT_S, help="seconds allowed for playing one level")
            p.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="output folder (default media/captured)")
            p.add_argument("--manifest", default=None, help="manifest file to update (default media/manifest.json)")
            p.add_argument("--report-dir", default=str(PREVIEW_DIR), help="where the report and contact sheet go")
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
