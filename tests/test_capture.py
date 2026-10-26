"""capture_gameplay: scene selection, metadata and bookkeeping (no browser needed).

An optional end-to-end test at the bottom drives a real browser against your
game file. It only runs when RINGSHIFT_GAME_HTML points at the game, Playwright
and Chromium are installed, and FFmpeg is available; otherwise it is skipped.
"""

import json
import os
import random
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import support  # noqa: F401
import capture_gameplay as cg
import media_tools as tools


def scene(mode, index, labels, cost):
    return {"mode": mode, "index": index, "id": cg.scene_id(mode, index), "labels": labels, "solution_cost": cost}


class ChooseScenesTests(unittest.TestCase):
    def setUp(self):
        self.scenes = [
            scene("easy", 0, ["Block"], 6),
            scene("easy", 1, ["Block"], 6),
            scene("easy", 2, ["Prism", "Mirror"], 8),
            scene("easy", 3, ["Echo"], 2),                 # too trivial
            scene("easy", 4, ["Portal"], 30),              # too long
            scene("easy", 5, ["Portal"], None),            # no solution
        ]

    def test_unsuitable_levels_are_never_chosen(self):
        chosen = cg.choose_scenes(self.scenes, [], 10, rng=random.Random(1))
        self.assertEqual({s["id"] for s in chosen}, {"easy:1", "easy:2", "easy:3"})

    def test_captured_levels_are_skipped_unless_forced(self):
        history = [{"scene": "easy:1", "labels": ["Block"]}]
        chosen = cg.choose_scenes(self.scenes, history, 10, rng=random.Random(1))
        self.assertNotIn("easy:1", {s["id"] for s in chosen})
        forced = cg.choose_scenes(self.scenes, history, 10, rng=random.Random(1), force=True)
        self.assertIn("easy:1", {s["id"] for s in forced})

    def test_prefers_gimmicks_that_were_not_shown_recently(self):
        history = [{"scene": "easy:99", "labels": ["Block"]}]
        chosen = cg.choose_scenes(self.scenes, history, 1, rng=random.Random(1))
        self.assertEqual(chosen[0]["id"], "easy:3")   # Prism + Mirror are new; Block was just shown

    def test_a_batch_spreads_across_different_gimmicks(self):
        chosen = cg.choose_scenes(self.scenes, [], 2, rng=random.Random(3))
        labels = [tuple(s["labels"]) for s in chosen]
        self.assertEqual(len(set(labels)), 2)

    def test_asking_for_more_than_exists_returns_what_exists(self):
        self.assertEqual(len(cg.choose_scenes(self.scenes, [], 99)), 3)

    def test_nothing_left_returns_empty(self):
        history = [{"scene": s["id"], "labels": s["labels"]} for s in self.scenes]
        self.assertEqual(cg.choose_scenes(self.scenes, history, 3), [])


class MetadataTests(unittest.TestCase):
    def test_labels_become_selector_topic_tags(self):
        self.assertEqual(cg.tags_for_labels(["Portal", "Echo", "Half-Block"]),
                         ["black hole", "satellite", "half-block", "block"])
        self.assertEqual(cg.tags_for_labels([]), [])
        self.assertIn("mechanism", cg.tags_for_labels(["Mirror", "Block", "Portal", "Prism"]))

    def test_theme_detection(self):
        self.assertEqual(cg.theme_from_classes("inGame theme-ice"), "ice")
        self.assertIsNone(cg.theme_from_classes("inGame"))
        self.assertIsNone(cg.theme_from_classes(""))
        self.assertIsNone(cg.theme_from_classes(None))

    def test_description_states_only_level_data(self):
        s = scene("hard", 10, ["Echo", "Portal"], 11)
        text = cg.describe_scene(s, {"cost": 11}, None, "video")
        self.assertIn("Level 11 (Hard)", text)
        self.assertIn("Echo, Portal gimmicks", text)
        self.assertIn("11 moves", text)
        self.assertIn("BREACHED", text)
        self.assertNotIn("visual theme", text)

        themed = cg.describe_scene(s, {"cost": 11}, "ice", "puzzle")
        self.assertIn("'ice' visual theme", themed)
        self.assertIn("no locks solved", themed)

    def test_file_names_are_descriptive_and_safe(self):
        s = scene("normal", 19, ["Half-Block", "Mirror"], 8)
        name = cg.clip_filename(s, "clip", datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.assertEqual(name, "rs_normal_L20_halfblock-mirror_clip_20261026")


class RecordResultsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "media" / "captured").mkdir(parents=True)
        self.game = self.dir / "game.html"
        self.game.write_text("<html>game</html>", encoding="utf-8")

    def item(self, name, kind="video"):
        path = self.dir / "media" / "captured" / name
        path.write_bytes(b"x")
        return {
            "path": path, "type": "video" if kind == "video" else "image", "kind": kind,
            "info": {"width": 1080, "height": 1350, "duration": 9.43, "bytes": 1},
            "scene": scene("hard", 10, ["Echo", "Portal"], 11),
            "solution": {"cost": 11}, "theme": None, "moves": 11,
        }

    def test_tags_and_history_are_written_once_per_scene(self):
        tags_file = self.dir / "media" / "media_tags.json"
        history_file = self.dir / "data" / "capture_history.json"
        tags_file.write_text(json.dumps({"media/old.png": ["prism"]}), encoding="utf-8")

        files = cg.record_results(
            [self.item("a.mp4"), self.item("a.png", "solved")], self.game,
            tags_file=tags_file, history_file=history_file, base_dir=self.dir,
            now=datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc))

        self.assertEqual(files, ["media/captured/a.mp4", "media/captured/a.png"])

        tags = json.loads(tags_file.read_text(encoding="utf-8"))
        self.assertEqual(tags["media/old.png"], ["prism"])        # existing entries untouched
        clip = tags["media/captured/a.mp4"]
        self.assertEqual(clip["tags"], ["satellite", "black hole"])
        self.assertEqual(clip["type"], "video")
        self.assertEqual(clip["duration"], 9.4)
        self.assertEqual(clip["scene"], "hard:11")
        self.assertEqual(clip["source"], "capture_gameplay.py")
        self.assertEqual(len(clip["game_build"]), 12)
        self.assertIn("Level 11 (Hard)", clip["description"])

        history = json.loads(history_file.read_text(encoding="utf-8"))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["scene"], "hard:11")
        self.assertEqual(history[0]["files"], files)

    def test_captured_scene_is_not_chosen_again(self):
        history_file = self.dir / "history.json"
        cg.record_results([self.item("a.mp4")], self.game, tags_file=self.dir / "tags.json",
                          history_file=history_file, base_dir=self.dir)
        history = json.loads(history_file.read_text(encoding="utf-8"))
        again = cg.choose_scenes([scene("hard", 10, ["Echo", "Portal"], 11)], history, 1)
        self.assertEqual(again, [])

    def test_fingerprint_changes_when_the_game_changes(self):
        first = cg.game_fingerprint(self.game)
        self.game.write_text("<html>game v2</html>", encoding="utf-8")
        self.assertNotEqual(first, cg.game_fingerprint(self.game))


class CommandLineTests(unittest.TestCase):
    def test_missing_game_file_is_a_clear_error_not_a_traceback(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cg.main(["list", "--game", "/definitely/not/here.html"])
        self.assertEqual(code, 2)
        self.assertIn("game file not found", err.getvalue())


GAME = os.environ.get("RINGSHIFT_GAME_HTML")


def _browser_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


@unittest.skipUnless(GAME and Path(GAME).is_file() and tools.have_ffmpeg() and _browser_available(),
                     "set RINGSHIFT_GAME_HTML and install Playwright+Chromium+FFmpeg to run the real capture")
class RealGameCaptureTests(unittest.TestCase):
    """Drives the real game. Slow (about 30s); proves the capture really plays and wins a level."""

    def test_capture_one_level_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            with cg.GameSession(GAME) as session:
                scenes = session.scenes()
                self.assertGreaterEqual(len(scenes), 25)
                target = next(s for s in scenes if s["id"] == "easy:17")
                results = cg.capture_scene(session, target, tmp, log=lambda *_: None)

            kinds = {r["kind"] for r in results}
            self.assertEqual(kinds, {"video", "puzzle", "solved"})
            for item in results:
                self.assertEqual(tools.validate_media(item["path"]), [], item["path"])
            video = next(r for r in results if r["kind"] == "video")
            self.assertGreater(video["info"]["duration"], 4)


if __name__ == "__main__":
    unittest.main()
