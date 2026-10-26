"""Shared helpers for the offline tests.

Safety rules enforced here for EVERY test:
- Any real network call raises AssertionError (requests.post/get/Session.request
  are replaced), so a test can never reach Buffer, X, or Gemini.
- Only fake keys are present in the environment.
- Counter, media history, media files and generated posts all live in a
  temporary directory, never in the real repository data.
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

BUFFER_KEY = "TEST-BUFFER-KEY-DO-NOT-USE"
GEMINI_KEY = "TEST-GEMINI-KEY-DO-NOT-USE"


def _install_genai_stub():
    """Replace google.genai so importing autopilot never needs the real SDK."""
    google = sys.modules.get("google")

    if google is None:
        google = types.ModuleType("google")
        google.__path__ = []
        sys.modules["google"] = google

    genai = types.ModuleType("google.genai")

    class Client:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Gemini client must not be created in offline tests")

    genai.Client = Client
    google.genai = genai
    sys.modules["google.genai"] = genai


_install_genai_stub()

import analytics as analytics_script  # noqa: E402
import autopilot  # noqa: E402
import env_check  # noqa: E402
import media_selector  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("response has no JSON body")
        return self._payload


def queued_response(assets=True):
    """A successful createPost response; assets=False simulates no media confirmation."""
    if assets is True:
        asset_list = [{"id": "asset-1", "mimeType": "image/png"}]
    else:
        asset_list = assets

    return FakeResponse(
        200,
        {
            "data": {
                "createPost": {
                    "post": {
                        "id": "post-1",
                        "text": "queued text",
                        "dueAt": "2026-01-01T04:00:00.000Z",
                        "assets": asset_list,
                    }
                }
            }
        },
    )


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(mock.patch.stopall)
        self.tmp = Path(self._tmp.name)

        mock.patch.dict(
            os.environ,
            {
                "BUFFER_API_KEY": BUFFER_KEY,
                "GEMINI_API_KEY": GEMINI_KEY,
                "BUFFER_API_URL": "https://buffer.invalid",
            },
        ).start()

        def blocked(*args, **kwargs):
            raise AssertionError("Network call attempted during an offline test")

        self.post = mock.patch("requests.post", side_effect=blocked).start()
        mock.patch("requests.get", side_effect=blocked).start()
        mock.patch("requests.Session.request", side_effect=blocked).start()

        (self.tmp / "media" / "gameplay").mkdir(parents=True)
        (self.tmp / "data").mkdir()
        (self.tmp / "content").mkdir()

        self.image_rel = "media/gameplay/black_hole.png"
        self.video_rel = "media/gameplay/clip one.mp4"
        (self.tmp / self.image_rel).write_bytes(b"png")
        (self.tmp / self.video_rel).write_bytes(b"mp4")

        self.counter_file = self.tmp / "data" / "post_counter.json"
        self.history_file = self.tmp / "data" / "media_history.json"
        self.tags_file = self.tmp / "media" / "media_tags.json"
        self.posts_file = self.tmp / "content" / "generated_posts.md"

        mock.patch.object(media_selector, "BASE_DIR", self.tmp).start()
        mock.patch.object(media_selector, "MEDIA_DIR", self.tmp / "media").start()
        mock.patch.object(media_selector, "TAGS_FILE", self.tags_file).start()
        mock.patch.object(media_selector, "HISTORY_FILE", self.history_file).start()
        mock.patch.object(autopilot, "COUNTER_FILE", self.counter_file).start()
        mock.patch.object(autopilot, "POSTS_FILE", self.posts_file).start()

        # Deterministic: always the first sorted candidate (black_hole.png).
        mock.patch.object(
            media_selector.random, "choice", side_effect=lambda seq: seq[0]
        ).start()

        self.counter_file.write_text(json.dumps({"successful_posts": 3}), encoding="utf-8")

    def state(self):
        """Snapshot of the persisted state a failed publish must not change."""
        return (
            self.counter_file.read_text(encoding="utf-8")
            if self.counter_file.exists()
            else None,
            self.history_file.read_text(encoding="utf-8")
            if self.history_file.exists()
            else None,
        )
