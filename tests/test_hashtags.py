"""Hashtag top-up (scripts/hashtags.py): every caption reaches 3 relevant tags without breaking any post rule."""

import unittest
import unittest.mock
from datetime import datetime, timezone

import support  # noqa: F401  (blocks network, sets up paths)
import hashtags
import post_rules

THU = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)      # a Thursday in Riyadh
SAT = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
WED = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)

# Real captions that went out with only two tags (data/post_log.json, Oct 31 - Nov 3).
REAL = [
    "The mirror in this chalkboard setup just sits there waiting for angles to line up. #Ringshift #indiegames",
    "Having the portal, amplifier, and echo mechanics together on a circular puzzle creates fascinating design "
    "challenges when I build Ringshift in the Urushi Tray theme. #Ringshift #gamedev",
]


class CompleteTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch.object(hashtags, "ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_two_tag_posts_reach_three_and_pass_the_post_rules(self):
        for text in REAL:
            out = hashtags.complete(text, now=THU)
            self.assertTrue(out.startswith(text), out)
            self.assertEqual(post_rules.count_hashtags(out), 3, out)
            self.assertEqual(post_rules.validate_post(out), [], out)

    def test_brand_tag_added_when_missing(self):
        out = hashtags.complete("Four rings, one centre.", now=THU)
        self.assertIn("#Ringshift", out)
        self.assertEqual(post_rules.count_hashtags(out), 3)

    def test_enough_tags_is_left_alone(self):
        text = "Bend the light. #Ringshift #puzzlegame #indiedev"
        self.assertEqual(hashtags.complete(text, now=THU), text)

    def test_no_duplicate_tags_case_insensitive(self):
        out = hashtags.complete("Mirrors everywhere. #ringshift #PuzzleGame", now=THU, seed=0)
        tags = [t.lower() for t in post_rules.HASHTAG_RE.findall(out)]
        self.assertEqual(len(tags), len(set(tags)), out)

    def test_never_exceeds_x_length_adds_fewer_instead(self):
        text = "x" * 268 + " #Ringshift"          # 279 weighted: no extra tag fits
        out = hashtags.complete(text, now=THU)
        self.assertLessEqual(post_rules.weighted_length(out), post_rules.X_MAX_WEIGHTED_LENGTH)
        self.assertEqual(out, text)                 # nothing fits; caption unchanged rather than cut

    def test_developer_posts_get_developer_tags(self):
        out = hashtags.complete("I built this level around one prism. #Ringshift", developer_led=True, now=THU)
        extra = set(post_rules.HASHTAG_RE.findall(out)) - {"#Ringshift"}
        self.assertTrue(extra <= set(hashtags.DEVELOPER_TAGS), out)

    def test_screenshot_saturday_only_on_saturday_with_media(self):
        self.assertIn("#ScreenshotSaturday", hashtags.complete("New level. #Ringshift", now=SAT))
        self.assertNotIn("#ScreenshotSaturday", hashtags.complete("New level. #Ringshift", now=THU))
        self.assertNotIn("#ScreenshotSaturday", hashtags.complete("New level. #Ringshift", now=SAT, has_media=False))

    def test_wip_wednesday_only_for_developer_posts(self):
        self.assertIn("#WIPWednesday", hashtags.complete("I made this. #Ringshift", developer_led=True, now=WED))
        self.assertNotIn("#WIPWednesday", hashtags.complete("Look at this. #Ringshift", now=WED))

    def test_seed_rotates_the_tail(self):
        tails = {hashtags.complete("A level. #Ringshift", now=THU, seed=s) for s in range(5)}
        self.assertGreater(len(tails), 1)

    def test_deterministic(self):
        a = hashtags.complete("A level. #Ringshift", now=THU, seed=7)
        b = hashtags.complete("A level. #Ringshift", now=THU, seed=7)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()


class PipelineWiringTests(unittest.TestCase):
    """The draft flow tops captions up before screening, so the queued text is the topped-up one."""

    def test_draft_flow_screens_and_drafts_the_topped_up_caption(self):
        import test_main_flow
        case = test_main_flow.MainFlowTests("test_duplicate_is_skipped_but_a_fresh_candidate_becomes_the_draft")
        case.setUp()
        try:
            with unittest.mock.patch.object(hashtags, "ENABLED", True):
                import json
                fresh = "Two Portals and one purple lock."
                unittest.mock.patch.object(test_main_flow.autopilot, "posts_history", "").start()
                case.candidates.return_value = [fresh]
                draft_path = case.tmp / "d.json"
                error, _ = case.run_main(argv=["--dry-run", "--draft-file", str(draft_path)])
                self.assertIsNone(error)
                text = json.loads(draft_path.read_text(encoding="utf-8"))["text"]
                self.assertTrue(text.startswith(fresh), text)
                self.assertGreaterEqual(post_rules.count_hashtags(text), 3, text)
                self.assertEqual(post_rules.validate_post(text), [], text)
                self.assertIn(text, support.critic_texts(case.critic))     # the critic saw exactly this text
        finally:
            case.doCleanups()
            case.tearDown()
