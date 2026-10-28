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

# The offline tests run INSIDE GitHub Actions on the real runner, where these variables point at the files of the REAL job
# (its summary, outputs, environment). Several tests run the whole draft flow, which writes a draft summary; with the
# variable present each one appended a fake "Ringshift draft" (a test fixture with its own approval code) to the real
# job summary of the workflow run, so a single run showed dozens of drafts. Tests never get these files.
ACTIONS_FILE_VARIABLES = ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_STATE", "GITHUB_PATH")
for _variable in ACTIONS_FILE_VARIABLES:
    os.environ.pop(_variable, None)

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


def png_bytes(width=320, height=320, seed=1):
    """A real, decodable PNG (noise, so it is not tiny). Fixtures must be real files: a 3-byte placeholder is
    rejected by the code under test, exactly as it must be in production."""
    import random
    import struct
    import zlib

    rng = random.Random(seed)
    rows = b"".join(b"\x00" + rng.randbytes(width * 3) for _ in range(height))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b""))


def mp4_bytes(payload=6000, seed=1):
    """A structurally valid MP4 (ftyp + moov + mdat boxes that add up to the file size). Passes header inspection;
    it is not playable, so tests that need a real decode build clips with FFmpeg instead."""
    import random
    import struct

    def box(kind, data):
        return struct.pack(">I", 8 + len(data)) + kind + data

    return (box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41") + box(b"moov", b"\x00" * 64)
            + box(b"mdat", random.Random(seed).randbytes(payload)))


def ok_check(path, source="repo"):
    """A passing media-check record for a real file (what check_media_for_approval returns), for building drafts."""
    import hashlib
    data = Path(path).read_bytes()
    return {"status": "ok", "checked_at": "2026-10-27T06:00:00+00:00", "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "format": "png", "width": 320, "height": 320, "decode_proof": "ffprobe", "source": source}


class StreamResponse:
    """What requests.get(..., stream=True) returns, for serving fixture bytes."""

    def __init__(self, status_code=200, body=b"", content_type="application/octet-stream"):
        self.status_code = status_code
        self._body = body
        self.headers = {"content-type": content_type, "content-length": str(len(body))}

    def iter_content(self, size):
        for start in range(0, len(self._body), size):
            yield self._body[start:start + size]

    def close(self):
        pass


MANIFEST_DESCRIPTION = "Level 11 in a purple-toned theme, with Echo, Portal and Block gimmicks."


def write_manifest(root, *, only=None, overrides=None):
    """Write media/manifest.json into a temporary checkout, with one genuine-looking record per media file in it
    (a hand-described recording of level 11, SHA-256 of the bytes as they are NOW). A checkout that has media has its
    manifest; a test that wants a missing, stale or odd record passes `overrides` ({path: {field: value}}) or edits the file."""
    import media_manifest as mm
    root = Path(root)
    manifest = mm.empty_manifest()
    for path in sorted((root / "media").rglob("*")):
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".mp4") or (only and relative not in only):
            continue
        video = path.suffix.lower() == ".mp4"
        technical = {"width": 320, "height": 320, "aspect": "other", "bytes": path.stat().st_size,
                     "duration": 6.0 if video else None, "sha256": mm.sha256_file(path)}
        item = mm.make_item(
            relative, base_dir=root, technical=technical, hashes={}, source="manual-recording", mode="hard", level=11,
            description=MANIFEST_DESCRIPTION, mechanisms=["Echo", "Portal", "Block"], mechanisms_source="description",
            verification="reviewed", puzzle_state="unknown", shows_action=video,
            validation={"status": "ok", "checked_at": "2026-10-26T00:00:00+00:00", "problems": []})
        for field, value in (overrides or {}).get(relative, {}).items():
            item[field] = value
        mm.upsert(manifest, item)
    (root / "media").mkdir(exist_ok=True)
    (root / "media" / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def serve_media(test, root, *, missing=(), overrides=None, status=None):
    """Make the raw GitHub URLs of files under `root` answer from disk (HEAD and GET), the way raw.githubusercontent.com
    would. `missing` paths answer 404; `overrides` maps a repo path to different bytes (a stale copy on GitHub);
    `status` forces one HTTP status for everything. Returns the list of URLs requested. No real network is used."""
    import media_selector
    if not (Path(root) / "media" / "manifest.json").exists():
        write_manifest(root)                  # a checkout that serves media has its manifest (see write_manifest)
    requested = []
    prefix = media_selector.RAW_REPO + "/main/"
    overrides = overrides or {}

    def body_for(url):
        if not url.startswith(prefix):
            return None
        relative = url[len(prefix):]
        if relative in missing:
            return None
        if relative in overrides:
            return overrides[relative]
        path = Path(root) / relative
        return path.read_bytes() if path.is_file() else None

    def fake_get(url, **kwargs):
        requested.append(("GET", url))
        body = body_for(url)
        if status:
            return StreamResponse(status)
        return StreamResponse(404) if body is None else StreamResponse(200, body)

    def fake_head(url, **kwargs):
        requested.append(("HEAD", url))
        body = body_for(url)
        if status:
            return StreamResponse(status)
        return StreamResponse(404) if body is None else StreamResponse(200, body)

    mock.patch("requests.get", side_effect=fake_get).start()
    mock.patch("requests.head", side_effect=fake_head).start()
    return requested


def critic_texts(critic):
    """The caption texts a mocked critique_post was asked to review, in order. Tests assert on WHAT reached the critic:
    since a run that rejects every first-round caption writes a second round (see caption_plan), "the critic was never
    called" is no longer the property that matters; "this caption never reached it" is."""
    return [(c.args[0] if c.args else c.kwargs.get("post")) for c in critic.call_args_list]


REJECTING_CRITIQUE = ("CLAIMS:\n- \"x\" => UNSUPPORTED\n\nHook: 5\nCuriosity: 5\nNatural tone: 5\nRingshift relevance: 5\n"
                      "Originality: 5\nViral potential: 5\n\nKEEP: NO\n\nMAIN PROBLEM:\nx\n\nIMPROVED VERSION:\nNot needed.\n")


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
                "GITHUB_ACTIONS": "false",      # tests may run inside Actions; keep them on the offline code paths
                "GITHUB_ACTOR": "tester",
            },
        ).start()

        def blocked(*args, **kwargs):
            raise AssertionError("Network call attempted during an offline test")

        mock.patch.dict(os.environ).start()
        for variable in ACTIONS_FILE_VARIABLES:           # restored after each test; none of them points at a real file
            os.environ.pop(variable, None)

        self.post = mock.patch("requests.post", side_effect=blocked).start()
        mock.patch("requests.get", side_effect=blocked).start()
        self.head = mock.patch("requests.head", side_effect=blocked).start()
        mock.patch("time.sleep", side_effect=lambda *_: None).start()
        mock.patch("requests.Session.request", side_effect=blocked).start()

        (self.tmp / "media" / "gameplay").mkdir(parents=True)
        (self.tmp / "data").mkdir()
        (self.tmp / "content").mkdir()

        self.image_rel = "media/gameplay/black_hole.png"
        self.video_rel = "media/gameplay/clip one.mp4"
        (self.tmp / self.image_rel).write_bytes(png_bytes())
        (self.tmp / self.video_rel).write_bytes(mp4_bytes())

        self.counter_file = self.tmp / "data" / "post_counter.json"
        self.history_file = self.tmp / "data" / "media_history.json"
        self.tags_file = self.tmp / "media" / "media_tags.json"
        self.posts_file = self.tmp / "content" / "generated_posts.md"
        self.post_log_file = self.tmp / "data" / "post_log.json"

        mock.patch.object(media_selector, "BASE_DIR", self.tmp).start()
        mock.patch.object(media_selector, "MEDIA_DIR", self.tmp / "media").start()
        mock.patch.object(media_selector, "TAGS_FILE", self.tags_file).start()
        mock.patch.object(media_selector, "HISTORY_FILE", self.history_file).start()
        mock.patch.object(autopilot, "COUNTER_FILE", self.counter_file).start()
        mock.patch.object(autopilot, "POSTS_FILE", self.posts_file).start()
        mock.patch.object(autopilot, "POST_LOG_FILE", self.post_log_file).start()
        mock.patch.object(autopilot, "PROJECT_FILE", self.tmp / "data" / "project.json").start()
        # Verified project facts come from a temporary file (none by default) and the day-based rotation offset is
        # fixed, so which post type a test gets never depends on the repository's facts or on today's date.
        self.facts_file = self.tmp / "data" / "project_facts.json"
        mock.patch.object(autopilot, "FACTS_FILE", self.facts_file).start()
        mock.patch.object(autopilot, "draft_rotation_offset", return_value=0).start()
        # The "is a post already waiting in Buffer" guard needs the network, so it is a no-op by
        # default here and tested on its own (test_publish_guards.py) with injected data.
        self.guard = mock.patch.object(autopilot, "ensure_safe_to_publish").start()
        # Test fixtures are throwaway files with no real URL, so the media checks run in "fixture" mode, which is only
        # reachable from tests (it is a constant, not an environment variable) and stamps drafts as unapprovable.
        mock.patch.object(autopilot, "MEDIA_CHECK_MODE", "fixture").start()
        autopilot.DRAFTS_THIS_RUN.clear()          # the per-run approval-code limit must not leak between tests

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
