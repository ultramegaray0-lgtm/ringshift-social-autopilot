"""Build the November 2026 campaign assets from REAL captured Ringshift gameplay (media/captured, media/gameplay).

Nothing is staged or mocked: every frame of gameplay comes from the repository's captured clips (recorded from the
real game by scripts/capture_gameplay.py or by hand). Only title/end cards and captions are drawn on top.

Outputs (campaign/assets/):
  ringshift_trailer_9x16.mp4    ~30 s vertical trailer (Shorts, Reels, Reddit/X mobile)
  ringshift_trailer_16x9.mp4    same cut, landscape (itch.io, YouTube, Reddit desktop, press)
  looks_impossible_hard18.mp4   0/4 locks after 15 moves -> "Can you see it?"   (challenge angle)
  satisfying_solve_hard24.mp4   every lock lights at once                     (satisfying-solution angle)
  mechanism_prism.mp4           prism splits a beam, labelled                 (mechanism angle)
  simple_vs_advanced.jpg        Easy board vs Hard board, side by side        (comparison angle)
  challenge_hard24_puzzle.jpg   unsolved Hard 24 with a challenge line        (challenge still)
Run:  python campaign/build_assets.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "media"
OUT = ROOT / "campaign" / "assets"
TMP = OUT / "_scenes"
W, H, FPS = 1080, 1920, 30
BOLD = "/usr/share/fonts/opentype/inter/Inter-Bold.otf"
REG = "/usr/share/fonts/opentype/inter/Inter-Regular.otf"
ENC = ["-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", str(FPS), "-an"]
BG = (8, 14, 22)
TEAL = (86, 240, 190)
URL = "ringshift.itch.io/ringshift"


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def font(path, size):
    return ImageFont.truetype(path, size)


def card(lines, path, size=(W, H)):
    """Dark card with centred lines [(text, font_path, px, colour)]."""
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    # faint rings, echoing the board
    cx, cy = size[0] // 2, int(size[1] * 0.40)
    for r, a in ((330, 40), (250, 55), (170, 70)):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(30, 60 + a, 80 + a), width=6)
    d.line([cx - 330, cy, cx + 330, cy], fill=(60, 120, 230), width=4)
    d.line([cx, cy - 330, cx, cy + 330], fill=(60, 200, 120), width=4)
    y = int(size[1] * 0.40) + 400
    for text, fp, px, col in lines:
        f = font(fp, px)
        w = d.textlength(text, font=f)
        d.text(((size[0] - w) / 2, y), text, font=f, fill=col)
        y += int(px * 1.45)
    img.save(path, quality=95)
    return path


def still_scene(png, seconds, out):
    tmp = out.with_suffix(".part.mp4")
    run(["ffmpeg", "-y", "-framerate", str(FPS), "-i", str(png), "-vf",
         f"scale={W}:{H},format=yuv420p,loop=-1:1,trim=duration={seconds},fade=in:0:8,fade=out:st={seconds - 0.3}:d=0.3",
         *ENC, str(tmp)])
    tmp.rename(out)
    return out


def _esc(text):
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")


def clip_scene(src, out, *, start=None, from_end=None, seconds, caption, sub=None, fade=True):
    """A slice of a real clip, fitted into 1080x1920 (padded, never stretched), with a caption band."""
    tmp = out.with_suffix(".part.mp4")
    seek = ["-sseof", str(-from_end)] if from_end else ["-ss", str(start or 0)]
    vf = [f"scale={W}:{H}:force_original_aspect_ratio=decrease", f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x080e16",
          "drawbox=x=0:y=1640:w=iw:h=170:color=0x080e16@0.88:t=fill",
          f"drawtext=fontfile={BOLD}:text='{_esc(caption)}':fontcolor=white:fontsize=66:x=(w-tw)/2:y=1662"]
    if sub:
        vf.append(f"drawtext=fontfile={REG}:text='{_esc(sub)}':fontcolor=0x56f0be:fontsize=40:x=(w-tw)/2:y=1748")
    if fade:
        vf += ["fade=in:0:6", f"fade=out:st={seconds - 0.25}:d=0.25"]
    vf.append("format=yuv420p")
    run(["ffmpeg", "-y", *seek, "-i", str(src), "-t", str(seconds), "-vf", ",".join(vf), *ENC, str(tmp)])
    tmp.rename(out)
    return out


def concat(parts, out):
    listing = TMP / (out.stem + ".txt")
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    tmp = out.with_suffix(".part.mp4")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(tmp)])
    tmp.rename(out)
    return out


def landscape(vertical, out):
    """16:9 from the vertical cut: the vertical frame centred over a blurred, darkened copy of itself."""
    tmp = out.with_suffix(".part.mp4")
    run(["ffmpeg", "-y", "-i", str(vertical), "-filter_complex",
         "[0:v]scale=1920:-2,crop=1920:1080,boxblur=24:2,eq=brightness=-0.25[bg];"
         "[0:v]scale=-2:1080[fg];[bg][fg]overlay=(W-w)/2:0,format=yuv420p",
         "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-r", str(FPS), "-an", "-movflags", "+faststart", str(tmp)])
    tmp.rename(out)


def frame(src, out, at_end=None, at=None):
    seek = ["-sseof", str(-at_end)] if at_end else ["-ss", str(at or 0)]
    run(["ffmpeg", "-y", *seek, "-i", str(src), "-frames:v", "1", "-q:v", "2", str(out)])
    return Image.open(out).convert("RGB")


def main():
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required")
    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    C, G = MEDIA / "captured", MEDIA / "gameplay"
    easy = C / "rs_normal_L08_mirror-halfblock-block_clip_20261026.mp4"
    stuck = G / "ringshift_hard_stuck_gameplay.mp4"
    prism = G / "ringshift_prism_gameplay.mp4"
    hard25 = C / "rs_hard_L25_mirror-portal-amplifier_clip_20261026.mp4"
    hard24 = C / "rs_hard_L24_prism-mirror-halfblock_clip_20261026_9x16.mp4"
    rocket = G / "ringshift_rocket_attack_gameplay.mp4"

    title = still_scene(card([("RINGSHIFT", BOLD, 120, (255, 255, 255)),
                              ("Rotate the rings.", REG, 54, (210, 220, 230)),
                              ("Light every lock at once.", REG, 54, TEAL)], TMP / "title.png"), 2.4, TMP / "s01.mp4")
    scenes = [
        title,
        clip_scene(easy, TMP / "s02.mp4", from_end=4.0, seconds=4.0, caption="It starts simple", sub="Normal 8"),
        clip_scene(stuck, TMP / "s03.mp4", from_end=4.0, seconds=4.0, caption="Then it doesn’t", sub="Hard 18 · 15 moves, 0 of 4 locks"),
        clip_scene(prism, TMP / "s04.mp4", start=2.0, seconds=4.0, caption="Prisms split a beam in two"),
        clip_scene(hard25, TMP / "s05.mp4", from_end=4.5, seconds=4.5, caption="Portals. Amplifiers. Echoes."),
        clip_scene(hard24, TMP / "s06.mp4", from_end=4.5, seconds=4.5, caption="Every lock, all at once", sub="Hard 24 · solved"),
        clip_scene(rocket, TMP / "s07.mp4", start=3.0, seconds=3.2, caption="Plus special modes", sub="Rocket Attack"),
        still_scene(card([("110 LEVELS", BOLD, 104, (255, 255, 255)),
                          ("Free demo · plays in your browser", REG, 50, (210, 220, 230)),
                          (URL, BOLD, 56, TEAL)], TMP / "end.png"), 3.2, TMP / "s08.mp4"),
    ]
    trailer = concat(scenes, OUT / "ringshift_trailer_9x16.mp4")
    landscape(trailer, OUT / "ringshift_trailer_16x9.mp4")

    # Single-angle clips (no fades inside, so they loop cleanly on feeds)
    clip_scene(stuck, OUT / "looks_impossible_hard18.mp4", start=0, seconds=15.5,
               caption="15 moves. 0 of 4 locks.", sub="Hard 18 · can you see it?", fade=False)
    clip_scene(hard24, OUT / "satisfying_solve_hard24.mp4", start=0, seconds=8.6,
               caption="Every lock, all at once", sub="Hard 24 · 3 rings, 3 locks", fade=False)
    clip_scene(prism, OUT / "mechanism_prism.mp4", start=0, seconds=9.4,
               caption="A prism splits one beam into two", sub="both branches can light different locks", fade=False)

    # Simple vs advanced still: Easy 5 (one mechanism) next to Hard 15 (five), both real screenshots
    stills(C, G)


def stills(C, G):
    a = Image.open(G / "09_ice_theme_easy5.png").convert("RGB")
    b = Image.open(G / "06_hard15_five_mechanisms.png").convert("RGB")
    canvas = Image.new("RGB", (a.width + b.width + 40, a.height + 200), BG)
    canvas.paste(a, (0, 200)); canvas.paste(b, (a.width + 40, 200))
    d = ImageDraw.Draw(canvas)
    f, g = font(BOLD, 76), font(REG, 44)
    for text, sub, x0, w in (("Easy 5", "1 mechanism", 0, a.width), ("Hard 15", "5 mechanisms", a.width + 40, b.width)):
        d.text((x0 + (w - d.textlength(text, font=f)) / 2, 30), text, font=f, fill=(255, 255, 255))
        d.text((x0 + (w - d.textlength(sub, font=g)) / 2, 125), sub, font=g, fill=TEAL)
    canvas.save(OUT / "simple_vs_advanced.jpg", quality=92)

    # Challenge still: the real unsolved Hard 24 board with one line on it
    p = Image.open(C / "rs_hard_L24_prism-mirror-halfblock_puzzle_20261026_9x16.jpg").convert("RGB")
    d = ImageDraw.Draw(p)
    d.rectangle([0, p.height - 300, p.width, p.height - 120], fill=BG)
    f1, f2 = font(BOLD, 56), font(REG, 40)
    for text, ft, y, col in (("3 rings. 3 locks. Which ring first?", f1, p.height - 280, (255, 255, 255)),
                             ("Ringshift · Hard 24", f2, p.height - 190, TEAL)):
        d.text(((p.width - d.textlength(text, font=ft)) / 2, y), text, font=ft, fill=col)
    p.save(OUT / "challenge_hard24_puzzle.jpg", quality=92)
    print("\n".join(sorted(str(x.relative_to(ROOT)) for x in OUT.iterdir() if x.is_file())))


if __name__ == "__main__":
    if "--stills-only" in sys.argv:
        OUT.mkdir(parents=True, exist_ok=True)
        stills(MEDIA / "captured", MEDIA / "gameplay")
    else:
        main()
