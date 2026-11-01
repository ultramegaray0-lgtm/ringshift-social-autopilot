"""TikTok-Output: the re-check of a finished video and saving it on the tiktok-output branch.

Root cause these tests guard (run 41207634144, 2026-10-31): the save step ran `git -c http...extraheader=...` inside
the actions/checkout working copy, which already has that header in its local config, so git sent two Authorization
headers and GitHub refused with HTTP 400 "Duplicate header: Authorization". TikTok-Output stayed empty.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (adds scripts/ to sys.path and blocks network)
import media_store
import tiktok_output as to

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
VIDEO_WORKFLOW = (WORKFLOWS / "tiktok-video.yml").read_text(encoding="utf-8")
UPLOAD_WORKFLOW = (WORKFLOWS / "tiktok-upload.yml").read_text(encoding="utf-8")
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
HAVE_GIT = shutil.which("git") is not None
HEADER_KEY = "http.https://github.com/.extraheader"


def git(cwd, *args):
    result = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                            cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True)


def make_mp4(path, seconds=2.0, size=(1080, 1920), seed=1):
    """A real H.264 video with enough detail to be a realistic size (different seeds give different bytes)."""
    w, h = size
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration={seconds}",
           "-vf", f"noise=alls=40:allf=t:all_seed={seed}",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p", str(path))
    return Path(path)


def make_bundle(folder, stem="ringshift-tiktok-20261101-gameplay-abcd1234", seconds=1.0, size=(1080, 1920),
                created_at="2026-11-01T07:45:00+00:00", run_id="111"):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    video = make_mp4(folder / f"{stem}.mp4", seconds, size, seed=int(hashlib.sha256(stem.encode()).hexdigest()[:6], 16))
    ffmpeg("-i", str(video), "-frames:v", "1", "-vf", "scale=540:-2", str(folder / f"{stem}_preview.jpg"))
    data = video.read_bytes()
    metadata = {
        "status": "produced", "format": "gameplay", "created_at": created_at, "run_id": run_id,
        "caption_suggestion": "Ringshift: rotate the rings. Play the demo (link in bio).",
        "output": {"file": video.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                   "width": size[0], "height": size[1], "duration": seconds},
        "validation": {"ok": True, "checks": {"media": {"ok": True, "detail": "ok"}, "board_visible": {"ok": True, "detail": "ok"}}},
    }
    (folder / f"{stem}.json").write_text(json.dumps(metadata), encoding="utf-8")
    return to.find_bundle(folder)


def rewrite_metadata(bundle, **changes):
    meta = json.loads(bundle["metadata"].read_text())
    for key, value in changes.items():
        if "." in key:
            outer, inner = key.split(".", 1)
            meta[outer][inner] = value
        else:
            meta[key] = value
    bundle["metadata"].write_text(json.dumps(meta))


# ================================================================================================ re-check

@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class VerifyBundleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_a_real_validated_1080x1920_video_passes(self):
        bundle = make_bundle(self.dir)
        problems, meta = to.verify_bundle(bundle)
        self.assertEqual(problems, [])
        self.assertGreater(bundle["video"].stat().st_size, to.MIN_VIDEO_BYTES)

    def test_an_empty_video_is_never_accepted(self):
        bundle = make_bundle(self.dir)
        bundle["video"].write_bytes(b"")
        problems, _ = to.verify_bundle(bundle)
        self.assertTrue(any("bytes" in p for p in problems), problems)

    def test_a_changed_video_fails_the_sha256(self):
        bundle = make_bundle(self.dir)
        data = bytearray(bundle["video"].read_bytes())
        data[-100] ^= 0xFF
        bundle["video"].write_bytes(bytes(data))
        problems, _ = to.verify_bundle(bundle)
        self.assertTrue(any("SHA-256" in p for p in problems), problems)

    def test_a_truncated_video_with_matching_metadata_does_not_decode(self):
        bundle = make_bundle(self.dir, seconds=3.0)
        data = bundle["video"].read_bytes()[: int(bundle["video"].stat().st_size * 0.6)]
        bundle["video"].write_bytes(data)
        rewrite_metadata(bundle, **{"output.sha256": hashlib.sha256(data).hexdigest(), "output.bytes": len(data)})
        problems, _ = to.verify_bundle(bundle)
        self.assertTrue(problems, "a truncated MP4 must be refused")

    def test_unvalidated_or_failed_videos_are_refused(self):
        bundle = make_bundle(self.dir)
        rewrite_metadata(bundle, status="failed")
        self.assertTrue(any("status" in p for p in to.verify_bundle(bundle)[0]))
        rewrite_metadata(bundle, status="produced", validation={"ok": True, "checks": {"board_visible": {"ok": False}}})
        self.assertTrue(any("board_visible" in p for p in to.verify_bundle(bundle)[0]))
        rewrite_metadata(bundle, validation={"ok": False, "checks": {}})
        self.assertTrue(any("quality check" in p for p in to.verify_bundle(bundle)[0]))

    def test_the_wrong_resolution_is_refused(self):
        bundle = make_bundle(self.dir, size=(720, 1280), seconds=3.0)
        problems, _ = to.verify_bundle(bundle)
        self.assertTrue(any("720x1280" in p for p in problems), problems)

    def test_a_missing_preview_or_metadata_is_an_error(self):
        bundle = make_bundle(self.dir)
        bundle["preview"].unlink()
        with self.assertRaises(to.OutputError):
            to.find_bundle(self.dir)
        bundle["metadata"].unlink()
        with self.assertRaises(to.OutputError):
            to.find_bundle(self.dir)

    def test_verify_cli_exits_non_zero_on_a_bad_bundle(self):
        bundle = make_bundle(self.dir)
        bundle["video"].write_bytes(b"x")
        with mock.patch("sys.stderr"):
            self.assertEqual(to.main(["verify", "--dir", str(self.dir)]), 1)


# ================================================================================================ the branch

class GitRemoteCase(unittest.TestCase):
    """A local bare repository standing in for GitHub, and a working copy standing in for actions/checkout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.base = base
        self.remote = base / "remote.git"
        git(base, "init", "-q", "--bare", str(self.remote))
        self.repo = base / "checkout"
        git(base, "init", "-q", str(self.repo))
        git(self.repo, "checkout", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hi", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-q", "origin", "main")
        self.main_before = git(self.remote, "rev-parse", "main")

    def bundle(self, name, created_at, seconds=1.0):
        bundle = make_bundle(self.base / name, stem=f"ringshift-tiktok-{created_at[:10].replace('-', '')}-{name}",
                             created_at=created_at, seconds=seconds)
        return bundle, json.loads(bundle["metadata"].read_text())

    def files(self):
        return sorted(git(self.remote, "ls-tree", "-r", "--name-only", to.OUTPUT_BRANCH).split())


@unittest.skipUnless(HAVE_GIT and HAVE_FFMPEG, "git and ffmpeg needed")
class SaveBranchTests(GitRemoteCase):

    def test_first_save_creates_the_branch_with_video_metadata_and_preview(self):
        bundle, meta = self.bundle("aaa", "2026-11-01T07:45:00+00:00")
        result = to.save(bundle, meta, repo_dir=self.repo, source_run_id="999", repository="o/r")
        self.assertEqual(result["status"], "saved")
        name = result["name"]
        self.assertEqual(name, "20261101T074500Z_ringshift-tiktok-20261101-aaa")
        self.assertEqual(self.files(), sorted([f"TikTok-Output/{name}.mp4", f"TikTok-Output/{name}.json",
                                               f"TikTok-Output/{name}_preview.jpg", "TikTok-Output/index.json",
                                               "TikTok-Output/README.md"]))
        stored = subprocess.run(["git", "show", f"{to.OUTPUT_BRANCH}:TikTok-Output/{name}.mp4"], cwd=self.remote,
                                capture_output=True).stdout
        self.assertEqual(stored, bundle["video"].read_bytes())
        index = json.loads(git(self.remote, "show", f"{to.OUTPUT_BRANCH}:TikTok-Output/index.json"))
        self.assertEqual(index["videos"][0]["source_run_id"], "999")
        self.assertEqual(index["videos"][0]["source_run_url"], "https://github.com/o/r/actions/runs/999")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.main_before)

    def test_saving_the_same_video_again_changes_nothing(self):
        bundle, meta = self.bundle("aaa", "2026-11-01T07:45:00+00:00")
        to.save(bundle, meta, repo_dir=self.repo)
        head = git(self.remote, "rev-parse", to.OUTPUT_BRANCH)
        again = to.save(bundle, meta, repo_dir=self.repo)
        self.assertEqual(again["status"], "already-saved")
        self.assertEqual(git(self.remote, "rev-parse", to.OUTPUT_BRANCH), head)

    def test_rolling_retention_keeps_the_newest_and_history_stays_one_commit(self):
        for i, day in enumerate(("01", "02", "03")):
            bundle, meta = self.bundle(f"v{i}", f"2026-11-{day}T07:45:00+00:00")
            to.save(bundle, meta, repo_dir=self.repo, keep=2)
        videos = [f for f in self.files() if f.endswith(".mp4")]
        self.assertEqual(len(videos), 2)
        self.assertFalse(any("v0" in f for f in self.files()), "the oldest video and its companions are gone")
        self.assertEqual(git(self.remote, "rev-list", "--count", to.OUTPUT_BRANCH), "1")
        index = json.loads(git(self.remote, "show", f"{to.OUTPUT_BRANCH}:TikTok-Output/index.json"))
        self.assertEqual([e["name"][-2:] for e in index["videos"]], ["v2", "v1"])

    def test_a_save_that_races_another_save_retries_and_keeps_both(self):
        first, first_meta = self.bundle("aaa", "2026-11-01T07:45:00+00:00")
        second, second_meta = self.bundle("bbb", "2026-11-02T07:45:00+00:00")
        real_git = media_store._git
        raced = []

        def racing_git(cwd, *args, check=True):
            if args[:1] == ("push",) and not raced:
                raced.append(True)
                to.save(first, first_meta, repo_dir=self.repo)       # another run saves in between
            return real_git(cwd, *args, check=check)

        with mock.patch.object(media_store, "_git", side_effect=racing_git):
            result = to.save(second, second_meta, repo_dir=self.repo)
        self.assertEqual(result["status"], "saved")
        names = [f for f in self.files() if f.endswith(".mp4")]
        self.assertEqual(len(names), 2, names)

    def test_refuses_protected_branches(self):
        bundle, meta = self.bundle("aaa", "2026-11-01T07:45:00+00:00")
        for branch in ("main", "master", "media-store", "tiktok-state", "tiktok-auth-state", "refs/heads/x", ""):
            with self.assertRaises(to.OutputError):
                to.save(bundle, meta, repo_dir=self.repo, branch=branch)

    def test_an_unreachable_remote_is_an_error_not_a_missing_branch(self):
        git(self.repo, "remote", "set-url", "origin", str(self.base / "does-not-exist.git"))
        bundle, meta = self.bundle("aaa", "2026-11-01T07:45:00+00:00")
        with self.assertRaises(to.OutputError):
            to.save(bundle, meta, repo_dir=self.repo)


@unittest.skipUnless(HAVE_GIT and HAVE_FFMPEG, "git and ffmpeg needed")
class DuplicateAuthorizationHeaderRegressionTests(GitRemoteCase):
    """The checkout carries actions/checkout's Authorization header in its local git config; the workflow passes the
    same header again in RINGSHIFT_STORE_AUTH_HEADER. Every git command must see it exactly once."""

    def test_every_git_command_sees_the_authorization_header_exactly_once(self):
        git(self.repo, "config", "--local", HEADER_KEY, "AUTHORIZATION: basic Y2hlY2tvdXQ=")      # what checkout does
        header = git(self.repo, "config", "--get", HEADER_KEY)
        real_git = media_store._git
        seen = []

        def counting_git(cwd, *args, check=True):
            if args and args[0] in ("ls-remote", "fetch", "push"):
                counted = subprocess.run(["git", "config", "--get-all", HEADER_KEY], cwd=str(cwd),
                                         capture_output=True, text=True, env=media_store._git_env())
                seen.append((args[0], Path(cwd).resolve(), counted.stdout.strip().splitlines()))
                self.assertFalse(any("extraheader" in a for a in args), "the header must never be on a command line")
            return real_git(cwd, *args, check=check)

        bundle, meta = self.bundle("aaa", "2026-11-01T07:45:00+00:00")
        with mock.patch.dict(os.environ, {media_store.AUTH_HEADER_ENV: header}), \
                mock.patch.object(media_store, "_git", side_effect=counting_git):
            to.save(bundle, meta, repo_dir=self.repo)
        self.assertEqual({op for op, _, _ in seen}, {"ls-remote", "push"})
        for op, cwd, values in seen:
            self.assertNotEqual(cwd, self.repo.resolve(), f"{op} ran inside the checkout")
            self.assertEqual(values, [header], f"{op} would send {len(values)} Authorization header(s)")

    def test_the_old_inline_approach_really_sent_two_headers(self):
        """Documents the bug: `git -c <key>=<header>` inside the checkout yields two values for the key."""
        git(self.repo, "config", "--local", HEADER_KEY, "AUTHORIZATION: basic Y2hlY2tvdXQ=")
        header = git(self.repo, "config", "--get", HEADER_KEY)
        values = git(self.repo, "-c", f"{HEADER_KEY}={header}", "config", "--get-all", HEADER_KEY).splitlines()
        self.assertEqual(len(values), 2)


@unittest.skipUnless(HAVE_GIT and HAVE_FFMPEG, "git and ffmpeg needed")
class SaveCommandLineTests(unittest.TestCase):
    def test_save_cli_writes_summary_and_outputs_and_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote = base / "remote.git"
            git(base, "init", "-q", "--bare", str(remote))
            repo = base / "checkout"
            git(base, "init", "-q", str(repo))
            git(repo, "remote", "add", "origin", str(remote))
            make_bundle(base / "art")
            summary, outputs = base / "summary.md", base / "outputs.txt"
            env = {"GITHUB_STEP_SUMMARY": str(summary), "GITHUB_OUTPUT": str(outputs), "GITHUB_REPOSITORY": "o/r"}
            real_save = to.save
            with mock.patch.dict(os.environ, env):
                with mock.patch.object(to, "save", side_effect=lambda b, m, **k: real_save(b, m, repo_dir=repo, **k)):
                    self.assertEqual(to.main(["save", "--dir", str(base / "art"), "--source-run-id", "5"]), 0)
                    self.assertEqual(to.main(["save", "--dir", str(base / "art"), "--source-run-id", "5"]), 0)
            text = summary.read_text()
            self.assertIn("TikTok-Output: SAVED", text)
            self.assertIn("already saved", text)
            self.assertIn("https://github.com/o/r/blob/tiktok-output/TikTok-Output/", text)
            self.assertIn("saved=saved", outputs.read_text())
            self.assertIn("saved=already-saved", outputs.read_text())

            git(repo, "remote", "set-url", "origin", str(base / "missing.git"))
            make_bundle(base / "art2", stem="ringshift-tiktok-20261102-gameplay-ffff0000")
            with mock.patch.dict(os.environ, env), mock.patch("sys.stderr"):
                with mock.patch.object(to, "save", side_effect=lambda b, m, **k: real_save(b, m, repo_dir=repo, **k)):
                    self.assertEqual(to.main(["save", "--dir", str(base / "art2")]), 1)
            self.assertIn("NOT SAVED", summary.read_text())


# ================================================================================================ re-runs

class RerunHandsOffTheExistingVideoTests(unittest.TestCase):
    """10/31: the video was made and the clip recorded, but the save failed. A re-run of that run says "already-done";
    it must still name the earlier artifact so the save job can save it, instead of producing nothing."""

    def test_already_done_names_the_earlier_artifact(self):
        import tiktok_video as tv
        with tempfile.TemporaryDirectory() as tmp:
            outputs = Path(tmp) / "out.txt"
            previous = {"output_name": "ringshift-tiktok-20261031-defining-america-in-a-single-wor-c9b65473"}
            done = ("already-done", {"status": "already-done", "previous": previous, "why": "already made"}, {})
            args = mock.Mock(state=str(Path(tmp) / "s.json"), seed=None, funny_file=None, funny_dir=str(tv.FUNNY_DIR),
                             out_dir=tmp, gameplay_dir=tmp, game_dir=None, gameplay="auto", allow_reuse=False,
                             width=1080, format="auto")
            with mock.patch.object(tv, "make_video", return_value=done), \
                    mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(outputs)}), mock.patch("builtins.print"):
                self.assertEqual(tv._cmd_make(args), 0)
            text = outputs.read_text()
        self.assertIn("status=already-done", text)
        self.assertIn(f"artifact_name={previous['output_name']}", text)


# ================================================================================================ workflow contract

def step(workflow, name):
    marker = f"- name: {name}\n"
    if marker not in workflow:
        raise AssertionError(f"no step named {name!r}")
    return workflow.split(marker, 1)[1].split("\n      - name:", 1)[0]


def job(workflow, name):
    block = workflow.split(f"\n  {name}:\n", 1)[1]
    return re.split(r"\n  [a-z][a-z-]*:\n", block, maxsplit=1)[0]


class OutputWorkflowContractTests(unittest.TestCase):
    def test_no_workflow_passes_the_auth_header_on_a_git_command_line(self):
        for path in WORKFLOWS.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"git[^\n]*-c\s+\"?http\.https://github\.com/\.extraheader=", path.name)

    def test_saving_is_its_own_job_using_the_tested_script(self):
        save = job(VIDEO_WORKFLOW, "save")
        self.assertIn("needs: video", save)
        self.assertIn("always()", save)
        self.assertIn("contents: write", save)
        self.assertIn("python scripts/tiktok_output.py save --dir tiktok-artifact", save)
        self.assertIn("RINGSHIFT_STORE_AUTH_HEADER=", step(VIDEO_WORKFLOW, "Save finished video to TikTok-Output"))
        self.assertNotIn("tiktok_output.py", job(VIDEO_WORKFLOW, "video"))

    def test_the_save_job_downloads_the_exact_artifact_through_the_api(self):
        download = step(VIDEO_WORKFLOW, "Download the video artifact")
        self.assertIn("name: ${{ steps.source.outputs.artifact_name }}", download)
        self.assertIn("run-id: ${{ steps.source.outputs.run_id }}", download)
        self.assertIn("github-token: ${{ github.token }}", download)

    def test_a_rerun_or_recovery_still_saves_the_existing_video(self):
        video = job(VIDEO_WORKFLOW, "video")
        self.assertIn("steps.make.outputs.status == 'already-done' && steps.make.outputs.artifact_name", video)
        self.assertIn("recover_run_id:", VIDEO_WORKFLOW)
        source = step(VIDEO_WORKFLOW, "Find the video artifact")
        self.assertIn("RECOVER_RUN_ID: ${{ inputs.recover_run_id }}", source)
        self.assertNotIn("${{", source.split("run: |", 1)[1])
        self.assertIn("^[0-9]+$", source)

    def test_the_handoff_passes_run_id_and_artifact_and_only_from_main(self):
        send = step(VIDEO_WORKFLOW, "Send to the TikTok inbox (draft)")
        self.assertIn("github.ref == 'refs/heads/main'", send)
        self.assertIn("gh workflow run tiktok-upload.yml", send)
        self.assertIn('-f source_run_id="$SOURCE_RUN_ID" -f artifact_name="$ARTIFACT_NAME"', send)
        self.assertIn("actions: write", job(VIDEO_WORKFLOW, "save"))
        self.assertLess(VIDEO_WORKFLOW.index("- name: Save finished video to TikTok-Output"),
                        VIDEO_WORKFLOW.index("- name: Send to the TikTok inbox (draft)"))

    def test_the_video_workflow_still_holds_no_tiktok_secret(self):
        self.assertNotIn("secrets.", VIDEO_WORKFLOW)
        self.assertNotIn("TIKTOK_", VIDEO_WORKFLOW)

    def test_the_upload_workflow_checks_setup_rechecks_and_dedupes(self):
        self.assertIn("source_run_id:", UPLOAD_WORKFLOW)
        self.assertIn("force:", UPLOAD_WORKFLOW)
        self.assertIn("python scripts/tiktok_output.py verify --dir tiktok-artifact", step(UPLOAD_WORKFLOW, "Re-check the video"))
        upload = step(UPLOAD_WORKFLOW, "Upload video to TikTok inbox as a draft")
        self.assertIn('--ledger "$RUNNER_TEMP/tiktok_state.json"', upload)
        self.assertNotIn("${{", upload.split("run: |", 1)[1])
        record = step(UPLOAD_WORKFLOW, "Record the upload (tiktok-state branch)")
        self.assertIn("always()", record)
        self.assertIn("tiktok_state.py push", record)
        self.assertLess(UPLOAD_WORKFLOW.index("- name: Check the TikTok setup"),
                        UPLOAD_WORKFLOW.index("- name: Upload video to TikTok inbox as a draft"))
        self.assertNotIn("video.publish", UPLOAD_WORKFLOW)
        self.assertNotIn("/v2/post/publish/video/init", (ROOT / "scripts" / "tiktok_upload.py").read_text())

    def test_x_buffer_workflows_do_not_mention_tiktok(self):
        for name in ("autopilot.yml", "game-lab.yml", "outreach.yml"):
            self.assertNotIn("tiktok", (WORKFLOWS / name).read_text(encoding="utf-8").lower(), name)


if __name__ == "__main__":
    unittest.main()
