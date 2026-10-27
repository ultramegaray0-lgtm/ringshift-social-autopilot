"""run_summary.py: the table at the top of a daily run never claims a post that Buffer did not confirm."""

import unittest

import support  # noqa: F401
import run_summary

MEDIA = {"status": "captured", "file": "store/rs_hard_L09_x_puzzle_20261028.jpg", "type": "image", "kind": "puzzle",
         "scene": "hard:9", "theme": "vinyl", "theme_name": "Vinyl Record", "game_file": "ringshift v0.33.html",
         "game_commit": "1e604e79ce28aef5", "run_id": "5"}


class SummaryTests(unittest.TestCase):
    def test_a_queued_post(self):
        text = run_summary.build({"WINDOW_OK": "true", "WINDOW_REASON": "nothing queued", "AUTOPUBLISH": "on"}, MEDIA,
                                 {"status": "queued", "buffer_id": "b1", "due_at": "2026-10-29T04:00:00Z", "text": "Hello",
                                  "critic_score": 41, "post_type": "challenge"})
        self.assertIn("POSTED TO THE BUFFER QUEUE", text)
        self.assertIn("`b1`", text)
        self.assertIn("Vinyl Record", text)
        self.assertIn("> Hello", text)

    def test_a_failed_capture_is_never_a_post(self):
        text = run_summary.build({"WINDOW_OK": "true", "MEDIA_STATUS": "failure"}, {"status": "failed", "why": "no level passed"}, None)
        self.assertIn("NOT POSTED", text)
        self.assertIn("NONE - no level passed", text)
        self.assertIn("not attempted", text)

    def test_a_closed_window_is_explained_not_reported_as_a_failure_or_a_post(self):
        text = run_summary.build({"WINDOW_OK": "false", "WINDOW_REASON": "a post was already queued for 2026-10-28"}, None, None)
        self.assertIn("NO POST NEEDED", text)
        self.assertNotIn("POSTED TO", text)

    def test_refused_and_disabled(self):
        refused = run_summary.build({"WINDOW_OK": "true"}, MEDIA, {"status": "refused", "reason": "Buffer already has a post waiting"})
        self.assertIn("NOT queued - Buffer already has a post waiting", refused)
        self.assertIn("NOT POSTED", refused)
        disabled = run_summary.build({"AUTOPUBLISH": "off"}, MEDIA, {"status": "disabled", "draft_id": "d-1"})
        self.assertIn("DRAFT ONLY", disabled)

    def test_only_this_runs_media_is_reported(self):
        state = {"runs": [dict(MEDIA, run_id="4"), dict(MEDIA, run_id="5", scene="hard:10")]}
        self.assertEqual(run_summary.this_runs_media(state, "5")["scene"], "hard:10")
        self.assertIsNone(run_summary.this_runs_media(state, "6"))


if __name__ == "__main__":
    unittest.main()
