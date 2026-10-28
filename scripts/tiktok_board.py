"""Find the Ringshift puzzle board in gameplay video and work out how to crop it for the TikTok layout.

Two ways to know where the board is, both measured, never guessed:

1. From the game itself (live capture): the browser reports the bounding box of everything drawn in the game's
   `#puzzle` and `#puzzleBeams` SVG layers (rings, rim locks, beams), in screen pixels (see BOARD_JS). That box is
   converted to video pixels with the ratio between the recorded frame width and the browser viewport width.
2. From the pixels (curated recordings in media/gameplay/, which have no DOM): the board is a set of concentric
   circles whose outer rim is a bright, static circle. Edge points vote for circle centres along their gradient
   direction (a gradient Hough transform), summed over several frames; the outermost radius around that centre that
   is edge-supported on most of its circumference is the outer rim. A least-squares circle fit then refines it.

The same pixel detector is used afterwards on the finished TikTok video to prove the whole board is visible and large.

Needs numpy for the pixel detector (requirements-tiktok.txt) and FFmpeg on PATH.
"""

import math
import subprocess
from pathlib import Path

import media_tools as tools

ANALYSIS_SIZE = 480            # frames are analysed with their longer side at this many pixels
MIN_RADIUS_FRACTION = 0.12     # of the shorter frame side
MAX_RADIUS_FRACTION = 0.52     # of the longer frame side
RIM_SUPPORT = 0.45             # share of the rim circumference that must show an edge for a circle to count
FRAME_SUPPORT = 0.30           # share of the rim that must show in ONE frame for that frame to count as showing the board
CLIP_TOLERANCE = 0.015         # a rim may poke this share of its radius outside the frame before it counts as cut off
CROP_MARGIN = 0.03             # space kept around the outer rim, as a share of the board diameter (lock glow, rim ticks)


class BoardError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------- browser measurement

# Bounding box, in CSS pixels of the page, of what the game actually draws for the board: the static layer (#puzzle:
# rings, gimmicks, rim and its locks) and the beam layer (#puzzleBeams). getBBox() is in the SVG's own units and
# getScreenCTM() maps them to the page, so the result follows the game's real layout at this window size.
BOARD_JS = """()=>{
  const ids = ['puzzle', 'puzzleBeams'];
  let box = null;
  const parts = {};
  for (const id of ids) {
    const svg = document.getElementById(id);
    if (!svg || typeof svg.getBBox !== 'function') continue;
    let bb; try { bb = svg.getBBox(); } catch (e) { continue; }
    if (!bb || bb.width < 1 || bb.height < 1) continue;
    const m = svg.getScreenCTM();
    if (!m) continue;
    const xs = [], ys = [];
    for (const [x, y] of [[bb.x, bb.y], [bb.x + bb.width, bb.y], [bb.x, bb.y + bb.height], [bb.x + bb.width, bb.y + bb.height]]) {
      const p = svg.createSVGPoint(); p.x = x; p.y = y; const q = p.matrixTransform(m); xs.push(q.x); ys.push(q.y);
    }
    const r = {left: Math.min(...xs), top: Math.min(...ys), right: Math.max(...xs), bottom: Math.max(...ys)};
    parts[id] = r;
    box = box ? {left: Math.min(box.left, r.left), top: Math.min(box.top, r.top),
                 right: Math.max(box.right, r.right), bottom: Math.max(box.bottom, r.bottom)} : r;
  }
  const svg = document.getElementById('puzzle');
  const animating = svg && svg.getAnimations ? svg.getAnimations().filter(a => a.playState === 'running').length : 0;
  return {box, parts, animating, viewport: {width: innerWidth, height: innerHeight}};
}"""


def circle_from_dom(box, viewport_width, frame_width):
    """(cx, cy, r) in video pixels from a BOARD_JS box (CSS pixels). The box encloses the whole drawn board, so its
    half-size is the outer radius. Raises BoardError for a missing or implausible box."""
    if not box or viewport_width <= 0 or frame_width <= 0:
        raise BoardError("the game reported no board box")
    width, height = box["right"] - box["left"], box["bottom"] - box["top"]
    if width < 100 or height < 100:
        raise BoardError(f"the game reported a board of only {width:.0f}x{height:.0f} CSS px")
    if abs(width - height) > 0.08 * max(width, height):
        raise BoardError(f"the board box is not square ({width:.0f}x{height:.0f}); the board may be cut off or animating")
    scale = frame_width / viewport_width
    return ((box["left"] + box["right"]) / 2 * scale, (box["top"] + box["bottom"]) / 2 * scale,
            max(width, height) / 2 * scale)


# --------------------------------------------------------------------------------------------- frame access

def analysis_size(width, height, longest=ANALYSIS_SIZE):
    """(w, h) for analysing a width x height frame, longer side = `longest`, both even."""
    if width >= height:
        w = longest
        h = max(2, int(round(longest * height / width / 2)) * 2)
    else:
        h = longest
        w = max(2, int(round(longest * width / height / 2)) * 2)
    return w, h


def read_gray_frames(path, times, size, crop=None):
    """Grayscale frames (numpy float32 arrays, h x w) at `times` seconds, scaled to `size` = (w, h). `crop` =
    (x, y, w, h) in source pixels is applied first. Frames that cannot be decoded are left out."""
    import numpy as np
    w, h = size
    chain = (f"crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]}," if crop else "") + f"scale={w}:{h}:flags=area,format=gray"
    frames = []
    for at in times:
        seek = ["-ss", f"{at:.3f}"] if at > 0 else []
        result = subprocess.run(["ffmpeg", "-v", "error", *seek, "-i", str(path), "-frames:v", "1", "-vf", chain,
                                 "-f", "rawvideo", "-"], capture_output=True, check=False)
        if result.returncode == 0 and len(result.stdout) == w * h:
            frames.append(np.frombuffer(result.stdout, dtype=np.uint8).astype(np.float32).reshape(h, w))
    return frames


def sample_times(start, duration, count):
    count = max(1, int(count))
    return [start + duration * (i + 0.5) / count for i in range(count)]


# --------------------------------------------------------------------------------------------- pixel detection

def _edges(image):
    """(gx, gy, magnitude) of a lightly smoothed frame."""
    import numpy as np
    padded = np.pad(image, 1, mode="edge")
    smooth = sum(padded[dy:dy + image.shape[0], dx:dx + image.shape[1]] for dy in range(3) for dx in range(3)) / 9.0
    gx = np.zeros_like(smooth)
    gy = np.zeros_like(smooth)
    gx[:, 1:-1] = smooth[:, 2:] - smooth[:, :-2]
    gy[1:-1, :] = smooth[2:, :] - smooth[:-2, :]
    return gx, gy, np.hypot(gx, gy)


def _edge_threshold(magnitude):
    import numpy as np
    return max(float(np.percentile(magnitude, 94)), 6.0)


def _vote_centres(frames, radii):
    """Accumulator of circle-centre votes (h x w) from every strong edge point of every frame."""
    import numpy as np
    h, w = frames[0].shape
    votes = np.zeros(h * w, dtype=np.float64)
    rng = np.random.default_rng(7)
    for image in frames:
        gx, gy, magnitude = _edges(image)
        ys, xs = np.nonzero(magnitude > _edge_threshold(magnitude))
        if len(xs) > 30000:                                   # plenty of points; keep the work bounded
            keep = rng.choice(len(xs), 30000, replace=False)
            ys, xs = ys[keep], xs[keep]
        if not len(xs):
            continue
        mag = magnitude[ys, xs]
        ux, uy = gx[ys, xs] / mag, gy[ys, xs] / mag
        for radius in radii:
            for sign in (1.0, -1.0):
                cx = np.rint(xs + sign * radius * ux).astype(np.int64)
                cy = np.rint(ys + sign * radius * uy).astype(np.int64)
                inside = (cx >= 0) & (cx < w) & (cy >= 0) & (cy < h)
                votes += np.bincount(cy[inside] * w + cx[inside], minlength=h * w)
    grid = votes.reshape(h, w)
    padded = np.pad(grid, 2)
    return sum(padded[dy:dy + h, dx:dx + w] for dy in range(5) for dx in range(5))


def rim_support(magnitude, threshold, cx, cy, radius, angles=360, band=1.5):
    """(share of the circle's points inside the frame that sit on an edge, share of points outside the frame)."""
    import numpy as np
    h, w = magnitude.shape
    theta = np.linspace(0, 2 * math.pi, angles, endpoint=False)
    hits = np.zeros(angles, dtype=bool)
    outside = np.ones(angles, dtype=bool)
    for offset in np.arange(-band, band + 0.01, 0.75):
        xs = np.rint(cx + (radius + offset) * np.cos(theta)).astype(np.int64)
        ys = np.rint(cy + (radius + offset) * np.sin(theta)).astype(np.int64)
        inside = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
        outside &= ~inside
        hits[inside] |= magnitude[ys[inside], xs[inside]] > threshold
    seen = (~outside).sum()
    return (hits.sum() / seen if seen else 0.0), outside.mean()


def _fit_circle(points):
    """Least-squares (Kasa) circle through points [(x, y), ...] -> (cx, cy, r)."""
    import numpy as np
    xs, ys = points[:, 0], points[:, 1]
    a = np.column_stack([xs, ys, np.ones_like(xs)])
    b = xs ** 2 + ys ** 2
    (c0, c1, c2), *_ = np.linalg.lstsq(a, b, rcond=None)
    cx, cy = c0 / 2, c1 / 2
    return float(cx), float(cy), float(math.sqrt(max(c2 + cx ** 2 + cy ** 2, 0.0)))


def detect_in_frames(frames):
    """Find the board's outer rim in analysis-size frames. Returns {cx, cy, r, support, frame_support: [...],
    outside_share} in analysis pixels, or raises BoardError when no convincing board circle is present."""
    import numpy as np
    if not frames:
        raise BoardError("no frame could be decoded")
    h, w = frames[0].shape
    r_min = max(12, int(min(w, h) * MIN_RADIUS_FRACTION))
    r_max = int(max(w, h) * MAX_RADIUS_FRACTION)
    radii = np.arange(r_min, r_max, 1.5)

    votes = _vote_centres(frames, radii)
    cy, cx = np.unravel_index(int(np.argmax(votes)), votes.shape)

    edges = [_edges(image)[2] for image in frames]
    thresholds = [_edge_threshold(m) for m in edges]

    def mean_support(x, y, radius):
        values = [rim_support(m, t, x, y, radius) for m, t in zip(edges, thresholds)]
        return sum(v[0] for v in values) / len(values), max(v[1] for v in values)

    # The outer rim: the LARGEST radius around this centre that is a well supported circle (inner rings are smaller).
    best = None
    for radius in np.arange(r_min, r_max, 1.0):
        support, outside = mean_support(cx, cy, radius)
        if support >= RIM_SUPPORT and outside <= 0.5:
            best = (float(radius), support)
    if best is None:
        raise BoardError("no circular board rim found (no well supported circle)")

    # Refine centre and radius with the edge points on that rim, from every frame.
    points = []
    theta = np.linspace(0, 2 * math.pi, 720, endpoint=False)
    for magnitude, threshold in zip(edges, thresholds):
        for offset in np.arange(-2.0, 2.01, 0.5):
            xs = np.rint(cx + (best[0] + offset) * np.cos(theta)).astype(np.int64)
            ys = np.rint(cy + (best[0] + offset) * np.sin(theta)).astype(np.int64)
            inside = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
            on_edge = np.zeros_like(inside)
            on_edge[inside] = magnitude[ys[inside], xs[inside]] > threshold
            points.extend(zip(xs[on_edge], ys[on_edge]))
    fx, fy, fr = _fit_circle(np.array(points, dtype=np.float64)) if len(points) >= 30 else (float(cx), float(cy), best[0])
    if math.hypot(fx - cx, fy - cy) > 0.08 * best[0] or abs(fr - best[0]) > 0.08 * best[0]:
        fx, fy, fr = float(cx), float(cy), best[0]          # the fit wandered off: keep the voted circle

    support, _ = mean_support(fx, fy, fr)
    per_frame = [rim_support(m, t, fx, fy, fr)[0] for m, t in zip(edges, thresholds)]
    theta = np.linspace(0, 2 * math.pi, 720, endpoint=False)
    xs, ys = fx + fr * np.cos(theta), fy + fr * np.sin(theta)
    tolerance = CLIP_TOLERANCE * fr
    outside_share = float(((xs < -tolerance) | (xs > w - 1 + tolerance) | (ys < -tolerance) | (ys > h - 1 + tolerance)).mean())
    return {"cx": fx, "cy": fy, "r": fr, "support": round(float(support), 3),
            "frame_support": [round(float(v), 3) for v in per_frame], "outside_share": round(outside_share, 4),
            "analysis_size": [w, h]}


def detect_board(path, start=0.0, duration=None, samples=8, info=None):
    """Find the board in a video segment. Returns a dict in SOURCE pixels: {cx, cy, r, support, frames_showing,
    frames_sampled, clipped, method}. Raises BoardError when there is no convincing, uncut board."""
    info = info or tools.probe(path)
    width, height = info["width"], info["height"]
    duration = duration if duration is not None else max(info["duration"] - start, 0.1)
    size = analysis_size(width, height)
    frames = read_gray_frames(path, sample_times(start, duration, samples), size)
    if len(frames) < max(2, samples // 2):
        raise BoardError(f"only {len(frames)} of {samples} frames could be decoded")
    found = detect_in_frames(frames)
    scale = width / size[0]
    showing = sum(1 for value in found["frame_support"] if value >= FRAME_SUPPORT)
    return {
        "cx": found["cx"] * scale, "cy": found["cy"] * scale, "r": found["r"] * scale,
        "support": found["support"], "frames_showing": showing, "frames_sampled": len(frames),
        "frame_support": found["frame_support"], "clipped": found["outside_share"] > 0.0,
        "outside_share": found["outside_share"], "method": "pixels",
    }


def check_board_detection(found, min_share=0.75):
    """Problems with a detect_board result for use as gameplay: [] when fine."""
    problems = []
    if found["clipped"]:
        problems.append(f"the board's outer rim is cut off by the frame edge ({found['outside_share']:.1%} of it)")
    if found["frames_showing"] < math.ceil(found["frames_sampled"] * min_share):
        problems.append(f"the board is visible in only {found['frames_showing']} of {found['frames_sampled']} sampled frames")
    return problems


# --------------------------------------------------------------------------------------------- crop maths (pure)

def crop_for_circle(cx, cy, r, frame_width, frame_height, margin=CROP_MARGIN):
    """The square crop that holds the whole circle plus `margin` (share of the diameter) on every side.

    Returns {"x", "y", "size", "pad"}: the crop is taken from the source frame padded by `pad` pixels on every side
    (so a board close to the frame edge is never cut; the padding is dark background), and x/y are in that padded
    frame. All values are even integers, as H.264 and FFmpeg's crop want."""
    if r <= 0:
        raise BoardError("board radius must be positive")
    size = int(math.ceil(2 * r * (1 + 2 * margin) / 2)) * 2
    left = cx - size / 2
    top = cy - size / 2
    need = max(0.0, -left, -top, left + size - frame_width, top + size - frame_height)
    pad = int(math.ceil(need / 2)) * 2
    x = int(round((left + pad) / 2)) * 2
    y = int(round((top + pad) / 2)) * 2
    x = min(max(x, 0), frame_width + 2 * pad - size)
    y = min(max(y, 0), frame_height + 2 * pad - size)
    return {"x": x, "y": y, "size": size, "pad": pad}


def crop_filter(crop, out_size, background="0x060a14"):
    """FFmpeg -vf chain: pad (if needed), crop the board square, scale it to out_size x out_size."""
    parts = []
    if crop["pad"]:
        p = crop["pad"]
        parts.append(f"pad=iw+{2 * p}:ih+{2 * p}:{p}:{p}:color={background}")
    parts.append(f"crop={crop['size']}:{crop['size']}:{crop['x']}:{crop['y']}")
    parts.append(f"scale={out_size}:{out_size}:flags=lanczos")
    return ",".join(parts)


def circle_in_crop(cx, cy, r, crop, out_size):
    """Where the board circle lands inside the scaled crop: (cx, cy, r) in output-square pixels."""
    scale = out_size / crop["size"]
    return ((cx + crop["pad"] - crop["x"]) * scale, (cy + crop["pad"] - crop["y"]) * scale, r * scale)


def agreement(a, b):
    """How far two circle estimates (dicts with cx, cy, r) differ, relative to the radius."""
    radius = max(a["r"], b["r"], 1e-6)
    return {"centre_offset": round(math.hypot(a["cx"] - b["cx"], a["cy"] - b["cy"]) / radius, 4),
            "radius_ratio": round(min(a["r"], b["r"]) / radius, 4)}


if __name__ == "__main__":
    import json
    import sys
    for name in sys.argv[1:]:
        try:
            print(name, json.dumps(detect_board(Path(name))))
        except (BoardError, tools.MediaToolError) as error:
            print(name, "error:", error)
