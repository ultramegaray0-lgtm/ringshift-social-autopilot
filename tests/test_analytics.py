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


if __name__ == "__main__":
    unittest.main()
