import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (adds scripts/ to sys.path and blocks network)
import tiktok_state as ts
import tiktok_auth
import tiktok_upload as uploader


def _no_network(*args, **kwargs):
    raise AssertionError("the TikTok uploader tried to reach the network in an offline test")


class ChunkPlanTests(unittest.TestCase):
    def test_small_video_is_one_chunk(self):
        self.assertEqual(uploader.chunk_plan(4 * 1024 * 1024), (4 * 1024 * 1024, 1))

    def test_large_video_uses_sequential_chunks(self):
        size = 140 * 1024 * 1024
        chunk, count = uploader.chunk_plan(size)
        self.assertEqual(chunk, 64 * 1024 * 1024)
        self.assertEqual(count, 2)
        self.assertGreaterEqual(size - chunk, uploader.MIN_CHUNK)
        self.assertLessEqual(size - chunk, 128 * 1024 * 1024)

    def test_trailing_bytes_are_absorbed_into_the_final_chunk(self):
        size = 70 * 1024 * 1024
        chunk, count = uploader.chunk_plan(size)
        self.assertEqual((chunk, count), (64 * 1024 * 1024, 1))
        self.assertEqual(size, 70 * 1024 * 1024)

    def test_final_chunk_never_falls_below_five_megabytes(self):
        for size in (65, 70, 129, 130):
            total = size * 1024 * 1024
            chunk, count = uploader.chunk_plan(total)
            final_size = total - chunk * (count - 1)
            self.assertGreaterEqual(chunk, uploader.MIN_CHUNK)
            self.assertLessEqual(chunk, uploader.MAX_CHUNK)
            self.assertGreaterEqual(final_size, chunk)
            self.assertLessEqual(final_size, 128 * 1024 * 1024)
            self.assertEqual(count, total // chunk)

    def test_invalid_sizes_are_rejected(self):
        for size in (0, -1, uploader.MAX_VIDEO_BYTES + 1):
            with self.subTest(size=size), self.assertRaises(uploader.TikTokUploadError):
                uploader.chunk_plan(size)


class TokenStateTests(unittest.TestCase):
    def test_artifact_must_be_validated_and_hash_matched(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"validated video")
            digest = hashlib.sha256(video.read_bytes()).hexdigest()
            metadata = {"status": "produced", "validation": {"ok": True}, "output": {"sha256": digest}}
            uploader.verify_artifact(video, metadata)
            metadata["output"]["sha256"] = "0" * 64
            with self.assertRaises(uploader.TikTokUploadError):
                uploader.verify_artifact(video, metadata)
            metadata["output"]["sha256"] = digest
            metadata["validation"]["ok"] = False
            with self.assertRaises(uploader.TikTokUploadError):
                uploader.verify_artifact(video, metadata)

    def test_refresh_token_is_encrypted_and_round_trips(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.enc"
            uploader.save_refresh_token(path, key, "secret-refresh-token")
            self.assertNotIn(b"secret-refresh-token", path.read_bytes())
            self.assertEqual(uploader.load_refresh_token(path, key, ""), "secret-refresh-token")

    def test_missing_initial_token_fails_closed(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(uploader.TikTokUploadError):
                uploader.load_refresh_token(Path(tmp) / "missing.enc", key, "")


class InboxHandoffTests(unittest.TestCase):
    """Duplicate protection and recording for the automatic generation -> inbox hand-off."""

    def setUp(self):
        from cryptography.fernet import Fernet
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.video = self.dir / "ringshift-tiktok-20261101-gameplay-abcd1234.mp4"
        self.video.write_bytes(b"validated video bytes")
        self.sha = hashlib.sha256(self.video.read_bytes()).hexdigest()
        self.metadata = self.dir / "meta.json"
        self.metadata.write_text(json.dumps({"status": "produced", "validation": {"ok": True},
                                             "output": {"sha256": self.sha}, "caption_suggestion": "Play the demo"}))
        self.ledger = self.dir / "tiktok_state.json"
        ts.save(ts.empty_state(), self.ledger)
        self.result = self.dir / "result.json"
        self.summary = self.dir / "summary.md"
        self.env = {"TIKTOK_CLIENT_KEY": "k", "TIKTOK_CLIENT_SECRET": "s", "TIKTOK_REFRESH_TOKEN": "r",
                    "TIKTOK_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(), "GITHUB_RUN_ID": "777",
                    "GITHUB_STEP_SUMMARY": str(self.summary)}
        mock.patch("urllib.request.urlopen", side_effect=_no_network).start()
        self.addCleanup(mock.patch.stopall)

    def main(self, *extra):
        argv = ["tiktok_upload.py", "--video", str(self.video), "--metadata", str(self.metadata),
                "--state", str(self.dir / "token.enc"), "--ledger", str(self.ledger), "--result", str(self.result),
                "--source-run-id", "41207634144", "--artifact-name", self.video.stem, *extra]
        with mock.patch.dict(os.environ, self.env), mock.patch.object(sys, "argv", argv), mock.patch("sys.stderr"), \
                mock.patch("builtins.print"):
            return uploader.main()

    def fake_tiktok(self, statuses=("PROCESSING_UPLOAD", "SEND_TO_USER_INBOX")):
        calls = {"upload": 0}
        sequence = list(statuses)

        def upload_file(video, token):
            calls["upload"] += 1
            return f"v_inbox_file~{calls['upload']}"

        mock.patch.object(uploader, "refresh_access_token", return_value=("access", "rotated")).start()
        mock.patch.object(uploader, "upload_file", side_effect=upload_file).start()
        mock.patch.object(uploader, "fetch_status",
                          side_effect=lambda *a: sequence.pop(0) if len(sequence) > 1 else sequence[0]).start()
        mock.patch.object(uploader.time, "sleep").start()
        return calls

    def test_a_successful_upload_is_recorded_with_its_publish_id_and_runs(self):
        calls = self.fake_tiktok()
        self.assertEqual(self.main(), 0)
        self.assertEqual(calls["upload"], 1)
        entry = ts.upload_for(ts.load(self.ledger), self.sha)
        self.assertEqual(entry["publish_id"], "v_inbox_file~1")
        self.assertEqual(entry["status"], "SEND_TO_USER_INBOX")
        self.assertEqual(entry["source_run_id"], "41207634144")
        self.assertEqual(entry["upload_run_id"], "777")
        self.assertEqual(json.loads(self.result.read_text())["publish_id"], "v_inbox_file~1")
        text = self.summary.read_text()
        self.assertIn("Inbox", text)
        self.assertIn("NOT public", text)

    def test_a_retry_of_the_same_video_never_makes_a_second_draft(self):
        calls = self.fake_tiktok()
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.main(), 0)
        self.assertEqual(calls["upload"], 1)
        self.assertEqual(json.loads(self.result.read_text())["status"], "ALREADY_UPLOADED")
        self.assertIn("already done", self.summary.read_text())

    def test_force_sends_it_again_on_purpose(self):
        calls = self.fake_tiktok()
        self.main()
        self.assertEqual(self.main("--force"), 0)
        self.assertEqual(calls["upload"], 2)

    def test_an_accepted_upload_is_recorded_even_if_the_status_checks_fail(self):
        calls = self.fake_tiktok()
        uploader.fetch_status.side_effect = uploader.TikTokUploadError("HTTP 500")
        self.assertEqual(self.main(), 0)
        self.assertEqual(calls["upload"], 1)
        self.assertEqual(ts.upload_for(ts.load(self.ledger), self.sha)["publish_id"], "v_inbox_file~1")
        self.assertEqual(self.main(), 0)
        self.assertEqual(calls["upload"], 1, "the re-run must not upload the accepted video again")

    def test_a_video_tiktok_failed_to_process_may_be_retried(self):
        calls = self.fake_tiktok(statuses=("FAILED",))
        self.assertEqual(self.main(), 1)
        self.assertIsNone(ts.upload_for(ts.load(self.ledger), self.sha))
        self.assertIn("FAILED", self.summary.read_text())
        uploader.fetch_status.side_effect = lambda *a: "SEND_TO_USER_INBOX"
        self.assertEqual(self.main(), 0)
        self.assertEqual(calls["upload"], 2)

    def test_an_unvalidated_video_is_refused_before_any_token_is_used(self):
        self.fake_tiktok()
        self.metadata.write_text(json.dumps({"status": "failed", "output": {"sha256": self.sha}}))
        self.assertEqual(self.main(), 1)
        uploader.refresh_access_token.assert_not_called()
        self.assertFalse(self.result.exists())

    def test_secrets_never_reach_the_summary_or_result(self):
        self.fake_tiktok()
        self.main()
        for path in (self.summary, self.result, self.ledger):
            text = path.read_text()
            for secret in ("rotated", "access", self.env["TIKTOK_TOKEN_ENCRYPTION_KEY"]):
                self.assertNotIn(f'"{secret}"', text)
            self.assertNotIn(self.env["TIKTOK_TOKEN_ENCRYPTION_KEY"], text)


class UploadLedgerStateTests(unittest.TestCase):
    def test_uploads_survive_merge_and_success_beats_failure(self):
        a = ts.record_upload(ts.empty_state(), "sha1", {"publish_id": "p1", "status": "FAILED", "at": "2026-11-02"})
        b = ts.record_upload(ts.empty_state(), "sha1", {"publish_id": "p0", "status": "SEND_TO_USER_INBOX", "at": "2026-11-01"})
        b = ts.record_upload(b, "sha2", {"publish_id": "p2", "status": "UPLOADED", "at": "2026-11-01"})
        merged = ts.merge(a, b)
        self.assertEqual(merged["uploads"]["sha1"]["publish_id"], "p0")
        self.assertEqual(ts.upload_for(merged, "sha2")["publish_id"], "p2")
        self.assertEqual(ts.merge(merged, merged), merged)

    def test_older_states_without_uploads_still_load(self):
        state = ts.normalise({"videos": [], "failures": {}})
        self.assertEqual(state["uploads"], {})
        self.assertIsNone(ts.upload_for(state, "x"))

    def test_an_upload_needs_a_publish_id(self):
        with self.assertRaises(ts.StateError):
            ts.record_upload(ts.empty_state(), "sha", {"status": "UPLOADED"})


class AuthHelperTests(unittest.TestCase):
    def test_the_link_asks_for_video_upload_only_never_publish(self):
        link = tiktok_auth.authorize_link("ck", "https://example.org/cb", "st")
        self.assertIn("scope=user.info.basic%2Cvideo.upload", link)
        self.assertNotIn("video.publish", link)
        self.assertIn("redirect_uri=https%3A%2F%2Fexample.org%2Fcb", link)

    def test_the_code_is_read_from_the_pasted_address(self):
        url = "https://example.org/cb?code=abc%2A123%21x&scopes=user.info.basic%2Cvideo.upload&state=st"
        self.assertEqual(tiktok_auth.code_from_redirect(url, "st"), "abc*123!x")

    def test_wrong_state_denial_or_missing_scope_are_refused(self):
        for url in ("https://e.org/cb?code=a&state=other", "https://e.org/cb?error=access_denied&state=st",
                    "https://e.org/cb?code=a&scopes=user.info.basic&state=st", "https://e.org/cb?state=st"):
            with self.subTest(url=url), self.assertRaises(tiktok_auth.AuthError):
                tiktok_auth.code_from_redirect(url, "st")


if __name__ == "__main__":
    unittest.main()
