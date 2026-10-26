"""post_rules: X-weighted length, claim lint, near-duplicate detection."""

import unittest

import support  # noqa: F401  (puts scripts/ on sys.path and installs the network guard imports)
import post_rules as rules


class WeightedLengthTests(unittest.TestCase):
    def test_plain_ascii_counts_one_each(self):
        self.assertEqual(rules.weighted_length("a" * 280), 280)
        self.assertEqual(rules.weighted_length(""), 0)

    def test_urls_always_cost_23(self):
        short = rules.weighted_length("see https://t.co/x")
        long = rules.weighted_length("see https://example.com/" + "a" * 200)
        self.assertEqual(short, long)
        self.assertEqual(short, len("see ") + 23)

    def test_wide_characters_and_emoji_cost_two(self):
        self.assertEqual(rules.weighted_length("日本"), 4)
        self.assertEqual(rules.weighted_length("🧩"), 2)

    def test_accented_latin_costs_one(self):
        self.assertEqual(rules.weighted_length("café"), 4)


class ValidatePostTests(unittest.TestCase):
    def test_good_post_passes(self):
        self.assertEqual(rules.validate_post("Would you have seen this one?\n\n#Ringshift"), [])

    def test_empty_post_fails(self):
        self.assertTrue(rules.validate_post("   "))

    def test_too_long_by_weight_not_by_characters(self):
        # 150 emoji is 150 characters but 300 weighted.
        problems = rules.validate_post("🧩" * 150)
        self.assertTrue(any("too long" in p for p in problems))

    def test_exactly_280_is_allowed(self):
        self.assertEqual(rules.validate_post("a" * 280), [])
        self.assertTrue(rules.validate_post("a" * 281))

    def test_hashtag_limit(self):
        self.assertEqual(rules.validate_post("one #Ringshift #indiegames"), [])
        self.assertTrue(rules.validate_post("one #a #b #c"))

    def test_mentions_are_rejected(self):
        self.assertTrue(any("@mention" in p for p in rules.validate_post("hello @someone")))

    def test_urls_need_a_configured_project_url(self):
        self.assertTrue(rules.validate_post("play https://example.com"))
        self.assertEqual(rules.validate_post("play https://example.com", allow_urls=True), [])

    def test_banned_openers(self):
        for text in ("Equip the UFO and watch", "Try this one", "Check out this level", "Ringshift is a puzzle"):
            with self.subTest(text=text):
                self.assertTrue(any("opener" in p for p in rules.validate_post(text)))

    def test_equipped_is_not_the_banned_imperative(self):
        self.assertEqual(rules.validate_post("Equipped the UFO power for this one."), [])

    def test_unsupported_claims_are_flagged(self):
        samples = [
            "Over 500 players have tried this level.",
            "Our award-winning puzzle game",
            "This is going viral",
            "Players love the prism levels",
            "Out now on Steam",
            "Coming soon",
            "Five-star reviews all round",
            "Now with 1,000 wishlists",
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertTrue(rules.find_unsupported_claims(text), text)

    def test_honest_posts_are_not_flagged(self):
        samples = [
            "I thought this level was broken until I noticed the mirror angle.",
            "Prisms split a beam in two. Can you light both locks?",
            "Three rings, one lock, one solution. Would you have seen it?",
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(rules.find_unsupported_claims(text), [])


class SimilarityTests(unittest.TestCase):
    def test_identical_ignoring_case_and_punctuation(self):
        self.assertGreaterEqual(rules.similarity("Would you have seen this?", "would you have SEEN this"), 0.99)

    def test_unrelated_posts_score_low(self):
        self.assertLess(
            rules.similarity("The prism splits my beam in two.", "Rain theme looks wild with the lock rim."),
            0.5,
        )

    def test_reworded_copy_is_a_near_duplicate(self):
        old = "I thought this level was broken until I finally noticed the mirror angle."
        new = "I thought this level was broken. Then I noticed the mirror angle."
        flagged, score, other = rules.is_near_duplicate(new, [old], threshold=0.7)
        self.assertTrue(flagged, score)
        self.assertEqual(other, old)

    def test_short_post_contained_in_long_post(self):
        self.assertTrue(rules.is_near_duplicate("There is only one solution.", ["There is only one solution. Find it?"])[0])

    def test_no_history_is_never_a_duplicate(self):
        self.assertFalse(rules.is_near_duplicate("anything", [])[0])


class ExtractTests(unittest.TestCase):
    def test_extract_generated_posts_handles_old_and_new_headers(self):
        markdown = (
            "\\# RINGSHIFT GENERATED POSTS\n\n\\## Post 001\n\nFirst post.\n\n#Ringshift\n\n"
            "## Post 002\n\nSecond post.\n\n### Critic\n\nKEEP: YES\n\n"
            "## Post 003\n\nThird post.\n\n### Critic\n\nKEEP: YES\n"
        )
        posts = rules.extract_generated_posts(markdown)
        self.assertEqual(posts, ["First post.\n\n#Ringshift", "Second post.", "Third post."])

    def test_extract_analytics_posts(self):
        markdown = (
            "# RINGSHIFT — X ANALYTICS\n\n## Post\n\nID: 1\n\n### Post\n\nHello there.\n\n#Ringshift\n\n"
            "### Metrics\n\nNo metrics.\n\n---\n\n## Post\n\nID: 2\n\n### Post\n\nSecond.\n\n### Metrics\n\n"
        )
        self.assertEqual(rules.extract_analytics_posts(markdown), ["Hello there.\n\n#Ringshift", "Second."])

    def test_empty_inputs(self):
        self.assertEqual(rules.extract_generated_posts(""), [])
        self.assertEqual(rules.extract_analytics_posts(None), [])


if __name__ == "__main__":
    unittest.main()
