"""Every two weeks: one experimental Ringshift theme, written in the game's own v2 theme framework, measured against the
39 themes the game already ships, and offered as a pull request on a lab/ branch of the game. Never merged here.

    python scripts/theme_lab.py run --game-dir game-src --out theme-out              # author + validate
    python scripts/theme_lab.py run --game-dir game-src --out theme-out --open-pr    # ...and propose it
    python scripts/theme_lab.py run ... --if-due 13      # do nothing unless the last experiment is 13+ days old

1. A concept is chosen, first from the visual directions listed in RINGSHIFT_KNOWLEDGE.md that the game does not have
   yet (Rain, Creepy, Treasure, Gemstone, Old black-and-white cartoon, Wood), then from a list of material/place ideas.
2. Gemini writes ONE `THEME_V2.<id> = {...}` entry, shown three complete themes from the game and the helper functions
   it may use. The text is checked before it ever runs: one statement, size limit, only allowed helpers (no network,
   no DOM access, no timers, no SVG filters), a new id and name, no mechanic names in the name or blurb.
3. It is inserted into a COPY of the stable build (before the game's theme registration, with a THEME_LIST row, its own
   rim, and "unlock not assigned yet" so the shop stays honest), and validated in a real browser:
   * the page loads with no JavaScript errors, every level and every existing theme is unchanged;
   * the theme applies and renders on three different levels with the board fully visible on a 540x960 and a
     390x844 screen, with no sideways scrolling;
   * readability and distinctness are MEASURED and compared with the shipped themes (theme_metrics.py): field
     brightness and busyness inside the range players already have, beam visibility at least as good as the least
     visible shipped theme, not a near-black "space" background, a colour palette clearly different from every
     existing theme, theme switch time and background size within the shipped range.
4. A failure goes back to Gemini once more with the measured reasons. A theme that passes is pushed to a new lab/
   branch of the game repository with a pull request showing screenshots and the measurements. A person merges it (or
   not) and assigns how players unlock it. The stable build and the live demo are never changed by this tool.
"""

import argparse
import json
import os
import re
import shutil
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import capture_gameplay as cg
import game_patch
import game_repo
import game_source
import game_terms
import theme_metrics

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "game_lab.json"
CALIBRATION_FILE = BASE_DIR / "data" / "theme_calibration.json"
BLOCK_NAME = "AUTO_THEME_EXPERIMENT"
REGISTER_ANCHOR = "(function v2Register(){"
EXAMPLE_THEMES = ("vinyl", "honey", "chalk")
MAX_CODE_CHARS = 12000
ATTEMPTS = 2
THEME_MODELS = ["gemini-3.5-flash", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash-lite"]

# Fixed policy limits; the measured ranges of the shipped themes are applied on top (see limits()).
POLICY = {"dark_background_max": 0.6, "nearest_min": 0.12, "env_chars_floor": 700_000}

CONCEPTS = [
    {"key": "rain", "brief": "RAIN (a direction listed in the game's knowledge file): a rain-streaked window at dusk. Running "
     "droplets and trails on glass, soft out-of-focus street lights behind it, cool blue-grey with a few warm lights."},
    {"key": "creepy", "brief": "CREEPY (knowledge-file direction), eerie but never gory: a candle-lit haunted study. Cobwebs in the "
     "corners, dusty shelves, a cracked portrait frame, sickly green-yellow candlelight, deep shadows."},
    {"key": "treasure", "brief": "TREASURE (knowledge-file direction): looking into an open pirate treasure chest. Heaped gold "
     "coins, a string of pearls, old rope and a weathered map corner; warm gold against dark wood."},
    {"key": "gemstone", "brief": "DIAMOND / GEMSTONE (knowledge-file direction): a jeweller's velvet tray of cut gems (emerald, "
     "sapphire, ruby) with crisp facet highlights and a brass loupe. Must look clearly different from the existing "
     "'Amethyst Geode' theme."},
    {"key": "cartoon", "brief": "OLD BLACK-AND-WHITE CARTOON (knowledge-file direction): 1930s rubber-hose animation. Ink "
     "outlines on off-white paper, halftone dots, film grain and a dark vignette; monochrome with at most one accent."},
    {"key": "wood", "brief": "WOOD (knowledge-file direction): a carpenter's workbench. Planed oak with long grain, a steel ruler, "
     "pencil marks, curled shavings; warm and tactile. Must differ from the existing 'Teak Deck' and 'Vinyl Record'."},
    {"key": "origami", "brief": "Origami table: folded paper cranes and creased sheets in soft pastel paper, crisp fold shadows."},
    {"key": "terrazzo", "brief": "Terrazzo floor: speckled stone chips in a pale cement, a brass divider strip, morning light."},
    {"key": "aquarium", "brief": "Aquarium glass: rippling water caustics, pebbles, a few slow fish silhouettes, teal and sand."},
    {"key": "bonsai", "brief": "Bonsai bench: moss, a shallow glazed pot, gnarled branches and raked gravel, quiet greens."},
    {"key": "circuitglass", "brief": "Watchmaker's loupe over a sapphire crystal: concentric guilloché engraving, cool silver."},
    {"key": "quilt", "brief": "Patchwork quilt: stitched fabric squares in faded indigo, mustard and red, visible thread seams."},
]

HELPERS = """Helpers the game defines (use only these; everything is plain SVG strings and DOM-free):
- RIM_R: board radius in SVG units (232). The board is centred at (0,0) in arena/hub/track coordinates.
- f1(x): round to 1 decimal for SVG attributes.
- el(tag, attrs, parent): create an SVG element under `parent` (used in arena/hub/track/rim.deco). Returns the element.
- v2Tag(tag, attrs, inner): SVG markup string (used in env()).
- v2Grad(id, [[offset, colour, opacity?], ...], 'linearGradient'|'radialGradient', attrs): gradient markup string.
- v2Rand(seed): deterministic random number generator; call R() for 0..1. Never use Math.random.
- v2Stars(tile, count, seed, rMin, rMax, opMin, opMax, colours, glow), v2Speckle(count, seed, x0, y0, w, h, colours, rMin, rMax, opMin, opMax): dot fields as markup.
- v2Blob(cx, cy, r, seed, k): path data of an irregular rounded blob.
- v2Rings(g, r0, r1, step, light, dark, wL, wD, opL, opD): concentric fine rings drawn into group g.
- v2Arena(svg, defs, {lip:[[off,col],...], field:[[off,col],...], edge, edgeOp, inner, gloss, shadow, tex(g, defs){...}, after(svg, defs){...}}): the board disc. Draw board textures in tex(); keep them calm.
- v2HubStd(h, {stops:[[off,col],...], edge, shape:'circle'|'hex'|'oct'|'square'|'gem'|'star8', r, ring, dot}): the centre hub.
- v2TrackStd(g, r, sel, n, {groove:[dark,light,w], line:[c,w,op], mark:[c,len,w,op], double:[c,op], sel:{glow:[c,w,op], under:[c,w], main:[c,w], hi, edges:[c,op], mark:[c,op]}}): a ring track; sel = this ring is selected.
- v2UiMk({ink:[r,g,b], line, bright, ring, sel, t0, t1, t2, accent, win, bg, font}): the HUD/menu colour tokens.
- v2RimDot(rimG, angle, radius, dotRadius, fill, opacity): a small decorative dot on the rim (inside rim.deco).
env() returns 1-3 layers: {svg} drawn into a 1000x1000 viewBox (cover), optionally {cls:'v2Drift', svg} for a slowly drifting light layer."""

FORBIDDEN = re.compile(
    r"\b(?:fetch|XMLHttpRequest|WebSocket|eval|Function|setTimeout|setInterval|requestAnimationFrame|import|require|"
    r"localStorage|sessionStorage|indexedDB|navigator|postMessage|Worker|Audio|"
    r"Math\.random|save|gstate|LEVELS|THEME_LIST|RIM_IDS|persistSave|traceAllBeams)\b|"
    r"\b(?:document|window|globalThis|self|location)\s*(?:\.\s*[A-Za-z_$]|\[)|new\s+Image\b|https?:|<script|<image|href|foreignObject|javascript:|"
    r"\bon(?:load|error|click|dblclick|mouse\w*|pointer\w*|touch\w*|focus|blur|key\w*|begin|end|repeat|animation\w*)\s*[=:]")


class ThemeLabError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_state():
    state = read_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("themes", [])
    state.setdefault("levels", [])
    return state


def is_due(state, days, now=None):
    now = now or utc_now()
    times = [datetime.fromisoformat(t["at"]) for t in state.get("themes", []) if t.get("at")]
    return not times or now - max(times) >= timedelta(days=days)


def choose_concept(state, existing_names):
    """The first concept not tried yet whose key does not already appear in an existing theme name."""
    tried = {t.get("concept") for t in state.get("themes", [])}
    names = " ".join(existing_names).lower()
    for concept in CONCEPTS:
        if concept["key"] not in tried and concept["key"] not in names:
            return concept
    counts = {c["key"]: sum(1 for t in state.get("themes", []) if t.get("concept") == c["key"]) for c in CONCEPTS}
    return min(CONCEPTS, key=lambda c: counts[c["key"]])


# ------------------------------------------------------------------------------------------- authoring

def extract_theme_code(source, theme_id):
    declaration = f"THEME_V2.{theme_id} = {{"
    start = source.find(declaration)
    if start == -1:
        raise ThemeLabError(f"example theme {theme_id} not found in the game")
    end = game_patch.find_object_end(source, declaration)
    return source[start:end]


def build_prompt(concept, examples, themes, feedback=None):
    existing = "\n".join(f"- {t['id']}: {t['name']} - {t.get('blurb', '')}" for t in themes)
    parts = [
        "You are adding ONE new visual theme to Ringshift, an indie puzzle game where the player rotates rings so light "
        "beams reach locks on the rim. Themes are 100% cosmetic: they never change gameplay.",
        "",
        f"THEME TO MAKE: {concept['brief']}",
        "",
        "It must use the game's existing v2 theme framework exactly like the three complete examples below "
        "(same fields, same helpers, same coordinate systems). Make it visually distinct in palette, lighting, "
        "atmosphere, background and board texture, not a recolour.",
        "",
        "Hard rules (an automatic checker rejects anything else):",
        "1. Output exactly three header lines and one javascript code block, nothing else:",
        "   ID: <lowercase letters/digits, 3-15 chars, not an existing id>",
        "   NAME: <2-3 words, title case, not an existing name>",
        "   BLURB: <one sentence under 140 characters describing the look>",
        "   ```javascript",
        "   THEME_V2.<id> = { ... };",
        "   ```",
        "2. The code block is ONE statement assigning THEME_V2.<id>, under 11000 characters, with the fields id, name, "
        "blurb, pal, stageFx (one of '', 'v2-gloss', 'v2-sheen', 'v2-spot', 'v2-warmgloss'), swatch, badge, "
        "ui: v2UiMk({...}), env(), arena(svg, defs), hub(h), track(g, r, sel, n), beam:{ halo:0.10, haloC:0.17 }, rim:{...}.",
        "3. Use only the helpers listed below and plain arithmetic/loops. No network, no document/window access, no "
        "timers, no Math.random (use v2Rand), no <image>, no href, no event handlers. No SVG filters inside the board "
        "(arena, hub, track, rim); in env() a light feGaussianBlur is acceptable, as some shipped themes do.",
        "4. READABILITY FIRST: beams are bright white/blue/green lines and the rim locks are red and green. Inside the "
        "board (arena tex) keep the field calm and fairly dark-to-mid in brightness, low contrast texture, nothing that "
        "looks like a beam, a lock or a mechanism. Put the richness in env() (around the board) instead.",
        "5. NOT a space/starfield/cosmic/void background, and no large near-black areas: the surroundings must read "
        "as the concept in daylight or lamplight.",
        "6. Do not use the words Mirror, Portal, Amplifier, Block, Prism, Echo, Lock, Beam, Black Hole or Satellite in "
        "the name or blurb.",
        "",
        HELPERS,
        "",
        "Existing themes (do not copy their look or reuse their ids/names):",
        existing,
        "",
    ]
    if feedback:
        parts += ["YOUR PREVIOUS ATTEMPT WAS REJECTED. Fix every point:", *[f"- {item}" for item in feedback], ""]
    for theme_id, code in examples.items():
        parts += [f"EXAMPLE THEME ({theme_id}), complete and working:", "```javascript", code, "```", ""]
    return "\n".join(parts)


RESPONSE_RE = re.compile(r"ID:\s*(?P<id>\S+)\s*\n\s*NAME:\s*(?P<name>[^\n]+)\n\s*BLURB:\s*(?P<blurb>[^\n]+)\n.*?```(?:javascript|js)?\s*\n(?P<code>.*?)```",
                         re.S | re.I)


def parse_response(text):
    match = RESPONSE_RE.search(text or "")
    if not match:
        raise ThemeLabError("the answer did not have the ID / NAME / BLURB lines and one javascript block")
    return {key: match.group(key).strip().strip("`'\"") if key != "code" else match.group(key).strip()
            for key in ("id", "name", "blurb", "code")}


def static_problems(spec, themes):
    """Problems found by reading the text only (nothing is executed)."""
    problems = []
    ids = {t["id"] for t in themes}
    names = {t["name"].lower() for t in themes}
    theme_id, name, blurb, code = spec["id"], spec["name"], spec["blurb"], spec["code"]
    if not re.fullmatch(r"[a-z][a-z0-9]{2,14}", theme_id):
        problems.append(f"the id {theme_id!r} must be 3-15 lowercase letters/digits")
    if theme_id in ids:
        problems.append(f"the id {theme_id!r} already exists")
    if name.lower() in names or not 3 <= len(name) <= 30:
        problems.append(f"the name {name!r} is taken or not 3-30 characters")
    if len(blurb) > 160:
        problems.append("the blurb is longer than 160 characters")
    for text, label in ((name, "name"), (blurb, "blurb")):
        if game_terms.mechanics_mentioned(text) or game_terms.unknown_terms(text) or re.search(r"\b(lock|beam)s?\b", text, re.I):
            problems.append(f"the {label} uses a game mechanic word")
    if len(code) > MAX_CODE_CHARS:
        problems.append(f"the code is {len(code)} characters (limit {MAX_CODE_CHARS})")
    declaration = f"THEME_V2.{theme_id} = {{"
    if not code.startswith(declaration):
        problems.append(f"the code must start with `{declaration}`")
    else:
        try:
            end = game_patch.find_object_end(code, declaration)
            if code[end:].strip():
                problems.append("the code block contains more than the one THEME_V2 assignment")
        except game_patch.PatchError as error:
            problems.append(f"the code does not parse as one object literal ({error})")
    hits = sorted({m.group(0) for m in FORBIDDEN.finditer(code)})
    if hits:
        problems.append("the code uses things that are not allowed: " + ", ".join(hits[:8]))
    for field in ("env(", "arena(", "ui:", "pal:", "rim:", "hub(", "track("):
        if field not in code:
            problems.append(f"the code has no {field.rstrip('(:')} field")
    return problems


def ask_gemini(prompt, log=print):
    from google import genai
    from env_check import require_env
    client = genai.Client(api_key=require_env("GEMINI_API_KEY")["GEMINI_API_KEY"])
    last = None
    for model in THEME_MODELS:
        for attempt in (1, 2):
            try:
                log(f"Asking {model} for a theme ...")
                response = client.models.generate_content(model=model, contents=prompt)
                return response.text or "", model
            except Exception as error:     # unknown model, quota, transient: try again, then the next model
                last = error
                log(f"  {model} failed ({type(error).__name__}: {str(error)[:120]})")
                time.sleep(5 * attempt)
    raise ThemeLabError(f"no Gemini model answered ({type(last).__name__ if last else 'no model'})")


# ------------------------------------------------------------------------------------------- patch + validate

def theme_block(spec):
    row = game_patch.js_literal({"id": spec["id"], "name": spec["name"], "cost": 3, "free": False, "blurb": spec["blurb"]})
    tid = game_patch.js_literal(spec["id"])
    return "\n".join([
        f"// Experimental theme '{spec['name']}' ({spec.get('concept')}), written by Gemini through the theme lab and",
        "// validated automatically (see the pull request). Its unlock is not assigned: the shop says so until a person does.",
        f"THEME_LIST.push({row});",
        f"THEME_SETS[{tid}] = {{rim:{tid}}}; if(!RIM_IDS.includes({tid})) RIM_IDS.push({tid});",
        f"UNASSIGNED_THEMES.push({tid});",
        spec["code"],
    ])


def patch_source(source, spec):
    return game_patch.insert_before(source, REGISTER_ANCHOR, BLOCK_NAME, theme_block(spec))


PARSE_JS = """(src)=>{ try{ new Function(src); return null; } catch(e){ return String(e && e.message || e); } }"""
STATE_JS = """(id)=>({ v2: Object.keys(THEME_V2).sort(), row: (THEME_LIST.find(t=>t.id===id)||{}).name || null,
  name: THEME_V2[id] ? THEME_V2[id].name : null, unassigned: UNASSIGNED_THEMES.includes(id),
  levels: Object.fromEntries(Object.keys(LEVELS).map(m=>[m, (LEVELS[m]||[]).map(l=>JSON.stringify(l))])) })"""


def calibration(game, *, log=print):
    """Measurements of every shipped theme for this exact build (cached in data/theme_calibration.json)."""
    cached = read_json(CALIBRATION_FILE, {})
    if cached.get("build") == game["build"] and cached.get("metrics"):
        return cached["metrics"]
    with cg.GameSession(game["path"]) as session:
        ids = [t["id"] for t in session.themes()]
    log(f"Measuring the {len(ids)} shipped themes of build {game['build']} (once per build) ...")
    metrics = theme_metrics.measure_all(game["path"], ids, log=log)
    write_json(CALIBRATION_FILE, {"build": game["build"], "measured_at": utc_now().isoformat(timespec="seconds"), "metrics": metrics})
    return metrics


def limits(shipped):
    values = lambda key: [m[key] for m in shipped.values()]      # noqa: E731
    nearest = []
    for a, ma in shipped.items():
        nearest.append(min(theme_metrics.histogram_distance(ma["histogram"], mb["histogram"]) for b, mb in shipped.items() if b != a))
    nearest.sort()
    return {
        "field_mean": (min(values("field_mean")) - 5, max(values("field_mean")) + 8),
        "field_std_max": max(values("field_std")) * 1.1,
        "beam_change_min": min(values("beam_change")) * 0.93,
        "dark_background_max": POLICY["dark_background_max"],
        # at least the policy floor; above it only as far as the shipped themes themselves are apart (capped near their median)
        "nearest_min": max(POLICY["nearest_min"], min(0.2, nearest[max(0, len(nearest) // 10)] * 1.25)),
        "apply_ms_max": max(values("apply_ms")) * 1.5 + 25,
        "env_chars_max": max(POLICY["env_chars_floor"], max(values("env_chars")) * 1.2),
    }


def judge(metrics, shipped, bounds):
    """Measured problems of a new theme versus the shipped ones. Returns (problems, nearest_id, nearest_distance)."""
    problems = []
    low, high = bounds["field_mean"]
    if not low <= metrics["field_mean"] <= high:
        problems.append(f"the playing field brightness {metrics['field_mean']:.0f} is outside the shipped range {low:.0f}-{high:.0f}")
    if metrics["field_std"] > bounds["field_std_max"]:
        problems.append(f"the playing field is too busy (texture {metrics['field_std']:.1f} > {bounds['field_std_max']:.1f}); keep it calm")
    if metrics["beam_change"] < bounds["beam_change_min"]:
        problems.append(f"beams are harder to see than in any shipped theme ({metrics['beam_change']:.2f} < {bounds['beam_change_min']:.2f})")
    if metrics["dark_background"] > bounds["dark_background_max"]:
        problems.append(f"{metrics['dark_background']:.0%} of the surroundings is near-black (a space/void look); limit {bounds['dark_background_max']:.0%}")
    distances = {tid: theme_metrics.histogram_distance(metrics["histogram"], m["histogram"]) for tid, m in shipped.items()}
    nearest_id = min(distances, key=distances.get)
    if distances[nearest_id] < bounds["nearest_min"]:
        problems.append(f"its colours are too close to the existing theme '{nearest_id}' ({distances[nearest_id]:.3f} < {bounds['nearest_min']:.3f})")
    if metrics["apply_ms"] > bounds["apply_ms_max"]:
        problems.append(f"switching to it takes {metrics['apply_ms']:.0f} ms (limit {bounds['apply_ms_max']:.0f})")
    if metrics["env_chars"] > bounds["env_chars_max"]:
        problems.append(f"its background art is {metrics['env_chars'] / 1024:.0f} KB (limit {bounds['env_chars_max'] / 1024:.0f} KB)")
    return problems, nearest_id, distances[nearest_id]


def validate(game, patched_path, spec, shipped, out_dir, *, log=print):
    """Run the patched build. Returns a report {ok, problems, checks, metrics, previews}."""
    report = {"checks": [], "problems": [], "previews": []}

    def check(name, ok, detail=""):
        report["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        log(f"  [{'ok' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")
        if not ok:
            report["problems"].append(f"{name}: {detail}" if detail else name)
        return ok

    with cg.GameSession(game["path"]) as stable:
        parse_error = stable.page.evaluate(PARSE_JS, spec["code"])
        before = stable.page.evaluate(STATE_JS, spec["id"])
    if not check("the theme code parses as JavaScript", parse_error is None, parse_error or ""):
        report["ok"] = False
        return report

    with cg.GameSession(patched_path) as session:
        after = session.page.evaluate(STATE_JS, spec["id"])
        errors = list(session.errors)
    check("the patched build loads without JavaScript errors", not errors, "; ".join(e[:120] for e in errors[:2]))
    check("the game registers the theme under its name", after["name"] == spec["name"] and after["row"] == spec["name"],
          f"{after['row']!r}")
    check("the shop marks its unlock as not assigned yet", after["unassigned"])
    check("every existing theme is still there", set(before["v2"]) | {spec["id"]} == set(after["v2"]))
    check("every level in every mode is unchanged", before["levels"] == after["levels"])
    if report["problems"]:
        report["ok"] = False
        return report

    try:
        with cg.GameSession(patched_path) as session:
            metrics = theme_metrics.measure_theme(session, spec["id"])
            errors = list(session.errors)
        check("the theme renders on three levels without errors", not errors and metrics["shown"] == spec["id"],
              "; ".join(e[:120] for e in errors[:2]))
    except cg.CaptureError as error:
        check("the theme renders on three levels with the whole board visible", False, str(error))
        report["ok"] = False
        return report

    bounds = limits(shipped)
    measured, nearest_id, nearest = judge(metrics, shipped, bounds)
    report["metrics"] = {k: v for k, v in metrics.items() if k != "histogram"}
    report["metrics"]["nearest_theme"] = nearest_id
    report["metrics"]["nearest_distance"] = nearest
    report["bounds"] = bounds
    check("readability, distinctness and performance are within the shipped themes' range", not measured, "; ".join(measured))
    mobile = theme_metrics.mobile_check(patched_path, spec["id"])
    check("it fits a 390x844 phone screen without overlays or sideways scrolling", not mobile, "; ".join(mobile))

    others = [tid for tid in sorted(shipped)][:: max(1, len(shipped) // 3)][:3]
    with cg.GameSession(patched_path) as session:
        for other in others:
            session.set_theme(other)
            session.start_level("normal", 15)
            session.page.wait_for_timeout(300)
        check("other themes still switch and render in the patched build", not session.errors,
              f"tried {', '.join(others)}" + ("; " + session.errors[0][:120] if session.errors else ""))

    if not report["problems"]:
        out_dir = Path(out_dir)
        for mode, index in theme_metrics.REFERENCE_LEVELS:
            with cg.GameSession(patched_path) as session:
                scene = next(s for s in session.scenes() if s["id"] == cg.scene_id(mode, index))
                try:
                    shots = cg.capture_scene(session, scene, out_dir, kinds=("screenshot",), theme=spec["id"], frame_board=True,
                                             stills=("puzzle",), log=lambda *a: None)
                except cg.CaptureError as error:
                    check(f"a clean screenshot of {scene['id']}", False, str(error))
                    continue
                for shot in shots:
                    renamed = shot["path"].with_name(f"{spec['id']}_{shot['path'].name}")
                    shot["path"].rename(renamed)
                    report["previews"].append(str(renamed))
        check("clean, usable screenshots on three levels", len(report["previews"]) == len(theme_metrics.REFERENCE_LEVELS))

    report["ok"] = not report["problems"]
    return report


def pr_body(spec, report, game, previews, model):
    m, b = report["metrics"], report["bounds"]
    rows = [
        ("Playing-field brightness", f"{m['field_mean']:.1f}", f"{b['field_mean'][0]:.0f} - {b['field_mean'][1]:.0f}"),
        ("Playing-field busyness", f"{m['field_std']:.1f}", f"<= {b['field_std_max']:.1f}"),
        ("Beam visibility (change when a ring turns)", f"{m['beam_change']:.2f}", f">= {b['beam_change_min']:.2f}"),
        ("Near-black surroundings", f"{m['dark_background']:.0%}", f"<= {b['dark_background_max']:.0%}"),
        ("Colour distance to nearest theme", f"{m['nearest_distance']:.3f} ({m['nearest_theme']})", f">= {b['nearest_min']:.3f}"),
        ("Theme switch + redraw", f"{m['apply_ms']:.0f} ms", f"<= {b['apply_ms_max']:.0f} ms"),
        ("Background art", f"{m['env_chars'] / 1024:.0f} KB", f"<= {b['env_chars_max'] / 1024:.0f} KB"),
    ]
    lines = [
        f"An experimental theme, **{spec['name']}** (`{spec['id']}`): {spec['blurb']}",
        "",
        f"Concept: {spec.get('concept_brief')}",
        "",
        (f"Written by Gemini (`{model}`)" if str(model).startswith("gemini") else f"Written by {model}") + " in the game's v2 theme framework, checked by "
        "[ringshift-social-autopilot](https://github.com/ultramegaray0-lgtm/ringshift-social-autopilot) `scripts/theme_lab.py`, "
        f"then validated automatically on this exact build (base `{game['file']}` at `{str(game.get('commit') or '?')[:10]}`).",
        "",
        "**Nothing here is live.** The demo publishes only `main`. The theme is added to the shop with its unlock "
        "marked *not assigned yet*; assign it (Challenge gift, gift box, ...) if you keep it.",
        "",
        "### Checks",
        "",
        *[f"- {'✅' if c['ok'] else '❌'} {c['check']}" + (f" ({c['detail']})" if c['detail'] else "") for c in report["checks"]],
        "",
        "### Measurements against the 39 shipped themes",
        "",
        "| Measure | This theme | Accepted range (from the shipped themes) |",
        "|---|---|---|",
        *[f"| {a} | {v} | {r} |" for a, v, r in rows],
        "",
    ]
    for url in previews:
        lines.append(f"![{spec['name']} preview]({url})")
    lines += ["", "The checks prove it loads, renders, stays readable and differs from the other themes. They do not prove "
              "it is beautiful: please look at it in the game before merging."]
    return "\n".join(lines)


def run(args, log=print):
    state = load_state()
    if args.if_due and not is_due(state, args.if_due):
        log(f"NOT DUE: the last theme experiment is less than {args.if_due} days old.")
        return {"status": "not_due"}

    game = game_source.describe(args.game_dir)
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    (out / "previews").mkdir(parents=True)
    source = Path(game["path"]).read_text(encoding="utf-8")
    with cg.GameSession(game["path"]) as session:
        themes = session.themes()
    concept = choose_concept(state, [t["name"] for t in themes])
    log(f"Game {game['file']} at {game['commit']}; concept: {concept['key']}")
    examples = {tid: extract_theme_code(source, tid) for tid in EXAMPLE_THEMES}
    shipped = calibration(game, log=log)

    entry = {"at": utc_now().isoformat(timespec="seconds"), "concept": concept["key"], "game_commit": game["commit"],
             "attempts": []}
    feedback, accepted, report, model = None, None, None, None
    for attempt in range(1, ATTEMPTS + 1):
        if args.spec_file:      # a theme written by hand (or by another tool): the same checks, no Gemini call
            text, model = Path(args.spec_file).read_text(encoding="utf-8"), args.author or "hand-written"
        else:
            text, model = ask_gemini(build_prompt(concept, examples, themes, feedback), log=log)
        try:
            spec = parse_response(text)
        except ThemeLabError as error:
            feedback = [str(error)]
            entry["attempts"].append({"model": model, "outcome": "unparsable", "problems": feedback})
            if args.spec_file:
                break
            continue
        spec.update(concept=args.concept or concept["key"],
                    concept_brief=next((c["brief"] for c in CONCEPTS if c["key"] == args.concept), concept["brief"]))
        (out / f"attempt{attempt}_{spec['id']}.js").write_text(spec["code"], encoding="utf-8")
        problems = static_problems(spec, themes)
        if problems:
            log("Rejected before running: " + "; ".join(problems))
            feedback = problems
            entry["attempts"].append({"model": model, "id": spec["id"], "outcome": "static check failed", "problems": problems})
            if args.spec_file:
                break
            continue
        patched = out / game["file"]
        patched.write_text(patch_source(source, spec), encoding="utf-8")
        log(f"Validating '{spec['name']}' ({spec['id']}) ...")
        report = validate(game, patched, spec, shipped, out / "previews", log=log)
        entry["attempts"].append({"model": model, "id": spec["id"], "name": spec["name"],
                                  "outcome": "validated" if report["ok"] else "rejected", "problems": report["problems"],
                                  "metrics": report.get("metrics")})
        if report["ok"]:
            accepted = spec
            break
        feedback = report["problems"]
        if args.spec_file:
            break

    result = {"status": "rejected", "entry": entry}
    try:
        if accepted:
            entry.update(id=accepted["id"], name=accepted["name"], outcome="validated")
            (out / "report.json").write_text(json.dumps({"spec": accepted, "report": report}, indent=2), encoding="utf-8")
            result = {"status": "validated", "entry": entry, "spec": accepted}
            if args.open_pr:
                try:
                    game_repo.require_write_access()
                    previews = game_repo.host_previews(report, label=accepted["id"]).get(accepted["id"], [])
                    body = pr_body(accepted, report, game, previews, model)
                    (out / "pr_body.md").write_text(body, encoding="utf-8")
                    entry["pr"] = game_repo.propose(out / game["file"], game, branch_prefix=f"lab/theme-{accepted['id']}",
                                                    title=f"Theme lab: {accepted['name']} (experimental theme)", body=body, log=log)
                    entry["outcome"] = "proposed"
                    result["pr"] = entry["pr"]
                except game_repo.GameRepoError as error:
                    entry.update(outcome="validated, not proposed", error=str(error)[:400])
                    raise
        else:
            entry["outcome"] = "rejected"
            (out / "report.json").write_text(json.dumps({"entry": entry, "report": report}, indent=2), encoding="utf-8")
    finally:
        if not args.no_record:      # recorded even when proposing failed, so the two-week rhythm holds
            state["themes"] = (state["themes"] + [entry])[-100:]
            write_json(STATE_FILE, state)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Author, validate and propose one experimental Ringshift theme.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--game-dir", default="game-src")
    p.add_argument("--out", default="theme-out")
    p.add_argument("--open-pr", action="store_true")
    p.add_argument("--if-due", type=int, default=0, help="only run if the last experiment is at least this many days old")
    p.add_argument("--no-record", action="store_true", help="do not write data/game_lab.json")
    p.add_argument("--spec-file", help="validate this theme (ID/NAME/BLURB lines + a javascript block) instead of asking Gemini")
    p.add_argument("--author", help="with --spec-file: who wrote it, for the pull request (default 'hand-written')")
    p.add_argument("--concept", help="with --spec-file: the concept key it implements")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (game_source.GameSourceError, cg.CaptureError, game_patch.PatchError, game_repo.GameRepoError, ThemeLabError) as error:
        print(f"error: {error}", file=sys.stderr)
        _summary(f"### Theme lab\n\nError: {error}\n")
        return 2
    status = result["status"]
    if status == "validated":
        message = f"Experimental theme **{result['spec']['name']}** validated" + (f" and proposed: {result['pr']}" if result.get("pr") else ".")
    elif status == "not_due":
        message = "Not due yet (one experiment every two weeks)."
    else:
        last = (result["entry"]["attempts"] or [{}])[-1]
        message = "No theme was proposed: every attempt was rejected. Last reasons: " + "; ".join(last.get("problems") or ["(none recorded)"])
    print(message)
    _summary(f"### Theme lab\n\n{message}\n")
    return 0


def _summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
