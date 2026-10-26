"""main(): a publishing failure must fail the run, and duplicates must never be sent."""

import contextlib
import io
import json
import os
import unittest
from unittest import mock

import support
from support import FakeResponse, OfflineTestCase, queued_response
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

    def run_main(self):
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            try:
                autopilot.main()
                return None, out.getvalue()
            except BaseException as error:
                return error, out.getvalue()

    # ---- publishing failures fail the run -----------------------------------

    def test_buffer_401_makes_main_exit_nonzero_and_changes_nothing(self):
        before = self.state()
        self.candidates.return_value = ["Brand new idea about rings."]
        self.post.side_effect = None
        self.post.return_value = FakeResponse(401, text="Unauthorized")

        error, output = self.run_main()

        self.assertIsInstance(error, SystemExit)
        self.assertEqual(error.code, 1)
        self.assertIn("NOT confirmed as queued", output)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.post.call_count, 1)
        self.assertNotIn(support.BUFFER_KEY, output)

    def test_missing_media_confirmation_makes_main_exit_nonzero(self):
        before = self.state()
        self.candidates.return_value = ["Brand new idea about rings."]
        self.post.side_effect = None
        self.post.return_value = queued_response(assets=[])

        error, _ = self.run_main()

        self.assertIsInstance(error, SystemExit)
        self.assertEqual(error.code, 1)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.post.call_count, 1)

    def test_success_returns_normally_and_advances_state_once(self):
        self.candidates.return_value = ["Brand new idea about rings."]
        self.post.side_effect = None
        self.post.return_value = queued_response()

        error, output = self.run_main()

        self.assertIsNone(error)
        self.assertIn("BEST CANDIDATE ACCEPTED", output)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(
            json.loads(self.counter_file.read_text(encoding="utf-8")),
            {"successful_posts": 4},
        )
        self.assertEqual(
            json.loads(self.history_file.read_text(encoding="utf-8")), [self.image_rel]
        )

    # ---- fail fast on keys ---------------------------------------------------

    def test_missing_keys_exit_before_any_gemini_or_buffer_call(self):
        for name in ("BUFFER_API_KEY", "GEMINI_API_KEY"):
            with self.subTest(missing=name):
                self.candidates.reset_mock()
                self.post.reset_mock()
                before = self.state()

                with mock.patch.dict(os.environ, {name: ""}):
                    error, output = self.run_main()

                self.assertIsInstance(error, SystemExit)
                self.assertIn(name, str(error))
                self.assertNotIn(support.BUFFER_KEY, str(error))
                self.assertNotIn(support.GEMINI_KEY, str(error))
                self.candidates.assert_not_called()
                self.assertEqual(self.post.call_count, 0)
                self.assertEqual(self.state(), before)

    # ---- duplicate prevention -----------------------------------------------

    def test_is_duplicate_post_ignores_case_and_punctuation(self):
        existing = "## Post 001\n\nThere is a THIN line, between a block (and) a trap!\n\n#Ringshift\n"
        self.assertTrue(
            autopilot.is_duplicate_post("there is a thin line between a block and a trap", existing)
        )
        self.assertFalse(autopilot.is_duplicate_post("A completely different thought", existing))
        self.assertFalse(autopilot.is_duplicate_post("", existing))
        self.assertFalse(autopilot.is_duplicate_post("anything", "", None))

    def test_candidate_already_in_post_history_is_never_sent(self):
        text = "The beam bends and so does my sense of direction."
        mock.patch.object(autopilot, "posts_history", f"## Post 009\n\n{text}\n\n#Ringshift\n").start()
        self.candidates.return_value = [text]

        error, output = self.run_main()

        self.assertIsNone(error)
        self.assertIn("duplicate of an existing post", output)
        self.assertIn("NO CANDIDATE PASSED", output)
        self.critic.assert_not_called()
        self.assertEqual(self.post.call_count, 0)

    def test_candidate_already_queued_in_buffer_is_never_sent(self):
        # analytics.md lists posts Buffer already holds (queued or sent), which is
        # what protects a re-run after "queued but media unconfirmed".
        text = "Is anyone else bad at predicting beam bounces?"
        mock.patch.object(autopilot, "analytics", f"### Post\n\n{text}\n\n#Ringshift\n").start()
        self.candidates.return_value = [text.upper()]

        error, output = self.run_main()

        self.assertIsNone(error)
        self.assertIn("NO CANDIDATE PASSED", output)
        self.assertEqual(self.post.call_count, 0)

    def test_duplicate_is_skipped_but_a_fresh_candidate_is_sent_exactly_once(self):
        old = "An idea that was already posted."
        fresh = "A genuinely new thought about beams."
        mock.patch.object(autopilot, "posts_history", old).start()
        self.candidates.return_value = [old, fresh]
        self.post.side_effect = None
        self.post.return_value = queued_response()

        error, _ = self.run_main()

        self.assertIsNone(error)
        self.assertEqual(self.post.call_count, 1)
        sent_text = self.post.call_args.kwargs["json"]["variables"]["input"]["text"]
        self.assertEqual(sent_text, fresh)
        self.assertEqual(self.critic.call_count, 1)

    def test_rejected_candidates_are_not_sent(self):
        self.critic.return_value = CRITIC_NO
        self.candidates.return_value = ["Something the critic dislikes."]

        error, output = self.run_main()

        self.assertIsNone(error)
        self.assertIn("NO CANDIDATE PASSED", output)
        self.assertEqual(self.post.call_count, 0)


if __name__ == "__main__":
    unittest.main()
