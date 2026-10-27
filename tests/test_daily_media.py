"""daily_media.py: planning, freshness, hosting records, and that fresh hosted media passes the existing media gate.

No browser and no network: capture sessions, the media-store upload and the public URL are faked. The real capture
is covered by test_capture.RealGameCaptureTests and DailyMediaRealGameTests below (only with RINGSHIFT_GAME_HTML).
"""

import contextlib
import hashlib
import io
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support
from support import OfflineTestCase, png_bytes
import autopilot
import capture_gameplay as cg
import daily_media as dm
import media_manifest as mm
import media_selector
import media_tools

THEMES = [{"id": "space", "name": "Deep Field", "assigned": True}, {"id": "vinyl", "name": "Vinyl Record", "assigned": True},
          {"id": "honey", "name": "Honeycomb", "assigned": True}, {"id": "secret", "name": "Not Yet", "assigned": False}]


def run(kind="puzzle", theme="vinyl", scene="hard:9", status="captured"):
    return {"status": status, "kind": kind, "theme": theme, "scene": scene, "labels": ["Prism"]}


def scene(mode, index, labels, cost):
    return {"mode": mode, "index": index, "id": cg.scene_id(mode, index), "labels": labels, "solution_cost": cost}


class PlanningTests(unittest.TestCase):
    def test_the_kind_cycle_gives_a_video_every_third_post_alternating_clip_and_teaser(self):
        kinds = []
        state = {"runs": []}
        for _ in range(12):
            kind = dm.next_kind(state)
            kinds.append(kind)
            state["runs"].append(run(kind))
        self.assertEqual([k in dm.VIDEO_KINDS for k in kinds], [False, False, True] * 4)
        self.assertEqual([k for k in kinds if k in dm.VIDEO_KINDS], ["clip", "teaser"] * 2)

    def test_failed_runs_do_not_advance_the_cycle(self):
        state = {"runs": [run("puzzle"), run(status="failed"), run(status="failed")]}
        self.assertEqual(dm.next_kind(state), "puzzle")       # second slot, not the video

    def test_a_video_falls_back_to_a_still_never_to_a_fake_video(self):
        self.assertEqual(dm.plan_attempts("clip"), ["clip", "puzzle"])
        self.assertEqual(dm.plan_attempts("teaser"), ["teaser", "puzzle"])
        self.assertEqual(dm.plan_attempts("puzzle"), ["puzzle"])

    def test_theme_choice_never_uses_unassigned_themes_and_avoids_recent_ones(self):
        rng = random.Random(1)
        state = {"known_themes": ["space", "vinyl", "honey", "secret"], "runs": [run(theme="vinyl"), run(theme="honey")]}
        theme, _ = dm.choose_theme(THEMES, state, rng)
        self.assertEqual(theme["id"], "space")
        for seed in range(20):
            theme, _ = dm.choose_theme(THEMES, {"known_themes": [], "runs": []}, random.Random(seed))
            self.assertNotEqual(theme["id"], "secret")

    def test_a_theme_that_is_new_in_the_game_is_shown_first(self):
        state = {"known_themes": ["space", "vinyl"], "runs": [run(theme="space")] * 3}
        theme, reason = dm.choose_theme(THEMES, state, random.Random(3))
        self.assertEqual(theme["id"], "honey")
        self.assertIn("new in the game", reason)

    def test_on_the_first_run_nothing_counts_as_new(self):
        theme, reason = dm.choose_theme(THEMES, {"known_themes": [], "runs": []}, random.Random(3))
        self.assertNotIn("new in the game", reason)

    def test_no_themes_is_an_error(self):
        with self.assertRaises(dm.DailyMediaError):
            dm.choose_theme([], {"runs": []}, random.Random(1))

    def test_scene_choice_respects_the_kind_and_needs_a_named_mechanic(self):
        scenes = [scene("hard", 0, ["Prism"], 4), scene("hard", 1, ["Prism", "Mirror"], 12),
                  scene("hard", 2, ["Rim Emitter"], 12), scene("hard", 3, ["Portal"], 30)]
        clip = dm.choose_scenes(scenes, "clip", [], random.Random(1))
        self.assertEqual([s["id"] for s in clip], ["hard:2"])                  # only 9-16 moves and a canonical mechanic
        still = dm.choose_scenes(scenes, "puzzle", [], random.Random(1))
        self.assertEqual({s["id"] for s in still}, {"hard:1", "hard:2"})

    def test_once_every_level_was_shown_the_least_recent_come_back(self):
        scenes = [scene("hard", i, ["Prism"], 10) for i in range(3)]
        history = [{"scene": "hard:3", "labels": ["Prism"]}, {"scene": "hard:1", "labels": ["Prism"]}, {"scene": "hard:2", "labels": ["Prism"]}]
        chosen = dm.choose_scenes(scenes, "puzzle", history, random.Random(1), count=3)
        self.assertEqual([s["id"] for s in chosen], ["hard:3", "hard:1", "hard:2"])


class StillCheckTests(unittest.TestCase):
    def setUp(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow is not installed")
        self.dir = Path(tempfile.mkdtemp())

    def image(self, name, fill=None):
        from PIL import Image
        path = self.dir / name
        if fill is not None:
            Image.new("RGB", (400, 500), fill).save(path)
        else:
            path.write_bytes(png_bytes(400, 500, seed=4))
        return path

    def test_a_blank_or_loading_screen_is_rejected(self):
        problems = cg.still_problems(self.image("blank.png", (12, 16, 24)))
        self.assertTrue(problems)
        self.assertTrue(any("flat" in p or "colours" in p for p in problems))

    def test_a_detailed_picture_passes(self):
        self.assertEqual(cg.still_problems(self.image("noise.png")), [])

    def test_an_unreadable_file_is_a_problem_not_a_crash(self):
        bad = self.dir / "bad.png"
        bad.write_bytes(b"not an image")
        self.assertTrue(cg.still_problems(bad))


class FreshnessTests(unittest.TestCase):
    def test_identical_bytes_are_never_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.png"
            path.write_bytes(png_bytes())
            manifest = {"items": [{"path": "store/old.png", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}
            self.assertIn("identical bytes", dm.freshness_problems(path, "image", manifest)[0])

    def test_a_near_duplicate_picture_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cg, "find_near_duplicate", return_value="old-id"):
            path = Path(tmp) / "a.png"
            path.write_bytes(png_bytes())
            self.assertIn("old-id", dm.freshness_problems(path, "image", {"items": []})[0])


class FakeSession:
    """Stands in for capture_gameplay.GameSession (no browser)."""

    def __init__(self, *args, errors=(), **kwargs):
        self.errors = list(errors)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CaptureOneTests(unittest.TestCase):
    def test_page_errors_during_a_capture_discard_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "x.jpg"
            out.write_bytes(png_bytes())
            result = {"path": out, "type": "image", "kind": "puzzle"}
            with mock.patch.object(cg, "capture_scene", return_value=[result]):
                with self.assertRaises(cg.CaptureError):
                    dm.capture_one("game.html", scene("hard", 8, ["Prism"], 9), "puzzle", "vinyl", tmp,
                                   session_factory=lambda *a, **k: FakeSession(errors=["TypeError: x"]), log=lambda *a: None)
            self.assertFalse(out.exists())

    def test_only_the_requested_still_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            puzzle, solved = Path(tmp) / "p.jpg", Path(tmp) / "s.jpg"
            for path in (puzzle, solved):
                path.write_bytes(png_bytes())
            results = [{"path": puzzle, "type": "image", "kind": "puzzle"}, {"path": solved, "type": "image", "kind": "solved"}]
            with mock.patch.object(cg, "capture_scene", return_value=results) as capture:
                kept = dm.capture_one("game.html", scene("hard", 8, ["Prism"], 9), "solved", "vinyl", tmp,
                                      session_factory=lambda *a, **k: FakeSession(), log=lambda *a: None)
            self.assertEqual(kept["path"], solved)
            self.assertFalse(puzzle.exists())
            self.assertEqual(capture.call_args.kwargs["theme"], "vinyl")
            self.assertTrue(capture.call_args.kwargs["frame_board"])


class WaitForUrlTests(unittest.TestCase):
    def test_waits_until_the_exact_bytes_are_served(self):
        body = b"x" * 5000
        answers = [support.StreamResponse(404), support.StreamResponse(200, b"stale"), support.StreamResponse(200, body)]
        sleeps = []
        ok = dm.wait_for_url("https://raw/x", hashlib.sha256(body).hexdigest(), get=lambda *a, **k: answers.pop(0),
                             sleep=sleeps.append)
        self.assertTrue(ok)
        self.assertEqual(len(sleeps), 2)

    def test_gives_up_honestly(self):
        ok = dm.wait_for_url("https://raw/x", "0" * 64, get=lambda *a, **k: support.StreamResponse(404), sleep=lambda s: None)
        self.assertFalse(ok)


class HostedRecordTests(OfflineTestCase):
    """host_and_record writes records the EXISTING media gate accepts (strict mode, provenance, decode proof)."""

    def setUp(self):
        super().setUp()
        self.local = self.tmp / "capture" / "rs_hard_L09_prism-mirror_puzzle_20261028.png"
        self.local.parent.mkdir()
        self.local.write_bytes(png_bytes(540, 675, seed=9))
        self.sha = hashlib.sha256(self.local.read_bytes()).hexdigest()
        self.url = "https://raw.githubusercontent.com/ultramegaray0-lgtm/ringshift-social-autopilot/media-store/store/" + self.local.name
        info = {"width": 540, "height": 675, "bytes": self.local.stat().st_size, "duration": 0.0}
        self.result = {"path": self.local, "type": "image", "kind": "puzzle", "info": info,
                       "scene": scene("hard", 8, ["Prism", "Mirror"], 11), "solution": {"cost": 11},
                       "theme": "vinyl", "theme_name": "Vinyl Record", "aspect": "4:5", "pace": 1.0}
        # FFmpeg-free: the technical facts and decode check of the local file are stubbed (decoding is tested elsewhere).
        mock.patch.object(mm, "probe_file", return_value={"width": 540, "height": 675, "aspect": "4:5", "bytes": info["bytes"],
                                                          "duration": None, "sha256": self.sha}).start()
        mock.patch.object(mm, "check_decodes", return_value=("ok", [])).start()
        mock.patch.object(mm, "fingerprints", return_value=["0" * 64]).start()
        self.game = {"build": "d2fa19f31776", "commit": "1e604e79ce28aef5c9376dd8bb0a6e12755ad406", "file": "ringshift v0.33.html"}

    def host(self, kept_names=None):
        state = {"runs": [], "known_themes": []}
        made = {"result": self.result, "kind": "puzzle", "requested_kind": "puzzle", "scene": self.result["scene"],
                "theme": {"id": "vinyl", "name": "Vinyl Record"}, "attempts": [{"outcome": "kept"}], "themes": ["vinyl"]}
        upload = lambda files: [{"name": self.local.name, "file": str(self.local), "url": self.url, "type": "image",  # noqa: E731
                                 "bytes": self.local.stat().st_size}]
        kept = kept_names if kept_names is not None else [self.local.name]
        return dm.host_and_record(
            made, self.game, now=dm.utc_now(), run_id="999", upload=upload, list_store=lambda: [{"name": n} for n in kept],
            wait=lambda url, sha: True, state=state, base_dir=self.tmp, tags_file=self.tags_file,
            manifest_file=self.tmp / "media" / "manifest.json", history_file=self.tmp / "data" / "capture_history.json",
            log=lambda *a: None)

    def test_the_record_carries_full_provenance_and_validates(self):
        entry = self.host()
        manifest = mm.load_manifest(self.tmp / "media" / "manifest.json")
        record = mm.by_path(manifest)["store/" + self.local.name]
        self.assertTrue(record["hosted_only"])
        self.assertEqual(record["sha256"], self.sha)
        self.assertEqual(record["capture"]["game_commit"], self.game["commit"])
        self.assertEqual(record["capture"]["game_build"], self.game["build"])
        self.assertEqual(record["game"]["theme"], "vinyl")
        self.assertEqual(record["game"]["level"], 9)
        self.assertEqual(record["mechanisms"], ["Prism", "Mirror"])
        self.assertIn("'Vinyl Record' visual theme", record["description"])
        tags = json.loads(self.tags_file.read_text(encoding="utf-8"))
        problems = [p for p in mm.validate_manifest(manifest, self.tmp, tags=tags, decode=False) if p["level"] == "error"]
        self.assertEqual(problems, [])
        self.assertEqual(entry["status"], "captured")
        self.assertEqual(entry["sha256"], self.sha)
        history = json.loads((self.tmp / "data" / "capture_history.json").read_text(encoding="utf-8"))
        self.assertEqual(history[-1]["scene"], "hard:9")

    def test_fresh_hosted_media_passes_the_strict_media_gate_and_provenance(self):
        self.host()
        body = self.local.read_bytes()
        mock.patch.object(autopilot, "MEDIA_CHECK_MODE", "strict").start()
        mock.patch.object(autopilot.media_tools, "have_ffmpeg", return_value=True).start()
        mock.patch.object(autopilot.media_tools, "validate_media", return_value=[]).start()
        mock.patch("requests.get", side_effect=lambda url, **k: support.StreamResponse(200, body) if url == self.url
                   else support.StreamResponse(404)).start()
        media = media_selector.select_media("", post_type={"id": "challenge"}, only_files={"store/" + self.local.name},
                                            require_description=True)
        self.assertEqual(media["url"], self.url)
        check = autopilot.check_media_for_approval(media, self.tmp)
        self.assertEqual(check["status"], "ok")
        self.assertEqual(check["source"], "url")
        self.assertEqual(check["provenance"]["source"], "capture_gameplay.py")
        self.assertEqual(check["provenance"]["level"], 9)
        # the writer and the caption anchors get the game's own theme name
        self.assertEqual(media["theme"], "Vinyl Record")
        import caption_quality
        self.assertIn("theme Vinyl Record", caption_quality.mentions_anchor("That Vinyl Record glow on the rim.",
                                                                           caption_quality.anchors_for(media)))

    def test_store_files_removed_by_retention_are_never_selected_again(self):
        self.host()
        older = "store/old.jpg"
        manifest = mm.load_manifest(self.tmp / "media" / "manifest.json")
        tags = json.loads(self.tags_file.read_text(encoding="utf-8"))
        clone = json.loads(json.dumps(mm.by_path(manifest)["store/" + self.local.name]))
        clone.update(path=older, id="old")
        manifest["items"].append(clone)
        tags[older] = clone["legacy"]
        marked = dm.prune_hosted(manifest, tags, {self.local.name})
        self.assertEqual(marked, [older])
        old = mm.by_path(manifest)[older]
        self.assertTrue(old["exclude"])
        self.assertEqual(old["post_categories"], [])
        self.assertEqual(old["legacy"], tags[older])          # the manifest's copy of the entry stays identical

    def test_a_url_that_never_serves_the_bytes_stops_the_run(self):
        made = {"result": self.result, "kind": "puzzle", "requested_kind": "puzzle", "scene": self.result["scene"],
                "theme": {"id": "vinyl"}, "attempts": [], "themes": []}
        with self.assertRaises(dm.DailyMediaError):
            dm.host_and_record(made, self.game, now=dm.utc_now(), upload=lambda f: [{"name": "x.jpg", "url": self.url, "file": "x"}],
                               list_store=lambda: [], wait=lambda *a: False, state={}, base_dir=self.tmp,
                               tags_file=self.tags_file, manifest_file=self.tmp / "media" / "manifest.json",
                               history_file=self.tmp / "data" / "h.json", log=lambda *a: None)
        self.assertFalse((self.tmp / "media" / "manifest.json").exists())


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state = self.dir / "daily_media.json"
        patcher = mock.patch.object(dm, "STATE_FILE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        for variable in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
            patcher = mock.patch.dict(os.environ, {variable: str(self.dir / variable)})
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return dm.main(argv)

    def test_a_missing_game_repository_is_a_clear_failure_with_no_media(self):
        code = self.run_main(["--game-dir", str(self.dir / "nowhere")])
        self.assertEqual(code, 2)
        self.assertIn("could not be loaded", (self.dir / "GITHUB_STEP_SUMMARY").read_text(encoding="utf-8"))
        self.assertFalse(self.state.exists())

    def test_when_nothing_passes_the_run_fails_and_records_why(self):
        game_dir = self.dir / "game"
        game_dir.mkdir()
        (game_dir / "ringshift v0.33.html").write_text("<html>" + "x" * 20000 + "</html>", encoding="utf-8")
        error = dm.DailyMediaError("no fresh puzzle media passed every check")
        error.attempts = [{"kind": "puzzle", "scene": "hard:9", "outcome": "rejected", "why": "too flat"}]
        with mock.patch.object(dm, "make_media", side_effect=error):
            code = self.run_main(["--game-dir", str(game_dir)])
        self.assertEqual(code, 3)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["runs"][-1]["status"], "failed")
        self.assertIn("media_file=\n", (self.dir / "GITHUB_OUTPUT").read_text(encoding="utf-8"))
        self.assertIn("NONE", (self.dir / "GITHUB_STEP_SUMMARY").read_text(encoding="utf-8"))


@unittest.skipUnless(os.environ.get("RINGSHIFT_GAME_HTML") and Path(os.environ.get("RINGSHIFT_GAME_HTML", "")).is_file()
                     and media_tools.have_ffmpeg(), "set RINGSHIFT_GAME_HTML to the game file (and install FFmpeg)")
class DailyMediaRealGameTests(unittest.TestCase):
    """Real browser, real game: one themed, board-framed still and one real clip, validated like the daily run."""

    def test_a_themed_still_and_a_real_clip(self):
        game = os.environ["RINGSHIFT_GAME_HTML"]
        with tempfile.TemporaryDirectory() as tmp:
            for kind in ("puzzle", "clip"):
                made = dm.make_media(game, {"runs": [], "known_themes": []}, rng=random.Random(5), kind=kind, out_dir=tmp,
                                     manifest={"items": []}, capture_history=[], log=lambda *a: None)
                result = made["result"]
                self.assertEqual(dm.media_problems(result), [])
                self.assertEqual(result["theme"], made["theme"]["id"])
                if kind == "clip":
                    info = media_tools.probe(result["path"])
                    self.assertGreaterEqual(info["duration"], 8.0)
                    self.assertEqual(info["height"], 1350)
                    self.assertTrue(media_tools.gameplay_visible(result["path"])[0])


if __name__ == "__main__":
    unittest.main()
