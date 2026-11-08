"""Tagged demo links, the angle bank, the autopilot's link screening and measured-only performance analysis."""
import json
import unittest
from unittest import mock

import support  # noqa: F401  (blocks network, sets up paths)

import angle_bank
import autopilot
import demo_link
import game_terms
import performance
import post_rules
import tracking

URL = "https://ringshift.itch.io/ringshift"


class TrackingTests(unittest.TestCase):
    def test_tagged_link_has_source_medium_campaign_content(self):
        url = tracking.tagged(URL, "bluesky", "Angles", "A01 Rule")
        self.assertEqual(url, URL + "?utm_source=bluesky&utm_medium=social&utm_campaign=angles&utm_content=a01-rule")
        self.assertEqual(tracking.tags(url)["utm_content"], "a01-rule")

    def test_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            tracking.tagged(URL, "myspace", "x")

    def test_project_link_detection(self):
        self.assertTrue(tracking.is_project_link(URL, URL))
        self.assertTrue(tracking.is_project_link(URL + "/?utm_source=x&utm_medium=social.", URL))
        self.assertFalse(tracking.is_project_link(URL + "?ref=evil", URL))
        self.assertFalse(tracking.is_project_link("https://ringshift.itch.io/other?utm_source=x", URL))
        self.assertFalse(tracking.is_project_link("https://t.co/abc", URL))

    def test_every_channel_maps_to_one_medium(self):
        self.assertTrue(all(tracking.CHANNELS[s] in {"social", "video", "community", "portal", "outreach"}
                            for s in tracking.CHANNELS))


class AutopilotLinkTests(unittest.TestCase):
    PROJECT = {"project_url": URL, "link_every_n_posts": 1}

    def test_tagged_demo_link_passes_screening_but_other_links_do_not(self):
        good = demo_link.append("A prism splits one beam into two. #Ringshift #puzzlegame #indiegame", self.PROJECT, 26,
                                enabled=True)
        problems = post_rules.validate_post(good, allow_urls=True, project_url=URL)
        self.assertEqual(problems, [])
        with mock.patch.object(autopilot, "is_duplicate_post", return_value=False):
            self.assertIsNone(autopilot.screen_candidate(good, [], self.PROJECT))
            bad = good.replace(URL, "https://example.com/ringshift")
            self.assertIn("other than the exact configured project URL", autopilot.screen_candidate(bad, [], self.PROJECT))

    def test_invite_link_rule_uses_the_tagged_url(self):
        rule = autopilot.link_rule({"id": "invite"}, self.PROJECT)
        self.assertIn(URL, rule)
        self.assertNotIn("utm_", rule)

    def test_cta_phrases_rotate_and_utm_content_is_the_post_number(self):
        texts = [demo_link.append("Rings and beams. #Ringshift", self.PROJECT, n, enabled=True) for n in (24, 26, 28, 30)]
        self.assertEqual(len({t.split("\n\n")[1].split(":")[0] for t in texts}), 4)
        self.assertIn(URL, texts[1])
        self.assertNotIn("utm_", texts[1])

    def test_first_impression_prompt_for_every_post_with_demo_link(self):
        self.assertIn("FIRST sentence", demo_link.FIRST_IMPRESSION)
        captured = {}

        def fake_generate(prompt):
            captured["prompt"] = prompt
            return mock.Mock(text="CANDIDATE 1:\na\n\nCANDIDATE 2:\nb\n\nCANDIDATE 3:\nc")

        with mock.patch.object(autopilot, "generate_with_retry", fake_generate):
            autopilot.generate_candidates({}, autopilot.POST_TYPES[0], dict(self.PROJECT, demo_link_due=True))
            self.assertIn("THIS POST WILL END WITH A LINK", captured["prompt"])
            autopilot.generate_candidates({}, autopilot.POST_TYPES[0], dict(self.PROJECT))
            self.assertIn("THIS POST WILL END WITH A LINK", captured["prompt"])


class AngleBankTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = angle_bank.load()
        cls.angles = cls.data["angles"]

    def test_at_least_fifteen_angles_with_unique_ids_and_assets_present(self):
        self.assertGreaterEqual(len(self.angles), 15)
        self.assertEqual(len({a["id"] for a in self.angles}), len(self.angles))

    def test_every_angle_passes_rules_lengths_and_links(self):
        for angle in self.angles:
            with self.subTest(angle=angle["id"]):
                self.assertEqual(angle_bank.problems(angle, URL, self.data["campaign"]), [])

    def test_no_unknown_mechanic_names(self):
        for angle in self.angles:
            with self.subTest(angle=angle["id"]):
                self.assertEqual(game_terms.unknown_terms(angle["x"] + " " + angle["bluesky"]), [])

    def test_angles_are_genuinely_different(self):
        texts = [(a["id"], a["x"].replace("{link}", "")) for a in self.angles]
        for i, (ida, a) in enumerate(texts):
            for idb, b in texts[i + 1:]:
                self.assertLess(post_rules.similarity(a, b), 0.6, f"{ida} vs {idb}")

    def test_render_gives_one_tagged_link_per_platform_and_angle(self):
        text, links = angle_bank.render(self.data, URL)
        self.assertEqual(len(links), 3 * len(self.angles))
        self.assertEqual(len({l["url"] for l in links}), len(links))
        self.assertIn("utm_content=a01-rule", text)


def _post(pid, imp, clicks=0, text="text", status="sent", date="2026-11-01"):
    m = None if imp is None else {"Impressions": imp, "Clicks": clicks}
    return {"id": pid, "status": status, "text": text, "sentAt": date + "T04:00:00Z", "metrics": m}


class PerformanceTests(unittest.TestCase):
    def test_media_kind_from_capture_names(self):
        self.assertEqual(performance.media_kind("store/rs_hard_L02_mirror-echo-block_solved_20261106.jpg"), "solved")
        self.assertEqual(performance.media_kind("media/gameplay/ringshift_prism_gameplay.mp4"), "video")
        self.assertEqual(performance.media_kind(None), "unknown")

    def test_link_status_uses_the_queued_text_when_x_shows_tco(self):
        self.assertEqual(performance.link_status("Play https://t.co/x", URL, f"Play {URL}?utm_source=x"), "demo")
        self.assertEqual(performance.link_status("Look https://t.co/x", URL), "other")
        self.assertEqual(performance.link_status("No link", URL), "none")

    def test_unmeasured_posts_never_count_as_zero(self):
        rows = performance.join([_post("a", None), _post("b", 10)], [], URL)
        g = performance.group(rows, "link")["none"]
        self.assertEqual((g["posts"], g["measured"], g["impressions"]), (2, 1, 10))
        self.assertFalse(g["comparable"])

    def test_underperformers_need_five_measured_and_use_own_median(self):
        few = performance.join([_post(str(i), 20) for i in range(4)], [], URL)
        self.assertEqual(performance.underperformers(few), [])
        rows = performance.join([_post(str(i), 20) for i in range(5)] + [_post("low", 3)] +
                                [_post("hi", 150, text=f"Play {URL}")], [], URL)
        reasons = {row["impressions"]: why for row, why in performance.underperformers(rows)}
        self.assertIn("under half the recent median", reasons[3])
        self.assertIn("0 clicks", reasons[150])

    def test_recommendations_wait_for_enough_data(self):
        rows = performance.join([_post("a", 10)], [], URL)
        recs = performance.recommendations(rows)
        self.assertTrue(any("Not enough linked posts" in r for r in recs))
        self.assertTrue(all("%" not in r or "CTR" in r for r in recs))

    def test_recommendation_compares_only_measured_groups(self):
        log = [{"buffer_id": f"s{i}", "media_file": f"rs_hard_L01_x_solved_2026.jpg", "post_type": "visual"} for i in range(3)]
        log += [{"buffer_id": f"p{i}", "media_file": f"rs_hard_L01_x_puzzle_2026.jpg", "post_type": "challenge"} for i in range(3)]
        posts = [_post(f"s{i}", 40) for i in range(3)] + [_post(f"p{i}", 10) for i in range(3)]
        recs = performance.recommendations(performance.join(posts, log, URL))
        self.assertTrue(any("'solved' averages 40 impressions over 3 posts" in r for r in recs))


if __name__ == "__main__":
    unittest.main()
