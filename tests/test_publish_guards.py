"""Guards that make a second or half-recorded post impossible, and the draft/preview output."""

import contextlib
import io
import json
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import support
from support import FakeResponse, OfflineTestCase, queued_response
import autopilot

NOW = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
CRITIC_YES = (
    "Hook: 8\nCuriosity: 8\nNatural tone: 8\nRingshift relevance: 8\n"
    "Originality: 8\nViral potential: 8\n\nCLAIMS:\nNONE\n\nKEEP: YES\n"
)
ROOT = Path(__file__).resolve().parent.parent
REAL_GUARD = autopilot.ensure_safe_to_publish      # captured at import, before support's per-test no-op patch


def edge(post_id="p1", due=None, sent=None):
    return {"node": {"id": post_id, "dueAt": due, "sentAt": sent}}


def iso(delta_hours):
    return (NOW + timedelta(hours=delta_hours)).isoformat().replace("+00:00", "Z")


class EnsureSafeToPublishTests(OfflineTestCase):
    def check(self, edges=None, **kw):
        fetch = kw.pop("fetch", None) or (lambda: edges or [])
        return REAL_GUARD(now=NOW, fetch=fetch, allow_extra=kw.pop("allow_extra", False))

    def test_nothing_waiting_is_allowed(self):
        self.check([edge("old", due=iso(-30), sent=iso(-30))])

    def test_a_post_waiting_in_buffer_blocks(self):
        with self.assertRaisesRegex(RuntimeError, "already has a post waiting"):
            self.check([edge("w", due=iso(+18))])

    def test_sent_and_long_overdue_posts_do_not_block(self):
        self.check([edge("sent", due=iso(-2), sent=iso(-2)), edge("stale", due=iso(-48))])

    def test_recent_post_in_our_log_blocks_even_if_buffer_looks_empty(self):
        autopilot.POST_LOG_FILE.write_text(json.dumps([{"queued_at": iso(-3)}]), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "already queued"):
            self.check([])

    def test_old_log_entry_does_not_block(self):
        autopilot.POST_LOG_FILE.write_text(json.dumps([{"queued_at": iso(-20)}]), encoding="utf-8")
        self.check([])

    def test_unreachable_buffer_fails_closed(self):
        def boom():
            raise ConnectionError("down")
        with self.assertRaisesRegex(RuntimeError, "Could not check Buffer"):
            self.check(fetch=boom)

    def test_missing_key_exit_also_fails_closed(self):
        def missing():
            raise SystemExit("Configuration error")
        with self.assertRaisesRegex(RuntimeError, "Could not check Buffer"):
            self.check(fetch=missing)

    def test_override_is_explicit_and_skips_the_checks(self):
        fetch = mock.Mock()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.check(fetch=fetch, allow_extra=True)
        fetch.assert_not_called()
        self.assertIn("OVERRIDDEN", out.getvalue())

    def test_environment_variable_is_the_override(self):
        fetch = mock.Mock(return_value=[edge("w", due=iso(+5))])
        with mock.patch.dict(os.environ, {"RINGSHIFT_ALLOW_EXTRA_POST": "1"}), contextlib.redirect_stdout(io.StringIO()):
            REAL_GUARD(now=NOW, fetch=fetch)
        with mock.patch.dict(os.environ, {"RINGSHIFT_ALLOW_EXTRA_POST": "0"}), self.assertRaises(RuntimeError):
            REAL_GUARD(now=NOW, fetch=fetch)


class StateSafetyTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.post.side_effect = None
        self.post.return_value = queued_response()

    def send(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return autopilot.send_to_buffer("A fresh post about prisms.")

    def test_corrupt_post_log_stops_everything_before_buffer_is_called(self):
        self.post_log_file.write_text("{not json", encoding="utf-8")
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
            self.send()
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.post_log_file.read_text(encoding="utf-8"), "{not json")   # not overwritten

    def test_log_that_is_not_a_list_is_rejected(self):
        self.post_log_file.write_text('{"a": 1}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "JSON list"):
            self.send()
        self.assertEqual(self.post.call_count, 0)

    def test_unwritable_data_directory_stops_before_posting(self):
        with mock.patch.object(os, "access", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "cannot be recorded"):
                self.send()
        self.assertEqual(self.post.call_count, 0)

    def test_all_three_state_files_are_updated_after_one_success(self):
        self.send()
        self.assertEqual(json.loads(self.counter_file.read_text(encoding="utf-8")), {"successful_posts": 4})
        self.assertEqual(len(json.loads(self.post_log_file.read_text(encoding="utf-8"))), 1)
        self.assertEqual(len(json.loads(self.history_file.read_text(encoding="utf-8"))), 1)
        leftovers = [p.name for p in (self.tmp / "data").iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_atomic_write_keeps_the_old_file_if_the_write_fails(self):
        target = self.tmp / "data" / "x.json"
        target.write_text("old", encoding="utf-8")
        with mock.patch.object(os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                autopilot.atomic_write(target, "new")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertEqual([p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_a_rerun_with_the_same_text_never_posts_again(self):
        self.send()
        with self.assertRaisesRegex(RuntimeError, "already in data/post_log.json"):
            self.send()
        self.assertEqual(self.post.call_count, 1)


class MainGuardTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.candidates = mock.patch.object(autopilot, "generate_candidates",
                                            return_value=["A Portal in a purple level."]).start()
        mock.patch.object(autopilot, "critique_post", return_value=CRITIC_YES).start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.post.side_effect = None
        self.post.return_value = queued_response()

    def run_main(self, argv=None):
        out = io.StringIO()
        error = None
        with contextlib.redirect_stdout(out):
            try:
                autopilot.main(argv)
            except SystemExit as exc:
                error = exc
        return error, out.getvalue()

    def test_a_draft_run_never_calls_the_guard_or_buffer(self):
        error, output = self.run_main(["--dry-run"])
        self.assertIsNone(error)
        self.guard.assert_not_called()
        self.assertEqual(self.post.call_count, 0)

    def test_a_run_without_a_mode_never_calls_the_guard_or_buffer(self):
        error, _ = self.run_main()
        self.assertIsNotNone(error)
        self.guard.assert_not_called()
        self.assertEqual(self.post.call_count, 0)
        self.assertFalse(self.post_log_file.exists())

    def test_the_draft_describes_media_as_assets_not_text(self):
        self.tags_file.write_text(json.dumps({"media/gameplay/black_hole.png": {"tags": ["black hole"], "description": "A Portal in a purple level."}}),
                                  encoding="utf-8")
        path = self.tmp / "draft.json"
        self.run_main(["--dry-run", "--draft-file", str(path)])
        draft = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("http", draft["text"])
        self.assertEqual(draft["buffer_assets_payload"], [{"image": {"url": draft["media"]["url"]}}])
        self.assertTrue(draft["media"]["url"].startswith("https://raw.githubusercontent.com/"))
        self.assertEqual(draft["target"]["buffer_channel_id"], autopilot.BUFFER_CHANNEL_ID)
        self.assertEqual(self.post.call_count, 0)


class DraftFileTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        mock.patch.object(autopilot, "generate_candidates", return_value=["A Portal in a purple level."]).start()
        mock.patch.object(autopilot, "critique_post", return_value=CRITIC_YES).start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.tags_file.write_text(json.dumps({"media/gameplay/black_hole.png": {
            "tags": ["black hole"], "description": "A portal level."}}), encoding="utf-8")
        self.draft = self.tmp / "draft.json"

    def dry_run(self, **env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            autopilot.main(["--dry-run", "--draft-file", str(self.draft)])
        return out.getvalue()

    def strict(self):
        """What a real Actions run does: strict media checks against (fake) raw GitHub bytes and the checkout's manifest."""
        mock.patch.object(autopilot, "MEDIA_CHECK_MODE", "strict").start()
        mock.patch.object(autopilot.media_tools, "have_ffmpeg", return_value=True).start()
        mock.patch.object(autopilot.media_tools, "validate_media", return_value=[]).start()
        support.serve_media(self, self.tmp)

    def test_a_process_inside_github_actions_refuses_fixture_mode(self):
        with self.assertRaises(SystemExit) as raised:
            self.dry_run(GITHUB_ACTIONS="true")
        self.assertIn("test-fixture media mode", str(raised.exception))
        self.assertFalse(self.draft.exists())

    def test_draft_contains_everything_needed_for_approval(self):
        self.dry_run()
        draft = json.loads(self.draft.read_text(encoding="utf-8"))
        self.assertIs(draft["sent"], False)
        self.assertEqual(draft["text"], "A Portal in a purple level.")
        self.assertEqual(draft["target"]["buffer_channel_id"], autopilot.BUFFER_CHANNEL_ID)
        self.assertEqual(draft["media"]["file"], "media/gameplay/black_hole.png")
        self.assertTrue(draft["media"]["url"].startswith("https://raw.githubusercontent.com/"))
        self.assertEqual(draft["buffer_assets_payload"], [{"image": {"url": draft["media"]["url"]}}])
        self.assertFalse(draft["text_contains_media_url"])
        # Fixture mode (tests only): the draft says so and approvals refuse it (see test_media_gate).
        self.assertTrue(draft["media"]["url_check"].startswith("FIXTURE"))
        self.assertEqual(draft["media"]["check"]["status"], "fixture")

    def test_draft_never_contains_a_secret(self):
        self.dry_run()
        text = self.draft.read_text(encoding="utf-8")
        self.assertNotIn(support.BUFFER_KEY, text)
        self.assertNotIn(support.GEMINI_KEY, text)

    def test_dry_run_with_a_draft_file_still_sends_and_saves_nothing(self):
        before = self.state()
        self.dry_run()
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.post_log_file.exists())

    def test_annotation_is_printed_only_inside_github_actions(self):
        self.assertNotIn("::notice", self.dry_run(GITHUB_ACTIONS="false"))
        self.strict()
        output = self.dry_run(GITHUB_ACTIONS="true")
        line = next(l for l in output.splitlines() if l.startswith("::notice"))
        self.assertNotIn("\n", line)
        self.assertIn("black_hole.png", line)
        self.assertNotIn(support.BUFFER_KEY, line)

    def test_a_preview_with_no_passing_candidate_still_explains_itself(self):
        autopilot.generate_candidates.return_value = ["Over 5,000 players love this puzzle!", "A Portal in a purple level."]
        autopilot.critique_post.return_value = CRITIC_YES.replace("KEEP: YES", "KEEP: NO")
        self.dry_run()
        report = json.loads(self.draft.read_text(encoding="utf-8"))
        self.assertEqual(report["result"], "no_candidate_passed")
        self.assertIs(report["sent"], False)
        first_round = [a["outcome"] for a in report["attempts"] if a.get("round") == 1]
        self.assertEqual(first_round, ["rejected before the critic", "critic rejected"])
        self.assertIn("failed post rules", report["attempts"][0]["reason"])
        self.assertTrue(any(a.get("round") == 2 for a in report["attempts"]), "a second round was tried")
        self.assertIn("Nothing was published", report["diagnosis"]["summary"])
        self.assertEqual(report["media"]["file"], "media/gameplay/black_hole.png")

    def test_winning_draft_lists_every_attempt(self):
        self.dry_run()
        draft = json.loads(self.draft.read_text(encoding="utf-8"))
        self.assertEqual(draft["result"], "winner")
        self.assertEqual(len(draft["attempts"]), 1)

    def test_the_annotation_stays_far_below_githubs_size_limit_and_names_the_draft(self):
        self.strict()
        autopilot.generate_candidates.return_value = ["Over 5,000 players love this puzzle!"] * 3
        autopilot.critique_post.return_value = CRITIC_YES.replace("KEEP: YES", "KEEP: NO") + "x" * 6000
        output = self.dry_run(GITHUB_ACTIONS="true")
        line = next(l for l in output.splitlines() if l.startswith("::notice"))
        self.assertLess(len(line), 2500)
        winner_output = None
        autopilot.generate_candidates.return_value = ["A Portal in a purple level."]
        autopilot.critique_post.return_value = CRITIC_YES
        winner_output = self.dry_run(GITHUB_ACTIONS="true")
        winner = next(l for l in winner_output.splitlines() if l.startswith("::notice"))
        draft = json.loads(self.draft.read_text(encoding="utf-8"))
        self.assertIn(draft["draft_id"], winner)
        self.assertIn(draft["media"]["url"], winner)
        self.assertIn(draft["target"]["buffer_channel_id"], winner)
        self.assertLess(len(winner), 2500)

    def test_a_failing_url_check_means_no_draft_and_no_approval_code(self):
        # Strict mode with media that GitHub does not serve (404): the preview must not produce an approval code.
        mock.patch.object(autopilot, "MEDIA_CHECK_MODE", "strict").start()
        support.serve_media(self, self.tmp, missing={"media/gameplay/black_hole.png"})
        output = self.dry_run()
        report = json.loads(self.draft.read_text(encoding="utf-8"))
        self.assertEqual(report["result"], "no_candidate_passed")
        self.assertNotIn("draft_id", report)
        self.assertNotIn("DRAFT ID", output)
        self.assertIn("HTTP 404", json.dumps(report["post_types_skipped"]))


if __name__ == "__main__":
    unittest.main()
