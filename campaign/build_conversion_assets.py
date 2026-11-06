"""Conversion-sprint assets, all from real captured frames and clips (media/captured). Nothing is staged.

  before_after_hard25.jpg / before_after_normal20.jpg   unsolved and solved frame of the same level, side by side
  itch_solve_hard24.gif                                  ~6 s loop for the itch.io description (<= 3 MB)
  itch/screenshots/01..06_*.jpg                          itch.io screenshot set in display order (see ITCH_PAGE.md)

Run: python campaign/build_conversion_assets.py
"""
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
CAP = ROOT / "media" / "captured"
GAME = ROOT / "media" / "gameplay"
OUT = ROOT / "campaign" / "assets"
SHOTS = ROOT / "campaign" / "itch" / "screenshots"
BOLD = "/usr/share/fonts/opentype/inter/Inter-Bold.otf"
REG = "/usr/share/fonts/opentype/inter/Inter-Regular.otf"
BG = (8, 14, 22)
TEAL = (86, 240, 190)
RED = (255, 107, 114)


def before_after(level_file_stem, label, out):
    before = Image.open(CAP / f"{level_file_stem}_puzzle_20261026.jpg").convert("RGB")
    after = Image.open(CAP / f"{level_file_stem}_solved_20261026.jpg").convert("RGB")
    gap, head = 32, 190
    canvas = Image.new("RGB", (before.width + after.width + gap, before.height + head), BG)
    canvas.paste(before, (0, head))
    canvas.paste(after, (before.width + gap, head))
    d = ImageDraw.Draw(canvas)
    big, small = ImageFont.truetype(BOLD, 76), ImageFont.truetype(REG, 42)
    for text, sub, col, x0 in (("Before", "nothing lit", RED, 0), ("After", "every lock lit", TEAL, before.width + gap)):
        d.text((x0 + (before.width - d.textlength(text, font=big)) / 2, 28), text, font=big, fill=(255, 255, 255))
        d.text((x0 + (before.width - d.textlength(sub, font=small)) / 2, 118), sub, font=small, fill=col)
    d.text((canvas.width - 20 - d.textlength(label, font=small), canvas.height - 64), label, font=small, fill=(200, 210, 220))
    canvas.save(out, quality=92)


def gif(src, out, start, seconds, width=480, fps=15):
    palette = OUT / "_palette.png"
    vf = f"fps={fps},scale={width}:-2:flags=lanczos"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(seconds), "-i", str(src),
                    "-vf", f"{vf},palettegen=stats_mode=diff", str(palette)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(seconds), "-i", str(src), "-i", str(palette),
                    "-lavfi", f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4", "-loop", "0", str(out)], check=True)
    palette.unlink()


def screenshots():
    """itch screenshot order: understand -> want -> trust (see campaign/itch/ITCH_PAGE.md)."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    order = [
        ("01_hard24_solved.jpg", CAP / "rs_hard_L24_prism-mirror-halfblock_solved_20261026_16x9.jpg"),
        ("02_hard24_unsolved.jpg", CAP / "rs_hard_L24_prism-mirror-halfblock_puzzle_20261026_16x9.jpg"),
        ("03_easy5_vs_hard15.jpg", OUT / "simple_vs_advanced.jpg"),
        ("04_before_after_hard25.jpg", OUT / "before_after_hard25.jpg"),
        ("05_hard1_halfblock_portal.jpg", CAP / "rs_hard_L01_halfblock-portal-block_solved_20261026_16x9.jpg"),
        ("06_lava_theme_normal20.jpg", GAME / "08_lava_theme_normal20.png"),
    ]
    for name, src in order:
        img = Image.open(src).convert("RGB")
        img.save(SHOTS / name, quality=90)


def main():
    before_after("rs_hard_L25_mirror-portal-amplifier", "Ringshift · Hard 25", OUT / "before_after_hard25.jpg")
    before_after("rs_normal_L20_mirror-portal-block", "Ringshift · Normal 20", OUT / "before_after_normal20.jpg")
    gif(CAP / "rs_hard_L24_prism-mirror-halfblock_clip_20261026_16x9.mp4", OUT / "itch_solve_hard24.gif", start=3.0, seconds=6.3)
    screenshots()
    for p in sorted(list(OUT.glob("before_after_*")) + [OUT / "itch_solve_hard24.gif"] + sorted(SHOTS.iterdir())):
        print(f"{p.relative_to(ROOT)}  {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is required")
    main()
