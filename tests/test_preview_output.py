"""Regression tests for the run #37 report: a manual run showed dozens of "draft" notices (test fixtures with their own
approval codes) instead of exactly one draft.

What actually happened: the unit tests in the workflow's "tests" job run the whole draft flow, and the flow appends a draft
summary to $GITHUB_STEP_SUMMARY. On the runner that variable points at the REAL job summary, so every test that made a
draft (temporary black_hole.png, fixture mode, fresh approval code) wrote it there. The real preview job writes one.

These tests pin down: tests can never reach the Actions files; a fixture or unchecked draft is never announced as a draft;
a real preview makes exactly one draft and one code; the explicit media_file pin is honoured; only media with genuine
provenance in the manifest can become an approvable draft; and captions must describe the file that was selected.
All offline: no network, no keys, nothing is sent.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support
from support import OfflineTestCase
from test_media_gate import StrictCase, CRITIC_YES, DESC, FACTS, GOOD
import approvals
import autopilot
import media_selector

ROOT = support.ROOT


# ---------------------------------------------------------------- the reported symptom, end to end

class TestsNeverTouchTheRealActionsFiles(unittest.TestCase):
    """Run the modules that make drafts the way the workflow's tests job does (Actions variables set), in a subprocess."""

    MODULES = ("test_main_flow.py", "test_publish_guards.py", "test_verify_and_claims.py", "test_autopilot_hardening.py")

    def test_the_draft_making_tests_leave_the_job_summary_and_annotations_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            summary, output = Path(folder) / "summary.md", Path(folder) / "output.txt"
            env = dict(os.environ, GITHUB_ACTIONS="true", GITHUB_STEP_SUMMARY=str(summary), GITHUB_OUTPUT=str(output),
                       PYTHONDONTWRITEBYTECODE="1")
            for module in self.MODULES:
                with self.subTest(module=module):
                    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"), "-p", module],
                                            env=env, capture_output=True, text=True, cwd=ROOT, timeout=300)
                    self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
                    seen = result.stdout + result.stderr
                    self.assertNotIn("::notice title=Ringshift preview", seen, "a test announced a draft to the Actions log")
                    self.assertNotIn("Ringshift draft", summary.read_text(encoding="utf-8") if summary.exists() else "",
                                     f"{module} wrote a draft into the job summary")
            self.assertTrue(not summary.exists() or summary.read_text(encoding="utf-8") == "", "the job summary was written to")
            self.assertTrue(not output.exists() or output.read_text(encoding="utf-8") == "")

    def test_the_test_support_removes_the_actions_file_variables(self):
        for variable in support.ACTIONS_FILE_VARIABLES:
            self.assertNotIn(variable, os.environ, f"{variable} must not exist while tests run")


class FixtureDraftsAreNeverAnnounced(OfflineTestCase):
    """Fixture mode is the unit-test mode. Nothing it produces may look like a draft in a run."""

    def setUp(self):
        super().setUp()
        self.summary = self.tmp / "summary.md"
        self.draft_path = self.tmp / "draft.json"
        mock.patch.object(autopilot, "generate_candidates", return_value=[GOOD]).start()
        mock.patch.object(autopilot, "critique_post", return_value=CRITIC_YES).start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"tags": ["portal"], "description": DESC}}), encoding="utf-8")

    def dry_run(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.summary)}), contextlib.redirect_stdout(out):
            autopilot.main(["--dry-run", "--draft-file", str(self.draft_path)])
        return out.getvalue()

    def test_a_fixture_draft_is_not_written_to_the_job_summary(self):
        output = self.dry_run()
        draft = json.loads(self.draft_path.read_text(encoding="utf-8"))
        self.assertEqual(draft["media"]["check"]["status"], "fixture")
        self.assertFalse(self.summary.exists() and self.summary.read_text(encoding="utf-8"),
                         "a fixture draft reached the job summary")
        self.assertIn("not approvable", output)

    def test_a_fixture_draft_is_not_announced_even_inside_actions(self):
        self.dry_run()
        draft = json.loads(self.draft_path.read_text(encoding="utf-8"))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), contextlib.redirect_stdout(out):
            autopilot.write_draft_file(draft, str(self.tmp / "again.json"))
        self.assertNotIn("::notice", out.getvalue())

    def test_the_summary_writer_itself_refuses_fixture_mode(self):
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.summary)}):
            autopilot.write_step_summary("## Ringshift draft - NOT sent")
        self.assertFalse(self.summary.exists())

    def test_a_real_actions_process_refuses_to_run_in_fixture_mode(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), self.assertRaises(SystemExit) as raised:
            autopilot.main(["--dry-run", "--draft-file", str(self.draft_path)])
        self.assertIn("test-fixture media mode", str(raised.exception))
        self.assertFalse(self.draft_path.exists())

    def test_the_unapprovable_kinds_are_not_approvable_drafts(self):
        ok = {"result": "winner", "draft_id": "d-12345678-abcd", "media": {"check": {"status": "ok"}}}
        self.assertTrue(autopilot.is_approvable_draft(ok))
        for broken in ({**ok, "media": {"check": {"status": "fixture"}}}, {**ok, "media": {}}, {**ok, "result": "no_candidate_passed"},
                       {**ok, "draft_id": None}, None, {}):
            self.assertFalse(autopilot.is_approvable_draft(broken), broken)


# ---------------------------------------------------------------- exactly one draft per real preview

class OneDraftPerRealPreview(StrictCase):
    def setUp(self):
        super().setUp()
        self.summary = self.tmp / "summary.md"
        self.draft_path = self.tmp / "draft.json"
        mock.patch.object(autopilot, "generate_candidates", return_value=[GOOD]).start()
        mock.patch.object(autopilot, "critique_post", return_value=CRITIC_YES).start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"tags": ["portal"], "description": DESC}}), encoding="utf-8")

    def dry_run(self, *extra, actions=False):
        out = io.StringIO()
        env = {"GITHUB_STEP_SUMMARY": str(self.summary), "GITHUB_ACTIONS": "true" if actions else "false"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            autopilot.main(["--dry-run", "--draft-file", str(self.draft_path), *extra])
        return out.getvalue()

    def test_one_run_writes_one_draft_one_code_one_summary_one_notice(self):
        output = self.dry_run(actions=True)
        draft = json.loads(self.draft_path.read_text(encoding="utf-8"))
        text = self.summary.read_text(encoding="utf-8")
        self.assertEqual(text.count("## Ringshift draft"), 1)
        self.assertEqual(text.count("(the approval code):**"), 1)
        self.assertIn(draft["draft_id"], text)
        self.assertEqual(output.count("DRAFT ID (approval code)"), 1)
        self.assertEqual(sum(1 for line in output.splitlines() if line.startswith("::notice title=Ringshift preview")), 1)
        self.assertEqual(autopilot.DRAFTS_THIS_RUN, [draft["draft_id"]])
        self.assertEqual(draft["media"]["check"]["status"], "ok")

    def test_a_second_approval_code_cannot_be_issued_in_the_same_run(self):
        self.dry_run()
        draft = json.loads(self.draft_path.read_text(encoding="utf-8"))
        with self.assertRaises(RuntimeError):
            autopilot.build_draft(GOOD, {**self.media, **FACTS}, {"id": "mechanic"}, 50, "", [], draft["media"]["check"])
        self.assertEqual(len(autopilot.DRAFTS_THIS_RUN), 1)

    def test_the_summary_shows_where_the_media_came_from(self):
        self.dry_run()
        text = self.summary.read_text(encoding="utf-8")
        self.assertIn("manifest", text)
        self.assertIn("manual-recording", text)
        self.assertIn("level 11", text)

    def test_a_run_without_usable_media_writes_no_draft_anywhere(self):
        (self.tmp / "media" / "manifest.json").unlink()
        output = self.dry_run(actions=True)
        report = json.loads(self.draft_path.read_text(encoding="utf-8"))
        self.assertEqual(report["result"], "no_candidate_passed")
        self.assertNotIn("draft_id", report)
        self.assertFalse(self.summary.exists() and self.summary.read_text(encoding="utf-8"))
        self.assertEqual(autopilot.DRAFTS_THIS_RUN, [])
        self.assertNotIn("DRAFT ID", output)


# ---------------------------------------------------------------- the explicit media_file pin

class MediaFilePinIsHonoured(StrictCase):
    SECOND = "media/gameplay/second_level.png"

    def setUp(self):
        super().setUp()
        (self.tmp / self.SECOND).write_bytes(support.png_bytes(seed=2))
        support.write_manifest(self.tmp)
        self.draft_path = self.tmp / "draft.json"
        mock.patch.object(autopilot, "generate_candidates", return_value=[GOOD]).start()
        mock.patch.object(autopilot, "critique_post", return_value=CRITIC_YES).start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"tags": ["portal"], "description": DESC},
                                              self.SECOND: {"tags": ["portal"], "description": DESC}}), encoding="utf-8")

    def dry_run(self, pin):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            autopilot.main(["--dry-run", "--draft-file", str(self.draft_path), "--media-file", pin])
        return out.getvalue()

    def test_the_draft_is_about_the_pinned_file_whichever_file_the_rotation_would_choose(self):
        for pin in (self.image_rel, self.SECOND):
            with self.subTest(pin=pin):
                self.draft_path.unlink(missing_ok=True)
                self.dry_run(pin)
                draft = json.loads(self.draft_path.read_text(encoding="utf-8"))
                self.assertEqual(draft["media"]["file"], pin)
                self.assertEqual(draft["pinned_media"], pin)
                self.assertEqual(draft["media"]["check"]["provenance"]["manifest_sha256"], draft["media"]["sha256"])

    def test_a_pin_that_cannot_be_used_gives_no_draft_and_never_another_file(self):
        support.write_manifest(self.tmp, overrides={self.SECOND: {"capture": {"source": "unknown"}}})
        self.dry_run(self.SECOND)
        report = json.loads(self.draft_path.read_text(encoding="utf-8"))
        self.assertEqual(report["result"], "no_candidate_passed")
        self.assertNotIn("draft_id", report)
        self.assertNotEqual((report.get("media") or {}).get("file"), self.image_rel)

    def test_the_run_stops_if_the_chosen_file_is_not_the_pinned_one(self):
        other = ({"id": "mechanic", "label": "Mechanic"},
                 {"file": self.image_rel, "type": "image", "url": media_selector.raw_url(self.image_rel), **FACTS}, [])
        with mock.patch.object(autopilot, "choose_post_and_media", return_value=other), self.assertRaises(SystemExit) as raised:
            self.dry_run(self.SECOND)
        self.assertIn("was pinned", str(raised.exception))
        self.assertFalse(self.draft_path.exists())


# ---------------------------------------------------------------- genuine gameplay provenance

class ProvenanceIsRequiredForApproval(StrictCase):
    """A valid signature, a successful decode and the right bytes are not proof of gameplay: the manifest must vouch for it."""

    def spoil(self, **fields):
        manifest = support.write_manifest(self.tmp, overrides={self.image_rel: fields})
        return manifest

    def test_a_genuine_record_passes_and_is_reported(self):
        provenance = self.check()["provenance"]
        self.assertEqual((provenance["source"], provenance["level"], provenance["verification"]), ("manual-recording", 11, "reviewed"))
        self.assertEqual(provenance["manifest_sha256"], approvals.sha256_file(self.tmp / self.image_rel))

    def test_a_perfectly_valid_decodable_file_the_manifest_does_not_know_is_refused(self):
        support.write_manifest(self.tmp, only={self.video_rel})
        with self.assertRaises(autopilot.MediaUnavailableError) as raised:
            self.check()
        self.assertIn("not in media/manifest.json", str(raised.exception))

    def test_no_manifest_at_all_is_refused(self):
        (self.tmp / "media" / "manifest.json").unlink()
        with self.assertRaises(autopilot.MediaUnavailableError) as raised:
            self.check()
        self.assertIn("no media/manifest.json", str(raised.exception))

    def test_every_way_the_record_can_fail_to_vouch_for_the_file(self):
        cases = {
            "unknown source": ({"capture": {"source": "unknown"}}, "no recognised gameplay source"),
            "a test fixture source": ({"capture": {"source": "test-fixture"}}, "no recognised gameplay source"),
            "no source": ({"capture": {}}, "no recognised gameplay source"),
            "excluded": ({"exclude": True}, "excluded"),
            "validation failed": ({"validation": {"status": "failed", "problems": ["x"]}}, "does not mark"),
            "validation never run": ({"validation": {"status": "unchecked", "problems": []}}, "does not mark"),
            "unverified": ({"verification": {"status": "unverified"}}, "in the manifest"),
            "other bytes": ({"sha256": "0" * 64}, "different bytes"),
            "no level": ({"game": {"mode": "hard", "level": None}}, "no recorded level"),
            "no mechanisms": ({"mechanisms": []}, "no recorded level"),
            "no description": ({"description": " "}, "no recorded level"),
            "capture tool without a game build": ({"capture": {"source": "capture_gameplay.py", "game_build": None},
                                                   "verification": {"status": "verified"}}, "game build"),
            "capture tool but only 'reviewed'": ({"capture": {"source": "capture_gameplay.py", "game_build": "abc"},
                                                  "verification": {"status": "reviewed"}}, "must be verified"),
        }
        for label, (fields, expected) in cases.items():
            with self.subTest(label):
                self.spoil(**fields)
                with self.assertRaises(autopilot.MediaUnavailableError) as raised:
                    self.check()
                self.assertIn(expected, str(raised.exception))

    def test_the_capture_tool_record_with_a_game_build_passes(self):
        self.spoil(capture={"source": "capture_gameplay.py", "game_build": "13a0195f794a"}, verification={"status": "verified"})
        self.assertEqual(self.check()["provenance"]["game_build"], "13a0195f794a")

    def test_a_draft_cannot_be_made_for_unvouched_media(self):
        self.spoil(capture={"source": "unknown"})
        with self.assertRaises(autopilot.MediaUnavailableError):
            autopilot.build_draft(GOOD, {**self.media, **FACTS}, {"id": "mechanic"}, 50, "", [])
        self.assertEqual(autopilot.DRAFTS_THIS_RUN, [])

    def test_publish_time_asks_again(self):
        mock.patch.object(autopilot, "generate_candidates", return_value=[GOOD]).start()
        mock.patch.object(autopilot, "critique_post", return_value=CRITIC_YES).start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"tags": ["portal"], "description": DESC}}), encoding="utf-8")
        draft_path = self.tmp / "draft.json"
        with contextlib.redirect_stdout(io.StringIO()):
            autopilot.main(["--dry-run", "--draft-file", str(draft_path)])
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        self.spoil(capture={"source": "unknown"})                       # the manifest no longer vouches for it
        with self.assertRaises(approvals.ApprovalError) as raised:
            autopilot.prepare_approved(str(draft_path), draft["draft_id"])
        self.assertEqual(raised.exception.reason, "media_missing")
        self.assertIn("gameplay source", str(raised.exception))


class RealRepositoryMedia(unittest.TestCase):
    """The committed media, not fixtures."""

    def setUp(self):
        patcher = mock.patch.object(media_selector, "MEDIA_DIR", ROOT / "media")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manifest = json.loads((ROOT / "media" / "manifest.json").read_text(encoding="utf-8"))["items"]

    def provenance(self, relative):
        return autopilot.manifest_provenance(relative, approvals.sha256_file(ROOT / relative))

    def test_the_screenshot_chosen_for_the_first_post_has_genuine_provenance(self):
        provenance = self.provenance("media/gameplay/10_void_theme_hard11.png")
        self.assertEqual((provenance["level"], provenance["source"]), (11, "manual-recording"))
        self.assertEqual(sorted(provenance["mechanisms"]), ["Block", "Echo", "Portal"])

    def test_every_capture_tool_file_marked_valid_has_provenance_and_nothing_fixture_like_is_indexed(self):
        for item in self.manifest:
            self.assertNotEqual(item["path"].split("/")[-1], "black_hole.png", "the test fixture's name is in the manifest")
            self.assertNotEqual((item.get("capture") or {}).get("source"), "test-fixture")
            if (item.get("capture") or {}).get("source") == "capture_gameplay.py" and (item.get("validation") or {}).get("status") == "ok":
                with self.subTest(item["path"]):
                    self.assertTrue(self.provenance(item["path"])["game_build"])

    def test_the_test_fixture_file_name_is_not_a_repository_file(self):
        self.assertFalse((ROOT / "media" / "gameplay" / "black_hole.png").exists())


# ---------------------------------------------------------------- no fixture fallback, and captions describe the file

class FixturesAreNeverFallbackMedia(StrictCase):
    def test_media_missing_from_the_manifest_is_never_chosen_instead(self):
        support.write_manifest(self.tmp, only={self.video_rel})               # the PNG fixture exists on disk but is unlisted
        project = autopilot.load_project()
        facts = autopilot.load_facts()
        try:
            _, media, _ = autopilot.choose_post_and_media(
                project, facts, counter=3, check=lambda m: autopilot.check_media_for_approval(m, self.tmp))
            self.assertEqual(media["file"], self.video_rel)
        except autopilot.NoSuitableMediaError:
            pass                                                              # also fine: no draft at all
        # and with nothing listed there is nothing to choose
        support.write_manifest(self.tmp, only={"media/none.png"})
        with self.assertRaises(autopilot.NoSuitableMediaError):
            autopilot.choose_post_and_media(project, facts, counter=3,
                                            check=lambda m: autopilot.check_media_for_approval(m, self.tmp))


class CaptionsDescribeTheSelectedImage(unittest.TestCase):
    MEDIA = {"file": "media/gameplay/10_void_theme_hard11.png", "type": "image", **FACTS}

    def reason(self, caption, recent=()):
        with mock.patch.object(autopilot, "posts_history", ""), mock.patch.object(autopilot, "analytics", ""):
            return autopilot.screen_candidate(caption, list(recent), {}, self.MEDIA, {"id": "mechanic"})

    def test_a_caption_grounded_in_the_image_passes(self):
        self.assertIsNone(self.reason("An Echo and a Portal on Level 11, in a purple theme."))

    def test_a_caption_that_names_something_the_image_does_not_show_is_rejected(self):
        self.assertTrue(self.reason("A Prism splits the beam on Level 11."))

    def test_a_caption_about_another_level_is_rejected(self):
        self.assertTrue(self.reason("Level 12 has a Portal and an Echo."))

    def test_generic_and_repeated_captions_are_rejected(self):
        for caption in ("Which ring would you turn first?", "Brand new idea about rings.", "A genuinely new thought about beams."):
            with self.subTest(caption):
                self.assertTrue(self.reason(caption))
        self.assertTrue(self.reason("The Portal sends the Echo to a purple lock on Level 11.",
                                    recent=["Level 11 purple: the Echo goes through the Portal to a lock."]))


if __name__ == "__main__":
    unittest.main()
