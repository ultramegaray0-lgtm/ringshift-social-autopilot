"""send_to_buffer(): authentication errors, missing media confirmation,
successful queueing, and the guarantees around state and duplicates.

Every request is mocked; nothing here can reach Buffer.
"""

import contextlib
import io
import json
import os
import unittest
from unittest import mock

import support
from support import FakeResponse, OfflineTestCase, queued_response
import autopilot


class SendToBufferTests(OfflineTestCase):
    def send(self, post="A fresh post about mirrors."):
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            try:
                return autopilot.send_to_buffer(post), out.getvalue(), None
            except BaseException as error:  # captured so tests can inspect it
                return None, out.getvalue(), error

    def assertNothingAdvanced(self, before):
        self.assertEqual(self.state(), before, "counter or media history changed on a failed publish")

    # ---- successful queueing -------------------------------------------------

    def test_success_advances_counter_and_records_media_once(self):
        self.post.side_effect = None
        self.post.return_value = queued_response(assets=True)

        result, _, error = self.send()

        self.assertIsNone(error)
        self.assertTrue(result)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(
            json.loads(self.counter_file.read_text(encoding="utf-8")),
            {"successful_posts": 4},
        )
        self.assertEqual(
            json.loads(self.history_file.read_text(encoding="utf-8")),
            [self.image_rel],
        )

    def test_request_matches_the_known_good_buffer_schema(self):
        """Pins the schema that Buffer has been accepting, so it cannot drift silently."""
        self.post.side_effect = None
        self.post.return_value = queued_response()

        self.send("Mirrors everywhere.")

        args, kwargs = self.post.call_args
        self.assertEqual(args[0], "https://buffer.invalid")  # BUFFER_API_URL override
        self.assertEqual(
            kwargs["headers"],
            {
                "Authorization": f"Bearer {support.BUFFER_KEY}",
                "Content-Type": "application/json",
            },
        )

        body = kwargs["json"]
        self.assertIn("mutation CreatePost($input: CreatePostInput!)", body["query"])
        self.assertIn("createPost(input: $input)", body["query"])
        self.assertIn("... on PostActionSuccess", body["query"])
        self.assertIn("... on MutationError", body["query"])

        post_input = body["variables"]["input"]
        self.assertEqual(post_input["text"], "Mirrors everywhere.")
        self.assertEqual(post_input["schedulingType"], "automatic")
        self.assertEqual(post_input["mode"], "addToQueue")
        self.assertTrue(post_input["channelId"])
        self.assertEqual(len(post_input["assets"]), 1)
        self.assertEqual(
            post_input["assets"][0],
            {"image": {"url": media_url(self.image_rel)}},
        )

    def test_default_endpoint_is_buffer_when_no_override_is_set(self):
        self.post.side_effect = None
        self.post.return_value = queued_response()

        with mock.patch.dict(os.environ):
            os.environ.pop("BUFFER_API_URL")
            self.send()

        self.assertEqual(self.post.call_args[0][0], "https://api.buffer.com")

    def test_video_files_use_the_video_asset_shape(self):
        self.post.side_effect = None
        self.post.return_value = queued_response()
        self.image_path = self.tmp / self.image_rel
        self.image_path.unlink()  # leave only the (space-named) video

        self.send()

        asset = self.post.call_args.kwargs["json"]["variables"]["input"]["assets"][0]
        self.assertEqual(asset, {"video": {"url": media_url(self.video_rel)}})
        self.assertIn("clip%20one.mp4", asset["video"]["url"])

    # ---- authentication errors ----------------------------------------------

    def test_http_401_raises_and_changes_nothing(self):
        before = self.state()
        self.post.side_effect = None
        self.post.return_value = FakeResponse(401, text='{"message": "Unauthorized"}')

        result, output, error = self.send()

        self.assertIsNone(result)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("401", str(error))
        self.assertNothingAdvanced(before)
        self.assertEqual(self.post.call_count, 1, "a failed request must never be retried")
        self.assertNotIn(support.BUFFER_KEY, output)
        self.assertNotIn(support.BUFFER_KEY, str(error))

    def test_graphql_unauthorized_error_raises_and_changes_nothing(self):
        before = self.state()
        self.post.side_effect = None
        self.post.return_value = FakeResponse(
            200,
            {"errors": [{"message": "Unauthorized", "extensions": {"code": "UNAUTHORIZED"}}]},
        )

        _, output, error = self.send()

        self.assertIsInstance(error, RuntimeError)
        self.assertIn("UNAUTHORIZED", str(error))
        self.assertNothingAdvanced(before)
        self.assertEqual(self.post.call_count, 1)
        self.assertNotIn(support.BUFFER_KEY, output)

    def test_mutation_error_raises_and_changes_nothing(self):
        before = self.state()
        self.post.side_effect = None
        self.post.return_value = FakeResponse(
            200, {"data": {"createPost": {"message": "Channel not found"}}}
        )

        _, _, error = self.send()

        self.assertIsInstance(error, RuntimeError)
        self.assertIn("Channel not found", str(error))
        self.assertNothingAdvanced(before)

    # ---- missing media confirmation -----------------------------------------

    def test_queued_post_without_confirmed_media_raises_and_changes_nothing(self):
        for assets in ([], None):
            with self.subTest(assets=assets):
                before = self.state()
                self.post.reset_mock()
                self.post.side_effect = None
                self.post.return_value = queued_response(assets=assets)

                _, _, error = self.send()

                self.assertIsInstance(error, RuntimeError)
                self.assertIn("did not confirm attached media", str(error))
                self.assertIn("avoid duplicates", str(error))
                self.assertNothingAdvanced(before)
                self.assertEqual(self.post.call_count, 1, "must not retry: the post may already be queued")

    # ---- missing or malformed key -------------------------------------------

    def test_missing_key_exits_before_any_request_or_state_change(self):
        for bad in ("", "   ", "Bearer something"):
            with self.subTest(key=bad):
                before = self.state()
                self.post.reset_mock()

                with mock.patch.dict(os.environ, {"BUFFER_API_KEY": bad}):
                    _, _, error = self.send()

                self.assertIsInstance(error, SystemExit)
                self.assertEqual(self.post.call_count, 0)
                self.assertNothingAdvanced(before)

    def test_selection_alone_never_records_media(self):
        media_selector = support.media_selector
        media_selector.select_media("a post about mirrors")
        self.assertFalse(self.history_file.exists())


def media_url(relative):
    from urllib.parse import quote

    return (
        "https://raw.githubusercontent.com/ultramegaray0-lgtm/ringshift-social-autopilot/main/"
        + quote(relative, safe="/")
    )


if __name__ == "__main__":
    unittest.main()
