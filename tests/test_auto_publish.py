"""The unattended daily post: same checks as publish-approved, at most one post per Riyadh day, honest outcomes.

Offline: Buffer is a mock (requests.post), GitHub claims are local files, the media is served from disk by a fake raw
GitHub, and the media checks run in STRICT mode (the mode the workflow uses).
"""

import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

import support
import approvals
import autopilot
import test_approvals as base

CRITIC_YES = ("Hook: 8\nCuriosity: 8\nNatural tone: 8\nRingshift relevance: 8\nOriginality: 8\nViral potential: 8\n\n"
              "CLAIMS:\nNONE\n\nKEEP: YES\n")


class AutoPublishTests(base.DraftTestCase):
    def setUp(self):
        super().setUp()
        self.candidates = mock.patch.object(autopilot, "generate_candidates", return_value=[base.TEXT]).start()
        self.critic = mock.patch.object(autopilot, "critique_post", return_value=CRITIC_YES).start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"tags": ["black hole"], "description": base.DESCRIPTION}}), encoding="utf-8")
        self.days_dir = self.tmp / "data" / "used_days"
        mock.patch.object(approvals, "default_claimer", lambda *_: approvals.LocalFileClaim(self.claims_dir)).start()
        mock.patch.object(approvals, "day_claimer", lambda *_: approvals.LocalFileClaim(self.days_dir)).start()
        mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "777"}).start()
        os.environ.pop("RINGSHIFT_AUTOPUBLISH", None)
        self.draft_out = self.tmp / "preview_draft.json"
        self.result_file = self.tmp / "auto_result.json"

    def auto(self, *extra):
        argv = ["--auto-publish", "--draft-file", str(self.draft_out), "--result-file", str(self.result_file),
                "--media-file", self.image_rel, *extra]
        out, error = io.StringIO(), None
        with contextlib.redirect_stdout(out):
            try:
                autopilot.main(argv)
            except SystemExit as exc:
                error = exc
        return error, out.getvalue()

    def result(self):
        return json.loads(self.result_file.read_text(encoding="utf-8"))

    def test_a_passing_draft_is_published_once_with_its_media_and_logged_as_unattended(self):
        error, output = self.auto()
        self.assertIsNone(error, output)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.sent()["text"], base.TEXT)
        self.assertEqual(self.sent()["assets"], [{"image": {"url": support.media_selector.raw_url(self.image_rel)}}])
        log = json.loads(self.post_log_file.read_text(encoding="utf-8"))
        self.assertEqual(log[-1]["approved_by"], "autopilot (unattended daily run)")
        self.assertEqual(self.result()["status"], "queued")
        self.assertEqual(self.result()["buffer_id"], "post-1")
        self.assertIn("DAILY POST QUEUED WITH MEDIA CONFIRMED", output)
        self.assertEqual(len(list(self.days_dir.iterdir())), 1)            # the day is claimed
        self.assertEqual(len(self.claim_files()), 1)                       # and so is the draft

    def test_a_second_run_on_the_same_day_never_posts_again_even_if_the_repo_state_was_lost(self):
        self.auto()
        # The worst case: the first run's commit never reached main (failed push), so the post log, the post history
        # and the Buffer check are all blind. Only the day claim (a git tag in Actions) is left, and it is enough.
        self.post_log_file.unlink()
        self.posts_file.unlink()
        error, output = self.auto()
        self.assertEqual(error.code, 1)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.result()["status"], "refused")
        self.assertIn("already_used", self.result()["reason"])

    def test_the_day_is_claimed_before_the_draft_and_before_buffer(self):
        order = []
        real_claim = approvals.LocalFileClaim.claim

        def tracking(claimer, key, info=None):
            order.append("day" if claimer.directory == self.days_dir else "draft")
            return real_claim(claimer, key, info)

        self.post.side_effect = lambda *a, **k: order.append("buffer") or support.queued_response()
        with mock.patch.object(approvals.LocalFileClaim, "claim", tracking):
            self.auto()
        self.assertEqual(order, ["day", "draft", "buffer"])

    def test_a_check_failure_before_claiming_uses_up_nothing(self):
        self.guard.side_effect = RuntimeError("Buffer already has a post waiting")
        error, _ = self.auto()
        self.assertEqual(error.code, 1)
        self.assertEqual(self.post.call_count, 0)
        self.assertFalse(self.days_dir.exists())
        self.assertEqual(self.claim_files(), [])
        self.assertEqual(self.result()["status"], "refused")

    def test_no_caption_passing_means_no_post_and_a_failed_run(self):
        self.critic.return_value = CRITIC_YES.replace("KEEP: YES", "KEEP: NO")
        error, output = self.auto()
        self.assertEqual(error.code, 1)
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.result()["status"], "no_draft")
        self.assertIn("NOT PUBLISHED", output)

    def test_switched_off_writes_a_draft_only_and_does_not_fail(self):
        with mock.patch.dict(os.environ, {"RINGSHIFT_AUTOPUBLISH": "off"}):
            error, output = self.auto()
        self.assertIsNone(error, output)
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.result()["status"], "disabled")
        self.assertTrue(json.loads(self.draft_out.read_text(encoding="utf-8"))["draft_id"])
        self.assertIn("SWITCHED OFF", output)

    def test_a_buffer_failure_after_claiming_is_reported_as_failed(self):
        self.post.side_effect = None
        self.post.return_value = support.FakeResponse(500, text="boom")
        error, output = self.auto()
        self.assertEqual(error.code, 1)
        self.assertEqual(self.result()["status"], "failed")
        self.assertIn("check Buffer", self.result()["reason"])

    def test_both_keys_are_required_before_any_gemini_call(self):
        with mock.patch.dict(os.environ, {"BUFFER_API_KEY": ""}):
            error, _ = self.auto()
        self.assertIn("BUFFER_API_KEY", str(error.code))
        self.candidates.assert_not_called()

    def test_auto_publish_needs_a_draft_file_and_no_other_mode(self):
        for argv in (["--auto-publish"], ["--auto-publish", "--dry-run", "--draft-file", "x.json"]):
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    autopilot.main(argv)
        self.assertEqual(self.post.call_count, 0)

    def test_a_store_path_from_the_media_store_branch_can_be_pinned(self):
        self.assertEqual(autopilot.validate_pinned_media("store/rs_hard_L09_x_puzzle_20261028.jpg"),
                         "store/rs_hard_L09_x_puzzle_20261028.jpg")
        for bad in ("store/../x.jpg", "/etc/passwd", "https://x/y.jpg", "data/x.jpg"):
            with self.assertRaises(SystemExit):
                autopilot.validate_pinned_media(bad)


class PublishWindowTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.dir = Path(tempfile.mkdtemp())
        self.claims = approvals.LocalFileClaim(self.dir)
        patcher = mock.patch.object(autopilot, "POST_LOG_FILE", self.dir / "post_log.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc)

    def test_open_when_nothing_was_posted_today_and_buffer_is_empty(self):
        ok, reason = autopilot.check_publish_window(now=self.now, day_claimer=self.claims, fetch=lambda: [])
        self.assertTrue(ok, reason)

    def test_closed_when_today_already_has_a_post(self):
        self.claims.claim("2026-10-28")
        ok, reason = autopilot.check_publish_window(now=self.now, day_claimer=self.claims, fetch=lambda: [])
        self.assertFalse(ok)
        self.assertIn("already queued", reason)

    def test_closed_when_buffer_has_one_waiting(self):
        waiting = [{"node": {"id": "p", "sentAt": None, "dueAt": "2026-10-29T04:00:00Z"}}]
        ok, reason = autopilot.check_publish_window(now=self.now, day_claimer=self.claims, fetch=lambda: waiting)
        self.assertFalse(ok)
        self.assertIn("waiting", reason)

    def test_fails_closed_when_buffer_cannot_be_read(self):
        def broken():
            raise RuntimeError("timeout")
        ok, reason = autopilot.check_publish_window(now=self.now, day_claimer=self.claims, fetch=broken)
        self.assertFalse(ok)

    def test_the_day_is_the_riyadh_day(self):
        self.assertEqual(autopilot.local_day(datetime(2026, 10, 27, 21, 30, tzinfo=timezone.utc)), "2026-10-28")
        self.assertEqual(autopilot.local_day(datetime(2026, 10, 27, 20, 59, tzinfo=timezone.utc)), "2026-10-27")

    def test_the_kill_switch_values(self):
        for value, enabled in (("", True), ("on", True), ("OFF", False), ("draft-only", False), ("false", False), ("0", False)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"RINGSHIFT_AUTOPUBLISH": value}):
                self.assertEqual(autopilot.autopublish_enabled(), enabled)


class DayClaimerTests(unittest.TestCase):
    def test_in_actions_the_day_claim_is_a_separate_tag_namespace_that_is_never_a_rehearsal(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t", "GITHUB_SHA": "s"}):
            claimer = approvals.day_claimer("data")
        self.assertEqual(claimer.prefix, "ringshift-day-")
        self.assertFalse(claimer.rehearsal)
        with self.assertRaises(RuntimeError):
            claimer.release("2026-10-28")


if __name__ == "__main__":
    unittest.main()
