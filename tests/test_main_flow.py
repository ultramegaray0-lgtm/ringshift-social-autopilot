"""main(): there is no way to publish from it except an approved draft; drafts never send or save anything."""

import contextlib
import io
import json
import os
import unittest
from unittest import mock

import support
from support import OfflineTestCase
import autopilot

CRITIC_YES = (
    "Hook: 8\nCuriosity: 8\nNatural tone: 8\nRingshift relevance: 8\n"
    "Originality: 8\nViral potential: 8\n\nCLAIMS:\nNONE\n\nKEEP: YES\n"
)
CRITIC_NO = CRITIC_YES.replace("KEEP: YES", "KEEP: NO")


class MainFlowTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.candidates = mock.patch.object(autopilot, "generate_candidates").start()
        self.critic = mock.patch.object(autopilot, "critique_post", return_value=CRITIC_YES).start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        # verified media facts: captions are matched against them (see caption_quality)
        self.tags_file.write_text(json.dumps({self.image_rel: {
            "tags": ["black hole"], "description": "Level 11 in a purple-toned theme, with Echo, Portal and Block gimmicks."}}),
            encoding="utf-8")

    def run_main(self, argv=("--dry-run",)):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                autopilot.main(list(argv))
                return None, out.getvalue()
            except BaseException as error:
                return error, out.getvalue()

    # ---- no autonomous publishing -------------------------------------------

    def test_running_without_a_mode_refuses_and_does_nothing(self):
        before = self.state()
        self.candidates.return_value = ["Purple light behind the Portal."]

        error, _ = self.run_main(argv=())

        self.assertIsInstance(error, SystemExit)
        self.assertIn("does not publish by default", str(error.code))
        self.candidates.assert_not_called()
        self.critic.assert_not_called()
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.post_log_file.exists())

    def test_the_old_flags_do_not_exist_and_nothing_is_sent(self):
        for argv in (["--live"], ["--publish"], ["--no-dry-run"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                error, _ = self.run_main(argv=argv)
                self.assertIsInstance(error, SystemExit)
                self.assertEqual(self.post.call_count, 0)

    def test_publish_and_dry_run_cannot_be_combined(self):
        error, _ = self.run_main(argv=["--dry-run", "--publish-approved", "x.json", "--approval-code", "d-00000000-0000"])
        self.assertIsInstance(error, SystemExit)
        self.assertEqual(self.post.call_count, 0)

    def test_a_winning_draft_sends_nothing_and_changes_no_state(self):
        before = self.state()
        self.candidates.return_value = ["Purple light behind the Portal."]

        error, output = self.run_main()

        self.assertIsNone(error)
        self.assertIn("DRAFT COMPLETE", output)
        self.assertIn("nothing was saved or sent", output)
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.post_log_file.exists())
        self.assertFalse(self.posts_file.exists())
        self.guard.assert_not_called()

    def test_a_draft_run_works_with_no_buffer_key_at_all(self):
        self.candidates.return_value = ["Purple light behind the Portal."]
        with mock.patch.dict(os.environ, {"BUFFER_API_KEY": ""}):
            error, output = self.run_main()
        self.assertIsNone(error)
        self.assertIn("DRAFT COMPLETE", output)
        self.assertEqual(self.post.call_count, 0)

    def test_the_draft_run_never_leaks_keys(self):
        self.candidates.return_value = ["Purple light behind the Portal."]
        _, output = self.run_main()
        self.assertNotIn(support.BUFFER_KEY, output)
        self.assertNotIn(support.GEMINI_KEY, output)

    # ---- fail fast on the one key a draft needs ---------------------------------

    def test_missing_gemini_key_exits_before_any_generation(self):
        before = self.state()
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            error, _ = self.run_main()
        self.assertIsInstance(error, SystemExit)
        self.assertIn("GEMINI_API_KEY", str(error))
        self.assertNotIn(support.GEMINI_KEY, str(error))
        self.candidates.assert_not_called()
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.state(), before)

    # ---- duplicate prevention (a duplicate never even becomes a draft) -----------

    def test_is_duplicate_post_ignores_case_and_punctuation(self):
        existing = "## Post 001\n\nThere is a THIN line, between a block (and) a trap!\n\n#Ringshift\n"
        self.assertTrue(autopilot.is_duplicate_post("there is a thin line between a block and a trap", existing))
        self.assertFalse(autopilot.is_duplicate_post("A completely different thought", existing))
        self.assertFalse(autopilot.is_duplicate_post("", existing))
        self.assertFalse(autopilot.is_duplicate_post("anything", "", None))

    def test_candidate_already_in_post_history_never_becomes_a_draft(self):
        text = "The beam bends and so does my sense of direction."
        mock.patch.object(autopilot, "posts_history", f"## Post 009\n\n{text}\n\n#Ringshift\n").start()
        self.candidates.return_value = [text]

        error, output = self.run_main()

        self.assertIsNone(error)
        self.assertIn("repeats an earlier post", output)
        self.assertNotIn(text, support.critic_texts(self.critic))
        self.assertNotIn(text, output.split("--- WINNER ---")[1] if "--- WINNER ---" in output else "")

    def test_candidate_already_queued_in_buffer_never_becomes_a_draft(self):
        text = "Is anyone else bad at predicting beam bounces?"
        mock.patch.object(autopilot, "analytics", f"### Post\n\n{text}\n\n#Ringshift\n").start()
        self.candidates.return_value = [text.upper()]

        error, output = self.run_main()

        self.assertIsNone(error)
        self.assertFalse({text, text.upper()} & set(support.critic_texts(self.critic)))
        self.assertNotIn(text.upper(), output.split("--- WINNER ---")[1] if "--- WINNER ---" in output else "")

    def test_duplicate_is_skipped_but_a_fresh_candidate_becomes_the_draft(self):
        old = "An idea that was already posted."
        fresh = "Two Portals and one purple lock."
        mock.patch.object(autopilot, "posts_history", old).start()
        self.candidates.return_value = [old, fresh]
        draft_path = self.tmp / "d.json"

        error, _ = self.run_main(argv=["--dry-run", "--draft-file", str(draft_path)])

        self.assertIsNone(error)
        self.assertEqual(json.loads(draft_path.read_text(encoding="utf-8"))["text"], fresh)
        self.assertEqual(self.critic.call_count, 1)
        self.assertEqual(self.post.call_count, 0)

    def test_rejected_candidates_do_not_become_drafts(self):
        self.critic.return_value = CRITIC_NO
        self.candidates.return_value = ["Something the critic dislikes."]
        draft_path = self.tmp / "d.json"

        error, output = self.run_main(argv=["--dry-run", "--draft-file", str(draft_path)])

        self.assertIsNone(error)
        self.assertIn("NO CANDIDATE PASSED", output)
        self.assertEqual(json.loads(draft_path.read_text(encoding="utf-8"))["result"], "no_candidate_passed")
        self.assertEqual(self.post.call_count, 0)


if __name__ == "__main__":
    unittest.main()
