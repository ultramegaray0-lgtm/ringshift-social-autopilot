"""Regression tests for PR #4 follow-up: invalid media, missing GitHub media, bypass attempts, fail-closed approvals,
duplicate and generic captions, and per-run limits. All offline: no network, no API keys, nothing is sent.

The "3 bytes" and "HTTP 404" seen in the CI output were test fixtures (a b"png" placeholder and a mocked 404).
These tests make sure such a file can never be treated as media by the real code, in any mode.
"""

import contextlib
import copy
import io
import json
import os
import struct
import unittest
from pathlib import Path
from unittest import mock

import support
from support import OfflineTestCase
import approvals
import autopilot
import caption_quality as cq
import media_manifest as mm
import media_selector
import media_tools as tools

CRITIC_YES = ('CLAIMS:\n- "Portal" => MEDIA evidence: "Portal"\n\nHook: 8\nCuriosity: 8\nNatural tone: 8\n'
              'Ringshift relevance: 8\nOriginality: 8\nViral potential: 8\n\nKEEP: YES\n')
DESC = "Level 11 in a purple-toned theme, with Echo, Portal and Block gimmicks."
GOOD = "A Portal in a purple level."


def jpeg_bytes(width=400, height=400, size=3000, end=True):
    """A structurally valid JPEG header (SOI, SOF0 with the size, padding, EOI): enough for header inspection."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    body = b"\xff\xd8" + sof + b"\x00" * max(size - 20, 0)
    return body + (b"\xff\xd9" if end else b"")


def write(directory, name, data):
    path = Path(directory) / name
    path.write_bytes(data)
    return path


class InspectFileTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp())

    def problems(self, name, data):
        return tools.inspect_file(write(self.dir, name, data))["problems"]

    def test_a_zero_byte_image_is_invalid(self):
        self.assertTrue(any("empty" in p for p in self.problems("a.png", b"")))

    def test_the_three_byte_placeholder_is_invalid(self):
        problems = self.problems("black_hole.png", b"png")
        self.assertTrue(any("only 3 bytes" in p for p in problems), problems)
        self.assertTrue(any("signature" in p for p in problems), problems)

    def test_a_tiny_but_well_formed_png_is_invalid(self):
        tiny = support.png_bytes(2, 2)
        problems = self.problems("a.png", tiny)
        self.assertTrue(any("too small" in p for p in problems), problems)

    def test_a_real_png_passes(self):
        result = tools.inspect_file(write(self.dir, "a.png", support.png_bytes(320, 320)))
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["width"], result["height"], result["format"]), (320, 320, "png"))

    def test_a_small_picture_is_rejected_even_if_it_is_a_valid_png(self):
        problems = self.problems("a.png", support.png_bytes(100, 100))
        self.assertTrue(any("too small" in p for p in problems), problems)

    def test_a_real_jpeg_header_passes_and_a_truncated_one_does_not(self):
        self.assertTrue(tools.inspect_file(write(self.dir, "a.jpg", jpeg_bytes()))["ok"])
        problems = self.problems("b.jpg", jpeg_bytes(end=False))
        self.assertTrue(any("truncated" in p for p in problems), problems)

    def test_the_extension_must_match_the_real_format(self):
        problems = self.problems("a.jpg", support.png_bytes())
        self.assertTrue(any("extension says jpg but the contents are png" in p for p in problems), problems)

    def test_an_html_error_page_saved_as_png_is_invalid(self):
        page = b"<html><body>404: Not Found</body></html>" * 60
        self.assertTrue(any("signature" in p for p in self.problems("a.png", page)))

    def test_a_truncated_png_is_invalid(self):
        data = support.png_bytes()
        problems = self.problems("a.png", data[:-40])
        self.assertTrue(any("truncated" in p for p in problems), problems)

    def test_a_damaged_png_header_is_invalid(self):
        data = bytearray(support.png_bytes())
        data[12:16] = b"XXXX"
        self.assertTrue(any("header is damaged" in p for p in self.problems("a.png", bytes(data))))

    def test_a_missing_file_and_an_unsupported_type_are_invalid(self):
        self.assertIn("file does not exist", tools.inspect_file(self.dir / "nope.png")["problems"])
        self.assertTrue(any("unsupported" in p for p in self.problems("a.gif", b"GIF89a" + b"0" * 2000)))

    def test_mp4_structure_is_checked_without_ffmpeg(self):
        good = support.mp4_bytes()
        self.assertTrue(tools.inspect_file(write(self.dir, "a.mp4", good))["ok"])
        cut = self.problems("b.mp4", good[: len(good) // 2])
        self.assertTrue(any("truncated" in p or "past the end" in p for p in cut), cut)
        self.assertTrue(any("only 3 bytes" in p for p in self.problems("c.mp4", b"mp4")))

    def test_an_mp4_without_frames_is_incomplete(self):
        def box(kind, data):
            return struct.pack(">I", 8 + len(data)) + kind + data
        no_mdat = box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41") + box(b"moov", b"\x00" * 6000)
        self.assertTrue(any("mdat" in p for p in self.problems("a.mp4", no_mdat)))

    def test_verification_fails_closed_without_ffmpeg(self):
        path = write(self.dir, "a.png", support.png_bytes())
        with mock.patch.object(tools, "have_ffmpeg", return_value=False):
            problems = tools.verify_media_file(path)
        self.assertTrue(any("could not be decoded" in p for p in problems), problems)

    @unittest.skipUnless(tools.have_ffmpeg(), "ffmpeg/ffprobe not installed")
    def test_a_real_decode_accepts_a_real_png(self):
        self.assertEqual(tools.verify_media_file(write(self.dir, "a.png", support.png_bytes())), [])


class SelectorSkipsInvalidFilesTests(OfflineTestCase):
    def test_a_placeholder_file_is_never_a_candidate(self):
        (self.tmp / self.image_rel).write_bytes(b"png")
        files, skipped = media_selector._list_candidates({})
        self.assertNotIn(self.image_rel, [f["file"] for f in files])
        self.assertTrue(any(name == self.image_rel and "not a valid image" in why for name, why in skipped), skipped)

    def test_with_only_invalid_files_nothing_is_selected(self):
        (self.tmp / self.image_rel).write_bytes(b"png")
        (self.tmp / self.video_rel).write_bytes(b"mp4")
        with self.assertRaises(RuntimeError):
            media_selector.select_media("")

    def test_the_manifest_validator_reports_an_invalid_file_as_an_error(self):
        (self.tmp / self.image_rel).write_bytes(b"png")
        item = mm.make_item(self.image_rel, base_dir=self.tmp, media_type="image", description="A Portal level.",
                            technical={"width": 320, "height": 320, "aspect": "other", "bytes": 3, "duration": None, "sha256": None},
                            validation={"status": "ok", "checked_at": None, "problems": []}, hashes=[])
        manifest = {"schema": mm.SCHEMA, "items": [item]}
        errors = [p for p in mm.validate_manifest(manifest, self.tmp, decode=False, check_hashes=False) if p["level"] == "error"]
        self.assertTrue(any("not a valid media file" in e["message"] for e in errors), errors)


class StrictCase(OfflineTestCase):
    """Strict media checking with real fixture files served by a fake raw.githubusercontent.com."""

    def setUp(self):
        super().setUp()
        mock.patch.object(autopilot, "MEDIA_CHECK_MODE", "strict").start()
        mock.patch.object(autopilot.media_tools, "have_ffmpeg", return_value=True).start()
        mock.patch.object(autopilot.media_tools, "validate_media", return_value=[]).start()
        self.requested = support.serve_media(self, self.tmp)
        self.media = {"file": self.image_rel, "type": "image", "url": media_selector.raw_url(self.image_rel)}

    def check(self, media=None, **kw):
        return autopilot.check_media_for_approval(media or self.media, self.tmp, **kw)


class MediaCheckTests(StrictCase):
    def test_a_real_served_file_passes_with_a_full_record(self):
        record = self.check()
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["sha256"], approvals.sha256_file(self.tmp / self.image_rel))
        self.assertEqual((record["format"], record["source"], record["decode_proof"]), ("png", "repo", "ffprobe"))

    def test_raw_github_404_fails_closed(self):
        support.serve_media(self, self.tmp, missing={self.image_rel})
        with self.assertRaises(autopilot.MediaUnavailableError) as raised:
            self.check()
        self.assertIn("HTTP 404", str(raised.exception))

    def test_a_redirect_or_server_error_fails_closed(self):
        for code in (301, 302, 500, 503):
            support.serve_media(self, self.tmp, status=code)
            with self.subTest(status=code), self.assertRaises(autopilot.MediaUnavailableError):
                self.check()

    def test_unreachable_media_fails_closed(self):
        import requests
        mock.patch("requests.get", side_effect=requests.ConnectionError("down")).start()
        with self.assertRaises(autopilot.MediaUnavailableError) as raised:
            self.check()
        self.assertIn("could not reach", str(raised.exception))

    def test_stale_media_on_github_is_a_mismatch(self):
        support.serve_media(self, self.tmp, overrides={self.image_rel: support.png_bytes(seed=42)})
        with self.assertRaises(autopilot.MediaUnavailableError) as raised:
            self.check()
        self.assertIn("different bytes", str(raised.exception))

    def test_github_serving_an_html_page_with_200_is_rejected(self):
        support.serve_media(self, self.tmp, overrides={self.image_rel: b"<html>not found</html>" * 100})
        with self.assertRaises(autopilot.MediaUnavailableError) as raised:
            self.check()
        self.assertIn("does not serve a valid image", str(raised.exception))

    def test_a_tiny_local_file_is_rejected_before_any_network_use(self):
        (self.tmp / self.image_rel).write_bytes(b"png")
        del self.requested[:]
        with self.assertRaises(autopilot.MediaUnavailableError) as raised:
            self.check()
        self.assertIn("not a valid image", str(raised.exception))
        self.assertEqual(self.requested, [])

    def test_a_missing_local_file_without_a_url_fails(self):
        with self.assertRaises(autopilot.MediaUnavailableError):
            self.check({"file": "media/gameplay/gone.png", "type": "image", "url": ""})

    def test_a_clip_that_does_not_decode_fails(self):
        mock.patch.object(autopilot.media_tools, "validate_media", return_value=["no video stream"]).start()
        with self.assertRaises(autopilot.MediaUnavailableError) as raised:
            self.check()
        self.assertIn("does not decode", str(raised.exception))

    def test_without_ffmpeg_a_decode_must_be_proven_by_the_manifest_for_these_exact_bytes(self):
        mock.patch.object(autopilot.media_tools, "have_ffmpeg", return_value=False).start()
        with self.assertRaises(autopilot.MediaUnavailableError) as raised:
            self.check()
        self.assertIn("cannot prove the media decodes", str(raised.exception))
        sha = approvals.sha256_file(self.tmp / self.image_rel)
        proven = dict(self.media, validation_status="ok", manifest_sha256=sha)
        self.assertEqual(self.check(proven)["decode_proof"], "manifest")
        stale = dict(self.media, validation_status="ok", manifest_sha256="0" * 64)
        with self.assertRaises(autopilot.MediaUnavailableError):
            self.check(stale)

    def test_media_hosted_elsewhere_is_checked_at_its_url(self):
        url = media_selector.RAW_REPO + "/media-store/store/clip.png"
        body = support.png_bytes(seed=5)
        mock.patch("requests.get", side_effect=lambda u, **k: support.StreamResponse(200, body)).start()
        record = autopilot.check_media_for_approval({"file": "store/clip.png", "type": "image", "url": url}, self.tmp)
        self.assertEqual((record["source"], record["status"]), ("url", "ok"))

    def test_the_head_preflight_has_no_bypass(self):
        support.serve_media(self, self.tmp, missing={self.image_rel})
        with mock.patch.dict(os.environ, {"RINGSHIFT_VERIFY_MEDIA": "0"}), contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(autopilot.MediaUnavailableError):
                autopilot.verify_media_url(self.media)
            with self.assertRaises(autopilot.MediaUnavailableError):
                self.check()
        self.assertIn("ignored", out.getvalue())


class BypassAttemptTests(StrictCase):
    """Every way someone might try to switch the media gate off, or to approve something that was never checked."""

    def setUp(self):
        super().setUp()
        mock.patch.object(autopilot, "generate_candidates", return_value=[GOOD]).start()
        mock.patch.object(autopilot, "critique_post", return_value=CRITIC_YES).start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"tags": ["portal"], "description": DESC}}), encoding="utf-8")
        self.draft_path = self.tmp / "draft.json"

    def dry_run(self, **env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            autopilot.main(["--dry-run", "--draft-file", str(self.draft_path)])
        return out.getvalue()

    def report(self):
        return json.loads(self.draft_path.read_text(encoding="utf-8"))

    def test_with_valid_media_a_verified_draft_is_created_and_can_be_approved(self):
        output = self.dry_run()
        draft = self.report()
        self.assertIn("DRAFT COMPLETE", output)
        self.assertEqual(draft["media"]["check"]["status"], "ok")
        self.assertEqual(draft["media"]["check"]["sha256"], draft["media"]["sha256"])
        media = approvals.verify_draft(draft, draft["draft_id"], channel_id=autopilot.BUFFER_CHANNEL_ID, repo_root=self.tmp,
                                       raw_prefix=media_selector.RAW_REPO)
        self.assertEqual(media["file"], self.image_rel)

    def test_missing_media_means_no_draft_and_no_approval_code(self):
        support.serve_media(self, self.tmp, missing={self.image_rel})
        output = self.dry_run()
        report = self.report()
        self.assertEqual(report["result"], "no_candidate_passed")
        self.assertNotIn("draft_id", report)
        self.assertNotIn("approval", output.lower().replace("approval code is", ""))
        self.assertEqual(autopilot.DRAFTS_THIS_RUN, [])

    def test_stale_unreachable_and_invalid_media_each_mean_no_draft(self):
        import requests
        scenarios = {
            "stale": lambda: support.serve_media(self, self.tmp, overrides={self.image_rel: support.png_bytes(seed=3)}),
            "html": lambda: support.serve_media(self, self.tmp, overrides={self.image_rel: b"<html></html>" * 200}),
            "server error": lambda: support.serve_media(self, self.tmp, status=503),
            "unreachable": lambda: mock.patch("requests.get", side_effect=requests.ConnectionError("x")).start(),
            "local placeholder": lambda: (self.tmp / self.image_rel).write_bytes(b"png"),
        }
        for name, arrange in scenarios.items():
            with self.subTest(name):
                self.draft_path.unlink(missing_ok=True)
                arrange()
                self.dry_run()
                report = self.report()
                self.assertEqual(report["result"], "no_candidate_passed")
                self.assertNotIn("draft_id", report)

    def test_the_old_environment_switch_does_not_skip_the_checks(self):
        support.serve_media(self, self.tmp, missing={self.image_rel})
        for value in ("0", "false", "off"):
            self.draft_path.unlink(missing_ok=True)
            self.dry_run(RINGSHIFT_VERIFY_MEDIA=value)
            self.assertNotIn("draft_id", self.report(), value)

    def test_no_environment_variable_or_option_can_select_fixture_mode(self):
        support.serve_media(self, self.tmp, missing={self.image_rel})
        self.dry_run(RINGSHIFT_MEDIA_CHECK_MODE="fixture", MEDIA_CHECK_MODE="fixture", RINGSHIFT_TEST_FIXTURES="1")
        self.assertNotIn("draft_id", self.report())
        source = (support.SCRIPTS / "autopilot.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"environ[^\n]*MEDIA_CHECK")
        parser_help = autopilot.parse_args.__code__.co_consts
        self.assertFalse(any(isinstance(c, str) and ("fixture" in c.lower() or "verify-media" in c.lower()) for c in parser_help))

    def test_build_draft_refuses_unverified_or_failed_checks(self):
        media = {"file": self.image_rel, "type": "image", "url": media_selector.raw_url(self.image_rel)}
        for check in ({"status": "failed"}, {"status": "skipped"}, {}):
            with self.subTest(check=check), self.assertRaises(autopilot.MediaUnavailableError):
                autopilot.build_draft(GOOD, media, autopilot.POST_TYPES[0], 40, "c", [], check)
        support.serve_media(self, self.tmp, missing={self.image_rel})
        with self.assertRaises(autopilot.MediaUnavailableError):          # no check supplied: it is made, and fails
            autopilot.build_draft(GOOD, media, autopilot.POST_TYPES[0], 40, "c", [])
        self.assertEqual(autopilot.DRAFTS_THIS_RUN, [])

    def test_a_fixture_draft_exists_but_can_never_be_approved(self):
        mock.patch.object(autopilot, "MEDIA_CHECK_MODE", "fixture").start()
        output = self.dry_run()
        draft = self.report()
        self.assertEqual(draft["media"]["check"]["status"], "fixture")
        self.assertIn("TEST FIXTURE", autopilot.format_draft_summary(draft))
        with self.assertRaises(approvals.ApprovalError) as raised:
            approvals.verify_draft(draft, draft["draft_id"], channel_id=autopilot.BUFFER_CHANNEL_ID, repo_root=self.tmp,
                                   raw_prefix=media_selector.RAW_REPO)
        self.assertEqual(raised.exception.reason, "unverified_media")
        self.assertIn("DRAFT COMPLETE", output)

    def test_publishing_a_fixture_draft_is_refused_before_anything_is_claimed_or_sent(self):
        mock.patch.object(autopilot, "MEDIA_CHECK_MODE", "fixture").start()
        self.dry_run()
        draft = self.report()
        mock.patch.object(autopilot, "MEDIA_CHECK_MODE", "strict").start()
        with self.assertRaises(approvals.ApprovalError):
            autopilot.publish_approved(str(self.draft_path), draft["draft_id"], claimer=approvals.LocalFileClaim(self.tmp / "claims"))
        self.assertEqual(self.post.call_count, 0)
        self.assertFalse((self.tmp / "claims").exists() and any((self.tmp / "claims").iterdir()))

    def test_hand_editing_the_check_to_ok_breaks_the_hash(self):
        mock.patch.object(autopilot, "MEDIA_CHECK_MODE", "fixture").start()
        self.dry_run()
        draft = self.report()
        forged = copy.deepcopy(draft)
        forged["media"]["check"]["status"] = "ok"
        with self.assertRaises(approvals.ApprovalError) as raised:
            approvals.verify_draft(forged, forged["draft_id"], channel_id=autopilot.BUFFER_CHANNEL_ID, repo_root=self.tmp,
                                   raw_prefix=media_selector.RAW_REPO)
        self.assertEqual(raised.exception.reason, "tampered")

    def test_a_forged_draft_with_a_matching_hash_but_no_real_check_is_refused(self):
        draft = approvals.create_draft(GOOD, self.media, autopilot.BUFFER_CHANNEL_ID, post_type="hook", run_id="1",
                                       repo_root=self.tmp)                      # no media_check at all
        with self.assertRaises(approvals.ApprovalError) as raised:
            approvals.verify_draft(draft, draft["draft_id"], channel_id=autopilot.BUFFER_CHANNEL_ID, repo_root=self.tmp,
                                   raw_prefix=media_selector.RAW_REPO)
        self.assertEqual(raised.exception.reason, "unverified_media")

    def test_an_approved_draft_is_rechecked_against_github_at_publish_time(self):
        self.dry_run()
        draft = self.report()
        support.serve_media(self, self.tmp, overrides={self.image_rel: support.png_bytes(seed=77)})      # stale copy pushed
        with self.assertRaises(approvals.ApprovalError) as raised:
            autopilot.verify_approved(str(self.draft_path), draft["draft_id"])
        self.assertEqual(raised.exception.reason, "media_missing")
        self.assertEqual(self.post.call_count, 0)

    def test_bytes_and_hash_must_agree_with_the_check(self):
        self.dry_run()
        draft = self.report()
        for field, value in (("bytes", 5), ("sha256", "f" * 64)):
            forged = copy.deepcopy(draft)
            forged["media"][field] = value
            forged["content_hash"] = approvals.content_hash(forged)
            forged["draft_id"] = f"d-{forged['content_hash'][:8]}-abcd"
            with self.subTest(field=field), self.assertRaises(approvals.ApprovalError):
                approvals.verify_draft(forged, forged["draft_id"], channel_id=autopilot.BUFFER_CHANNEL_ID, repo_root=self.tmp,
                                       raw_prefix=media_selector.RAW_REPO)


class CaptionQualityTests(unittest.TestCase):
    MEDIA = {"mechanisms": ["Portal", "Echo"], "level": 11, "theme": "purple", "mode": "hard", "puzzle_state": "unsolved",
             "type": "image", "description": DESC}

    def problems(self, text, media=None, post_type="challenge"):
        return cq.quality_problems(text, media or self.MEDIA, post_type)

    def test_the_reported_generic_captions_are_rejected(self):
        for text in ("Which ring would you turn first?", "Brand new idea about rings.", "A genuinely new thought about beams."):
            with self.subTest(text=text):
                self.assertTrue(self.problems(text), text)

    def test_filler_phrases_are_rejected_even_with_a_media_detail_present(self):
        for text in ("This Portal level is a total game changer!", "Check this out: a Portal.", "Amazing Portal vibes",
                     "You won't believe this Echo", "Portal. Stay tuned."):
            with self.subTest(text=text):
                self.assertTrue(any("filler" in p for p in self.problems(text)), (text, self.problems(text)))

    def test_specific_captions_tied_to_the_media_pass(self):
        for text in ("A Portal in a purple level.", "Level 11: the Echo and the Portal share one rim.",
                     "Purple glow, two Portals, one lock left."):
            with self.subTest(text=text):
                self.assertEqual(self.problems(text), [])

    def test_a_caption_must_refer_to_something_about_this_media(self):
        problems = self.problems("A calm evening and a hot drink.")
        self.assertTrue(any("does not refer to anything verified" in p for p in problems), problems)

    def test_a_caption_about_another_level_is_rejected(self):
        self.assertTrue(any("shows level 11" in p for p in self.problems("Level 24 and a Portal.")))

    def test_media_with_no_verified_facts_cannot_carry_a_caption(self):
        problems = cq.anchor_problems("A Portal in a purple level.", {"type": "image"}, "challenge")
        self.assertTrue(any("no verified facts" in p for p in problems), problems)

    def test_progress_posts_must_state_a_verified_fact(self):
        facts = [{"id": "f", "kind": "progress", "statement": "Ringshift now has seventy five levels.", "source": "list"}]
        self.assertEqual(cq.anchor_problems("Seventy five levels now.", self.MEDIA, "progress", facts), [])
        self.assertTrue(cq.anchor_problems("A Portal in a purple level.", self.MEDIA, "progress", facts))

    def test_reworded_and_re_hashtagged_captions_are_the_same_idea(self):
        base = "The Portal sends the beam to the purple lock."
        for other in ("To the purple lock, the Portal sends the beam!", "the portal sends the beam to the purple lock #Ringshift #puzzle",
                      "The Portal sends a beam to a purple lock."):
            with self.subTest(other=other):
                self.assertTrue(cq.same_idea(base, other)[0], other)

    def test_one_question_reworded_is_a_repeat(self):
        self.assertTrue(cq.same_idea("Which ring would you turn first?", "Which ring would you rotate first?")[0])
        self.assertTrue(cq.same_idea("Which ring would you turn first?", "which ring would you turn first")[0])

    def test_different_points_are_not_repeats(self):
        self.assertFalse(cq.same_idea("A Portal in a purple level.", "Level 11 puts an Echo next to a Block.")[0])

    def test_a_batch_is_reduced_to_genuinely_distinct_candidates(self):
        batch = ["The Portal sends the beam to the purple lock.", "To the purple lock, the Portal sends the beam!",
                 "The Portal sends a beam to a purple lock.", "Level 11 puts an Echo beside a Block."]
        kept, dropped = cq.distinct_candidates(batch)
        self.assertEqual(kept, [batch[0], batch[3]])
        self.assertEqual(len(dropped), 2)
        self.assertTrue(all("another candidate" in why for _, why in dropped))

    def test_candidates_repeating_earlier_posts_are_dropped(self):
        kept, dropped = cq.distinct_candidates(["Level 11 puts an Echo beside a Block."], ["Level 11 puts an echo beside a block"])
        self.assertEqual(kept, [])
        self.assertIn("an earlier post", dropped[0][1])

    def test_the_candidate_limit_is_enforced(self):
        kept, dropped = cq.distinct_candidates(["Purple Portal glow at the rim.", "An Echo copy beside the Block.",
                                                "Level 11 has a Portal, a Block and an Echo.", "Two Blocks guard the Portal."], limit=3)
        self.assertEqual(len(kept), 3)
        self.assertTrue(any("limit" in why for _, why in dropped))

    def test_empty_candidates_are_dropped(self):
        self.assertEqual(cq.distinct_candidates(["", "  ", "#tag"])[0], [])


class PipelineLimitTests(OfflineTestCase):
    """The preview pipeline: generic captions never reach the critic; one run issues at most one approval code."""

    def setUp(self):
        super().setUp()
        self.candidates = mock.patch.object(autopilot, "generate_candidates").start()
        self.critic = mock.patch.object(autopilot, "critique_post", return_value=CRITIC_YES).start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"tags": ["portal"], "description": DESC}}), encoding="utf-8")
        self.draft_path = self.tmp / "d.json"

    def run_main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            autopilot.main(["--dry-run", "--draft-file", str(self.draft_path)])
        return out.getvalue()

    def test_the_reported_generic_drafts_never_reach_the_critic(self):
        self.candidates.return_value = ["Which ring would you turn first?", "Brand new idea about rings.", "A genuinely new thought about beams."]
        output = self.run_main()
        self.assertIn("NO CANDIDATE PASSED", output)
        self.critic.assert_not_called()
        self.assertNotIn("draft_id", json.loads(self.draft_path.read_text(encoding="utf-8")))

    def test_three_rewordings_of_one_idea_cost_one_critic_call_and_make_one_draft(self):
        self.candidates.return_value = ["The Portal sends the beam to the purple lock.", "To the purple lock, the Portal sends the beam!",
                                        "The Portal sends a beam to a purple lock."]
        output = self.run_main()
        self.assertEqual(self.critic.call_count, 1)
        self.assertEqual(output.count("DRAFT ID (approval code)"), 1)
        self.assertEqual(len(autopilot.DRAFTS_THIS_RUN), 1)

    def test_more_candidates_than_the_limit_are_never_all_reviewed(self):
        many = [f"{w} sits next to the Portal." for w in ("A purple Block", "An Echo", "The rim lock", "One Mirror", "The Prism", "A Portal pair")]
        self.candidates.return_value = many
        self.critic.return_value = CRITIC_YES.replace("KEEP: YES", "KEEP: NO")
        self.run_main()
        self.assertLessEqual(self.critic.call_count, autopilot.MAX_CRITIC_CALLS)
        attempts = json.loads(self.draft_path.read_text(encoding="utf-8"))["attempts"]
        self.assertLessEqual(len([a for a in attempts if a["outcome"] in ("critic KEEP", "critic rejected")]), autopilot.MAX_CRITIC_CALLS)

    def test_the_critic_budget_is_a_hard_limit(self):
        self.assertEqual(autopilot.MAX_CRITIC_CALLS, autopilot.CANDIDATE_COUNT + 1)
        self.assertEqual(autopilot.MAX_DRAFTS_PER_RUN, 1)

    def test_a_run_never_issues_a_second_approval_code(self):
        self.candidates.return_value = [GOOD]
        self.run_main()
        media = {"file": self.image_rel, "type": "image", "url": media_selector.raw_url(self.image_rel)}
        with self.assertRaises(RuntimeError) as raised:
            autopilot.build_draft("A Portal at the rim.", media, autopilot.POST_TYPES[0], 40, "c", [], {"status": "fixture"})
        self.assertIn("never issues more", str(raised.exception))

    def test_a_new_run_starts_with_a_clean_limit(self):
        self.candidates.return_value = [GOOD]
        self.run_main()
        self.candidates.return_value = ["Purple light behind the Portal."]
        self.run_main()
        self.assertEqual(len(autopilot.DRAFTS_THIS_RUN), 1)

    def test_a_caption_for_another_level_is_rejected_before_the_critic(self):
        mm.save_manifest({"schema": mm.SCHEMA, "items": [
            mm.make_item(self.image_rel, base_dir=self.tmp, media_type="image", description=DESC, puzzle_state="unsolved",
                         mode="hard", level=11, technical={"width": 320, "height": 320, "aspect": "other", "bytes": 3, "duration": None,
                                                            "sha256": None},
                         validation={"status": "ok", "checked_at": None, "problems": []}, hashes=[])]},
            self.tmp / "media" / "manifest.json")
        self.candidates.return_value = ["Level 24 and a Portal."]
        output = self.run_main()
        self.assertIn("shows level 11", output)
        self.critic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
