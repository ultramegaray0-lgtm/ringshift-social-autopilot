"""media_tools: probing, validation and clip preparation, using tiny synthetic clips.

These tests call the local ffmpeg/ffprobe binaries only; they never touch the
network. They are skipped (not failed) on machines without FFmpeg.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
import media_tools as tools

NEEDS_FFMPEG = unittest.skipUnless(tools.have_ffmpeg(), "ffmpeg/ffprobe not installed")


def ffmpeg(*args):
    result = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode(errors="replace"))


def make_video(path, source="testsrc2=size=540x960:rate=30", seconds=2, codec="libx264", extra=()):
    ffmpeg("-f", "lavfi", "-i", source, "-t", str(seconds), "-pix_fmt", "yuv420p",
           "-c:v", codec, *extra, str(path))


@NEEDS_FFMPEG
class ValidateMediaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_moving_clip_is_valid(self):
        clip = self.dir / "ok.mp4"
        make_video(clip)
        self.assertEqual(tools.validate_media(clip), [])

    def test_black_clip_is_rejected_as_not_gameplay(self):
        clip = self.dir / "black.mp4"
        make_video(clip, source="color=c=black:size=540x960:rate=30")
        problems = tools.validate_media(clip)
        self.assertTrue(any("not visible" in p for p in problems), problems)

    def test_frozen_clip_is_rejected(self):
        still = self.dir / "still.png"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=540x960", "-frames:v", "1", str(still))
        clip = self.dir / "frozen.mp4"
        ffmpeg("-loop", "1", "-i", str(still), "-t", "2", "-r", "30", "-pix_fmt", "yuv420p",
               "-c:v", "libx264", str(clip))
        problems = tools.validate_media(clip)
        self.assertTrue(any("never changes" in p for p in problems), problems)

    def test_valid_screenshot_passes_and_blank_screenshot_fails(self):
        good = self.dir / "good.png"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=540x960", "-frames:v", "1", str(good))
        blank = self.dir / "blank.png"
        ffmpeg("-f", "lavfi", "-i", "color=c=0x101010:size=540x960", "-frames:v", "1", str(blank))
        self.assertEqual(tools.validate_media(good), [])
        self.assertTrue(tools.validate_media(blank))

    def test_jpeg_screenshots_validate_and_convert_image_writes_small_jpegs(self):
        source = self.dir / "shot.png"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1080x1920", "-frames:v", "1", str(source))
        jpg = self.dir / "shot.jpg"
        info = tools.convert_image(source, jpg, aspect="4:5")
        self.assertEqual((info["width"], info["height"]), (1080, 1350))
        self.assertEqual(tools.validate_media(jpg), [])          # regression: -ss on a JPEG returned no frame
        blank = self.dir / "blank.jpg"
        ffmpeg("-f", "lavfi", "-i", "color=c=0x101010:size=540x960", "-frames:v", "1", str(blank))
        self.assertTrue(tools.validate_media(blank))

    def test_wrong_codec_is_rejected(self):
        clip = self.dir / "mpeg4.mp4"
        make_video(clip, codec="mpeg4")
        self.assertTrue(any("H.264" in p for p in tools.validate_media(clip)))

    def test_size_and_duration_limits(self):
        clip = self.dir / "long.mp4"
        make_video(clip, seconds=3)
        self.assertTrue(any("too long" in p for p in tools.validate_media(clip, max_seconds=2)))
        self.assertTrue(any("MB" in p for p in tools.validate_media(clip, max_video_bytes=1000)))

    def test_too_short_clip(self):
        clip = self.dir / "short.mp4"
        make_video(clip, seconds=0.5)
        self.assertTrue(any("too short" in p for p in tools.validate_media(clip, min_seconds=1.0)))

    def test_odd_dimensions_are_rejected(self):
        clip = self.dir / "odd.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=541x961:rate=30", "-t", "2", "-pix_fmt", "yuv444p",
               "-c:v", "libx264", str(clip))
        self.assertTrue(tools.validate_media(clip))

    def test_oversized_dimensions_are_rejected(self):
        clip = self.dir / "huge.mp4"
        make_video(clip, source="testsrc2=size=2400x1200:rate=30")
        self.assertTrue(any("too large" in p for p in tools.validate_media(clip, check_visible=False)))

    def test_missing_empty_and_unsupported_files(self):
        self.assertTrue(tools.validate_media(self.dir / "nope.mp4"))
        empty = self.dir / "empty.mp4"
        empty.write_bytes(b"")
        self.assertEqual(tools.validate_media(empty), ["file is empty"])
        text = self.dir / "notes.txt"
        text.write_text("hi")
        self.assertTrue(any("unsupported" in p for p in tools.validate_media(text)))

    def test_garbage_file_with_video_extension_is_reported_not_raised(self):
        junk = self.dir / "junk.mp4"
        junk.write_bytes(b"not a video at all" * 50)
        problems = tools.validate_media(junk)
        self.assertTrue(problems)


@NEEDS_FFMPEG
class PrepareClipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_encode_video_to_4_5_is_valid_and_correct_size(self):
        source = self.dir / "src.mp4"
        make_video(source, seconds=3)
        out = self.dir / "out.mp4"
        info = tools.encode_video(source, out, aspect="4:5")
        self.assertEqual((info["width"], info["height"]), (1080, 1350))
        self.assertEqual(info["codec"], "h264")
        self.assertFalse(info["has_audio"])
        self.assertEqual(tools.validate_media(out), [])

    def test_landscape_source_is_cropped_not_stretched(self):
        vf, size = tools.aspect_filter(1920, 1080, "9:16")
        self.assertTrue(vf.startswith("crop="))
        self.assertEqual(size, (1080, 1920))

    def test_matching_aspect_needs_no_crop(self):
        vf, _ = tools.aspect_filter(540, 960, "9:16")
        self.assertFalse(vf.startswith("crop="))

    def test_trim_start_and_duration(self):
        source = self.dir / "src.mp4"
        make_video(source, seconds=4)
        out = self.dir / "trim.mp4"
        info = tools.encode_video(source, out, aspect="9:16", start=1, duration=2)
        self.assertAlmostEqual(info["duration"], 2.0, delta=0.2)

    def test_optional_zoom_still_produces_a_valid_clip(self):
        source = self.dir / "src.mp4"
        make_video(source, seconds=2)
        out = self.dir / "zoom.mp4"
        info = tools.encode_video(source, out, aspect="4:5", zoom=1.05)
        self.assertEqual((info["width"], info["height"]), (1080, 1350))
        self.assertEqual(tools.validate_media(out), [])

    def test_encode_frames_respects_timestamps(self):
        frames = []
        for index in range(6):
            frame = self.dir / f"f{index}.png"
            ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=540x960:rate=1,setpts=PTS+{index}/TB", "-frames:v", "1", str(frame))
            frames.append((index * 0.5, frame))
        out = self.dir / "frames.mp4"
        info = tools.encode_frames(frames, out, aspect="9:16", hold_last=0.5)
        # 6 frames spaced 0.5s apart + 0.5s hold = about 3 seconds of video.
        self.assertAlmostEqual(info["duration"], 3.0, delta=0.4)

    def test_encode_frames_needs_two_frames(self):
        with self.assertRaises(tools.MediaToolError):
            tools.encode_frames([(0.0, self.dir / "x.png")], self.dir / "o.mp4")

    def test_unknown_aspect_is_an_error(self):
        with self.assertRaises(tools.MediaToolError):
            tools.aspect_filter(540, 960, "7:3")

    def test_poster_and_image_conversion(self):
        source = self.dir / "src.mp4"
        make_video(source, seconds=2)
        poster = tools.make_poster(source, self.dir / "poster.png")
        self.assertTrue(poster.exists())
        info = tools.convert_image(poster, self.dir / "cropped.png", aspect="1:1")
        self.assertEqual((info["width"], info["height"]), (1080, 1080))


if __name__ == "__main__":
    unittest.main()
