"""Verification without publishing, and the single-use claim mechanism, against an in-memory fake of GitHub's ref API.

Everything here is OFFLINE. The fake behaves the way GitHub's documented REST API does (201 / 422 'Reference already
exists' / 200 / 404 / 204); whether the real GitHub does the same is shown only by a `rehearse-approval` Actions run.
"""

import contextlib
import io
import json
import os
import shutil
import unittest
from datetime import timedelta
from unittest import mock

import support
from support import FakeResponse
import approvals
import autopilot
import test_approvals as base


class FakeGitHubRefs:
    """Just enough of /git/refs to exercise create / read / delete of tags, and to record every ref touched."""

    def __init__(self):
        self.refs = set()
        self.touched = []
        self.fail = {}               # method name -> FakeResponse (or Exception) to return instead

    def _maybe_fail(self, kind):
        outcome = self.fail.get(kind)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def post(self, url, headers=None, json=None, timeout=None):
        forced = self._maybe_fail("post")
        if forced:
            return forced
        ref = json["ref"]
        self.touched.append(ref)
        if ref in self.refs:
            return FakeResponse(422, text='{"message":"Reference already exists"}')
        self.refs.add(ref)
        return FakeResponse(201, {})

    def get(self, url, headers=None, timeout=None):
        forced = self._maybe_fail("get")
        if forced:
            return forced
        ref = "refs/tags/" + url.split("/git/ref/tags/", 1)[1]
        self.touched.append(ref)
        return FakeResponse(200, {}) if ref in self.refs else FakeResponse(404, text='{"message":"Not Found"}')

    def delete(self, url, headers=None, timeout=None):
        forced = self._maybe_fail("delete")
        if forced:
            return forced
        ref = "refs/tags/" + url.split("/git/refs/tags/", 1)[1]
        self.touched.append(ref)
        if ref in self.refs:
            self.refs.remove(ref)
            return FakeResponse(204, text="")
        return FakeResponse(404, text="")

    def claimer(self):
        return approvals.GitHubRefClaim("owner/repo", "ghs_SECRET", "sha1", post=self.post, get=self.get, delete=self.delete)


class VerifyOnlyTests(base.DraftTestCase):
    def verify(self, draft=None, code=None, run_id="777", claimer=None):
        draft = draft or json.loads(self.draft_file.read_text(encoding="utf-8"))
        argv = ["--verify-approved", str(self.draft_file), "--approval-code", draft["draft_id"] if code is None else code]
        if run_id:
            argv += ["--expected-run-id", run_id]
        out, error = io.StringIO(), None
        with contextlib.redirect_stdout(out), mock.patch.object(approvals, "default_claimer", lambda *_: claimer or approvals.LocalFileClaim(self.claims_dir)):
            try:
                autopilot.main(argv)
            except SystemExit as exc:
                error = exc
        return error, out.getvalue()

    def assert_untouched(self):
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.claim_files(), [])
        self.assertFalse(self.post_log_file.exists())
        self.assertFalse(self.posts_file.exists())
        self.assertEqual(json.loads(self.counter_file.read_text(encoding="utf-8")), {"successful_posts": 3})

    def test_a_good_draft_verifies_and_nothing_is_claimed_or_sent(self):
        draft = self.store(self.make())
        error, output = self.verify()
        self.assertIsNone(error, output)
        self.assertIn(f"VERIFIED draft {draft['draft_id']}", output)
        self.assertIn("nothing was claimed or sent", output)
        self.assert_untouched()
        self.guard.assert_not_called()

    def test_verification_needs_neither_the_buffer_key_nor_the_gemini_key(self):
        self.store(self.make())
        with mock.patch.dict(os.environ, {"BUFFER_API_KEY": "", "GEMINI_API_KEY": ""}):
            error, output = self.verify()
        self.assertIsNone(error, output)
        self.assertNotIn(support.BUFFER_KEY, output)

    def test_verifying_does_not_use_the_draft_up(self):
        draft = self.store(self.make())
        for _ in range(3):
            self.assertIsNone(self.verify()[0])
        self.assertIsNone(self.publish()[0])                          # still publishable afterwards, exactly once
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.sent()["text"], draft["text"])

    def test_every_kind_of_bad_approval_is_refused(self):
        cases = {
            "wrong_code": lambda: dict(code="d-00000000-0000"),
            "wrong_run": lambda: dict(run_id="999"),
        }
        for reason, kwargs in cases.items():
            with self.subTest(reason=reason):
                self.store(self.make())
                error, output = self.verify(**kwargs())
                self.assertEqual(error.code, 1)
                self.assertIn("NOT VERIFIED", output)
                self.assertIn(reason, output)
        self.assert_untouched()

    def test_tampered_expired_and_media_changed_drafts_are_refused(self):
        original = self.make()
        edited = json.loads(json.dumps(original))
        edited["text"] = "Different words entirely."
        self.store(edited)
        self.assertIn("tampered", self.verify(draft=original)[1])

        self.store(self.make(now=base.now_utc() - timedelta(days=3)))
        self.assertIn("expired", self.verify()[1])

        self.store(self.make())
        (self.tmp / self.image_rel).write_bytes(b"changed")
        self.assertIn("media_changed", self.verify()[1])
        self.assert_untouched()

    def test_a_used_draft_is_refused_by_either_record(self):
        draft = self.store(self.make())
        self.claims_dir.mkdir(parents=True)
        (self.claims_dir / f"{draft['draft_id']}.json").write_text("{}", encoding="utf-8")
        error, output = self.verify()
        self.assertEqual(error.code, 1)
        self.assertIn("already_used", output)

        (self.claims_dir / f"{draft['draft_id']}.json").unlink()
        self.post_log_file.write_text(json.dumps([{"draft_id": draft["draft_id"], "text": "x"}]), encoding="utf-8")
        error, output = self.verify()
        self.assertEqual(error.code, 1)
        self.assertIn("already published", output)

    def test_a_missing_draft_file_is_refused(self):
        draft = self.make()
        error, output = self.verify(draft=draft)
        self.assertEqual(error.code, 1)
        self.assertIn("missing", output)

    def test_a_claim_tag_that_exists_on_github_is_seen_as_used(self):
        draft = self.store(self.make())
        github = FakeGitHubRefs()
        github.refs.add(f"refs/tags/{approvals.CLAIM_TAG_PREFIX}{draft['draft_id']}")
        error, output = self.verify(claimer=github.claimer())
        self.assertEqual(error.code, 1)
        self.assertIn("already_used", output)

    def test_if_github_cannot_say_whether_it_was_used_verification_fails_closed(self):
        self.store(self.make())
        github = FakeGitHubRefs()
        for outcome in (FakeResponse(403, text="no"), FakeResponse(500, text="x"), ConnectionError("down")):
            github.fail["get"] = outcome
            with self.subTest(outcome=repr(outcome)[:30]):
                error, output = self.verify(claimer=github.claimer())
                self.assertEqual(error.code, 1)
                self.assertNotIn("ghs_SECRET", output)


class ArtifactRoundTripTests(base.DraftTestCase):
    """The draft file the workflow uploads is the file a later run downloads: generate -> copy bytes -> verify -> publish."""

    def generate(self):
        mock.patch.object(autopilot, "generate_candidates", return_value=[base.TEXT]).start()
        mock.patch.object(autopilot, "critique_post", return_value=(
            "Hook: 8\nCuriosity: 8\nNatural tone: 8\nRingshift relevance: 8\nOriginality: 8\nViral potential: 8\n\n"
            "CLAIMS:\nNONE\n\nKEEP: YES\n")).start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"tags": ["black hole"], "description": base.DESCRIPTION}}), encoding="utf-8")
        produced = self.tmp / "preview_draft.json"
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "777"}):
            autopilot.main(["--dry-run", "--draft-file", str(produced)])
        return produced

    def test_the_generated_file_survives_a_copy_and_verifies_and_publishes_exactly_once(self):
        produced = self.generate()
        shutil.copyfile(produced, self.draft_file)                           # what upload-artifact + download-artifact do
        draft = json.loads(self.draft_file.read_text(encoding="utf-8"))
        self.assertEqual(self.post.call_count, 0)

        error, output = self.publish(run_id="777")
        self.assertIsNone(error, output)
        self.assertEqual(self.sent()["text"], base.TEXT)
        self.assertEqual(self.sent()["assets"], [{"image": {"url": draft["media"]["url"]}}])
        error, output = self.publish(run_id="777")
        self.assertEqual(error.code, 1)
        self.assertEqual(self.post.call_count, 1)

    def test_one_changed_character_in_the_downloaded_file_is_refused(self):
        produced = self.generate()
        raw = produced.read_text(encoding="utf-8")
        self.draft_file.write_text(raw.replace(base.TEXT, base.TEXT.replace("first", "last"), 1), encoding="utf-8")
        draft_id = json.loads(raw)["draft_id"]
        error, output = self.publish(draft={"draft_id": draft_id}, run_id="777")
        self.assertEqual(error.code, 1)
        self.assertIn("tampered", output)
        self.assertEqual(self.post.call_count, 0)

    def test_a_file_from_a_different_run_than_the_one_found_is_refused(self):
        produced = self.generate()
        shutil.copyfile(produced, self.draft_file)
        error, output = self.publish(run_id="778")
        self.assertEqual(error.code, 1)
        self.assertIn("wrong_run", output)
        self.assertEqual(self.post.call_count, 0)


class GitHubClaimTests(base.DraftTestCase):
    def test_is_used_reads_the_tag(self):
        github = FakeGitHubRefs()
        claimer = github.claimer()
        self.assertFalse(claimer.is_used("d-aaaaaaaa-1111"))
        claimer.claim("d-aaaaaaaa-1111")
        self.assertTrue(claimer.is_used("d-aaaaaaaa-1111"))
        self.assertFalse(claimer.is_used("d-aaaaaaaa-2222"))

    def test_is_used_fails_closed_and_never_leaks_the_token(self):
        github = FakeGitHubRefs()
        for outcome in (FakeResponse(403, text="no"), FakeResponse(500, text="x"), ConnectionError("down")):
            github.fail["get"] = outcome
            with self.subTest(outcome=repr(outcome)[:30]), self.assertRaises(approvals.ClaimUnavailableError) as ctx:
                github.claimer().is_used("d-aaaaaaaa-1111")
            self.assertNotIn("ghs_SECRET", str(ctx.exception))

    def test_a_real_claim_can_never_be_released_by_code(self):
        github = FakeGitHubRefs()
        real = github.claimer()
        real.claim("d-aaaaaaaa-1111")
        with self.assertRaises(RuntimeError):
            real.release("d-aaaaaaaa-1111")
        self.assertTrue(real.is_used("d-aaaaaaaa-1111"))
        local = approvals.LocalFileClaim(self.claims_dir)
        local.claim("d-aaaaaaaa-2222")
        with self.assertRaises(RuntimeError):
            local.release("d-aaaaaaaa-2222")
        self.assertTrue(local.is_used("d-aaaaaaaa-2222"))

    def test_two_runs_racing_for_one_draft_publish_it_once(self):
        """Both runs pass the 'is it used?' check; only the atomic tag creation decides, and the loser sends nothing."""
        draft = self.store(self.make())
        github = FakeGitHubRefs()

        def racing_claimer():
            claimer = github.claimer()
            claimer.is_used = lambda draft_id: False                  # the check happens before either run claims
            return claimer

        with contextlib.redirect_stdout(io.StringIO()):
            autopilot.publish_approved(str(self.draft_file), draft["draft_id"], "777", claimer=racing_claimer())

            # The second run has lost the post-log record (a different runner), so only the tag can stop it.
            self.post_log_file.unlink()
            with self.assertRaises(approvals.AlreadyUsedError):
                autopilot.publish_approved(str(self.draft_file), draft["draft_id"], "777", claimer=racing_claimer())
        self.assertEqual(self.post.call_count, 1)

    def test_publish_uses_the_github_claim_before_buffer_and_keeps_it_after_a_failure(self):
        draft = self.store(self.make())
        github = FakeGitHubRefs()
        self.post.return_value = FakeResponse(500, text="boom")
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            autopilot.publish_approved(str(self.draft_file), draft["draft_id"], "777", claimer=github.claimer())
        self.assertEqual(github.refs, {f"refs/tags/{approvals.CLAIM_TAG_PREFIX}{draft['draft_id']}"})
        with self.assertRaises(approvals.AlreadyUsedError):
            autopilot.publish_approved(str(self.draft_file), draft["draft_id"], "777", claimer=github.claimer())
        self.assertEqual(self.post.call_count, 1)


class ClaimRehearsalTests(base.DraftTestCase):
    def test_rehearsal_creates_repeats_and_removes_a_throwaway_tag(self):
        github = FakeGitHubRefs()
        steps = approvals.rehearse_claims(github.claimer())
        self.assertEqual(github.refs, set())
        self.assertEqual([name for name, _ in steps],
                         ["fresh ID reads as unused", "first claim", "claim reads back as used",
                          "second claim of the same ID", "throwaway claim removed"])
        self.assertIn("refused", dict(steps)["second claim of the same ID"])

    def test_rehearsal_never_touches_the_real_claim_namespace(self):
        github = FakeGitHubRefs()
        github.refs.add(f"refs/tags/{approvals.CLAIM_TAG_PREFIX}d-aaaaaaaa-1111")
        approvals.rehearse_claims(github.claimer())
        self.assertTrue(github.touched)
        for ref in github.touched:
            self.assertTrue(ref.startswith(f"refs/tags/{approvals.REHEARSAL_TAG_PREFIX}"), ref)
        self.assertEqual(github.refs, {f"refs/tags/{approvals.CLAIM_TAG_PREFIX}d-aaaaaaaa-1111"})

    def test_a_backend_that_accepts_a_second_claim_is_reported_as_broken_and_cleaned_up(self):
        github = FakeGitHubRefs()
        github.post = lambda url, headers=None, json=None, timeout=None: FakeResponse(201, {}) if github.refs.add(json["ref"]) is None else None
        with self.assertRaises(approvals.ClaimUnavailableError) as ctx:
            approvals.rehearse_claims(github.claimer())
        self.assertIn("double publishing", str(ctx.exception))
        self.assertEqual(github.refs, set())

    def test_a_cleanup_failure_is_reported(self):
        github = FakeGitHubRefs()
        github.fail["delete"] = FakeResponse(403, text="no")
        with self.assertRaises(approvals.ClaimUnavailableError):
            approvals.rehearse_claims(github.claimer())

    def test_rehearsal_with_the_local_backend(self):
        steps = approvals.rehearse_claims(approvals.LocalFileClaim(self.claims_dir))
        self.assertEqual(steps[-1], ("throwaway claim removed", "ok"))
        self.assertEqual(self.claim_files(), [])

    def test_the_command_refuses_to_run_outside_actions(self):
        with self.assertRaises(SystemExit) as ctx:
            autopilot.main(["--rehearse-claims"])
        self.assertIn("GitHub Actions", str(ctx.exception))

    def test_the_command_reports_success_inside_actions(self):
        github = FakeGitHubRefs()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), \
                mock.patch.object(approvals, "default_claimer", lambda *_: github.claimer()), \
                contextlib.redirect_stdout(out):
            autopilot.main(["--rehearse-claims"])
        self.assertIn("CLAIM MECHANISM OK", out.getvalue())
        self.assertNotIn("ghs_SECRET", out.getvalue())
        self.assertEqual(github.refs, set())
        self.assertEqual(self.post.call_count, 0)


if __name__ == "__main__":
    unittest.main()
