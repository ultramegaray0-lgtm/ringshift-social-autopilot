"""TikTok video automation: clip selection, state tracking, board detection and crop maths, audio handling, the demo
card's placement, output validation and failure cases, and that the X autopilot never sees the TikTok folders.

Media fixtures are real files made on the fly with FFmpeg/Pillow (a board-like animation with a KNOWN centre and radius,
and funny clips with known audio), so every check runs against real decoding. No network is used (support.py blocks
it). Tests that need FFmpeg or numpy are skipped where they are missing, except in CI, which sets
RINGSHIFT_REQUIRE_FFMPEG=1 and installs both: there a missing tool is a failure, not a skip.

The optional real-game test at the bottom plays a real level in Chromium when RINGSHIFT_GAME_DIR points at a checkout
of the ringshift-game repository (and Playwright/Chromium are installed).
"""

import json
import math
import os
import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (blocks network, sets up paths)
import media_manifest
import media_selector
import media_tools as tools
import tiktok_board as tb
import tiktok_state as ts
import tiktok_video as tv

try:
    import numpy as np
    from PIL import Image, ImageDraw
    HAVE_PY_DEPS = True
except ImportError:  # pragma: no cover - CI installs requirements-tiktok.txt
    HAVE_PY_DEPS = False

HAVE_FFMPEG = tools.have_ffmpeg()
REQUIRED = os.environ.get("RINGSHIFT_REQUIRE_FFMPEG") == "1"
HAVE_GIT = shutil.which("git") is not None
ROOT = support.ROOT
WORKFLOW = (ROOT / ".github" / "workflows" / "tiktok-video.yml").read_text(encoding="utf-8")


def needs_media(test):
    """Skip without FFmpeg + numpy + Pillow, but fail in CI, where they must be present."""
    def wrapper(self, *args, **kwargs):
        if not (HAVE_FFMPEG and HAVE_PY_DEPS):
            if REQUIRED:
                self.fail("RINGSHIFT_REQUIRE_FFMPEG=1 but FFmpeg, numpy or Pillow is missing")
            self.skipTest("FFmpeg, numpy or Pillow not installed")
        return test(self, *args, **kwargs)
    wrapper.__name__ = test.__name__
    wrapper.__doc__ = test.__doc__
    return wrapper


# ------------------------------------------------------------------------------------------------ fixtures

def run(command):
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode(errors="replace")[-600:])


def make_funny(path, seconds=4.0, size=(640, 360), audio="aac", tone=True):
    """A funny-clip stand-in: moving test picture and a clearly patterned sound (a warbling tone). audio=None for a
    silent clip; "pcm" writes a MOV with uncompressed audio (forces the AAC re-encode path)."""
    w, h = size
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=30:d={seconds}"]
    if audio:
        expr = "0.45*sin(2*PI*(330+180*sin(2*PI*0.9*t))*t)*(0.55+0.45*sin(2*PI*2.5*t))" if tone else "0"
        command += ["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d={seconds}"]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio == "aac":
        command += ["-c:a", "aac", "-b:a", "160k"]
    elif audio == "pcm":
        command += ["-c:a", "pcm_s16le"]
    command += ["-shortest", str(path)]
    run(command)
    return Path(path)


def board_frame(size, centre, radius, angle):
    """A board-like picture: dark background, a bright outer rim, coloured lock segments inside it, inner rings and a
    rotating beam. The OUTER RIM is exactly `radius` around `centre` (the ground truth for the detector)."""
    w, h = size
    image = Image.new("RGB", (w, h), (6, 10, 20))
    draw = ImageDraw.Draw(image)
    cx, cy = centre
    rng = random.Random(int(angle * 100))
    for _ in range(80):                                   # stars
        x, y = rng.randrange(w), rng.randrange(h)
        draw.point((x, y), fill=(120, 130, 160))
    for index in range(8):                                # lock band segments just inside the rim
        start = index * 45 + 4
        colour = [(200, 40, 40), (40, 200, 90), (40, 70, 120), (200, 120, 30)][index % 4]
        draw.arc((cx - radius + 6, cy - radius + 6, cx + radius - 6, cy + radius - 6), start, start + 37, fill=colour,
                 width=max(4, radius // 7))
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=(170, 200, 230), width=4)
    for fraction in (0.72, 0.5, 0.3):
        r = radius * fraction
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(90, 110, 140), width=2)
    for offset in (0, 2.1):
        x = cx + radius * 0.8 * math.cos(angle + offset)
        y = cy + radius * 0.8 * math.sin(angle + offset)
        draw.line((cx - (x - cx), cy - (y - cy), x, y), fill=(120, 255, 160), width=3)
    return image


def make_gameplay(path, size=(960, 540), centre=(470, 270), radius=230, seconds=4.0, fps=15, tone_hz=1000):
    """A gameplay recording stand-in with a known board and a LOUD sound track of its own (which must never reach
    the TikTok video)."""
    path = Path(path)
    with tempfile.TemporaryDirectory() as tmp:
        frames = int(seconds * fps)
        for index in range(frames):
            board_frame(size, centre, radius, index * 0.08).save(Path(tmp) / f"f{index:04d}.png")
        command = ["ffmpeg", "-v", "error", "-y", "-framerate", str(fps), "-i", str(Path(tmp) / "f%04d.png")]
        if tone_hz:
            command += ["-f", "lavfi", "-i", f"sine=frequency={tone_hz}:sample_rate=44100:duration={seconds}",
                        "-filter:a", "volume=4", "-c:a", "aac"]
        command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-shortest", str(path)]
        run(command)
    return path


def tone_energy(path, hz, seconds=3.0, rate=8000):
    """Share of the audio's energy at `hz` (FFT), to prove the gameplay tone is absent."""
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-t", str(seconds), "-map", "0:a:0", "-ac", "1",
                             "-ar", str(rate), "-f", "s16le", "-"], capture_output=True, check=True)
    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float64)
    spectrum = np.abs(np.fft.rfft(samples)) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1 / rate)
    band = (freqs > hz - 15) & (freqs < hz + 15)
    return float(spectrum[band].sum() / (spectrum.sum() + 1e-9))


class TempRepo(unittest.TestCase):
    """A temporary checkout layout: media/funny, media/gameplay, an output folder and a state file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.funny = self.root / "media" / "funny"
        self.gameplay = self.root / "media" / "gameplay"
        self.out = self.root / "out"
        for folder in (self.funny, self.gameplay, self.out):
            folder.mkdir(parents=True)
        self.state = ts.empty_state()


# ================================================================================================ clip selection

def ok_check(path):
    return {"duration": 5.0, "width": 640, "height": 360, "rotation": 0, "bytes": 5000, "video_codec": "h264",
            "has_audio": True, "audio_codec": "aac", "audio_channels": 2, "audio_rate": 44100, "audio_streams": 1}, []


class ClipSelectionTests(TempRepo):
    def write(self, name, content=None):
        path = self.funny / name
        path.write_bytes(content if content is not None else name.encode() * 50)
        return path

    def test_an_empty_folder_is_a_clear_skip(self):
        chosen, report = tv.review_funny(self.state, self.funny, check=ok_check)
        self.assertIsNone(chosen)
        self.assertIn("no funny clips in media/funny/", tv.skip_reason(report))

    def test_readme_and_placeholders_are_ignored_and_unsupported_formats_are_explained(self):
        self.write("README.md")
        self.write(".gitkeep")
        self.write("clip.avi")
        chosen, report = tv.review_funny(self.state, self.funny, check=ok_check)
        self.assertIsNone(chosen)
        self.assertEqual([(r["file"].split("/")[-1], r["status"]) for r in report], [("clip.avi", "invalid")])
        self.assertIn("use MP4, MOV or WebM", report[0]["why"])

    def test_picks_the_first_unused_clip_by_name(self):
        self.write("b.mp4")
        self.write("a.mov")
        chosen, report = tv.review_funny(self.state, self.funny, check=ok_check)
        self.assertEqual(Path(chosen["file"]).name, "a.mov")
        self.assertEqual({Path(r["file"]).name: r["status"] for r in report}, {"a.mov": "eligible", "b.mp4": "eligible"})

    def test_a_used_clip_is_skipped_even_when_renamed(self):
        used = self.write("old.mp4", b"x" * 5000)
        ts.record_video(self.state, {"funny_sha256": tv.sha256_file(used), "funny_file": "media/funny/old.mp4",
                                     "output_name": "ringshift-tiktok-1"})
        used.rename(self.funny / "renamed.mp4")
        self.write("new.mp4", b"y" * 5000)
        chosen, report = tv.review_funny(self.state, self.funny, check=ok_check)
        self.assertEqual(Path(chosen["file"]).name, "new.mp4")
        renamed = next(r for r in report if r["file"].endswith("renamed.mp4"))
        self.assertEqual(renamed["status"], "used")
        self.assertIn("ringshift-tiktok-1", renamed["why"])

    def test_an_edited_clip_counts_as_new(self):
        clip = self.write("clip.mp4", b"x" * 5000)
        ts.record_video(self.state, {"funny_sha256": tv.sha256_file(clip), "funny_file": "clip.mp4"})
        clip.write_bytes(b"z" * 5000)
        chosen, _ = tv.review_funny(self.state, self.funny, check=ok_check)
        self.assertIsNotNone(chosen)

    def test_identical_files_are_used_once(self):
        self.write("a.mp4", b"same" * 2000)
        self.write("b.mp4", b"same" * 2000)
        chosen, report = tv.review_funny(self.state, self.funny, check=ok_check)
        self.assertEqual(Path(chosen["file"]).name, "a.mp4")
        self.assertEqual(report[-1]["status"], "duplicate")

    def test_invalid_clips_are_skipped_with_the_reason_and_the_next_valid_one_is_used(self):
        self.write("a.mp4")
        self.write("b.mp4")

        def check(path):
            return (None, ["contents are not a mp4 file"]) if path.name == "a.mp4" else ok_check(path)

        chosen, report = tv.review_funny(self.state, self.funny, check=check)
        self.assertEqual(Path(chosen["file"]).name, "b.mp4")
        self.assertEqual(report[0]["status"], "invalid")
        self.assertIn("not a mp4", report[0]["why"])

    def test_a_clip_that_failed_three_times_is_not_retried_automatically_but_can_be_asked_for(self):
        clip = self.write("bad.mp4")
        for _ in range(ts.MAX_FAILURES):
            ts.record_failure(self.state, tv.sha256_file(clip), "media/funny/bad.mp4", "ffmpeg broke")
        chosen, report = tv.review_funny(self.state, self.funny, check=ok_check)
        self.assertIsNone(chosen)
        self.assertEqual(report[0]["status"], "failed-before")
        chosen, _ = tv.review_funny(self.state, self.funny, wanted=clip, check=ok_check)
        self.assertIsNotNone(chosen)

    def test_asking_for_a_clip_picks_it_and_reuse_needs_permission(self):
        self.write("a.mp4")
        wanted = self.write("b.mp4")
        chosen, _ = tv.review_funny(self.state, self.funny, wanted=wanted, check=ok_check)
        self.assertEqual(Path(chosen["file"]).name, "b.mp4")
        ts.record_video(self.state, {"funny_sha256": chosen["sha256"], "funny_file": "b.mp4"})
        self.assertIsNone(tv.review_funny(self.state, self.funny, wanted=wanted, check=ok_check)[0])
        self.assertIsNotNone(tv.review_funny(self.state, self.funny, wanted=wanted, allow_reuse=True, check=ok_check)[0])

    def test_asking_for_a_file_outside_the_funny_folder_is_refused(self):
        outside = self.root / "elsewhere.mp4"
        outside.write_bytes(b"x" * 5000)
        with self.assertRaises(tv.TikTokError):
            tv.review_funny(self.state, self.funny, wanted=outside, check=ok_check)
        with self.assertRaises(tv.TikTokError):
            tv._resolve_funny_arg(str(outside), self.funny)
        with self.assertRaises(tv.TikTokError):
            tv._resolve_funny_arg("../../etc/passwd", self.funny)

    @needs_media
    def test_real_validation_of_clips(self):
        good = make_funny(self.funny / "good.mp4", seconds=3)
        info, problems = tv.check_funny(good)
        self.assertEqual(problems, [])
        self.assertTrue(info["has_audio"])
        self.assertEqual((info["width"], info["height"]), (640, 360))

        fake = self.funny / "fake.mp4"
        fake.write_bytes(b"not a video" * 1000)
        self.assertTrue(tv.check_funny(fake)[1])

        truncated = self.funny / "truncated.mp4"
        truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
        self.assertTrue(tv.check_funny(truncated)[1])

        short = make_funny(self.funny / "short.mp4", seconds=1)
        self.assertIn("too short", " ".join(tv.check_funny(short)[1]))

        with mock.patch.object(tv, "MAX_FUNNY_SECONDS", 2.5):
            self.assertIn("too long", " ".join(tv.check_funny(good)[1]))

    @needs_media
    def test_rotation_metadata_gives_the_displayed_size(self):
        clip = make_funny(self.root / "landscape.mp4", seconds=2)
        rotated = self.root / "rotated.mp4"
        run(["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(clip), "-c", "copy", str(rotated)])
        info = tv.probe_clip(rotated)
        self.assertEqual((info["width"], info["height"]), (360, 640))


# ================================================================================================ state tracking

class StateTests(unittest.TestCase):
    def test_record_and_query(self):
        state = ts.empty_state()
        ts.record_failure(state, "aaa", "a.mp4", "boom")
        ts.record_video(state, {"funny_sha256": "aaa", "funny_file": "a.mp4", "run_id": "77", "output_name": "v1",
                                "gameplay_sha256": ["g1"], "scenes": [{"scene": "hard:3", "labels": ["Prism"]}]})
        self.assertEqual(ts.used_hashes(state), {"aaa"})
        self.assertEqual(ts.video_for_run(state, "77")["output_name"], "v1")
        self.assertIsNone(ts.video_for_run(state, "78"))
        self.assertEqual(ts.failure_count(state, "aaa"), 0)          # a success clears earlier failures
        self.assertEqual(state["gameplay_uses"], {"g1": 1})
        self.assertEqual(state["scenes"][0]["scene"], "hard:3")

    def test_failures_count_up(self):
        state = ts.empty_state()
        for _ in range(3):
            ts.record_failure(state, "bbb", "b.mp4", "nope", run_id="1")
        self.assertEqual(ts.failure_count(state, "bbb"), 3)

    def test_garbage_is_normalised(self):
        for garbage in (None, [], "x", {"videos": "no", "failures": [], "gameplay_uses": {"a": "b"}}):
            state = ts.normalise(garbage)
            self.assertEqual(set(state), {"schema", "videos", "failures", "gameplay_uses", "scenes"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(ts.load(path), ts.empty_state())

    def test_save_and_load_round_trip(self):
        state = ts.record_video(ts.empty_state(), {"funny_sha256": "c", "funny_file": "c.mp4"})
        with tempfile.TemporaryDirectory() as tmp:
            ts.save(state, Path(tmp) / "d" / "s.json")
            self.assertEqual(ts.load(Path(tmp) / "d" / "s.json"), ts.normalise(state))

    def test_merge_is_a_union_and_idempotent(self):
        a = ts.record_video(ts.empty_state(), {"funny_sha256": "1", "funny_file": "1", "run_id": "r1", "at": "2026-01-01", "output_name": "o1"})
        b = ts.record_video(ts.empty_state(), {"funny_sha256": "2", "funny_file": "2", "run_id": "r2", "at": "2026-01-02", "output_name": "o2"})
        ts.record_failure(b, "3", "3", "x")
        merged = ts.merge(a, b)
        self.assertEqual(ts.used_hashes(merged), {"1", "2"})
        self.assertEqual(ts.failure_count(merged, "3"), 1)
        self.assertEqual(ts.merge(merged, merged), merged)
        self.assertEqual(ts.merge(merged, a), merged)


def git(cwd, *args):
    result = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                            cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


@unittest.skipUnless(HAVE_GIT, "git not installed")
class StateBranchTests(unittest.TestCase):
    """The state branch against a local bare repository: main is never touched, pushes merge instead of overwrite."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.remote = base / "remote.git"
        git(base, "init", "-q", "--bare", str(self.remote))
        self.repo = base / "work"
        git(base, "init", "-q", str(self.repo))
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hi", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-q", "origin", "main")
        self.main_before = git(self.remote, "rev-parse", "main")

    def test_pull_without_a_branch_is_an_empty_state(self):
        self.assertEqual(ts.pull(self.repo), ts.empty_state())

    def test_push_then_pull_and_main_is_untouched(self):
        state = ts.record_video(ts.empty_state(), {"funny_sha256": "abc", "funny_file": "a.mp4", "run_id": "5"})
        ts.push(state, self.repo)
        self.assertEqual(ts.used_hashes(ts.pull(self.repo)), {"abc"})
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.main_before)
        files = git(self.remote, "ls-tree", "--name-only", ts.STATE_BRANCH).split()
        self.assertEqual(sorted(files), ["README.md", "tiktok_state.json"])

    def test_two_runs_merge_instead_of_overwriting(self):
        ts.push(ts.record_video(ts.empty_state(), {"funny_sha256": "one", "funny_file": "1", "run_id": "1"}), self.repo)
        ts.push(ts.record_video(ts.empty_state(), {"funny_sha256": "two", "funny_file": "2", "run_id": "2"}), self.repo)
        self.assertEqual(ts.used_hashes(ts.pull(self.repo)), {"one", "two"})
        count = git(self.remote, "rev-list", "--count", ts.STATE_BRANCH)
        self.assertEqual(count, "2")                                   # appended, never force-pushed

    def test_pushing_the_same_state_twice_adds_no_commit(self):
        state = ts.record_video(ts.empty_state(), {"funny_sha256": "one", "funny_file": "1", "run_id": "1"})
        ts.push(state, self.repo)
        ts.push(state, self.repo)
        self.assertEqual(git(self.remote, "rev-list", "--count", ts.STATE_BRANCH), "1")

    def test_refuses_protected_branches(self):
        for branch in ("main", "master", "media-store", "refs/heads/x", ""):
            with self.assertRaises(ts.StateError):
                ts.push(ts.empty_state(), self.repo, branch=branch)


# ================================================================================================ crop maths

class CropMathTests(unittest.TestCase):
    def test_crop_is_centred_on_the_board_and_holds_it_with_the_margin(self):
        crop = tb.crop_for_circle(944, 568, 475, 1920, 1080)
        self.assertEqual(crop["pad"] % 2, 0)
        for key in ("x", "y", "size"):
            self.assertEqual(crop[key] % 2, 0, key)
        self.assertGreaterEqual(crop["size"], 2 * 475 * (1 + 2 * tb.CROP_MARGIN))
        cx, cy, r = tb.circle_in_crop(944, 568, 475, crop, 1000)
        self.assertAlmostEqual(cx, 500, delta=2)
        self.assertAlmostEqual(cy, 500, delta=2)
        self.assertLess(cx + r, 1000)
        self.assertGreater(cx - r, 0)
        self.assertGreater(r, 0.45 * 1000)                               # the board fills the square

    def test_a_board_near_the_frame_edge_is_padded_never_cut(self):
        crop = tb.crop_for_circle(950, 1040, 470, 1920, 1080)              # the rim runs 430 px past the bottom edge
        self.assertGreater(crop["pad"], 0)
        padded_w, padded_h = 1920 + 2 * crop["pad"], 1080 + 2 * crop["pad"]
        self.assertLessEqual(crop["x"] + crop["size"], padded_w)
        self.assertLessEqual(crop["y"] + crop["size"], padded_h)
        cx, cy, r = tb.circle_in_crop(950, 1040, 470, crop, 800)
        self.assertGreaterEqual(cy - r, 0)
        self.assertLessEqual(cy + r, 800)

    def test_crop_filter(self):
        self.assertEqual(tb.crop_filter({"x": 10, "y": 20, "size": 400, "pad": 0}, 300),
                         "crop=400:400:10:20,scale=300:300:flags=lanczos")
        self.assertTrue(tb.crop_filter({"x": 0, "y": 0, "size": 400, "pad": 8}, 300).startswith("pad=iw+16:ih+16:8:8"))

    def test_circle_from_the_game_geometry(self):
        box = {"left": 33.5, "top": 205.5, "right": 506.5, "bottom": 678.5}       # measured in a 540 px wide window
        cx, cy, r = tb.circle_from_dom(box, 540, 1080)                              # recorded at 1080 px wide
        self.assertAlmostEqual(cx, 540, delta=0.1)
        self.assertAlmostEqual(cy, 884, delta=0.1)
        self.assertAlmostEqual(r, 473, delta=0.1)

    def test_implausible_game_geometry_is_refused(self):
        for box in (None, {"left": 0, "top": 0, "right": 50, "bottom": 50},
                    {"left": 0, "top": 0, "right": 500, "bottom": 300}):
            with self.assertRaises(tb.BoardError):
                tb.circle_from_dom(box, 540, 1080)

    def test_agreement(self):
        self.assertEqual(tb.agreement({"cx": 0, "cy": 0, "r": 100}, {"cx": 3, "cy": 4, "r": 90}),
                         {"centre_offset": 0.05, "radius_ratio": 0.9})


# ================================================================================================ board detection

class BoardDetectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    @needs_media
    def test_finds_a_known_board_in_a_landscape_recording(self):
        clip = make_gameplay(self.dir / "g.mp4", size=(960, 540), centre=(470, 268), radius=232, tone_hz=None)
        found = tb.detect_board(clip)
        self.assertAlmostEqual(found["cx"], 470, delta=5)
        self.assertAlmostEqual(found["cy"], 268, delta=5)
        self.assertAlmostEqual(found["r"], 232, delta=6)
        self.assertEqual(tb.check_board_detection(found), [])

    @needs_media
    def test_finds_a_known_board_in_a_phone_recording(self):
        clip = make_gameplay(self.dir / "p.mp4", size=(540, 960), centre=(270, 430), radius=236, tone_hz=None)
        found = tb.detect_board(clip)
        self.assertAlmostEqual(found["cx"], 270, delta=5)
        self.assertAlmostEqual(found["cy"], 430, delta=5)
        self.assertAlmostEqual(found["r"], 236, delta=6)

    @needs_media
    def test_a_board_cut_off_by_the_frame_is_reported(self):
        clip = make_gameplay(self.dir / "cut.mp4", size=(960, 540), centre=(470, 330), radius=250, tone_hz=None)
        found = tb.detect_board(clip)
        self.assertTrue(found["clipped"])
        self.assertIn("cut off", " ".join(tb.check_board_detection(found)))

    @needs_media
    def test_no_board_in_a_blank_or_unrelated_picture(self):
        blank = self.dir / "blank.mp4"
        run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x101010:s=640x360:d=2", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", str(blank)])
        with self.assertRaises(tb.BoardError):
            tb.detect_board(blank)
        bars = self.dir / "bars.mp4"
        run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=2", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", str(bars)])
        try:
            found = tb.detect_board(bars)
        except tb.BoardError:
            return
        self.assertLess(found["support"], tb.RIM_SUPPORT + 0.2)       # if anything, only a weak accidental circle

    @needs_media
    def test_the_committed_curated_recordings_have_a_whole_board(self):
        """The real recordings in media/gameplay/: the detector finds one board, fully inside the frame, centred where
        the game draws it (desktop recordings: right of centre, phone recordings: upper middle)."""
        desktop = ROOT / "media" / "gameplay" / "2026-10-09 16-08-11.mp4"
        phone = ROOT / "media" / "gameplay" / "ringshift_prism_gameplay.mp4"
        if not (desktop.is_file() and phone.is_file()):
            self.skipTest("curated recordings not in this checkout")
        for path, (w, h) in ((desktop, (1920, 1080)), (phone, (1080, 1920))):
            found = tb.detect_board(path, 0.0, 4.0)
            self.assertEqual(tb.check_board_detection(found), [], path.name)
            self.assertGreater(found["support"], 0.9, path.name)
            self.assertGreater(found["r"], 0.35 * min(w, h), path.name)
            self.assertLessEqual(found["cx"] + found["r"], w)
            self.assertLessEqual(found["cy"] + found["r"], h)


# ================================================================================================ layout and demo card

class LayoutTests(unittest.TestCase):
    ASPECTS = [(1920, 1080), (1280, 720), (1080, 1080), (1080, 1350), (1080, 1920), (720, 1280), (3840, 1600)]

    def test_every_clip_shape_gives_a_valid_layout_at_both_sizes(self):
        for width in (1080, 720):
            for clip in self.ASPECTS:
                layout = tv.plan_layout(*clip, width, tv.even(width * 16 / 9))
                self.assertEqual(tv.layout_problems(layout), [], (width, clip))

    def test_the_card_is_in_the_gameplay_panel_inside_the_safe_area_and_above_the_board(self):
        layout = tv.plan_layout(1920, 1080)
        cta, board, safe = layout["cta"], layout["board"], layout["safe"]
        self.assertGreaterEqual(cta["y"], layout["top"]["h"])
        self.assertGreaterEqual(cta["x"], safe["left"])
        self.assertLessEqual(cta["x"] + cta["w"], safe["right"])
        self.assertGreaterEqual(cta["y"], safe["top"])
        self.assertLessEqual(cta["y"] + cta["h"], board["y"])
        self.assertLessEqual(board["y"] + board["size"], safe["bottom"])     # clear of TikTok's caption area
        self.assertLessEqual(board["x"] + board["size"], safe["right"])      # clear of the like/comment rail

    def test_the_board_gets_most_of_the_width(self):
        for clip in self.ASPECTS:
            layout = tv.plan_layout(*clip)
            self.assertGreaterEqual(layout["board"]["size"], 0.65 * layout["width"], clip)
        self.assertEqual(tv.plan_layout(1920, 1080)["top"]["h"], 608)        # a 16:9 clip fills the width exactly

    def test_broken_layouts_are_caught(self):
        layout = tv.plan_layout(1920, 1080)
        moved = json.loads(json.dumps(layout))
        moved["cta"]["y"] = moved["board"]["y"] + 10
        self.assertIn("the demo card overlaps the board area", tv.layout_problems(moved))
        low = json.loads(json.dumps(layout))
        low["board"]["y"] = 1700
        self.assertTrue(any("safe area" in p for p in tv.layout_problems(low)))
        up = json.loads(json.dumps(layout))
        up["cta"]["y"] = 100
        problems = tv.layout_problems(up)
        self.assertIn("the demo card is outside TikTok's safe area", problems)
        self.assertIn("the demo card is not in the gameplay panel", problems)

    def test_only_9_16_outputs(self):
        with self.assertRaises(tv.TikTokError):
            tv.plan_layout(1920, 1080, 1080, 1080)

    @needs_media
    def test_the_card_has_both_lines_inside_it_and_no_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            for width in (1080, 720):
                layout = tv.plan_layout(1920, 1080, width, tv.even(width * 16 / 9))
                card = tv.render_cta(layout["cta"]["w"], layout["cta"]["h"], Path(tmp) / f"c{width}.png", layout["scale"])
                self.assertEqual(tv.cta_problems(card, layout["cta"]["w"], layout["cta"]["h"]), [])
                with Image.open(card["path"]) as image:
                    self.assertEqual(image.size, (layout["cta"]["w"], layout["cta"]["h"]))
                    self.assertEqual(image.mode, "RGBA")
        self.assertEqual(tv.CTA_LINES, ("PLAY THE DEMO", "LINK IN BIO"))
        source = (ROOT / "scripts" / "tiktok_video.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"https?://[^\s\"']*(itch|demo|ringshift)")      # no invented demo URL

    def test_a_card_line_that_does_not_fit_is_reported(self):
        self.assertTrue(tv.cta_problems({"text_boxes": [(0, 0, 900, 40), (10, 50, 200, 90)]}, 760, 112))
        self.assertTrue(tv.cta_problems({"text_boxes": [(20, 10, 300, 40)]}, 760, 112))


# ================================================================================================ the full run

def curated_only(test):
    """Run make_video with the real curated planner and FFmpeg at 720x1280 (fast)."""
    return needs_media(test)


class MakeVideoTests(TempRepo):
    def make(self, **kwargs):
        kwargs.setdefault("gameplay", "curated")
        kwargs.setdefault("width", 720)
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": kwargs.pop("run_id", "")}):
            return tv.make_video(state=self.state, out_dir=self.out, funny_dir=self.funny, gameplay_dir=self.gameplay,
                                 log=lambda *a: None, **kwargs)

    @curated_only
    def test_end_to_end_audio_kept_gameplay_muted_board_whole_card_readable(self):
        make_funny(self.funny / "joke.mp4", seconds=5, size=(640, 360))
        make_gameplay(self.gameplay / "rec.mp4", seconds=3, tone_hz=1000)       # shorter: it must loop
        status, meta, paths = self.make()
        self.assertEqual(status, "produced", json.dumps(meta.get("validation"), indent=1)[:2000])
        video = paths["video"]
        info = tv.probe_clip(video)
        self.assertEqual((info["width"], info["height"]), (720, 1280))
        self.assertEqual(info["audio_streams"], 1)
        self.assertAlmostEqual(info["duration"], 5.0, delta=0.25)
        checks = meta["validation"]["checks"]
        self.assertTrue(all(c["ok"] for c in checks.values()))
        self.assertGreaterEqual(checks["audio_is_funny_clip"]["correlation"], 0.99)
        # The gameplay recording is almost all 1000 Hz tone; the video has no more 1000 Hz than the funny clip itself.
        self.assertGreater(tone_energy(self.gameplay / "rec.mp4", 1000), 0.5)
        self.assertLess(tone_energy(video, 1000), tone_energy(self.funny / "joke.mp4", 1000) + 0.002)
        self.assertLess(tone_energy(video, 1000), 0.02)
        self.assertTrue(meta["gameplay"]["looped"])
        self.assertTrue(meta["gameplay"]["muted"])
        self.assertGreaterEqual(checks["board_visible"]["fills_board_area"], 0.85)
        # metadata: everything the issue asks for
        self.assertEqual(meta["funny"]["file"].split("/")[-1], "joke.mp4")
        self.assertEqual(len(meta["funny"]["sha256"]), 64)
        self.assertEqual(meta["gameplay"]["segments"][0]["sha256"], tv.sha256_file(self.gameplay / "rec.mp4"))
        self.assertEqual(meta["output"]["sha256"], tv.sha256_file(video))
        self.assertIn("link in bio", meta["caption_suggestion"].lower())
        self.assertEqual(meta["publishing"]["mode"], "manual")
        self.assertEqual(json.loads(Path(paths["metadata"]).read_text())["status"], "produced")
        self.assertTrue(Path(paths["preview"]).is_file())

    @curated_only
    def test_non_aac_audio_is_converted_and_still_exactly_the_clip(self):
        make_funny(self.funny / "pcm.mov", seconds=4, audio="pcm")
        make_gameplay(self.gameplay / "rec.mp4", seconds=4)
        status, meta, paths = self.make()
        self.assertEqual(status, "produced", meta.get("error"))
        self.assertEqual(tv.probe_clip(paths["video"])["audio_codec"], "aac")
        self.assertGreaterEqual(meta["validation"]["checks"]["audio_is_funny_clip"]["correlation"], 0.97)

    @curated_only
    def test_a_silent_clip_gives_a_silent_video_with_no_music(self):
        make_funny(self.funny / "silent.mp4", seconds=3, audio=None)
        make_gameplay(self.gameplay / "rec.mp4", seconds=3)
        status, meta, paths = self.make()
        self.assertEqual(status, "produced", meta.get("error"))
        self.assertEqual(tv.probe_clip(paths["video"])["audio_streams"], 0)

    @needs_media
    def test_the_audio_check_catches_gameplay_sound_or_music_mixed_in(self):
        funny = make_funny(self.root / "f.mp4", seconds=4)
        mixed = self.root / "mixed.mp4"
        run(["ffmpeg", "-v", "error", "-y", "-i", str(funny), "-f", "lavfi", "-i", "sine=frequency=1000:duration=4",
             "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0[a]", "-map", "0:v", "-map", "[a]",
             "-c:v", "copy", "-c:a", "aac", str(mixed)])
        self.assertFalse(tv.audio_match(mixed, funny, 4.0)["ok"])
        self.assertTrue(tv.audio_match(funny, funny, 4.0)["ok"])
        other = make_funny(self.root / "other.mp4", seconds=4, tone=False)
        self.assertFalse(tv.audio_match(other, funny, 4.0)["ok"])                 # silence instead of the clip

    def test_no_clip_is_a_skip_not_a_failure(self):
        status, meta, paths = self.make()
        self.assertEqual(status, "skipped")
        self.assertIn("no funny clips", meta["why"])
        self.assertEqual(list(self.out.iterdir()), [])

    @needs_media
    def test_no_usable_gameplay_is_a_clear_skip(self):
        make_funny(self.funny / "joke.mp4", seconds=3)
        make_gameplay(self.gameplay / "cut.mp4", centre=(470, 330), radius=250, seconds=3)    # board cut off
        (self.gameplay / "fake.mp4").write_bytes(b"x" * 9000)
        status, meta, _ = self.make()
        self.assertEqual(status, "skipped")
        self.assertIn("no valid gameplay", meta["why"])
        reasons = " ".join(r["why"] for r in meta["gameplay_report"])
        self.assertIn("cut off", reasons)
        self.assertEqual(list(self.out.iterdir()), [])

    @needs_media
    def test_a_failed_render_leaves_no_video_and_says_why(self):
        make_funny(self.funny / "joke.mp4", seconds=3)
        make_gameplay(self.gameplay / "rec.mp4", seconds=3)
        real = tv._ffmpeg

        def broken(command, what):
            if what == "composing the video":
                raise tv.TikTokError("FFmpeg failed while composing the video: simulated")
            return real(command, what)

        with mock.patch.object(tv, "_ffmpeg", broken):
            status, meta, paths = self.make()
        self.assertEqual(status, "failed")
        self.assertIn("simulated", meta["error"])
        self.assertEqual([p.suffix for p in self.out.iterdir()], [".json"])       # metadata only, never an mp4

    @needs_media
    def test_a_video_that_fails_validation_is_deleted_and_reported(self):
        make_funny(self.funny / "joke.mp4", seconds=3)
        make_gameplay(self.gameplay / "rec.mp4", seconds=3)
        failing = {"ok": False, "checks": {"board_visible": {"ok": False, "detail": "simulated: board cut off"}}}
        with mock.patch.object(tv, "validate_output", return_value=failing):
            status, meta, paths = self.make()
        self.assertEqual(status, "failed")
        self.assertIn("board_visible", meta["error"])
        self.assertFalse(any(p.suffix == ".mp4" for p in self.out.iterdir()))

    @needs_media
    def test_validation_fails_a_wrong_or_broken_output(self):
        funny = make_funny(self.funny / "joke.mp4", seconds=3)
        info = tv.probe_clip(funny)
        layout = tv.plan_layout(info["width"], info["height"], 720, 1280)
        card = tv.render_cta(layout["cta"]["w"], layout["cta"]["h"], self.root / "c.png", layout["scale"])
        # the funny clip itself is not a valid TikTok output: wrong size, no board, no card
        result = tv.validate_output(funny, funny, info, layout, info["duration"], card)
        self.assertFalse(result["ok"])
        for name in ("resolution", "board_visible", "cta_visible"):
            self.assertFalse(result["checks"][name]["ok"], name)
        broken = self.root / "broken.mp4"
        broken.write_bytes(funny.read_bytes()[:3000])
        self.assertFalse(tv.validate_output(broken, funny, info, layout, info["duration"], card)["ok"])

    def test_a_rerun_of_a_run_that_already_made_its_video_makes_nothing(self):
        ts.record_video(self.state, {"funny_sha256": "abc", "funny_file": "x.mp4", "run_id": "4242", "output_name": "v"})
        (self.funny / "new.mp4").write_bytes(b"x" * 5000)
        status, meta, paths = self.make(run_id="4242")
        self.assertEqual(status, "already-done")
        self.assertEqual(paths, {})

    @needs_media
    def test_live_capture_failure_falls_back_to_curated_and_says_so(self):
        make_funny(self.funny / "joke.mp4", seconds=3)
        make_gameplay(self.gameplay / "rec.mp4", seconds=3)
        game = self.root / "game-src"
        game.mkdir()

        def broken_live(*args, **kwargs):
            raise RuntimeError("Chromium crashed")

        status, meta, _ = self.make(gameplay="auto", game_dir=game, live_planner=broken_live)
        self.assertEqual(status, "produced", meta.get("error"))
        self.assertEqual(meta["gameplay"]["source"], "curated")
        self.assertIn("Chromium crashed", meta["gameplay"]["fallback_reason"])

        # mode "live" must not quietly fall back
        (self.funny / "joke.mp4").rename(self.funny / "joke2.mp4")
        self.state = ts.empty_state()
        status, meta, _ = self.make(gameplay="live", game_dir=game, live_planner=broken_live)
        self.assertEqual(status, "failed")
        self.assertIn("Chromium crashed", meta["error"])

    def test_auto_without_a_game_checkout_uses_curated(self):
        (self.funny / "a.mp4").write_bytes(b"x")
        calls = []

        def curated(need, state, folder, log=print):
            calls.append(need)
            return [], [{"file": "x", "outcome": "skipped", "why": "test"}]

        with mock.patch.object(tv, "check_funny", ok_check):
            status, meta, _ = self.make(gameplay="auto", game_dir=None, curated_planner=curated)
        self.assertEqual(calls, [5.0])
        self.assertEqual(status, "skipped")
        self.assertIn("not checked out", meta["fallback_reason"])


class CommandLineTests(TempRepo):
    def test_make_records_state_only_for_produced_or_failed_and_writes_outputs(self):
        state_file = self.root / "state.json"
        outputs = self.root / "out.txt"
        summary = self.root / "summary.md"
        produced = {"status": "produced", "funny": {"file": "media/funny/a.mp4", "sha256": "f" * 64},
                    "output": {"sha256": "0" * 64, "file": "v.mp4", "width": 1080, "height": 1920, "duration": 5.0,
                               "bytes": 1000}, "run_id": "9",
                    "gameplay": {"source": "curated", "segments": [{"sha256": "g" * 64, "source": "curated"}], "looped": False},
                    "caption_suggestion": "c", "created_at": "2026-10-28T00:00:00+00:00",
                    "validation": {"checks": {"board_visible": {"ok": True, "detail": "ok"}}}}
        video = self.out / "ringshift-tiktok-x.mp4"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(outputs), "GITHUB_STEP_SUMMARY": str(summary)}), \
                mock.patch.object(tv, "make_video", return_value=("produced", produced, {"video": video, "metadata": self.out / "m.json",
                                                                                          "preview": self.out / "p.jpg"})):
            code = tv.main(["make", "--state", str(state_file), "--funny-dir", str(self.funny), "--out-dir", str(self.out)])
        self.assertEqual(code, 0)
        self.assertIn("status=produced", outputs.read_text())
        self.assertIn("artifact_name=ringshift-tiktok-x", outputs.read_text())
        self.assertIn("READY", summary.read_text())
        state = ts.load(state_file)
        self.assertEqual(ts.used_hashes(state), {"f" * 64})
        self.assertEqual(state["gameplay_uses"], {"g" * 64: 1})

        failed = {"status": "failed", "error": "boom", "funny": {"file": "media/funny/b.mp4", "sha256": "e" * 64}}
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(outputs), "GITHUB_STEP_SUMMARY": str(summary)}), \
                mock.patch.object(tv, "make_video", return_value=("failed", failed, {})):
            code = tv.main(["make", "--state", str(state_file), "--funny-dir", str(self.funny), "--out-dir", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("status=failed", outputs.read_text())
        self.assertEqual(ts.failure_count(ts.load(state_file), "e" * 64), 1)
        self.assertIn("FAILED (no video was produced)", summary.read_text())

    def test_an_unexpected_error_is_a_failed_run_not_a_crash_or_a_success(self):
        outputs = self.root / "out.txt"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(outputs)}), \
                mock.patch.object(tv, "make_video", side_effect=ValueError("surprise")):
            code = tv.main(["make", "--state", str(self.root / "s.json"), "--funny-dir", str(self.funny)])
        self.assertEqual(code, 1)
        self.assertIn("status=failed", outputs.read_text())

    def test_skip_exits_cleanly(self):
        outputs = self.root / "out.txt"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(outputs)}):
            code = tv.main(["make", "--state", str(self.root / "s.json"), "--funny-dir", str(self.funny),
                            "--gameplay-dir", str(self.gameplay), "--out-dir", str(self.out)])
        self.assertEqual(code, 0)
        self.assertIn("status=skipped", outputs.read_text())

    def test_status_reports_eligibility(self):
        outputs = self.root / "out.txt"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(outputs)}):
            tv.main(["status", "--state", str(self.root / "s.json"), "--funny-dir", str(self.funny)])
        self.assertIn("eligible=false", outputs.read_text())


# ================================================================================================ X stays separate

class XAutopilotIgnoresTikTokFoldersTests(unittest.TestCase):
    def test_paths(self):
        self.assertFalse(media_manifest.is_x_media_path("media/funny/cat.mp4"))
        self.assertFalse(media_manifest.is_x_media_path("media/tiktok-output/v.mp4"))
        self.assertTrue(media_manifest.is_x_media_path("media/gameplay/rec.mp4"))
        self.assertTrue(media_manifest.is_x_media_path("media/captured/x.jpg"))

    def test_the_selector_never_lists_a_funny_clip_or_a_tiktok_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for folder in ("funny", "tiktok-output", "gameplay"):
                (root / "media" / folder).mkdir(parents=True)
                (root / "media" / folder / "x.png").write_bytes(support.png_bytes())
            with mock.patch.object(media_selector, "BASE_DIR", root), mock.patch.object(media_selector, "MEDIA_DIR", root / "media"):
                items, _ = media_selector._list_candidates({})
        self.assertEqual([i["file"] for i in items], ["media/gameplay/x.png"])

    def test_manifest_sync_and_validation_never_index_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for folder in ("funny", "tiktok-output"):
                (root / "media" / folder).mkdir(parents=True)
                (root / "media" / folder / "x.png").write_bytes(support.png_bytes())
            media_manifest.sync(base_dir=root, validate=False, log=lambda *a: None)
            manifest = media_manifest.load_manifest(root / "media" / "manifest.json")
            self.assertEqual(manifest["items"], [])
            problems = media_manifest.validate_manifest(manifest, root, tags={}, decode=False)
            self.assertFalse([p for p in problems if "funny" in str(p) or "tiktok" in str(p)])

    def test_generated_videos_are_git_ignored_but_the_folder_readmes_are_not(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("media/tiktok-output/*", ignore)
        self.assertIn("!media/tiktok-output/README.md", ignore)
        self.assertIn("data/tiktok_state.json", ignore)
        for folder in ("funny", "gameplay", "tiktok-output"):
            self.assertTrue((ROOT / "media" / folder / "README.md").is_file(), folder)


# ================================================================================================ workflow contract

def step(name):
    marker = f"- name: {name}\n"
    if marker not in WORKFLOW:
        raise AssertionError(f"no step named {name!r}")
    block = WORKFLOW.split(marker, 1)[1]
    return block.split("\n      - name:", 1)[0]


class TikTokWorkflowTests(unittest.TestCase):
    def test_manual_and_scheduled(self):
        self.assertIn("workflow_dispatch:", WORKFLOW)
        self.assertRegex(WORKFLOW, r"schedule:\s+- cron: \"41 7 \* \* \*\"")

    def test_the_x_schedule_is_unchanged(self):
        autopilot = (ROOT / ".github" / "workflows" / "autopilot.yml").read_text(encoding="utf-8")
        self.assertIn('- cron: "0 6 * * *"', autopilot)
        self.assertIn('- cron: "37 13 * * *"', autopilot)
        self.assertNotIn("tiktok", autopilot.lower())

    def test_inputs_reach_the_script_through_env_only(self):
        make = step("Make the video")
        self.assertIn("FUNNY_FILE: ${{ inputs.funny_file }}", make)
        run_block = make.split("run: |", 1)[1]
        self.assertNotIn("${{", run_block)

    def test_the_video_is_an_artifact_with_retention_and_only_when_produced(self):
        upload = step("Upload the video (download it from this run's page)")
        self.assertIn("steps.make.outputs.status == 'produced'", upload)
        self.assertIn("name: ${{ steps.make.outputs.artifact_name }}", upload)
        self.assertIn("retention-days: 30", upload)
        self.assertIn("if-no-files-found: error", upload)
        failure = step("Upload the failure report")
        self.assertIn("metadata_file", failure)
        self.assertNotIn("video_file", failure)

    def test_state_is_saved_only_on_main_and_only_after_the_artifact_upload(self):
        save = step("Save which clips were used (tiktok-state branch)")
        self.assertIn("github.ref == 'refs/heads/main'", save)
        self.assertIn("steps.upload.outcome == 'success'", save)
        self.assertIn('--branch "$STATE_BRANCH"', save)
        self.assertLess(WORKFLOW.index("- name: Upload the video"), WORKFLOW.index("- name: Save which clips were used"))

    def test_live_capture_problems_cannot_fail_the_run_before_the_fallback(self):
        self.assertIn("continue-on-error: true", step("Install Chromium for live gameplay"))
        self.assertIn("continue-on-error: true", step("Check out the game (stable branch, read-only)"))

    def test_nothing_publishes_to_tiktok(self):
        for word in ("open.tiktokapis.com", "TIKTOK_", "video.publish", "access_token"):
            self.assertNotIn(word, WORKFLOW)
        for name in ("tiktok_video.py", "tiktok_board.py", "tiktok_state.py"):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertNotIn("tiktokapis", source)
            self.assertNotIn("requests.", source)


# ================================================================================================ real game (optional)

@unittest.skipUnless(os.environ.get("RINGSHIFT_GAME_DIR"), "set RINGSHIFT_GAME_DIR to a ringshift-game checkout")
class RealGameTests(TempRepo):
    def test_live_capture_board_geometry_matches_the_pixels(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("Playwright not installed")
        segments, report, game = tv.plan_live(8.0, self.state, os.environ["RINGSHIFT_GAME_DIR"], self.root,
                                              rng=random.Random(5), log=lambda *a: None, max_levels=1)
        segment = segments[0]
        self.assertEqual(segment["source"], "live")
        self.assertEqual(segment["bounds_method"], "game-dom")
        cross = segment["pixel_cross_check"]
        self.assertLess(cross["centre_offset"], 0.03, cross)
        self.assertGreater(cross["radius_ratio"], 0.9, cross)
        crop = segment["crop"]
        cx, cy, r = tb.circle_in_crop(segment["circle"]["cx"], segment["circle"]["cy"], segment["circle"]["r"], crop, 1000)
        self.assertGreater(cx - r, 0)
        self.assertLess(cx + r, 1000)


if __name__ == "__main__":
    unittest.main()
