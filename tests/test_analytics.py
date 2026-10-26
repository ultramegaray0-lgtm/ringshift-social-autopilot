import contextlib
import io
import os
import unittest
from unittest import mock

import support
from support import FakeResponse, OfflineTestCase
import analytics as analytics_script


class AnalyticsFetchTests(OfflineTestCase):
    def call(self):
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            try:
                return analytics_script.get_posts(), out.getvalue(), None
            except BaseException as error:
                return None, out.getvalue(), error

    def test_success_uses_a_bearer_header_with_the_validated_key(self):
        self.post.side_effect = None
        self.post.return_value = FakeResponse(
            200, {"data": {"posts": {"edges": [{"node": {"id": "1"}}]}}}
        )

        edges, _, error = self.call()

        self.assertIsNone(error)
        self.assertEqual(edges, [{"node": {"id": "1"}}])
        self.assertEqual(
            self.post.call_args.kwargs["headers"]["Authorization"],
            f"Bearer {support.BUFFER_KEY}",
        )

    def test_http_401_fails_loudly_with_the_status_and_no_key(self):
        self.post.side_effect = None
        self.post.return_value = FakeResponse(401, text="Unauthorized")

        _, output, error = self.call()

        self.assertIsInstance(error, RuntimeError)
        self.assertIn("401", str(error))
        self.assertNotIn(support.BUFFER_KEY, output)
        self.assertNotIn(support.BUFFER_KEY, str(error))

    def test_missing_or_malformed_key_exits_before_any_request(self):
        for bad in ("", "Bearer abc"):
            with self.subTest(key=bad):
                self.post.reset_mock()

                with mock.patch.dict(os.environ, {"BUFFER_API_KEY": bad}):
                    _, _, error = self.call()

                self.assertIsInstance(error, SystemExit)
                self.assertEqual(self.post.call_count, 0)


def page(ids, has_next, cursor=None):
    return FakeResponse(200, {"data": {"posts": {
        "edges": [{"node": {"id": i, "text": f"t{i}"}} for i in ids],
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }}})


class AnalyticsPaginationTests(OfflineTestCase):
    def fetch(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return analytics_script.get_posts()

    def test_follows_cursors_until_the_last_page(self):
        self.post.side_effect = [page(["a", "b"], True, "c1"), page(["c"], True, "c2"), page(["d"], False, "c3")]
        edges = self.fetch()
        self.assertEqual([e["node"]["id"] for e in edges], ["a", "b", "c", "d"])
        self.assertEqual(self.post.call_count, 3)
        variables = [c.kwargs["json"]["variables"] for c in self.post.call_args_list]
        self.assertNotIn("after", variables[0])
        self.assertEqual(variables[1]["after"], "c1")
        self.assertEqual(variables[2]["after"], "c2")

    def test_repeated_cursor_cannot_loop_forever(self):
        self.post.side_effect = None
        self.post.return_value = page(["a"], True, "same")
        self.fetch()
        self.assertLessEqual(self.post.call_count, 3)

    def test_page_limit_is_a_hard_stop(self):
        counter = iter(range(1000))
        self.post.side_effect = lambda *a, **k: page([f"p{next(counter)}"], True, f"cur{next(counter)}")
        self.fetch()
        self.assertEqual(self.post.call_count, analytics_script.MAX_PAGES)

    def test_posts_seen_on_two_pages_are_kept_once(self):
        self.post.side_effect = [page(["a", "b"], True, "c1"), page(["b", "c"], False, "c2")]
        self.assertEqual([e["node"]["id"] for e in self.fetch()], ["a", "b", "c"])

    def test_rejected_pagination_falls_back_to_the_single_page_query(self):
        rejected = FakeResponse(200, {"errors": [{"message": "Unknown argument first"}]})
        single = FakeResponse(200, {"data": {"posts": {"edges": [{"node": {"id": "x"}}]}}})
        self.post.side_effect = [rejected, single]
        self.assertEqual(self.fetch(), [{"node": {"id": "x"}}])
        self.assertNotIn("$first", self.post.call_args_list[1].kwargs["json"]["query"])

    def test_errors_after_the_first_page_still_fail_loudly(self):
        self.post.side_effect = [page(["a"], True, "c1"), FakeResponse(200, {"errors": [{"message": "boom"}]})]
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                analytics_script.get_posts()


class MetricsFileTests(OfflineTestCase):
    def test_only_reported_values_are_written(self):
        import json
        posts = [{"node": {
            "id": "1", "status": "sent", "text": "hello", "sentAt": "2026-10-24T04:00:16Z",
            "metrics": [{"name": "Impressions", "value": 2, "unit": "count"},
                        {"name": "Clicks", "value": None, "unit": "count"}],
            "metricsUpdatedAt": "2026-10-24T22:00:00Z"}},
            {"node": {"id": "2", "status": "scheduled", "text": "later", "metrics": None}}]
        path = self.tmp / "data" / "metrics.json"
        analytics_script.save_metrics(posts, path)
        records = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(records[0]["metrics"], {"Impressions": 2})     # null is not turned into 0
        self.assertEqual(records[1]["metrics"], {})


if __name__ == "__main__":
    unittest.main()
