"""Approved-draft publishing: validation, tampering, expiry, single use, failures. All offline."""

import contextlib
import copy
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import support
from support import FakeResponse, OfflineTestCase, queued_response
import approvals
import autopilot
import media_selector

TEXT = "Which ring would you turn first?"
DESCRIPTION = "Level 11 in a purple-toned theme, with Echo, Portal and Block gimmicks."


def now_utc():
    return datetime.now(timezone.utc)


class DraftTestCase(OfflineTestCase):
    """A repo with one real media file and helpers to create, store and approve drafts."""

    def setUp(self):
        super().setUp()
        self.candidates = mock.patch.object(autopilot, "generate_candidates",
                                            side_effect=AssertionError("publishing must never regenerate text")).start()
        mock.patch.object(autopilot, "critique_post", side_effect=AssertionError("no critic when publishing")).start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.draft_file = self.tmp / "approved" / "preview_draft.json"
        self.draft_file.parent.mkdir()
        self.claims_dir = self.tmp / "data" / "used_approvals"
        self.post.side_effect = None
        self.post.return_value = queued_response()
        # Approval and publishing always run the strict media checks: real files, served from disk by a fake raw
        # GitHub, with FFmpeg's decode result stubbed (real decoding is covered in test_media_gate).
        mock.patch.object(autopilot, "MEDIA_CHECK_MODE", "strict").start()
        mock.patch.object(autopilot.media_tools, "have_ffmpeg", return_value=True).start()
        mock.patch.object(autopilot.media_tools, "validate_media", return_value=[]).start()
        self.requested = support.serve_media(self, self.tmp)

    def media(self, **override):
        item = {"file": self.image_rel, "type": "image", "url": media_selector.raw_url(self.image_rel),
                "description": DESCRIPTION, "tags": ["black hole"]}
        item.update(override)
        return item

    def make(self, text=TEXT, now=None, ttl_hours=approvals.DRAFT_TTL_HOURS, media=None, **kw):
        media = media or self.media()
        if "media_check" not in kw and (self.tmp / media["file"]).is_file():
            kw["media_check"] = support.ok_check(self.tmp / media["file"])
        return approvals.create_draft(text, media, autopilot.BUFFER_CHANNEL_ID, post_type="hook",
                                      run_id="777", now=now, ttl_hours=ttl_hours, repo_root=self.tmp, **kw)

    def store(self, draft):
        self.draft_file.write_text(json.dumps(draft), encoding="utf-8")
        return draft

    def rehash(self, draft):
        """What a forger would do: edit the content, then recompute the hash and ID to match."""
        draft["content_hash"] = approvals.content_hash(draft)
        draft["draft_id"] = f"d-{draft['content_hash'][:8]}-abcd"
        return draft

    def verify(self, draft, code=None, **kw):
        kw.setdefault("channel_id", autopilot.BUFFER_CHANNEL_ID)
        kw.setdefault("repo_root", self.tmp)
        kw.setdefault("raw_prefix", media_selector.RAW_REPO)
        return approvals.verify_draft(draft, draft["draft_id"] if code is None else code, **kw)

    def publish(self, draft=None, code=None, run_id="777", argv_extra=()):
        draft = draft or json.loads(self.draft_file.read_text(encoding="utf-8"))
        argv = ["--publish-approved", str(self.draft_file), "--approval-code", draft["draft_id"] if code is None else code]
        if run_id:
            argv += ["--expected-run-id", run_id]
        out = io.StringIO()
        error = None
        with contextlib.redirect_stdout(out):
            try:
                autopilot.main(argv + list(argv_extra))
            except SystemExit as exc:
                error = exc
        return error, out.getvalue()

    def sent(self):
        return self.post.call_args.kwargs["json"]["variables"]["input"]

    def claim_files(self):
        return sorted(p.name for p in self.claims_dir.glob("*.json")) if self.claims_dir.exists() else []


class CreateAndVerifyTests(DraftTestCase):
    def test_draft_has_everything_needed_to_inspect_it(self):
        now = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
        draft = self.make(now=now)
        self.assertEqual(draft["text"], TEXT)
        self.assertEqual(draft["media"]["file"], self.image_rel)
        self.assertEqual(draft["media"]["url"], media_selector.raw_url(self.image_rel))
        self.assertEqual(draft["media"]["type"], "image")
        self.assertEqual(draft["target"]["buffer_channel_id"], autopilot.BUFFER_CHANNEL_ID)
        self.assertRegex(draft["draft_id"], r"^d-[0-9a-f]{8}-[0-9a-f]{4}$")
        self.assertEqual(draft["created_at"], "2026-10-27T06:00:00Z")
        self.assertEqual(draft["expires_at"], "2026-10-29T06:00:00Z")
        self.assertIs(draft["sent"], False)
        self.assertEqual(draft["media"]["bytes"], (self.tmp / self.image_rel).stat().st_size)      # the real file on disk
        self.assertGreater(draft["media"]["bytes"], 1000)
        self.assertEqual(len(draft["media"]["sha256"]), 64)

    def test_draft_ids_are_unique_even_for_identical_content(self):
        ids = {self.make()["draft_id"] for _ in range(20)}
        self.assertGreater(len(ids), 15)

    def test_a_valid_draft_verifies_and_returns_exactly_its_media(self):
        draft = self.make()
        media = self.verify(draft)
        self.assertEqual((media["file"], media["url"], media["type"]), (self.image_rel, draft["media"]["url"], "image"))

    def test_the_code_must_be_this_drafts_id(self):
        draft = self.make()
        for bad in ("", "d-00000000-0000", draft["draft_id"][:-1], draft["draft_id"] + "0", None):
            with self.subTest(code=bad), self.assertRaises(approvals.ApprovalError) as ctx:
                approvals.verify_draft(draft, bad, channel_id=autopilot.BUFFER_CHANNEL_ID, repo_root=self.tmp,
                                       raw_prefix=media_selector.RAW_REPO)
            self.assertEqual(ctx.exception.reason, "wrong_code")
        self.verify(draft, code=f"  {draft['draft_id']}\n")                   # surrounding whitespace is tolerated

    def test_code_of_another_draft_is_not_accepted(self):
        first, second = self.make(), self.make("A different question entirely?")
        with self.assertRaises(approvals.ApprovalError):
            self.verify(first, code=second["draft_id"])


class TamperingTests(DraftTestCase):
    def assert_tampered(self, edit, reason="tampered"):
        draft = self.make()
        edit(draft)
        with self.assertRaises(approvals.ApprovalError) as ctx:
            self.verify(draft)
        self.assertEqual(ctx.exception.reason, reason)

    def test_edited_text(self):
        self.assert_tampered(lambda d: d.update(text=TEXT + " Buy now!"))

    def test_edited_media_url(self):
        self.assert_tampered(lambda d: d["media"].update(url=media_selector.raw_url("media/gameplay/other.png")))

    def test_edited_media_file(self):
        self.assert_tampered(lambda d: d["media"].update(file="media/gameplay/other.png"))

    def test_edited_media_type(self):
        self.assert_tampered(lambda d: d["media"].update(type="video"))

    def test_edited_media_hash_or_size(self):
        self.assert_tampered(lambda d: d["media"].update(sha256="0" * 64))
        self.assert_tampered(lambda d: d["media"].update(bytes=999))

    def test_edited_description_cannot_be_used_to_pass_the_mechanics_check(self):
        self.assert_tampered(lambda d: d["media"].update(description="with Prism gimmicks"))

    def test_edited_target_channel(self):
        self.assert_tampered(lambda d: d["target"].update(buffer_channel_id="000000000000000000000000"))

    def test_extended_expiry_cannot_revive_a_draft(self):
        draft = self.make(now=now_utc() - timedelta(days=5), ttl_hours=1)
        draft["expires_at"] = (now_utc() + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        with self.assertRaises(approvals.ApprovalError) as ctx:
            self.verify(draft)
        self.assertEqual(ctx.exception.reason, "tampered")

    def test_edited_hash_or_id(self):
        self.assert_tampered(lambda d: d.update(content_hash="f" * 64))
        draft = self.make()
        draft["draft_id"] = "d-00000000-0000"
        with self.assertRaises(approvals.ApprovalError) as ctx:
            self.verify(draft, code="d-00000000-0000")
        self.assertEqual(ctx.exception.reason, "tampered")

    def test_malformed_drafts(self):
        for edit in (lambda d: d.pop("text"), lambda d: d.update(text="  "), lambda d: d.pop("media"),
                     lambda d: d.update(schema="other/9"), lambda d: d.update(draft_id="nope"),
                     lambda d: d.update(created_at="yesterday")):
            draft = self.make()
            edit(draft)
            with self.subTest(), self.assertRaises(approvals.ApprovalError) as ctx:
                self.verify(draft, code=draft.get("draft_id") or "x")
            self.assertIn(ctx.exception.reason, ("malformed", "tampered"))

    def test_a_forged_but_self_consistent_draft_is_still_held_to_every_other_check(self):
        draft = self.make()
        draft["media"]["url"] = "https://evil.example/x.png"
        self.rehash(draft)
        with self.assertRaises(approvals.ApprovalError) as ctx:
            self.verify(draft)
        self.assertEqual(ctx.exception.reason, "bad_media")

        draft = self.make()
        draft["target"]["buffer_channel_id"] = "000000000000000000000000"
        self.rehash(draft)
        with self.assertRaises(approvals.ApprovalError) as ctx:
            self.verify(draft)
        self.assertEqual(ctx.exception.reason, "wrong_channel")

        for path in ("media/../../etc/passwd", "scripts/autopilot.py", "/etc/passwd"):
            draft = self.make()
            draft["media"]["file"] = path
            draft["media"]["url"] = media_selector.raw_url("media/x.png")
            self.rehash(draft)
            with self.subTest(path=path), self.assertRaises(approvals.ApprovalError) as ctx:
                self.verify(draft)
            self.assertEqual(ctx.exception.reason, "bad_media")


class ExpiryAndProvenanceTests(DraftTestCase):
    def reason(self, draft, **kw):
        with self.assertRaises(approvals.ApprovalError) as ctx:
            self.verify(draft, **kw)
        return ctx.exception.reason

    def test_expired_draft_is_rejected(self):
        draft = self.make(now=now_utc() - timedelta(hours=49))
        self.assertEqual(self.reason(draft), "expired")

    def test_a_draft_just_inside_its_window_is_accepted(self):
        self.verify(self.make(now=now_utc() - timedelta(hours=47)))

    def test_exact_expiry_instant_is_expired(self):
        draft = self.make(now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(self.reason(draft, now=datetime(2026, 1, 3, tzinfo=timezone.utc)), "expired")

    def test_future_dated_draft_is_rejected(self):
        self.assertEqual(self.reason(self.make(now=now_utc() + timedelta(hours=3))), "not_yet_valid")

    def test_absurdly_long_validity_is_rejected(self):
        self.assertEqual(self.reason(self.make(ttl_hours=24 * 30)), "malformed")

    def test_draft_must_come_from_the_named_preview_run(self):
        draft = self.make()
        self.verify(draft, expected_run_id="777")
        self.assertEqual(self.reason(draft, expected_run_id="778"), "wrong_run")


class MediaChecksTests(DraftTestCase):
    def test_media_changed_after_the_preview_is_rejected(self):
        draft = self.make()
        (self.tmp / self.image_rel).write_bytes(support.png_bytes(seed=7))     # different pixels, still a valid image
        self.assertEqual(self.reason(draft), "media_changed")

    def test_media_replaced_by_a_tiny_placeholder_is_rejected_as_not_media(self):
        draft = self.make()
        (self.tmp / self.image_rel).write_bytes(b"png")
        self.assertEqual(self.reason(draft), "bad_media")

    def reason(self, draft):
        with self.assertRaises(approvals.ApprovalError) as ctx:
            self.verify(draft)
        return ctx.exception.reason

    def test_hosted_media_without_a_local_file_is_allowed_through_verify(self):
        media = self.media(file="store/clip.mp4", type="video", bytes=1234,
                           url=media_selector.RAW_REPO + "/media-store/store/clip.mp4")
        check = {"status": "ok", "sha256": "a" * 64, "bytes": 1234, "source": "url", "decode_proof": "ffprobe"}
        self.verify(self.make(media=media, media_check=check))

    def test_hosted_media_whose_check_was_not_made_at_its_url_is_rejected(self):
        media = self.media(file="store/clip.mp4", type="video", bytes=1234,
                           url=media_selector.RAW_REPO + "/media-store/store/clip.mp4")
        check = {"status": "ok", "sha256": "a" * 64, "bytes": 1234, "source": "repo", "decode_proof": "ffprobe"}
        self.assertEqual(self.reason(self.make(media=media, media_check=check)), "media_missing")

    def test_unknown_media_type(self):
        draft = self.make()
        draft["media"]["type"] = "document"
        self.assertEqual(self.reason(draft), "bad_media")


class ClaimTests(DraftTestCase):
    def test_local_claim_can_only_succeed_once(self):
        claimer = approvals.LocalFileClaim(self.claims_dir)
        claimer.claim("d-aaaaaaaa-1111", {"actor": "me"})
        with self.assertRaises(approvals.AlreadyUsedError):
            claimer.claim("d-aaaaaaaa-1111")
        claimer.claim("d-aaaaaaaa-2222")
        self.assertEqual(len(self.claim_files()), 2)

    def test_github_claim_creates_a_tag_ref_and_never_leaks_the_token(self):
        post = mock.Mock(return_value=FakeResponse(201, {}))
        approvals.GitHubRefClaim("owner/repo", "ghs_SECRET", "abc123", post=post).claim("d-aaaaaaaa-1111")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.github.com/repos/owner/repo/git/refs")
        self.assertEqual(kwargs["json"], {"ref": "refs/tags/ringshift-used-d-aaaaaaaa-1111", "sha": "abc123"})

    def test_github_claim_that_already_exists_means_already_used(self):
        post = mock.Mock(return_value=FakeResponse(422, text='{"message":"Reference already exists"}'))
        with self.assertRaises(approvals.AlreadyUsedError):
            approvals.GitHubRefClaim("o/r", "tok", "sha", post=post).claim("d-aaaaaaaa-1111")

    def test_any_other_github_answer_fails_closed_without_the_token_in_the_message(self):
        for response in (FakeResponse(403, text="no"), FakeResponse(500, text="x"),
                         FakeResponse(422, text='{"message":"Object does not exist"}')):
            post = mock.Mock(return_value=response)
            with self.subTest(status=response.status_code), self.assertRaises(approvals.ClaimUnavailableError) as ctx:
                approvals.GitHubRefClaim("o/r", "ghs_SECRET", "sha", post=post).claim("d-aaaaaaaa-1111")
            self.assertNotIn("ghs_SECRET", str(ctx.exception))

        post = mock.Mock(side_effect=ConnectionError("down"))
        with self.assertRaises(approvals.ClaimUnavailableError) as ctx:
            approvals.GitHubRefClaim("o/r", "ghs_SECRET", "sha", post=post).claim("d-aaaaaaaa-1111")
        self.assertNotIn("ghs_SECRET", str(ctx.exception))

    def test_in_actions_the_claim_backend_is_github_and_needs_its_environment(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "o/r",
                                          "GITHUB_TOKEN": "t", "GITHUB_SHA": "s"}):
            self.assertIsInstance(approvals.default_claimer(self.tmp / "data"), approvals.GitHubRefClaim)
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "",
                                          "GITHUB_SHA": "s"}):
            with self.assertRaises(approvals.ClaimUnavailableError):
                approvals.default_claimer(self.tmp / "data")
        self.assertIsInstance(approvals.default_claimer(self.tmp / "data"), approvals.LocalFileClaim)

    def test_load_draft_errors_are_clear(self):
        with self.assertRaises(approvals.ApprovalError) as ctx:
            approvals.load_draft(self.tmp / "nope.json")
        self.assertEqual(ctx.exception.reason, "missing")
        self.draft_file.write_text("{oops", encoding="utf-8")
        with self.assertRaises(approvals.ApprovalError) as ctx:
            approvals.load_draft(self.draft_file)
        self.assertEqual(ctx.exception.reason, "malformed")
        self.draft_file.write_text("[1]", encoding="utf-8")
        with self.assertRaises(approvals.ApprovalError):
            approvals.load_draft(self.draft_file)


class PublishApprovedTests(DraftTestCase):
    def test_publishes_exactly_the_approved_text_and_media_once(self):
        draft = self.store(self.make())
        error, output = self.publish()
        self.assertIsNone(error, output)
        self.assertEqual(self.post.call_count, 1)
        sent = self.sent()
        self.assertEqual(sent["text"], draft["text"])                         # byte for byte
        self.assertEqual(sent["assets"], [{"image": {"url": draft["media"]["url"]}}])
        self.assertEqual(sent["channelId"], draft["target"]["buffer_channel_id"])
        self.assertNotIn("http", sent["text"])
        self.assertIn("QUEUED WITH MEDIA CONFIRMED", output)
        self.candidates.assert_not_called()

    def test_state_is_recorded_with_the_draft_id(self):
        draft = self.store(self.make())
        self.publish()
        log = json.loads(self.post_log_file.read_text(encoding="utf-8"))
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["draft_id"], draft["draft_id"])
        self.assertEqual(log[0]["content_hash"], draft["content_hash"])
        self.assertEqual(log[0]["approved_by"], "tester")
        self.assertEqual(json.loads(self.counter_file.read_text(encoding="utf-8")), {"successful_posts": 4})
        self.assertEqual(self.claim_files(), [f"{draft['draft_id']}.json"])
        self.assertIn(draft["draft_id"], self.posts_file.read_text(encoding="utf-8"))

    def test_the_draft_is_claimed_before_buffer_is_called(self):
        draft = self.store(self.make())
        order = []
        real_claim = approvals.LocalFileClaim.claim

        def claim(this, *a, **k):
            order.append("claim")
            return real_claim(this, *a, **k)

        def post(*a, **k):
            order.append("post")
            return queued_response()

        self.post.side_effect = post
        with mock.patch.object(approvals.LocalFileClaim, "claim", claim):
            self.publish()
        self.assertEqual(order, ["claim", "post"])

    def test_a_used_approval_cannot_be_used_again(self):
        self.store(self.make())
        self.assertIsNone(self.publish()[0])
        error, output = self.publish()
        self.assertEqual(error.code, 1)
        self.assertIn("already_used", output)
        self.assertEqual(self.post.call_count, 1)

    def test_restart_after_a_lost_claim_is_still_stopped_by_the_post_log(self):
        draft = self.store(self.make())
        self.publish()
        for file in self.claims_dir.glob("*.json"):
            file.unlink()                                                    # e.g. a different runner without the claim
        error, output = self.publish()
        self.assertEqual(error.code, 1)
        self.assertIn("already published", output)
        self.assertEqual(self.post.call_count, 1)

    def test_restart_after_a_lost_post_log_is_still_stopped_by_the_claim(self):
        self.store(self.make())
        self.publish()
        self.post_log_file.unlink()
        error, output = self.publish()
        self.assertEqual(error.code, 1)
        self.assertEqual(self.post.call_count, 1)

    # ---- rejections that must not use the draft up ----
    def assert_rejected_and_unclaimed(self, error, output, reason=None):
        self.assertEqual(error.code, 1, output)
        self.assertIn("NOT PUBLISHING", output)
        if reason:
            self.assertIn(reason, output)
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.claim_files(), [])
        self.assertFalse(self.post_log_file.exists())
        self.assertEqual(json.loads(self.counter_file.read_text(encoding="utf-8")), {"successful_posts": 3})

    def test_tampered_draft_file_is_rejected(self):
        draft = self.make()
        self.store(draft)
        edited = copy.deepcopy(draft)
        edited["text"] = "Totally different words."
        self.store(edited)
        self.assert_rejected_and_unclaimed(*self.publish(draft=draft), reason="tampered")

    def test_wrong_approval_code_is_rejected(self):
        self.store(self.make())
        self.assert_rejected_and_unclaimed(*self.publish(code="d-00000000-0000"), reason="wrong_code")

    def test_expired_draft_is_rejected(self):
        self.store(self.make(now=now_utc() - timedelta(days=3)))
        self.assert_rejected_and_unclaimed(*self.publish(), reason="expired")

    def test_draft_from_another_run_is_rejected(self):
        self.store(self.make())
        self.assert_rejected_and_unclaimed(*self.publish(run_id="999"), reason="wrong_run")

    def test_missing_draft_file_is_rejected(self):
        draft = self.make()
        self.assert_rejected_and_unclaimed(*self.publish(draft=draft), reason="missing")

    def test_missing_media_file_online_is_rejected_and_the_draft_survives(self):
        draft = self.store(self.make())
        support.serve_media(self, self.tmp, missing={self.image_rel})              # raw GitHub answers 404
        self.assert_rejected_and_unclaimed(*self.publish(), reason="media_missing")

        # fixed later (the file is pushed): the same approval still works, because the rejection did not use it up
        support.serve_media(self, self.tmp)
        self.assertIsNone(self.publish()[0])
        self.assertEqual(self.sent()["text"], draft["text"])

    def test_media_deleted_locally_after_preview_is_rejected(self):
        self.store(self.make())
        (self.tmp / self.image_rel).write_bytes(support.png_bytes(seed=99))        # a different but valid image
        self.assert_rejected_and_unclaimed(*self.publish(), reason="media_changed")

    def test_a_waiting_post_in_buffer_blocks_without_using_the_draft_up(self):
        self.store(self.make())
        self.guard.side_effect = RuntimeError("Buffer already has a post waiting")
        self.assert_rejected_and_unclaimed(*self.publish(), reason="already has a post waiting")
        self.guard.side_effect = None
        self.assertIsNone(self.publish()[0])
        self.assertEqual(self.post.call_count, 1)

    def test_approval_does_not_override_the_post_rules(self):
        for bad in ("That black hole is wild", "Over 5,000 players love this", "Now with a Prism"):
            with self.subTest(text=bad):
                self.draft_file.unlink(missing_ok=True)
                self.store(self.make(text=bad))
                self.assert_rejected_and_unclaimed(*self.publish(), reason="post rules")

    def test_text_that_duplicates_an_existing_post_is_rejected(self):
        mock.patch.object(autopilot, "posts_history", TEXT).start()
        self.store(self.make())
        self.assert_rejected_and_unclaimed(*self.publish(), reason="post rules")

    def test_corrupt_post_log_stops_before_the_claim(self):
        self.store(self.make())
        self.post_log_file.write_text("{broken", encoding="utf-8")
        error, output = self.publish()
        self.assertEqual(error.code, 1)
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.claim_files(), [])

    def test_publishing_never_needs_or_uses_the_gemini_key(self):
        self.store(self.make())
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            self.assertIsNone(self.publish()[0])

    def test_publish_cannot_be_combined_with_dry_run(self):
        self.store(self.make())
        with self.assertRaises(SystemExit):
            autopilot.main(["--publish-approved", str(self.draft_file), "--approval-code", "x", "--dry-run"])
        self.assertEqual(self.post.call_count, 0)

    def test_missing_buffer_key_stops_everything(self):
        self.store(self.make())
        with mock.patch.dict(os.environ, {"BUFFER_API_KEY": ""}):
            error, _ = self.publish()
        self.assertIn("BUFFER_API_KEY", str(error.code))
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.claim_files(), [])

    # ---- failures AFTER the claim ----
    def assert_failed_after_claim(self, error, output, draft):
        self.assertEqual(error.code, 1)
        self.assertIn("CLAIMED", output)
        self.assertEqual(self.claim_files(), [f"{draft['draft_id']}.json"])
        self.assertEqual(json.loads(self.counter_file.read_text(encoding="utf-8")), {"successful_posts": 3})
        self.assertFalse(self.post_log_file.exists())
        self.assertNotIn(support.BUFFER_KEY, output)

    def test_buffer_http_error_keeps_the_claim_and_records_nothing(self):
        draft = self.store(self.make())
        self.post.return_value = FakeResponse(500, text="boom")
        self.assert_failed_after_claim(*self.publish(), draft)
        self.assertEqual(self.post.call_count, 1)

    def test_buffer_graphql_rejection_keeps_the_claim(self):
        draft = self.store(self.make())
        self.post.return_value = FakeResponse(200, {"data": {"createPost": {"message": "nope"}}})
        self.assert_failed_after_claim(*self.publish(), draft)

    def test_unconfirmed_media_keeps_the_claim_and_records_nothing(self):
        draft = self.store(self.make())
        self.post.return_value = queued_response(assets=[])
        self.assert_failed_after_claim(*self.publish(), draft)

    def test_network_exception_keeps_the_claim(self):
        import requests
        draft = self.store(self.make())
        self.post.side_effect = requests.Timeout("slow")
        self.assert_failed_after_claim(*self.publish(), draft)

    def test_after_any_failure_a_restart_cannot_publish_the_same_draft(self):
        self.store(self.make())
        self.post.return_value = FakeResponse(500, text="boom")
        self.publish()
        self.post.return_value = queued_response()
        error, output = self.publish()                                       # the workflow is re-run
        self.assertEqual(error.code, 1)
        self.assertIn("already_used", output)
        self.assertEqual(self.post.call_count, 1)

    def test_a_429_is_retried_but_still_publishes_once(self):
        self.store(self.make())
        limited = FakeResponse(429, text="slow")
        limited.headers = {"Retry-After": "1"}
        self.post.side_effect = [limited, queued_response()]
        self.assertIsNone(self.publish()[0])
        self.assertEqual(len(json.loads(self.post_log_file.read_text(encoding="utf-8"))), 1)

    def test_github_claim_unavailable_means_nothing_is_published(self):
        self.store(self.make())
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "o/r",
                                          "GITHUB_TOKEN": "t", "GITHUB_SHA": "s"}):
            with mock.patch("requests.post", side_effect=lambda url, **k: FakeResponse(403, text="no")
                            if "api.github.com" in url else queued_response()) as post:
                error, output = self.publish()
        self.assertEqual(error.code, 1)
        self.assertEqual([c.args[0] for c in post.call_args_list if "buffer" in c.args[0]], [])


if __name__ == "__main__":
    unittest.main()
