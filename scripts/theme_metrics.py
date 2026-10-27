"""Measure how a Ringshift theme looks and performs, from real renders of the real game.

Used by the theme lab to accept or reject an experimental theme against the themes the game already ships: every
limit is derived from measurements of the existing themes, so "readable" means "at least as readable as what players
already have", not a number picked from the air.

For each theme, on one reference level, in a phone-sized window:
* field_mean / field_std: brightness and busyness of the board's playing field (where beams run)
* beam_change: how much the board visibly changes when one ring turns one step (beams and locks moving); a theme that
  hides the beams scores low
* dark_background: share of the surroundings that is near-black (a generic "space" look scores high)
* histogram: a coarse colour histogram, to measure how different two themes look
* apply_ms: time for the game to switch to the theme and redraw the board (first, cold build)
* env_chars: size of the theme's generated background art
"""

import io
import math
import statistics

import capture_gameplay as cg

REFERENCE_LEVELS = (("hard", 8), ("normal", 15), ("easy", 19))      # Prism+Mirror, Mirror+Portal, Mirror+Block+Portal
WINDOW = {"width": 540, "height": 960}
FIELD_FRACTION = 0.36            # playing-field radius as a share of the board box width
HUD_BOTTOM = 100                 # CSS px: the HUD bar; excluded from the "surroundings"

APPLY_JS = """(id)=>{
  ensureThemeShape();
  if(!save.themeState.owned.includes(id)) save.themeState.owned.push(id);
  save.themeState.equipped = id; persistSave();
  const t0 = performance.now(); syncThemeToContext(); if(gstate.level) renderPuzzle(); const t1 = performance.now();
  const layers = (V2.envCache && V2.envCache[id]) || [];
  const chars = layers.reduce((n, L)=>n + ((L && L.svg) ? L.svg.length : 0) + ((L && L.css) ? L.css.length : 0), 0);
  return {ms: t1 - t0, chars, cur: V2.cur && V2.cur.id};
}"""

OVERFLOW_JS = """()=>({scroll: document.scrollingElement.scrollWidth, inner: innerWidth})"""


def _image(png_bytes):
    from PIL import Image
    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


def _luma(pixel):
    r, g, b = pixel
    return 0.299 * r + 0.587 * g + 0.114 * b


def analyse(before, after, board, scale):
    """Numbers from two screenshots (PIL images) of the same board, before and after one ring step."""
    width, height = before.size
    small = (width // 3, height // 3)
    a, b = before.resize(small), after.resize(small)
    factor = scale / 3.0
    cx = (board["left"] + board["width"] / 2) * factor
    cy = (board["top"] + board["height"] / 2) * factor
    field_r = board["width"] * FIELD_FRACTION * factor
    board_r = board["width"] / 2 * factor
    field, change, background = [], [], []
    pa, pb = a.load(), b.load()
    for y in range(small[1]):
        for x in range(small[0]):
            d = math.hypot(x - cx, y - cy)
            la = _luma(pa[x, y])
            if d <= field_r:
                field.append(la)
                change.append(abs(la - _luma(pb[x, y])))
            elif d > board_r and y > HUD_BOTTOM * factor:
                background.append(la)
    hist = [0] * 64
    for r, g, bl in a.getdata():
        hist[(r >> 6) * 16 + (g >> 6) * 4 + (bl >> 6)] += 1
    total = float(sum(hist)) or 1.0
    return {
        "field_mean": round(statistics.fmean(field), 1),
        "field_std": round(statistics.pstdev(field), 1),
        "beam_change": round(statistics.fmean(change), 2),
        "dark_background": round(sum(1 for v in background if v < 28) / max(len(background), 1), 3),
        "histogram": [round(h / total, 5) for h in hist],
    }


def histogram_distance(h1, h2):
    """Hellinger distance between two colour histograms: 0 = identical colours, 1 = nothing in common."""
    bc = sum(math.sqrt(p * q) for p, q in zip(h1, h2))
    return round(math.sqrt(max(0.0, 1.0 - bc)), 4)


def measure_theme(session, theme_id, levels=REFERENCE_LEVELS):
    """Metrics for one theme, averaged over the reference levels. Uses the session's page (already loaded)."""
    rows, timing = [], []
    for mode, index in levels:
        session.page.evaluate(
            "([m,i])=>{ window.__allowGo = true; try{ window.__origGoGame(m, i); } finally { window.__allowGo = false; } }",
            [mode, index])
        session.page.wait_for_timeout(250)
        timing.append(session.page.evaluate(APPLY_JS, theme_id))
        session.page.wait_for_timeout(700)
        screen = session.screen()
        if screen["problems"]:
            raise cg.CaptureError(f"{theme_id} {mode}:{index + 1}: " + "; ".join(screen["problems"]))
        before = _image(session.page.screenshot())
        session.page.keyboard.press("ArrowRight")
        session.page.wait_for_timeout(700)
        after = _image(session.page.screenshot())
        rows.append(analyse(before, after, screen["board"], before.size[0] / WINDOW["width"]))
    result = {key: round(statistics.fmean(row[key] for row in rows), 3)
              for key in ("field_mean", "field_std", "beam_change", "dark_background")}
    result["histogram"] = [round(statistics.fmean(col), 5) for col in zip(*(row["histogram"] for row in rows))]
    result["apply_ms"] = round(timing[0]["ms"], 1)
    result["env_chars"] = max(t["chars"] for t in timing)
    result["shown"] = timing[0]["cur"]
    return result


def measure_all(game_path, theme_ids, *, log=print):
    """{theme_id: metrics} for every theme, each in a fresh page so cold build times are comparable."""
    results = {}
    for theme_id in theme_ids:
        with cg.GameSession(game_path) as session:
            results[theme_id] = measure_theme(session, theme_id)
            if session.errors:
                results[theme_id]["page_errors"] = session.errors[:3]
        log(f"  {theme_id:12s} field {results[theme_id]['field_mean']:5.1f}/{results[theme_id]['field_std']:4.1f}  "
            f"beams {results[theme_id]['beam_change']:5.2f}  dark bg {results[theme_id]['dark_background']:.2f}  "
            f"{results[theme_id]['apply_ms']:6.1f} ms  {results[theme_id]['env_chars'] / 1024:6.0f} KB")
    return results


def mobile_check(game_path, theme_id, viewport=None):
    """Problems on a common phone size (390x844): board fully visible, no overlay, no sideways scrolling."""
    viewport = viewport or {"width": 390, "height": 844}
    problems = []
    with cg.GameSession(game_path) as session:
        session.page.set_viewport_size(viewport)
        session.set_theme(theme_id)
        session.start_level("hard", 8)
        session.page.wait_for_timeout(800)
        screen = session.screen()
        problems += screen["problems"]
        overflow = session.page.evaluate(OVERFLOW_JS)
        if overflow["scroll"] > overflow["inner"] + 1:
            problems.append(f"the page scrolls sideways at {viewport['width']}px ({overflow['scroll']}px wide)")
        problems += [f"JavaScript error: {e[:120]}" for e in session.errors]
    return problems
