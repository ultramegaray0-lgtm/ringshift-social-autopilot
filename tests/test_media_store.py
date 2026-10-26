"""media_store: hosting clips on the media-store branch, using a local bare repo as the 'remote'.

No network and no GitHub: git runs against temporary directories only.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import support  # noqa: F401
import media_store as store

HAVE_GIT = shutil.which("git") is not None


def git(cwd, *args):
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


@unittest.skipUnless(HAVE_GIT, "git not installed")
class PublishTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

        self.remote = self.dir / "remote.git"
        git(self.dir, "init", "-q", "--bare", str(self.remote))

        # A normal clone with a 'main' branch, so we can prove main is never touched.
        self.repo = self.dir / "work"
        git(self.dir, "init", "-q", str(self.repo))
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hello", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-q", "origin", "main")
        self.main_before = git(self.remote, "rev-parse", "main")

        self.clips = self.dir / "clips"
        self.clips.mkdir()

    def clip(self, name, content=b"video-bytes"):
        path = self.clips / name
        path.write_bytes(content)
        return path

    def publish(self, files, **kwargs):
        kwargs.setdefault("validate", False)
        return store.publish(files, repo_dir=self.repo, **kwargs)

    def remote_files(self):
        listing = git(self.remote, "ls-tree", "-r", "--name-only", store.STORE_BRANCH)
        return sorted(listing.splitlines())

    def test_publish_creates_branch_and_returns_urls(self):
        result = self.publish([self.clip("a.mp4")])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "a.mp4")
        self.assertEqual(result[0]["type"], "video")
        self.assertIn("/media-store/store/a.mp4", result[0]["url"])
        self.assertIn("store/a.mp4", self.remote_files())

    def test_main_branch_is_never_modified(self):
        self.publish([self.clip("a.mp4")])
        self.publish([self.clip("b.mp4")])
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.main_before)

    def test_history_is_squashed_to_one_commit(self):
        self.publish([self.clip("a.mp4")])
        self.publish([self.clip("b.mp4")])
        self.publish([self.clip("c.mp4")])
        count = git(self.remote, "rev-list", "--count", store.STORE_BRANCH)
        self.assertEqual(count, "1")
        self.assertEqual(
            [f for f in self.remote_files() if f.startswith("store/")],
            ["store/a.mp4", "store/b.mp4", "store/c.mp4"])

    def test_retention_drops_the_oldest_clips(self):
        for index, name in enumerate(["a.mp4", "b.mp4", "c.mp4"]):
            self.publish([self.clip(name)], keep=2,
                         now=datetime(2026, 1, 1 + index, tzinfo=timezone.utc))
        files = [f for f in self.remote_files() if f.startswith("store/")]
        self.assertEqual(files, ["store/b.mp4", "store/c.mp4"])

    def test_republishing_a_name_replaces_it_without_duplicates(self):
        self.publish([self.clip("a.mp4", b"one")])
        self.publish([self.clip("a.mp4", b"two-two")])
        manifest = json.loads(git(self.remote, "show", f"{store.STORE_BRANCH}:{store.MANIFEST}"))
        self.assertEqual([m["name"] for m in manifest], ["a.mp4"])
        self.assertEqual(manifest[0]["bytes"], len(b"two-two"))

    def test_protected_branches_are_refused(self):
        for branch in ("main", "master"):
            with self.assertRaises(store.StoreError):
                self.publish([self.clip("a.mp4")], branch=branch)
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.main_before)

    def test_missing_file_is_refused(self):
        with self.assertRaises(store.StoreError):
            self.publish([self.dir / "nope.mp4"])

    def test_invalid_media_is_never_uploaded(self):
        junk = self.clip("junk.mp4", b"not really a video" * 20)
        with self.assertRaises(store.StoreError) as ctx:
            self.publish([junk], validate=True)
        self.assertIn("failed validation", str(ctx.exception))
        branches = git(self.remote, "branch", "--list", store.STORE_BRANCH)
        self.assertEqual(branches, "")

    def test_no_push_builds_but_uploads_nothing(self):
        result = self.publish([self.clip("a.mp4")], push=False)
        self.assertEqual(len(result), 1)
        self.assertEqual(git(self.remote, "branch", "--list", store.STORE_BRANCH), "")

    def test_list_store_reads_the_manifest(self):
        self.assertEqual(store.list_store(self.repo), [])
        self.publish([self.clip("a.mp4")])
        names = [m["name"] for m in store.list_store(self.repo)]
        self.assertEqual(names, ["a.mp4"])


class HelperTests(unittest.TestCase):
    def test_owner_repo_parsing(self):
        for url in ("https://github.com/acme/widgets.git", "git@github.com:acme/widgets.git",
                    "https://github.com/acme/widgets", "https://x-access-token:abc@github.com/acme/widgets.git"):
            with self.subTest(url=url):
                self.assertEqual(store.owner_repo(url), ("acme", "widgets"))
        with self.assertRaises(store.StoreError):
            store.owner_repo("https://example.com/not-github")

    def test_safe_name(self):
        self.assertEqual(store.safe_name("My Clip (final).mp4"), "My_Clip_final_.mp4")
        self.assertNotIn(" ", store.safe_name("a b c.mp4"))
        self.assertEqual(store.safe_name("dir/evil name.mp4"), "evil_name.mp4")
        with self.assertRaises(store.StoreError):
            store.safe_name("...")

    def test_register_in_catalog_moves_metadata_to_the_hosted_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            tags_file = Path(tmp) / "media_tags.json"
            tags_file.write_text(json.dumps({
                "media/captured/a.mp4": {"tags": ["prism"], "description": "Level 9", "type": "video"},
                "media/keep.png": ["mirror"],
            }), encoding="utf-8")

            catalog = store.register_in_catalog(
                [{"name": "a.mp4", "file": "media/captured/a.mp4", "url": "https://h/store/a.mp4",
                  "type": "video", "bytes": 5}], tags_file=tags_file)

            self.assertNotIn("media/captured/a.mp4", catalog)
            hosted = catalog["store/a.mp4"]
            self.assertEqual(hosted["tags"], ["prism"])
            self.assertEqual(hosted["description"], "Level 9")
            self.assertEqual(hosted["url"], "https://h/store/a.mp4")
            self.assertEqual(hosted["ref"], "media-store")
            self.assertEqual(catalog["media/keep.png"], ["mirror"])
            self.assertEqual(json.loads(tags_file.read_text(encoding="utf-8")), catalog)

    def test_verify_urls_reports_each_url_without_following_redirects(self):
        class R:
            def __init__(self, code): self.status_code, self.headers = code, {"content-type": "video/mp4"}
        with mock.patch("requests.head", side_effect=[R(200), R(404), R(302)]) as head:
            results = store.verify_urls(["u1", "u2", "u3"])
        self.assertEqual([ok for _, ok, _ in results], [True, False, False])
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in head.call_args_list))


if __name__ == "__main__":
    unittest.main()
