"""New autopilot behaviour: 429 retry, media URL pre-flight, rule screening, post log, dry-run, media-first."""

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


def head_response(status=200, content_type="image/png", length=None):
    response = mock.Mock()
    response.status_code = status
    response.headers = {"content-type": content_type}
    if length is not None:
        response.headers["content-length"] = str(length)
    return response


class RateLimitRetryTests(OfflineTestCase):
    def send(self):
        return autopilot.post_to_buffer("https://buffer.invalid", {}, {})

    def test_429_is_retried_then_succeeds(self):
        limited = FakeResponse(429, text="slow down")
        limited.headers = {"Retry-After": "3"}
        self.post.side_effect = [limited, queued_response()]
        response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.post.call_count, 2)

    def test_429_gives_up_after_the_attempt_limit(self):
        limited = FakeResponse(429, text="slow down")
        limited.headers = {"Retry-After": "1"}
        self.post.side_effect = None
        self.post.return_value = limited
        self.assertEqual(self.send().status_code, 429)
        self.assertEqual(self.post.call_count, autopilot.BUFFER_RETRY_ATTEMPTS)

    def test_other_failures_are_never_retried(self):
        for status in (500, 502, 503, 400, 401):
            with self.subTest(status=status):
                self.post.reset_mock()
                self.post.side_effect = None
                self.post.return_value = FakeResponse(status, text="x")
                self.assertEqual(self.send().status_code, status)
                self.assertEqual(self.post.call_count, 1)

    def test_a_timeout_is_not_retried(self):
        import requests
        self.post.side_effect = requests.Timeout("slow")
        with self.assertRaises(requests.Timeout):
            self.send()
        self.assertEqual(self.post.call_count, 1)

    def test_wait_is_capped(self):
        limited = FakeResponse(429)
        limited.headers = {"Retry-After": "99999"}
        self.assertEqual(autopilot._retry_after_seconds(limited), autopilot.BUFFER_MAX_RETRY_WAIT)
        limited.headers = {}
        self.assertEqual(autopilot._retry_after_seconds(limited), 30)


class MediaPreflightTests(OfflineTestCase):
    media = {"url": "https://example.invalid/a.mp4", "bytes": 1000, "file": "a.mp4", "type": "video"}

    def setUp(self):
        super().setUp()
        mock.patch.object(autopilot, "MEDIA_CHECK_MODE", "strict").start()

    def check(self, response):
        self.head.side_effect = None
        self.head.return_value = response
        with contextlib.redirect_stdout(io.StringIO()):
            autopilot.verify_media_url(self.media)

    def test_good_url_passes_including_github_octet_stream(self):
        self.check(head_response(200, "application/octet-stream", 1000))
        self.check(head_response(200, "video/mp4", 1000))

    def test_missing_file_blocks_the_post(self):
        with self.assertRaisesRegex(autopilot.MediaUnavailableError, "404"):
            self.check(head_response(404, "text/plain"))

    def test_redirect_blocks_the_post(self):
        with self.assertRaises(autopilot.MediaUnavailableError):
            self.check(head_response(302, "text/html"))

    def test_html_page_blocks_the_post(self):
        with self.assertRaisesRegex(autopilot.MediaUnavailableError, "not an image or video"):
            self.check(head_response(200, "text/html", 1000))

    def test_stale_size_blocks_the_post(self):
        with self.assertRaisesRegex(autopilot.MediaUnavailableError, "bytes"):
            self.check(head_response(200, "video/mp4", 999))

    def test_network_error_blocks_the_post(self):
        import requests
        self.head.side_effect = requests.ConnectionError("down")
        with self.assertRaisesRegex(autopilot.MediaUnavailableError, "no post was created"):
            with contextlib.redirect_stdout(io.StringIO()):
                autopilot.verify_media_url(self.media)

    def test_preflight_failure_means_buffer_is_never_called(self):
        before = self.state()
        self.head.side_effect = None
        self.head.return_value = head_response(404, "text/plain")
        with self.assertRaises(autopilot.MediaUnavailableError):
            with contextlib.redirect_stdout(io.StringIO()):
                autopilot.send_to_buffer("A fresh post about prisms.")
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.state(), before)


class SendToBufferTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.post.side_effect = None
        self.post.return_value = queued_response()

    def send(self, text="A fresh post about prisms.", post_type=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return autopilot.send_to_buffer(text, None, post_type)

    def test_success_writes_a_post_log_entry(self):
        self.send(post_type=autopilot.POST_TYPES[2])
        log = json.loads(self.post_log_file.read_text(encoding="utf-8"))
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["buffer_id"], "post-1")
        self.assertEqual(log[0]["number"], 4)
        self.assertEqual(log[0]["post_type"], "mechanic")
        self.assertEqual(log[0]["confirmed_assets"], 1)
        self.assertNotIn(support.BUFFER_KEY, json.dumps(log))

    def test_same_text_is_refused_the_second_time(self):
        self.send()
        with self.assertRaisesRegex(RuntimeError, "already in data/post_log.json"):
            self.send("a fresh POST about prisms!")
        self.assertEqual(self.post.call_count, 1)

    def test_failure_writes_no_log_entry(self):
        self.post.return_value = FakeResponse(500, text="boom")
        with self.assertRaises(RuntimeError):
            self.send()
        self.assertFalse(self.post_log_file.exists())


class ScreeningTests(OfflineTestCase):
    project = {"project_url": None}

    def setUp(self):
        super().setUp()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()

    def test_clean_post_passes(self):
        self.assertIsNone(autopilot.screen_candidate("Can you spot where the beam bends?", [], self.project))

    def test_invented_claims_are_rejected(self):
        for text in ("Over 5,000 players love this puzzle.", "Now available on Steam!", "An award-winning puzzle game."):
            with self.subTest(text=text):
                reason = autopilot.screen_candidate(text, [], self.project)
                self.assertIn("failed post rules", reason)

    def test_urls_and_mentions_are_rejected(self):
        self.assertIn("failed", autopilot.screen_candidate("Look https://example.com/x", [], self.project))
        self.assertIn("failed", autopilot.screen_candidate("Thanks @someone for the idea", [], self.project))

    def test_project_url_is_allowed_only_when_configured(self):
        project = {"project_url": "https://example.com/ringshift"}
        self.assertIsNone(autopilot.screen_candidate(
            "Have a look at the project page https://example.com/ringshift", [], project))
        self.assertIn("other than", autopilot.screen_candidate(
            "Have a look https://example.com/other", [], project))

    def test_near_duplicates_are_rejected(self):
        recent = ["A Portal in a purple level."]
        reason = autopilot.screen_candidate("A Portal in a purple level. Which lock?", recent, self.project)
        self.assertTrue("too similar" in reason or "repeats an earlier post" in reason, reason)

    def test_the_same_idea_reworded_is_a_repeat_even_though_its_hash_differs(self):
        recent = ["The Portal sends the beam to the purple lock."]
        reason = autopilot.screen_candidate("To the purple lock, the Portal sends the beam!", recent, self.project)
        self.assertTrue("repeats an earlier post" in reason or "too similar" in reason, reason)
        import caption_quality
        self.assertTrue(caption_quality.same_idea(recent[0], "To the purple lock, the Portal sends the beam!")[0])

    def test_exact_duplicates_are_rejected(self):
        mock.patch.object(autopilot, "posts_history", "## Post 001\n\nSame words here today.\n").start()
        self.assertIn("duplicate", autopilot.screen_candidate("same words here today", [], self.project))


class PostTypeTests(OfflineTestCase):
    def test_types_rotate_with_the_counter(self):
        project = {"project_url": None}
        ids = [autopilot.pick_post_type(n, project)["id"] for n in range(10)]
        self.assertEqual(ids[:9], ["challenge", "hook", "mechanic", "observation", "visual",
                                   "solution", "moment", "feedback", "discovery"])
        # invite needs a URL, progress / behind_scenes need verified facts: all three are skipped, so it wraps round
        self.assertEqual(ids[9], "challenge")

    def test_invitation_type_needs_a_configured_url(self):
        project = {"project_url": "https://example.com/r"}
        self.assertEqual(autopilot.pick_post_type(9, project)["id"], "invite")

    def test_progress_types_need_verified_facts(self):
        project = {"project_url": None}
        facts = [{"id": "f1", "kind": "progress", "statement": "Ten new levels were added.", "source": "changelog"}]
        ids = [autopilot.pick_post_type(n, project, facts)["id"] for n in range(10)]
        self.assertIn("progress", ids)
        self.assertNotIn("behind_scenes", ids)

    def test_project_file_only_accepts_https(self):
        path = self.tmp / "data" / "project.json"
        path.write_text(json.dumps({"project_url": "http://x"}), encoding="utf-8")
        self.assertIsNone(autopilot.load_project()["project_url"])
        path.write_text(json.dumps({"project_url": "https://x.example/y"}), encoding="utf-8")
        self.assertEqual(autopilot.load_project()["project_url"], "https://x.example/y")
        path.write_text("not json", encoding="utf-8")
        self.assertIsNone(autopilot.load_project()["project_url"])


class DryRunAndMediaFirstTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.candidates = mock.patch.object(autopilot, "generate_candidates").start()
        mock.patch.object(autopilot, "critique_post", return_value=CRITIC_YES).start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.tags_file.write_text(json.dumps({
            self.image_rel: {"tags": ["black hole"], "description": "A portal level with two black holes."},
            self.video_rel: {"tags": ["prism"]},
        }), encoding="utf-8")

    def run_main(self, argv=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            autopilot.main(argv)
        return out.getvalue()

    def test_dry_run_sends_and_saves_nothing(self):
        self.candidates.return_value = ["A Portal in a purple level."]
        before = self.state()
        output = self.run_main(["--dry-run"])
        self.assertIn("DRAFT COMPLETE", output)
        self.assertIn("A portal level with two black holes.", output)
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.posts_file.exists())
        self.assertFalse(self.post_log_file.exists())

    def test_dry_run_does_not_need_a_buffer_key(self):
        self.candidates.return_value = ["A Portal in a purple level."]
        with mock.patch.dict(os.environ, {"BUFFER_API_KEY": ""}):
            self.assertIn("DRAFT COMPLETE", self.run_main(["--dry-run"]))

    def test_media_is_chosen_before_the_text_is_written(self):
        self.candidates.return_value = ["A Portal in a purple level."]
        self.run_main(["--dry-run"])
        args, _ = self.candidates.call_args
        media, post_type, project = args[:3]
        self.assertEqual(media["file"], self.image_rel)      # the one with a verified description
        self.assertEqual(post_type["id"], "observation")      # counter is 3 in these tests
        self.assertEqual(project, {"project_url": None})

    def test_rule_breaking_candidates_are_never_sent(self):
        bad = ["Over 10,000 players love this!", "Now available on Steam!"]
        self.candidates.return_value = bad
        output = self.run_main(["--dry-run"])
        self.assertFalse(set(bad) & set(support.critic_texts(autopilot.critique_post)))
        winner = output.split("--- WINNER ---")[1] if "--- WINNER ---" in output else ""
        self.assertFalse(any(text in winner for text in bad))
        self.assertEqual(self.post.call_count, 0)

    def test_the_selected_media_is_what_the_draft_would_post(self):
        self.candidates.return_value = ["A Portal in a purple level."]
        draft_path = self.tmp / "draft.json"
        self.run_main(["--dry-run", "--draft-file", str(draft_path)])
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        assets = draft["buffer_assets_payload"]
        self.assertEqual(len(assets), 1)
        self.assertIn("black_hole.png", assets[0]["image"]["url"])
        self.assertEqual(self.post.call_count, 0)


class PromptContextTests(unittest.TestCase):
    def test_glitch_text_is_cleaned_for_the_prompt(self):
        messy = "Hel̸̶lo ̀́ wor̴ld"
        self.assertEqual(autopilot.clean_for_prompt(messy), "Hello ́ world".replace(" ́", ""))

    def test_parse_candidates_strips_brackets(self):
        text = "CANDIDATE 1:\n[First one]\n\nCANDIDATE 2:\n[Second]\n\nCANDIDATE 3:\n[Third]"
        self.assertEqual(autopilot.parse_candidates(text), ["First one", "Second", "Third"])


if __name__ == "__main__":
    unittest.main()
