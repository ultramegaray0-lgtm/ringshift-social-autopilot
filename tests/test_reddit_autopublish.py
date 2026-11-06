import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import requests

from scripts.reddit_autopublish import RedditPublishError, check_state, run, validate_publish_gates


GOOD_ENV = {
    "REDDIT_API_APPROVED": "true",
    "REDDIT_COMMERCIAL_APPROVED": "true",
    "REDDIT_AUTOPUBLISH": "on",
    "REDDIT_APPROVAL_REFERENCE": "written-approval-record",
    "REDDIT_USERNAME": "ringshiftdev",
    "REDDIT_CLIENT_ID": "client-id",
    "REDDIT_CLIENT_SECRET": "client-secret",
    "REDDIT_REFRESH_TOKEN": "refresh-token",
}

GOOD_CONFIG = {
    "enabled": True,
    "api_access_approved": True,
    "commercial_use_approved": True,
    "approval_reference": "written-approval-record",
    "target_subreddit": "IndieGaming",
}


class RedditAutopublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.posts = self.root / "posts.json"
        self.manifest = self.root / "manifest.json"
        self.communities = self.root / "communities.json"
        self.state = self.root / "state.json"
        self.automation = self.root / "automation.json"
        self.posts.write_text(json.dumps([{
            "number": 7, "post_type": "dev_story", "text": "X source",
            "media_file": "media/a.png", "queued_at": "2026-11-05T10:00:00+00:00",
        }]))
        self.manifest.write_text(json.dumps({"items": [{
            "path": "media/a.png", "url": "https://cdn.example/a.png", "sha256": "abc123",
            "game": {"mode": "hard", "level": 3, "theme": "Test theme"},
            "mechanisms": ["Mirror"], "puzzle_state": "unsolved",
            "verification": {"status": "verified"}, "validation": {"status": "ok"},
        }]}))
        self.community = {
            "name": "IndieGaming", "rules_reviewed": True,
            "self_promo_allowed": True, "allow_links": True,
            "autopost_allowed": True, "rules_url": "https://www.reddit.com/r/IndieGaming/about/rules",
            "rules_reviewed_at": "2026-11-06",
            "autopost_permission_reference": "https://www.reddit.com/message/moderator-permission",
        }
        self.communities.write_text(json.dumps({"communities": [self.community]}))
        self.automation.write_text(json.dumps(GOOD_CONFIG))

    def tearDown(self):
        self.temp.cleanup()

    def test_publish_gate_requires_every_approval(self):
        with self.assertRaisesRegex(RedditPublishError, "REDDIT_API_APPROVED"):
            validate_publish_gates(self.community, {}, GOOD_CONFIG)
        validate_publish_gates(self.community, GOOD_ENV, GOOD_CONFIG)

    def test_publish_gate_requires_explicit_community_autopost_permission(self):
        community = dict(self.community, autopost_allowed=False)
        with self.assertRaisesRegex(RedditPublishError, "automated submissions"):
            validate_publish_gates(community, GOOD_ENV, GOOD_CONFIG)

    def test_publish_gate_requires_config_to_be_enabled(self):
        config = dict(GOOD_CONFIG, enabled=False)
        with self.assertRaisesRegex(RedditPublishError, "automation config"):
            validate_publish_gates(self.community, GOOD_ENV, config)

    def test_state_blocks_unresolved_and_recent_posts(self):
        with self.assertRaisesRegex(RedditPublishError, "unresolved"):
            check_state({"submissions": [{"status": "pending"}]}, 7, "IndieGaming")
        recent = {"submissions": [{
            "status": "posted", "published_at": "2026-11-05T10:00:00+00:00",
            "source_x_post_number": 6, "target_subreddit": "Other",
        }]}
        with self.assertRaisesRegex(RedditPublishError, "cooldown"):
            check_state(recent, 7, "IndieGaming", now=datetime(2026, 11, 6, tzinfo=timezone.utc))

    def test_draft_mode_is_offline_and_returns_disclosure(self):
        result = run("draft", "IndieGaming", state_path=self.state, post_log=self.posts,
                     manifest=self.manifest, communities_path=self.communities, environ={})
        self.assertEqual(result["status"], "DRAFT_ONLY")
        self.assertIn("developer of Ringshift", result["body"])
        self.assertFalse(self.state.exists())

    def test_unreviewed_community_can_only_generate_a_held_draft(self):
        self.community.update({
            "rules_reviewed": False, "self_promo_allowed": None,
            "allow_links": None, "rules_url": None, "rules_reviewed_at": None,
            "autopost_permission_reference": None,
        })
        self.communities.write_text(json.dumps({"communities": [self.community]}))
        result = run("draft", "IndieGaming", state_path=self.state, post_log=self.posts,
                     manifest=self.manifest, communities_path=self.communities, environ={})
        self.assertEqual(result["draft_review_status"], "HOLD_RULES_UNVERIFIED")
        self.assertNotIn("Free browser demo:", result["body"])
        self.assertNotIn("Gameplay capture:", result["body"])

    def test_promotion_disallowed_community_gets_no_media_or_demo_link(self):
        self.community["self_promo_allowed"] = False
        self.communities.write_text(json.dumps({"communities": [self.community]}))
        result = run("draft", "IndieGaming", state_path=self.state, post_log=self.posts,
                     manifest=self.manifest, communities_path=self.communities, environ={})
        self.assertEqual(result["draft_review_status"], "HOLD_PROMOTION_NOT_ALLOWED")
        self.assertNotIn("Free browser demo:", result["body"])
        self.assertNotIn("Gameplay capture:", result["body"])

    def test_publish_refuses_before_network_when_community_not_approved(self):
        self.community["autopost_allowed"] = False
        self.communities.write_text(json.dumps({"communities": [self.community]}))
        session = Mock()
        with self.assertRaisesRegex(RedditPublishError, "automated submissions"):
            run("publish", "IndieGaming", state_path=self.state, post_log=self.posts,
                manifest=self.manifest, communities_path=self.communities,
                automation_config_path=self.automation, environ=GOOD_ENV, session=session)
        session.post.assert_not_called()

    def test_ambiguous_network_failure_is_claimed_and_never_retried(self):
        run("prepare", "IndieGaming", state_path=self.state, post_log=self.posts,
            manifest=self.manifest, communities_path=self.communities,
            automation_config_path=self.automation, environ=GOOD_ENV)
        session = Mock()
        session.post.side_effect = requests.Timeout("timed out")
        with self.assertRaises(requests.Timeout):
            run("publish", "IndieGaming", state_path=self.state, post_log=self.posts,
                manifest=self.manifest, communities_path=self.communities,
                automation_config_path=self.automation, environ=GOOD_ENV, session=session,
                continue_pending=True)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["submissions"][0]["status"], "unknown")
        with self.assertRaisesRegex(RedditPublishError, "unresolved"):
            check_state(state, 8, "IndieGaming")

    def test_successful_publish_records_reddit_post_and_permalink(self):
        token_response = Mock()
        token_response.raise_for_status.return_value = None
        token_response.json.return_value = {"access_token": "fake-access-token"}
        submit_response = Mock()
        submit_response.raise_for_status.return_value = None
        submit_response.json.return_value = {
            "json": {"errors": [], "data": {
                "name": "t3_test123", "url": "https://www.reddit.com/r/IndieGaming/comments/test123/example/",
            }}
        }
        now = datetime(2026, 11, 6, tzinfo=timezone.utc)
        run("prepare", "IndieGaming", state_path=self.state, post_log=self.posts,
            manifest=self.manifest, communities_path=self.communities,
            automation_config_path=self.automation, environ=GOOD_ENV, now=now)
        session = Mock()
        session.post.side_effect = [token_response, submit_response]
        result = run("publish", "IndieGaming", state_path=self.state, post_log=self.posts,
                     manifest=self.manifest, communities_path=self.communities,
                     automation_config_path=self.automation, environ=GOOD_ENV, session=session,
                     now=now, continue_pending=True)
        self.assertEqual(result["status"], "POSTED")
        self.assertEqual(result["reddit_id"], "t3_test123")
        state = json.loads(self.state.read_text())
        self.assertEqual(state["submissions"][0]["status"], "posted")
        self.assertEqual(session.post.call_count, 2)

    def test_prepare_persists_pending_claim_without_calling_reddit(self):
        session = Mock()
        result = run("prepare", "IndieGaming", state_path=self.state, post_log=self.posts,
                     manifest=self.manifest, communities_path=self.communities,
                     automation_config_path=self.automation, environ=GOOD_ENV, session=session)
        self.assertEqual(result["status"], "PENDING_CLAIM_RECORDED")
        self.assertEqual(json.loads(self.state.read_text())["submissions"][0]["status"], "pending")
        session.post.assert_not_called()

    def test_publish_refuses_without_precommitted_pending_claim(self):
        session = Mock()
        with self.assertRaisesRegex(RedditPublishError, "pre-committed pending claim"):
            run("publish", "IndieGaming", state_path=self.state, post_log=self.posts,
                manifest=self.manifest, communities_path=self.communities,
                automation_config_path=self.automation, environ=GOOD_ENV,
                session=session, continue_pending=True)
        session.post.assert_not_called()

    def test_source_cannot_be_submitted_twice(self):
        state = {"submissions": [{
            "source_x_post_number": 7, "target_subreddit": "IndieGaming",
            "status": "posted", "published_at": "2026-10-01T00:00:00+00:00",
        }]}
        with self.assertRaisesRegex(RedditPublishError, "already been used"):
            check_state(state, 7, "IndieGaming", now=datetime(2026, 11, 6, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
