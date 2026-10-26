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
                                            return_value=["Which ring would you turn first?"]).start()
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

    def test_blocked_guard_means_nothing_is_saved_or_sent(self):
        self.guard.side_effect = RuntimeError("Buffer already has a post waiting")
        before = self.state()
        error, output = self.run_main()
        self.assertEqual(error.code, 1)
        self.assertIn("NOT PUBLISHING", output)
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.posts_file.exists())
        self.assertFalse(self.post_log_file.exists())

    def test_guard_runs_exactly_once_and_one_post_is_created(self):
        error, _ = self.run_main()
        self.assertIsNone(error)
        self.assertEqual(self.guard.call_count, 1)
        self.assertEqual(self.post.call_count, 1)

    def test_dry_run_never_calls_the_guard_or_buffer(self):
        self.run_main(["--dry-run"])
        self.guard.assert_not_called()
        self.assertEqual(self.post.call_count, 0)

    def test_media_travels_in_assets_not_in_the_text(self):
        self.tags_file.write_text(json.dumps({"media/gameplay/black_hole.png": {"tags": ["black hole"], "description": "d"}}),
                                  encoding="utf-8")
        self.run_main()
        sent = self.post.call_args.kwargs["json"]["variables"]["input"]
        self.assertNotIn("raw.githubusercontent.com", sent["text"])
        self.assertNotIn("http", sent["text"])
        self.assertEqual(len(sent["assets"]), 1)
        self.assertEqual(list(sent["assets"][0]), ["image"])         # typed asset item, not the deprecated shape
        self.assertTrue(sent["assets"][0]["image"]["url"].startswith("https://raw.githubusercontent.com/"))
        self.assertEqual(sent["channelId"], autopilot.BUFFER_CHANNEL_ID)
        self.assertEqual(sent["mode"], "addToQueue")

    def test_buffer_is_contacted_with_one_createpost_only(self):
        self.run_main()
        self.assertEqual(self.post.call_count, 1)
        self.assertIn("createPost", self.post.call_args.kwargs["json"]["query"])
        self.assertEqual(self.post.call_args.kwargs["json"]["query"].count("mutation"), 1)


class DraftFileTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        mock.patch.object(autopilot, "generate_candidates", return_value=["Which ring would you turn first?"]).start()
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

    def test_draft_contains_everything_needed_for_approval(self):
        self.dry_run()
        draft = json.loads(self.draft.read_text(encoding="utf-8"))
        self.assertIs(draft["sent"], False)
        self.assertEqual(draft["text"], "Which ring would you turn first?")
        self.assertEqual(draft["target"]["buffer_channel_id"], autopilot.BUFFER_CHANNEL_ID)
        self.assertEqual(draft["media"]["file"], "media/gameplay/black_hole.png")
        self.assertTrue(draft["media"]["url"].startswith("https://raw.githubusercontent.com/"))
        self.assertEqual(draft["buffer_assets_payload"], [{"image": {"url": draft["media"]["url"]}}])
        self.assertFalse(draft["text_contains_media_url"])
        self.assertTrue(draft["media"]["url_check"].startswith("skipped"))      # tests disable the network check

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
        output = self.dry_run(GITHUB_ACTIONS="true")
        line = next(l for l in output.splitlines() if l.startswith("::notice"))
        self.assertNotIn("\n", line)
        self.assertIn("black_hole.png", line)
        self.assertNotIn(support.BUFFER_KEY, line)

    def test_a_preview_with_no_passing_candidate_still_explains_itself(self):
        autopilot.generate_candidates.return_value = ["Over 5,000 players love this puzzle!", "Which ring would you turn first?"]
        autopilot.critique_post.return_value = CRITIC_YES.replace("KEEP: YES", "KEEP: NO")
        self.dry_run()
        report = json.loads(self.draft.read_text(encoding="utf-8"))
        self.assertEqual(report["result"], "no_candidate_passed")
        self.assertIs(report["sent"], False)
        outcomes = [a["outcome"] for a in report["attempts"]]
        self.assertEqual(outcomes, ["rejected before the critic", "critic rejected"])
        self.assertIn("failed post rules", report["attempts"][0]["reason"])
        self.assertEqual(report["media"]["file"], "media/gameplay/black_hole.png")

    def test_winning_draft_lists_every_attempt(self):
        self.dry_run()
        draft = json.loads(self.draft.read_text(encoding="utf-8"))
        self.assertEqual(draft["result"], "winner")
        self.assertEqual(len(draft["attempts"]), 1)

    def test_the_annotation_stays_far_below_githubs_size_limit_and_names_the_draft(self):
        autopilot.generate_candidates.return_value = ["Over 5,000 players love this puzzle!"] * 3
        autopilot.critique_post.return_value = CRITIC_YES.replace("KEEP: YES", "KEEP: NO") + "x" * 6000
        output = self.dry_run(GITHUB_ACTIONS="true")
        line = next(l for l in output.splitlines() if l.startswith("::notice"))
        self.assertLess(len(line), 2500)
        winner_output = None
        autopilot.generate_candidates.return_value = ["Which ring would you turn first?"]
        autopilot.critique_post.return_value = CRITIC_YES
        winner_output = self.dry_run(GITHUB_ACTIONS="true")
        winner = next(l for l in winner_output.splitlines() if l.startswith("::notice"))
        draft = json.loads(self.draft.read_text(encoding="utf-8"))
        self.assertIn(draft["draft_id"], winner)
        self.assertIn(draft["media"]["url"], winner)
        self.assertIn(draft["target"]["buffer_channel_id"], winner)
        self.assertLess(len(winner), 2500)

    def test_url_check_failure_is_reported_in_the_draft_not_hidden(self):
        mock.patch.dict(os.environ, {"RINGSHIFT_VERIFY_MEDIA": "1"}).start()
        resp = mock.Mock(status_code=404, headers={"content-type": "text/plain"})
        self.head.side_effect = None
        self.head.return_value = resp
        self.dry_run(RINGSHIFT_VERIFY_MEDIA="1")
        draft = json.loads(self.draft.read_text(encoding="utf-8"))
        self.assertTrue(draft["media"]["url_check"].startswith("FAILED"))


class WorkflowContractTests(unittest.TestCase):
    """Text-level checks of the workflow file (no YAML library needed)."""

    text = (ROOT / ".github" / "workflows" / "autopilot.yml").read_text(encoding="utf-8")
    steps = re.split(r"(?m)^      - ", text)[1:]

    def step(self, name):
        return next(s for s in self.steps if s.splitlines()[0] in (f"name: {name}", f"uses: {name}") or f"name: {name}" in s.splitlines()[0])

    def test_manual_modes_cannot_generate_and_post_by_themselves(self):
        self.assertRegex(self.text, r"options:\s+- test\s+- preview\s+- publish-approved\s+default: test")
        self.assertNotIn("inputs.mode == 'live'", self.text)

    def test_the_schedule_is_unchanged(self):
        self.assertIn('- cron: "0 6 * * *"', self.text)

    def test_runs_never_overlap_and_are_never_cancelled(self):
        self.assertRegex(self.text, r"concurrency:\s+group: ringshift-autopilot\s+cancel-in-progress: false")

    def test_exactly_two_steps_can_publish_and_only_the_schedule_generates(self):
        publishing = [s for s in self.steps if re.search(r"scripts/autopilot\.py(?!\s+--dry-run)", s) and "python" in s]
        self.assertEqual(len(publishing), 2)
        scheduled = self.step("Run Ringshift autopilot")
        self.assertIn("if: github.event_name == 'schedule'\n", scheduled)
        self.assertNotIn("inputs", scheduled)
        approved = self.step("Publish the approved draft")
        self.assertIn("--publish-approved approved/preview_draft.json", approved)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.mode == 'publish-approved'", approved)

    def test_buffer_key_is_only_in_the_analytics_and_the_two_publishing_steps(self):
        holders = sorted(s.splitlines()[0] for s in self.steps if "BUFFER_API_KEY" in s)
        self.assertEqual(holders, ["name: Publish the approved draft", "name: Run Ringshift autopilot",
                                   "name: Update X analytics"])

    def test_the_approved_publish_step_has_no_gemini_key_and_no_inline_inputs(self):
        approved = self.step("Publish the approved draft")
        self.assertNotIn("GEMINI_API_KEY", approved)
        run_block = approved.split("run:", 1)[1]
        self.assertNotIn("${{", run_block)                    # inputs reach the shell only through env vars
        self.assertIn('--approval-code "$APPROVAL_CODE"', run_block)

    def test_preview_step_has_no_buffer_key_and_is_dry_run(self):
        preview = self.step("Preview a post (no publishing)")
        self.assertNotIn("BUFFER_API_KEY", preview)
        self.assertIn("--dry-run", preview)
        self.assertIn("inputs.mode == 'preview'", preview)

    def test_the_draft_is_saved_as_an_artifact_named_after_its_id(self):
        self.assertIn("name: ringshift-draft-${{ steps.draft.outputs.draft_id }}", self.text)
        self.assertIn("path: preview_draft.json", self.text)

    def test_publishing_must_be_dispatched_on_main_before_anything_else_runs(self):
        guard = self.step("Publishing only runs from main")
        self.assertIn("inputs.mode == 'publish-approved' && github.ref != 'refs/heads/main'", guard)
        self.assertIn("exit 1", guard)
        for later in ("Check the approval code", "Find the approved draft", "Update X analytics"):
            self.assertLess(self.text.index("Publishing only runs from main"), self.text.index(later))

    def test_a_malformed_approval_code_is_refused_before_any_download_or_key(self):
        check = self.step("Check the approval code")
        self.assertIn("^d-[0-9a-f]{8}-[0-9a-f]{4}$", check)
        self.assertLess(self.text.index("Check the approval code"), self.text.index("Find the approved draft"))
        self.assertLess(self.text.index("Find the approved draft"), self.text.index("Download the approved draft"))
        self.assertLess(self.text.index("Download the approved draft"), self.text.index("Publish the approved draft"))

    def test_exactly_one_unexpired_draft_must_match_the_code(self):
        find = self.step("Find the approved draft")
        self.assertIn(".expired == false", find)
        self.assertIn('[ "$count" != "1" ]', find)
        self.assertIn("name: ringshift-draft-${{ inputs.approval_code }}", self.step("Download the approved draft"))
        self.assertIn("run-id: ${{ steps.find.outputs.run_id }}", self.text)

    def test_least_privilege_permissions(self):
        self.assertRegex(self.text, r"(?m)^permissions:\s+contents: read")
        self.assertRegex(self.text, r"permissions:\s+contents: write[^\n]*\n\s+actions: read")

    def test_publishing_waits_for_the_tests_job(self):
        self.assertRegex(self.text, r"autopilot:\s+needs: tests")

    def test_state_is_saved_as_an_artifact_even_if_the_push_fails(self):
        self.assertLess(self.text.index("Keep a copy of the state files"), self.text.index("Save generated content"))
        self.assertIn("if: always()", self.step("Keep a copy of the state files"))

    def test_no_workflow_can_be_triggered_by_the_commit_it_makes(self):
        self.assertNotRegex(self.text, r"(?m)^  push:")
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("- main", ci)                       # push trigger excludes main
        self.assertNotIn("secrets.", ci)


if __name__ == "__main__":
    unittest.main()
