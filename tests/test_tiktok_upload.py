import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (adds scripts/ to sys.path and blocks network)
import tiktok_upload as uploader


class ChunkPlanTests(unittest.TestCase):
    def test_small_video_is_one_chunk(self):
        self.assertEqual(uploader.chunk_plan(4 * 1024 * 1024), (4 * 1024 * 1024, 1))

    def test_large_video_uses_sequential_chunks(self):
        size = 70 * 1024 * 1024
        chunk, count = uploader.chunk_plan(size)
        self.assertEqual(chunk, 64 * 1024 * 1024)
        self.assertEqual(count, 2)
        self.assertGreaterEqual(size - chunk, uploader.MIN_CHUNK)

    def test_final_chunk_never_falls_below_five_megabytes(self):
        for size in (65, 70, 129, 130):
            total = size * 1024 * 1024
            chunk, count = uploader.chunk_plan(total)
            final_size = total - chunk * (count - 1)
            self.assertGreaterEqual(chunk, uploader.MIN_CHUNK)
            self.assertLessEqual(chunk, uploader.MAX_CHUNK)
            self.assertGreaterEqual(final_size, chunk)

    def test_invalid_sizes_are_rejected(self):
        for size in (0, -1, uploader.MAX_VIDEO_BYTES + 1):
            with self.subTest(size=size), self.assertRaises(uploader.TikTokUploadError):
                uploader.chunk_plan(size)


class TokenStateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
