"""FFmpeg / ffprobe helpers: probing, validation and clip preparation for X.

Only the Python standard library is used. `ffmpeg` and `ffprobe` must be on
PATH (free downloads for Windows, macOS and Linux; preinstalled on GitHub's
ubuntu runners).

Run `python scripts/media_tools.py --help` for the command line.
"""

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# Limits used by default. X itself allows far more (512 MB / 140 s for video),
# but short, small files fetch reliably from GitHub and autoplay well in a feed.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 15 * 1024 * 1024
MIN_VIDEO_SECONDS = 1.0
MAX_VIDEO_SECONDS = 45.0
MAX_DIMENSION = 1920
MIN_DIMENSION = 300
MAX_FPS = 60

ASPECTS = {
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "16:9": (1280, 720),
}


class MediaToolError(RuntimeError):
    pass


# ---- header inspection (pure Python: needs no FFmpeg) -----------------------------------------------------
# A placeholder such as the 3 bytes b"png" must never count as an image. These checks read only the file's own
# bytes: size floor, the real signature (not just the extension), PNG/JPEG dimensions, and a complete ending.

MIN_IMAGE_BYTES = 1024
MIN_VIDEO_BYTES = 4096
MAX_IMAGE_DIMENSION = 8192

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"


def _sniff(head):
    """The real format named by the first bytes, or None."""
    if head.startswith(PNG_MAGIC):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if head[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip"):
        return "mp4"
    return None


def _jpeg_size(data):
    """(width, height) from the first SOF marker, or None."""
    position = 2
    while position + 9 < len(data):
        if data[position] != 0xFF:
            position += 1
            continue
        marker = data[position + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        length = int.from_bytes(data[position + 2:position + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return int.from_bytes(data[position + 7:position + 9], "big"), int.from_bytes(data[position + 5:position + 7], "big")
        position += 2 + max(length, 2)
    return None


def mp4_structure_problems(path, size):
    """Problems with the top-level box layout of an MP4/MOV (no FFmpeg): boxes must add up to the file size exactly
    and include both 'moov' (the index) and 'mdat' (the frames). Catches truncated or half-written clips."""
    seen = set()
    position = 0
    try:
        with open(path, "rb") as handle:
            while position < size:
                handle.seek(position)
                header = handle.read(16)
                if len(header) < 8:
                    return ["MP4 ends in the middle of a box (truncated)"]
                length = int.from_bytes(header[:4], "big")
                kind = header[4:8]
                if length == 1:
                    if len(header) < 16:
                        return ["MP4 ends in the middle of a box (truncated)"]
                    length = int.from_bytes(header[8:16], "big")
                elif length == 0:
                    length = size - position
                if length < 8 or position + length > size:
                    return [f"MP4 box '{kind.decode('latin-1')}' runs past the end of the file (truncated or damaged)"]
                seen.add(kind)
                position += length
    except OSError as error:
        return [f"cannot be read ({type(error).__name__})"]
    missing = [name.decode() for name in (b"moov", b"mdat") if name not in seen]
    return [f"MP4 has no '{name}' box (incomplete recording)" for name in missing]


def inspect_file(path):
    """Header-level facts and problems for one media file: {"ok", "problems", "format", "width", "height", "bytes"}.

    Never raises. `ok` is False for a missing/empty/tiny file, an extension that does not match the real format,
    a truncated PNG/JPEG, or an image smaller than MIN_DIMENSION. It does NOT prove a video plays: that needs
    FFmpeg (see verify_media_file)."""
    path = Path(path)
    result = {"ok": False, "problems": [], "format": None, "width": None, "height": None, "bytes": None}
    problems = result["problems"]

    try:
        size = path.stat().st_size
    except OSError:
        problems.append("file does not exist")
        return result

    result["bytes"] = size
    suffix = path.suffix.lower()
    is_video = suffix in VIDEO_EXTENSIONS
    is_image = suffix in IMAGE_EXTENSIONS

    if not (is_video or is_image):
        problems.append(f"unsupported file type {suffix or '(none)'}")
        return result
    if size == 0:
        problems.append("file is empty (0 bytes)")
        return result

    floor = MIN_VIDEO_BYTES if is_video else MIN_IMAGE_BYTES
    if size < floor:
        problems.append(f"file is only {size} bytes: too small to be a real {'video' if is_video else 'image'} (minimum {floor})")

    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
            data = handle.read(1 << 20) if suffix in (".jpg", ".jpeg") else b""
            handle.seek(max(size - 16, 0))
            tail = handle.read(16)
    except OSError as error:
        problems.append(f"cannot be read ({type(error).__name__})")
        return result

    found = _sniff(head)
    result["format"] = found
    expected = {".png": {"png"}, ".jpg": {"jpeg"}, ".jpeg": {"jpeg"}, ".webp": {"webp"},
                ".mp4": {"mp4"}, ".mov": {"mp4"}, ".webm": {"webm"}}[suffix]

    if found is None:
        problems.append(f"contents are not a {suffix[1:]} file (no valid file signature)")
    elif found not in expected:
        problems.append(f"extension says {suffix[1:]} but the contents are {found}")
    elif found == "png":
        if head[12:16] != b"IHDR":
            problems.append("PNG header is damaged")
        else:
            result["width"] = int.from_bytes(head[16:20], "big")
            result["height"] = int.from_bytes(head[20:24], "big")
        if size >= 24 and not tail.endswith(PNG_END[4:]):
            problems.append("PNG is truncated (no end marker)")
    elif found == "jpeg":
        dimensions = _jpeg_size(head + data)
        if dimensions:
            result["width"], result["height"] = dimensions
        else:
            problems.append("JPEG header is damaged (no size found)")
        if b"\xff\xd9" not in tail:
            problems.append("JPEG is truncated (no end marker)")

    elif found == "mp4" and size >= 8:
        problems.extend(mp4_structure_problems(path, size))

    if result["width"] is not None:
        if result["width"] < MIN_DIMENSION or result["height"] < MIN_DIMENSION:
            problems.append(f"image is too small ({result['width']}x{result['height']}; minimum {MIN_DIMENSION}x{MIN_DIMENSION})")
        if result["width"] > MAX_IMAGE_DIMENSION or result["height"] > MAX_IMAGE_DIMENSION:
            problems.append(f"image is too large ({result['width']}x{result['height']})")

    result["ok"] = not problems
    return result


def verify_media_file(path):
    """Everything that must be true of a file before it can be approved for posting: header inspection, then a
    real decode with FFmpeg. Returns a list of problems ([] = fit). Fails closed: without FFmpeg it reports that
    it could not check, instead of assuming the file is fine."""
    inspected = inspect_file(path)
    if not inspected["ok"]:
        return inspected["problems"]
    if not have_ffmpeg():
        return ["FFmpeg is not available, so the file could not be decoded; install ffmpeg/ffprobe (no approval without a decode check)"]
    return validate_media(path)


def have_ffmpeg():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _require_ffmpeg():
    if not have_ffmpeg():
        raise MediaToolError(
            "ffmpeg and ffprobe are required but were not found on PATH. "
            "Install FFmpeg (https://ffmpeg.org/download.html) and retry."
        )


def _run(command, **kwargs):
    return subprocess.run(command, capture_output=True, check=False, **kwargs)


def probe(path):
    """Facts about a media file from ffprobe. Raises MediaToolError if unreadable."""
    _require_ffmpeg()
    result = _run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ])

    if result.returncode != 0:
        raise MediaToolError(f"ffprobe could not read {path}: {result.stderr.decode(errors='replace').strip()[:300]}")

    data = json.loads(result.stdout or b"{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)

    if video is None:
        raise MediaToolError(f"{path} has no video/image stream")

    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        numerator, denominator = rate.split("/")
        fps = float(numerator) / float(denominator) if float(denominator) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    fmt = data.get("format", {})
    try:
        duration = float(fmt.get("duration") or video.get("duration") or 0)
    except ValueError:
        duration = 0.0

    return {
        "codec": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 2),
        "duration": round(duration, 3),
        "bytes": Path(path).stat().st_size,
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
        "nb_frames": int(video.get("nb_frames") or 0) if str(video.get("nb_frames") or "0").isdigit() else 0,
    }


def frame_gray(path, at=0.0, size=64):
    """A size x size grayscale thumbnail as bytes (for brightness / contrast checks)."""
    _require_ffmpeg()
    # No -ss at 0: seeking in a single JPEG image makes ffmpeg return nothing.
    seek = ["-ss", f"{at:.3f}"] if at > 0 else []
    result = _run([
        "ffmpeg", "-v", "error", *seek, "-i", str(path),
        "-frames:v", "1", "-vf", f"scale={size}:{size},format=gray",
        "-f", "rawvideo", "-",
    ])
    data = result.stdout

    if len(data) != size * size:
        return None

    return data


def gameplay_visible(path, info=None, samples=12):
    """(ok, detail). A clip counts as visible gameplay when most sampled frames
    have real contrast (not blank / solid colour) and, for video, the picture
    actually changes over time (not frozen)."""
    info = info or probe(path)
    is_video = Path(path).suffix.lower() in VIDEO_EXTENSIONS and info["duration"] > 0.2

    if is_video:
        duration = info["duration"]
        times = [duration * (i + 0.5) / samples for i in range(samples)]
    else:
        times = [0.0]

    frames = [frame_gray(path, t, size=96) for t in times]
    frames = [f for f in frames if f]

    if not frames:
        return False, "could not decode any frame"

    contrasts = [statistics.pstdev(f) for f in frames]
    contrasty = sum(1 for c in contrasts if c >= 6.0)

    if contrasty < max(1, int(len(frames) * 0.8 + 0.999)):
        return False, f"frames look blank or flat (contrast {', '.join(f'{c:.1f}' for c in contrasts)})"

    if is_video and len(frames) > 1:
        diffs = [
            sum(abs(a - b) for a, b in zip(frames[i], frames[i + 1])) / len(frames[i])
            for i in range(len(frames) - 1)
        ]
        # Calibrated on the real library: every genuine gameplay clip has a
        # frame-to-frame change of 3+ at this sampling; a static clip scores < 0.5.
        if max(diffs) < 0.5:
            return False, "picture never changes (frozen or static clip)"

    return True, "frames have contrast" + (" and motion" if is_video else "")


def validate_media(
    path,
    *,
    max_image_bytes=MAX_IMAGE_BYTES,
    max_video_bytes=MAX_VIDEO_BYTES,
    min_seconds=MIN_VIDEO_SECONDS,
    max_seconds=MAX_VIDEO_SECONDS,
    check_visible=True,
):
    """Return a list of problems (empty list = fit to attach to an X post)."""
    path = Path(path)
    problems = []

    if not path.is_file():
        return [f"file does not exist: {path}"]

    suffix = path.suffix.lower()
    is_video = suffix in VIDEO_EXTENSIONS
    is_image = suffix in IMAGE_EXTENSIONS

    if not (is_video or is_image):
        return [f"unsupported file type {suffix or '(none)'}"]

    if path.stat().st_size == 0:
        return ["file is empty"]

    try:
        info = probe(path)
    except MediaToolError as error:
        return [str(error)]

    limit = max_video_bytes if is_video else max_image_bytes
    if info["bytes"] > limit:
        problems.append(f"file is {info['bytes'] / 1048576:.1f} MB; the limit is {limit / 1048576:.0f} MB")

    if info["width"] < MIN_DIMENSION or info["height"] < MIN_DIMENSION:
        problems.append(f"too small ({info['width']}x{info['height']})")

    if info["width"] > MAX_DIMENSION or info["height"] > MAX_DIMENSION:
        problems.append(f"too large ({info['width']}x{info['height']}); the maximum is {MAX_DIMENSION}")

    if is_video:
        if info["codec"] != "h264":
            problems.append(f"video codec is {info['codec']}; use H.264")
        if info["pix_fmt"] != "yuv420p":
            problems.append(f"pixel format is {info['pix_fmt']}; use yuv420p")
        if info["duration"] < min_seconds:
            problems.append(f"too short ({info['duration']:.1f}s)")
        if info["duration"] > max_seconds:
            problems.append(f"too long ({info['duration']:.1f}s; the limit is {max_seconds:.0f}s)")
        if info["fps"] > MAX_FPS:
            problems.append(f"frame rate {info['fps']} is above {MAX_FPS}")
        if info["width"] % 2 or info["height"] % 2:
            problems.append("width and height must be even for H.264")

    if check_visible and not problems:
        ok, detail = gameplay_visible(path, info)
        if not ok:
            problems.append(f"gameplay not visible: {detail}")

    return problems


# ---- clip preparation --------------------------------------------------------

def aspect_filter(src_width, src_height, aspect="4:5", crop_y=0.0):
    """ffmpeg -vf chain that crops to `aspect` and scales to its standard size.

    `crop_y` (0..1) picks where a too-tall source is cropped from: 0 keeps the
    top (HUD and puzzle ring in Ringshift's portrait layout)."""
    if aspect not in ASPECTS:
        raise MediaToolError(f"unknown aspect {aspect!r}; choose from {sorted(ASPECTS)}")

    target_w, target_h = ASPECTS[aspect]
    target_ratio = target_w / target_h
    source_ratio = src_width / src_height

    filters = []

    if abs(source_ratio - target_ratio) > 0.002:
        if source_ratio > target_ratio:
            crop_w = int(round(src_height * target_ratio / 2) * 2)
            filters.append(f"crop={crop_w}:{src_height}:{(src_width - crop_w) // 2}:0")
        else:
            crop_h = int(round(src_width / target_ratio / 2) * 2)
            offset = int(max(0, (src_height - crop_h)) * min(max(crop_y, 0.0), 1.0))
            filters.append(f"crop={src_width}:{crop_h}:0:{offset}")

    filters.append(f"scale={target_w}:{target_h}:flags=lanczos")
    return ",".join(filters), (target_w, target_h)


def _zoom_filter(size, fps, seconds, zoom):
    width, height = size
    frames = max(int(seconds * fps), 1)
    step = (zoom - 1.0) / frames
    return (
        f"zoompan=z='min(1+{step:.6f}*on,{zoom})':d=1"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps}"
    )


def encode_video(source, destination, *, aspect="4:5", crop_y=0.0, start=None, duration=None,
                 fps=30, zoom=None, crf=21):
    """Re-encode `source` to an X-ready H.264 MP4 (silent, faststart, even dimensions)."""
    _require_ffmpeg()
    info = probe(source)
    vf, size = aspect_filter(info["width"], info["height"], aspect, crop_y)

    clip_seconds = duration if duration else max(info["duration"] - (start or 0), 0.1)

    if zoom and zoom > 1.0:
        vf = f"{vf},{_zoom_filter(size, fps, clip_seconds, zoom)}"

    command = ["ffmpeg", "-v", "error", "-y"]
    if start:
        command += ["-ss", f"{start:.3f}"]
    command += ["-i", str(source)]
    if duration:
        command += ["-t", f"{duration:.3f}"]
    command += [
        "-vf", vf, "-r", str(fps), "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(destination),
    ]

    result = _run(command)
    if result.returncode != 0:
        raise MediaToolError(f"ffmpeg failed: {result.stderr.decode(errors='replace').strip()[:400]}")

    return probe(destination)


def encode_frames(frames, destination, *, aspect="4:5", crop_y=0.0, fps=30, zoom=None, crf=21,
                  hold_last=0.0):
    """Encode timestamped screenshots [(seconds, path), ...] to an MP4.

    Browser screencasts arrive at an irregular rate; the timestamps are used so
    the clip plays at real speed instead of speeding up or slowing down."""
    _require_ffmpeg()

    if len(frames) < 2:
        raise MediaToolError("need at least two frames to make a video")

    frames = sorted(frames, key=lambda item: item[0])

    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "frames.txt"
        lines = []

        for index, (stamp, frame_path) in enumerate(frames):
            if index + 1 < len(frames):
                length = max(frames[index + 1][0] - stamp, 0.001)
            else:
                length = max(hold_last, 0.04)
            lines.append(f"file '{Path(frame_path).resolve().as_posix()}'")
            lines.append(f"duration {length:.4f}")

        # The concat demuxer ignores the final duration unless the last file repeats.
        lines.append(f"file '{Path(frames[-1][1]).resolve().as_posix()}'")
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # The concat demuxer pads the repeated final frame, so cap the length explicitly.
        total_seconds = (frames[-1][0] - frames[0][0]) + max(hold_last, 0.04)

        intermediate = Path(tmp) / "raw.mp4"
        first = _run([
            "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-t", f"{total_seconds:.4f}",
            "-vsync", "cfr", "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "12",
            "-pix_fmt", "yuv420p", str(intermediate),
        ])
        if first.returncode != 0:
            raise MediaToolError(f"ffmpeg failed: {first.stderr.decode(errors='replace').strip()[:400]}")

        return encode_video(intermediate, destination, aspect=aspect, crop_y=crop_y, fps=fps,
                            zoom=zoom, crf=crf)


def make_poster(video, destination, at=None):
    """Save one frame of `video` as a PNG (defaults to the middle of the clip)."""
    _require_ffmpeg()
    info = probe(video)
    moment = info["duration"] / 2 if at is None else at
    result = _run([
        "ffmpeg", "-v", "error", "-y", "-ss", f"{moment:.3f}", "-i", str(video),
        "-frames:v", "1", str(destination),
    ])
    if result.returncode != 0 or not Path(destination).exists():
        raise MediaToolError(f"could not extract a poster frame: {result.stderr.decode(errors='replace')[:300]}")
    return Path(destination)


def convert_image(source, destination, *, aspect="4:5", crop_y=0.0):
    """Crop/scale a screenshot to `aspect`. A .jpg destination is written as high-quality JPEG
    (a game screenshot is ~0.7 MB as PNG and ~0.15 MB as JPEG, which keeps the repo small)."""
    _require_ffmpeg()
    info = probe(source)
    vf, _ = aspect_filter(info["width"], info["height"], aspect, crop_y)
    quality = ["-q:v", "2"] if str(destination).lower().endswith((".jpg", ".jpeg")) else []
    result = _run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-vf", vf, "-frames:v", "1", *quality, str(destination)])
    if result.returncode != 0:
        raise MediaToolError(f"ffmpeg failed: {result.stderr.decode(errors='replace')[:300]}")
    return probe(destination)


# ---- command line ------------------------------------------------------------

def _cmd_check(args):
    failed = 0
    for name in args.files:
        problems = validate_media(name, max_video_bytes=int(args.max_mb * 1048576),
                                  max_seconds=args.max_seconds)
        if problems:
            failed += 1
            print(f"FAIL  {name}")
            for problem in problems:
                print(f"        - {problem}")
        else:
            info = probe(name)
            print(f"OK    {name}  {info['width']}x{info['height']}  {info['duration']:.1f}s  {info['bytes'] / 1048576:.1f} MB")
    return 1 if failed else 0


def _cmd_trim(args):
    info = encode_video(args.source, args.output, aspect=args.aspect, crop_y=args.crop_y,
                        start=args.start, duration=args.duration, zoom=args.zoom)
    problems = validate_media(args.output)
    print(f"wrote {args.output}: {info['width']}x{info['height']} {info['duration']:.1f}s {info['bytes'] / 1048576:.1f} MB")
    for problem in problems:
        print(f"  problem: {problem}")
    return 1 if problems else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate and prepare Ringshift media for X.")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate files against the X/Buffer limits")
    check.add_argument("files", nargs="+")
    check.add_argument("--max-mb", type=float, default=MAX_VIDEO_BYTES / 1048576)
    check.add_argument("--max-seconds", type=float, default=MAX_VIDEO_SECONDS)
    check.set_defaults(func=_cmd_check)

    trim = sub.add_parser("trim", help="trim/crop/re-encode a recording into an X-ready clip")
    trim.add_argument("source")
    trim.add_argument("output")
    trim.add_argument("--start", type=float, default=0.0)
    trim.add_argument("--duration", type=float, default=None)
    trim.add_argument("--aspect", default="4:5", choices=sorted(ASPECTS))
    trim.add_argument("--crop-y", type=float, default=0.0, help="0=keep top, 1=keep bottom of a too-tall source")
    trim.add_argument("--zoom", type=float, default=None, help="optional slow push-in, e.g. 1.06")
    trim.set_defaults(func=_cmd_trim)

    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except MediaToolError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
