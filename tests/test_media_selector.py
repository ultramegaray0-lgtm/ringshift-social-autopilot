import json
import unittest

import support
from support import OfflineTestCase
import media_selector as ms


class TopicMatchingTests(unittest.TestCase):
    """post_topics() must match whole words only."""

    def test_block_does_not_trigger_lock(self):
        topics = ms.post_topics("A very thin line between placing a block and trapping the beam")
        self.assertIn("block", topics)
        self.assertNotIn("lock", topics)

    def test_lock_still_matches_as_a_word_and_plural(self):
        self.assertIn("lock", ms.post_topics("the beam finally touches the rim lock"))
        self.assertIn("lock", ms.post_topics("three locks to open"))

    def test_coin_does_not_match_inside_other_words(self):
        self.assertNotIn("coin", ms.post_topics("what a coincidence"))
        self.assertIn("coin", ms.post_topics("grab the coin"))
        self.assertIn("coin", ms.post_topics("grab every coin."))

    def test_plurals_hyphens_case_and_punctuation(self):
        self.assertIn("black hole", ms.post_topics("PORTALS, everywhere!"))
        self.assertIn("black hole", ms.post_topics("a Black-Hole puzzle"))
        self.assertIn("block", ms.post_topics("a half-block bounce"))
        self.assertIn("satellite", ms.post_topics("the satellite's echo"))
        self.assertIn("mirror", ms.post_topics("Mirrors?"))

    def test_no_topic_in_unrelated_text(self):
        self.assertEqual(ms.post_topics("Nothing relevant here at all"), set())
        self.assertEqual(ms.post_topics(""), set())


class SelectionTests(OfflineTestCase):
    def write_tags(self, tags):
        self.tags_file.write_text(json.dumps(tags), encoding="utf-8")

    def test_tagged_file_is_chosen_on_a_topic_match(self):
        self.write_tags({self.video_rel: ["black hole"], self.image_rel: []})

        result = ms.select_media("A black hole portal puzzle")

        self.assertEqual(result["file"], self.video_rel)
        self.assertEqual(result["match"], "topic match")
        self.assertEqual(result["type"], "video")

    def test_a_block_post_does_not_match_media_tagged_only_lock(self):
        self.write_tags({self.video_rel: ["lock"], self.image_rel: []})

        result = ms.select_media("placing a block")

        self.assertEqual(result["match"], "random fallback")
        self.assertEqual(result["topics"], ["block"])

    def test_untagged_media_falls_back_to_random(self):
        result = ms.select_media("A black hole portal puzzle")
        self.assertEqual(result["match"], "random fallback")

    def test_selection_never_writes_history(self):
        ms.select_media("anything")
        self.assertFalse(self.history_file.exists())

    def test_recently_used_files_are_skipped(self):
        self.history_file.write_text(json.dumps([self.image_rel]), encoding="utf-8")
        self.assertEqual(ms.select_media("anything")["file"], self.video_rel)

    def test_url_percent_encodes_spaces(self):
        self.history_file.write_text(json.dumps([self.image_rel]), encoding="utf-8")
        self.assertTrue(ms.select_media("x")["url"].endswith("/media/gameplay/clip%20one.mp4"))

    def test_record_media_appends_and_keeps_the_last_100(self):
        self.history_file.write_text(
            json.dumps([f"media/old_{i}.png" for i in range(150)]), encoding="utf-8"
        )

        ms.record_media({"file": self.image_rel})

        history = json.loads(self.history_file.read_text(encoding="utf-8"))
        self.assertEqual(len(history), 100)
        self.assertEqual(history[-1], self.image_rel)


if __name__ == "__main__":
    unittest.main()
