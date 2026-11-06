import json
import tempfile
import unittest
from pathlib import Path

from scripts.reddit_repurpose import (
    RedditDraftError, build_draft, load_source, score_opportunity,
)


class RedditRepurposeTests(unittest.TestCase):
    def test_opportunity_rewards_high_score_low_comments_and_fit(self):
        strong = score_opportunity(1200, 4, 1.0, 3)
        weak = score_opportunity(40, 80, 0.2, 300)
        self.assertGreater(strong["score"], weak["score"])
        self.assertIn("manually supplied", strong["method"])

    def test_opportunity_clamps_bad_ranges(self):
        result = score_opportunity(-5, -2, 4, -10)
        self.assertEqual(result["inputs"]["upvotes_or_score"], 0)
        self.assertEqual(result["inputs"]["comments"], 0)
        self.assertEqual(result["inputs"]["topic_fit"], 1.0)
        self.assertEqual(result["inputs"]["age_hours"], 0.0)

    def test_draft_holds_when_rules_are_unknown(self):
        post = {"number": 4, "post_type": "dev_story", "text": "X source"}
        media = {
            "path": "media/a.png", "url": "https://example.com/a.png", "sha256": "abc",
            "game": {"mode": "hard", "level": 3}, "mechanisms": ["Mirror"],
            "puzzle_state": "unsolved", "description": "A verified puzzle capture."
        }
        community = {"name": "IndieGaming", "rules_reviewed": False,
                     "self_promo_allowed": None, "allow_links": None}
        result = build_draft(post, media, "IndieGaming", community)
        self.assertEqual(result["status"], "HOLD_RULES_UNVERIFIED")
        self.assertTrue(result["must_be_submitted_manually"])
        self.assertIn("Disclosure:", result["body"])
        self.assertNotIn("Free browser demo:", result["body"])

    def test_link_only_when_community_rules_allow_it(self):
        post = {"number": 4, "post_type": "dev_story", "text": "X source"}
        media = {"path": "media/a.png", "url": "https://example.com/a.png", "sha256": "abc",
                 "game": {"mode": "hard", "level": 3}, "mechanisms": ["Mirror"],
                 "puzzle_state": "solved", "description": "Verified."}
        community = {"name": "IndieGaming", "rules_reviewed": True,
                     "self_promo_allowed": True, "allow_links": True}
        result = build_draft(post, media, "IndieGaming", community)
        self.assertEqual(result["status"], "READY_FOR_HUMAN_REVIEW")
        self.assertIn("Free browser demo:", result["body"])

    def test_source_rejects_missing_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "posts.json").write_text(json.dumps([{"number": 1, "media_file": "missing.png"}]))
            (root / "manifest.json").write_text(json.dumps({"items": []}))
            with self.assertRaises(RedditDraftError):
                load_source(1, root / "posts.json", root / "manifest.json")


if __name__ == "__main__":
    unittest.main()
