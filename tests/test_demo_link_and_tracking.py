"""Demo link cadence on X posts, itch.io snapshot parsing, and the campaign report. Offline."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (blocks network, sets up paths)

import campaign_report
import demo_link
import itch_stats
import post_rules

URL = "https://ringshift.itch.io/ringshift"
PROJECT = {"project_url": URL, "link_every_n_posts": 1}


class DemoLinkTests(unittest.TestCase):
    def test_due_posts_get_the_link_before_their_hashtags(self):
        out = demo_link.append("Two prisms, one lock. Which ring moves first? #Ringshift #puzzlegame", PROJECT, 24,
                               enabled=True)
        self.assertIn(URL, out)
        self.assertNotIn("utm_", out)
        self.assertTrue(out.endswith("#Ringshift #puzzlegame"))
        self.assertEqual(len(post_rules.URL_RE.findall(out)), 1)

    def test_not_due_disabled_or_no_url_leaves_text_alone(self):
        text = "A mirror bends the beam. #Ringshift"
        self.assertIn(URL, demo_link.append(text, PROJECT, 25, enabled=True))
        self.assertEqual(demo_link.append(text, PROJECT, 24, enabled=False), text)
        self.assertEqual(demo_link.append(text, {"project_url": None}, 24, enabled=True), text)

    def test_never_adds_a_second_link(self):
        text = f"Come play: {URL} #Ringshift"
        self.assertEqual(demo_link.append(text, PROJECT, 24, enabled=True), text)

    def test_never_exceeds_x_length(self):
        text = ("word " * 52).strip()
        self.assertGreater(post_rules.weighted_length(text) + 40, 280)
        self.assertEqual(demo_link.append(text, PROJECT, 24, enabled=True), text)

    def test_linked_post_passes_post_rules(self):
        out = demo_link.append("The echo sends a copy through its paired mouth. #Ringshift #puzzlegame #indiegame",
                               PROJECT, 24, enabled=True)
        self.assertEqual(post_rules.validate_post(out, allow_urls=True, project_url=URL), [])

    def test_shared_link_does_not_make_posts_look_alike(self):
        a = demo_link.append("Prisms split a beam into two branches. #Ringshift", PROJECT, 24, enabled=True)
        b = demo_link.append("Half-blocks only stop one direction of travel. #Ringshift", PROJECT, 26, enabled=True)
        self.assertLess(post_rules.similarity(a, b), 0.7)

    def test_cadence_default_and_bad_values(self):
        self.assertEqual(demo_link.every_n({"project_url": URL}), 1)
        self.assertEqual(demo_link.every_n({"project_url": URL, "link_every_n_posts": "x"}), 1)
        self.assertEqual(demo_link.every_n({"project_url": URL, "link_every_n_posts": 0}), 1)
        self.assertTrue(demo_link.is_due({"project_url": URL, "link_every_n_posts": 1}, 7))


class ItchStatsTests(unittest.TestCase):
    GAMES = [{"id": 1, "title": "Other", "url": "https://ringshift.itch.io/other", "views_count": 3},
             {"id": 2, "title": "Ringshift", "url": URL + "/", "views_count": 120, "downloads_count": 40,
              "purchases_count": 0}]

    def test_picks_the_project_game(self):
        self.assertEqual(itch_stats.pick_game(self.GAMES, URL)["id"], 2)
        self.assertIsNone(itch_stats.pick_game(self.GAMES, "https://x.itch.io/y"))

    def test_one_snapshot_per_day_and_deltas(self):
        d1 = itch_stats.snapshot(self.GAMES[1], datetime(2026, 11, 7, 3, tzinfo=timezone.utc))
        later = dict(self.GAMES[1], views_count=150, downloads_count=None)
        d2 = itch_stats.snapshot(later, datetime(2026, 11, 8, 3, tzinfo=timezone.utc))
        d2b = itch_stats.snapshot(dict(later, views_count=155), datetime(2026, 11, 8, 9, tzinfo=timezone.utc))
        history = itch_stats.upsert(itch_stats.upsert(itch_stats.upsert([], d1), d2), d2b)
        self.assertEqual(len(history), 2)
        (date, delta), = itch_stats.daily_deltas(history)
        self.assertEqual((date, delta["views_count"], delta["downloads_count"]), ("2026-11-08", 35, None))

    def test_no_key_is_unavailable_not_failure(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ITCH_API_KEY": ""}):
            path = Path(tmp) / "s.json"
            self.assertEqual(itch_stats.main(["--stats-file", str(path)]), 0)
            self.assertFalse(path.exists())

    def test_api_call_uses_bearer_header(self):
        session = mock.Mock()
        session.get.return_value = mock.Mock(status_code=200, json=lambda: {"games": self.GAMES})
        self.assertEqual(itch_stats.fetch_games("k3y", session=session), self.GAMES)
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer k3y"})


class CampaignReportTests(unittest.TestCase):
    def test_missing_metrics_are_unavailable_not_zero(self):
        ledger = {"activities": [{"id": "a", "channel": "Reddit", "community": "r/WebGames", "kind": "post",
                                  "status": "prepared", "prepared_at": "2026-11-07"}]}
        posts = [{"status": "sent", "text": "no link", "metrics": {"Impressions": 10, "Clicks": 0}},
                 {"status": "sent", "text": f"play {URL}", "metrics": None}]
        text = campaign_report.render(ledger, posts, [], URL, now=datetime(2026, 11, 7, tzinfo=timezone.utc))
        self.assertIn("unavailable", text)
        self.assertIn("| with demo link | 1 | 0 | unavailable | unavailable | unavailable |", text)
        self.assertIn("| without link | 1 | 1 | 10 | 0 | 0.00% |", text)
        self.assertNotIn("other link", text.split("## Channel comparison")[0])
        self.assertIn("r/WebGames", text)

    def test_real_ledger_is_valid(self):
        ledger = json.loads((campaign_report.LEDGER_FILE).read_text(encoding="utf-8"))
        statuses = {"prepared", "published", "blocked", "removed"}
        ids = [a["id"] for a in ledger["activities"]]
        self.assertEqual(len(ids), len(set(ids)))
        for a in ledger["activities"]:
            self.assertIn(a["status"], statuses, a["id"])
            if a["status"] == "published":
                self.assertTrue(a.get("url") and a.get("date"), f"{a['id']}: published needs a verified URL and date")


if __name__ == "__main__":
    unittest.main()
