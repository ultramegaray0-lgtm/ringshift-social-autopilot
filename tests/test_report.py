"""report.py: only recorded numbers are used; missing data is never turned into zeros or trends."""

import csv
import json
import unittest

import support  # noqa: F401
from support import OfflineTestCase
import report


def rec(i, impressions, post_type="hook", status="sent", media_type="video", tags=None, x=None):
    metrics = {} if impressions is None else {"Impressions": impressions, "Reactions": 1}
    return {"id": f"p{i}", "text": f"text {i}", "status": status, "sentAt": "2026-10-2%dT04:00:00Z" % (i % 10),
            "buffer": metrics, "x": x or {}, "post_type": post_type, "media_type": media_type,
            "media_file": "a.mp4", "media_tags": tags or []}


class NumberTests(unittest.TestCase):
    def test_blank_and_dash_are_unknown_not_zero(self):
        for value in ("", "-", None, "n/a", "abc"):
            self.assertIsNone(report.number(value))
        self.assertEqual(report.number("1,234"), 1234)
        self.assertEqual(report.number("2.5%"), 2.5)
        self.assertEqual(report.number(0), 0)


class ReportTests(unittest.TestCase):
    def test_empty_report_says_so(self):
        self.assertIn("No posts found", report.build_report([], []))

    def test_too_little_data_ranks_nothing(self):
        text = report.build_report([rec(1, 5), rec(2, 0), rec(3, None)], [])
        self.assertIn("Not enough data to compare", text)
        self.assertIn("Fewer than 3 posts with data, so no conclusion", text)

    def test_zero_and_scheduled_posts_are_not_evidence(self):
        records = [rec(1, 0), rec(2, None), rec(3, 10, status="scheduled")]
        self.assertEqual([report.has_evidence(r) for r in records], [False, False, False])

    def test_totals_and_groups_use_only_posts_with_data(self):
        records = [rec(1, 10, "hook"), rec(2, 20, "hook"), rec(3, 30, "hook"), rec(4, 0, "challenge")]
        rows = {r["group"]: r for r in report.summarize(records, lambda r: r["post_type"])}
        self.assertEqual(rows["hook"]["with_data"], 3)
        self.assertEqual(rows["hook"]["totals"]["Impressions"], 60)
        self.assertEqual(rows["challenge"]["with_data"], 0)
        text = report.build_report(records, [])
        self.assertIn("| challenge | 1 | 0 |", text)
        self.assertIn("n/a", text)                       # no average for a group without data
        self.assertNotIn("Not enough data", text)

    def test_tags_split_a_post_into_each_mechanic(self):
        rows = report.summarize([rec(1, 5, tags=["prism", "mirror"])], lambda r: r["media_tags"])
        self.assertEqual({r["group"] for r in rows}, {"prism", "mirror"})

    def test_csv_value_wins_only_when_larger(self):
        r = rec(1, 10, x={"Impressions": 25, "Reactions": 0, "Bookmarks": 2})
        merged = report.best_metrics(r)
        self.assertEqual(merged["Impressions"], 25)
        self.assertEqual(merged["Reactions"], 1)
        self.assertEqual(merged["Bookmarks"], 2)

    def test_follower_change_is_not_attributed_to_a_post(self):
        text = report.build_report([rec(1, 5)], [("2026-10-01", 100), ("2026-10-20", 112)])
        self.assertIn("(+12)", text)
        self.assertIn("not caused by any one post", text)


class FilesTests(OfflineTestCase):
    def test_csv_import_matches_our_posts_and_ignores_unknown_columns(self):
        csv_path = self.tmp / "x.csv"
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as handle:
            w = csv.writer(handle)
            w.writerow(["Post text", "Impressions", "Likes", "Replies", "Mystery", "Bookmarks"])
            w.writerow(["Which ring first?", "1,200", "7", "-", "zzz", "2"])
            w.writerow(["Someone else's reply", "5", "0", "0", "q", "0"])
        store = self.tmp / "data" / "x_metrics.json"
        matched, other = report.import_csv(csv_path, store, known_texts=["which ring first"])
        self.assertEqual((matched, other), (1, 1))
        saved = json.loads(store.read_text(encoding="utf-8"))
        ours = next(v for v in saved.values() if v["ours"])
        self.assertEqual(ours["metrics"], {"Impressions": 1200, "Reactions": 7, "Bookmarks": 2})   # '-' dropped

    def test_csv_without_a_text_column_is_a_clear_error(self):
        csv_path = self.tmp / "bad.csv"
        csv_path.write_text("Date,Impressions\n2026-10-01,5\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Post text"):
            report.parse_x_csv(csv_path)

    def test_followers_log_replaces_a_date_and_stays_sorted(self):
        path = self.tmp / "data" / "followers.csv"
        report.log_followers(10, "2026-10-02", path)
        report.log_followers(5, "2026-10-01", path)
        rows = report.log_followers(11, "2026-10-02", path)
        self.assertEqual(rows, [("2026-10-01", 5), ("2026-10-02", 11)])
        with self.assertRaises(ValueError):
            report.log_followers(1, "02/10/2026", path)

    def test_collect_joins_post_log_to_buffer_metrics(self):
        metrics = [{"id": "b1", "text": "hi there", "status": "sent", "metrics": {"Impressions": 4}}]
        log = [{"buffer_id": "b1", "text": "hi there", "post_type": "mechanic", "media_type": "image",
                "media_file": "x.png", "media_tags": ["prism"]}]
        got = report.collect(metrics, log, {})
        self.assertEqual(got[0]["post_type"], "mechanic")
        self.assertEqual(got[0]["media_tags"], ["prism"])


if __name__ == "__main__":
    unittest.main()
