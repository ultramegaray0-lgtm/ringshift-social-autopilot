"""After the itch.io launch: posts may carry the itch page link and name itch.io; other storefronts stay refused."""

import unittest

import support  # noqa: F401
import post_rules

ITCH = "https://ringshift.itch.io/ringshift"


class ItchLinkTests(unittest.TestCase):
    def test_the_project_link_alone_is_not_a_storefront_claim(self):
        text = f"Four rings, one moment where every Lock lights. Play it here: {ITCH} #Ringshift #puzzlegame #indiegame"
        self.assertEqual(post_rules.validate_post(text, allow_urls=True, project_url=ITCH), [])
        # even without the project URL argument, a link is no longer read as a claim
        self.assertEqual(post_rules.find_unsupported_claims(text), [])

    def test_naming_itch_io_is_allowed_only_when_the_project_is_on_itch(self):
        text = f"Ringshift plays free in your browser on itch.io: {ITCH} #Ringshift #puzzlegame #webgames"
        self.assertEqual(post_rules.find_unsupported_claims(text, project_url=ITCH), [])
        self.assertIn("storefront or platform not in the knowledge base", post_rules.find_unsupported_claims(text))

    def test_other_storefronts_are_still_refused(self):
        for store in ("Steam", "the App Store", "Google Play"):
            text = f"Coming to {store} and itch.io. #Ringshift #puzzlegame #indiegame"
            self.assertIn("storefront or platform not in the knowledge base",
                          post_rules.find_unsupported_claims(text, project_url=ITCH), store)

    def test_lookalike_hosts_do_not_count_as_itch(self):
        self.assertFalse(post_rules._project_on_itch("https://itch.io.evil.example/ringshift"))
        self.assertFalse(post_rules._project_on_itch("https://ultramegaray0-lgtm.github.io/ringshift-game/"))
        self.assertTrue(post_rules._project_on_itch(ITCH))

    def test_release_claims_are_still_refused(self):
        text = f"Out now on itch.io! {ITCH} #Ringshift #puzzlegame #indiegame"
        self.assertIn("release or availability claim", post_rules.find_unsupported_claims(text, project_url=ITCH))


if __name__ == "__main__":
    unittest.main()
