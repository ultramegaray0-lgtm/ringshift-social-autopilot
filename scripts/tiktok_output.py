"""Check a finished TikTok video bundle and keep it in the TikTok-Output/ folder of the `tiktok-output` branch.

A bundle is what the TikTok video workflow uploads as an Actions artifact: one MP4, its metadata JSON and a preview
JPG, all named after the same stem. Before anything is saved the bundle is checked again, independently of the run
that made it, so an empty, truncated or unvalidated file can never reach the branch:

- the metadata says status "produced" and every validation check passed;
- the MP4's SHA-256 and byte count match the metadata, and it is at least MIN_VIDEO_BYTES;
- ffprobe sees one H.264 video stream at exactly 1080x1920 and the duration the metadata recorded;
- FFmpeg decodes every frame without an error (the file actually plays);
- the preview is a non-empty JPEG.

    python scripts/tiktok_output.py verify --dir tiktok-artifact
    python scripts/tiktok_output.py save   --dir tiktok-artifact --source-run-id 123456789

Why this is a script and not shell in the workflow: the first version of the save step ran `git -c
http.https://github.com/.extraheader=...` inside the actions/checkout working copy, which already carries that header in
its local config. Git then sent TWO Authorization headers and GitHub rejected every request with HTTP 400 ("Duplicate
header: Authorization"), so the branch was never created and TikTok-Output stayed empty. Here every git command runs in
a throwaway repository that has no credentials of its own; the one header is handed over through git's GIT_CONFIG_*
environment variables (media_store._git, the same path tiktok_state.py has used successfully), never on a command line.

The branch is a rolling snapshot: each save is a single new commit holding the newest KEEP videos, pushed with
--force-with-lease against the commit that was read, so history never grows and two saves cannot overwrite each other.
Saving the same video twice (a re-run, a retry) changes nothing.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import media_store

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_BRANCH = "tiktok-output"
FOLDER = "TikTok-Output"
INDEX = "index.json"
KEEP = 30                                  # rolling retention: the newest 30 videos stay on the branch
WIDTH, HEIGHT = 1080, 1920
MIN_VIDEO_BYTES = 100 * 1024               # a real 5-60 s 1080x1920 H.264 video is far larger; less means broken
MAX_VIDEO_BYTES = 287 * 1024 * 1024        # TikTok's upload limit, the same ceiling tiktok_video.py enforces
DURATION_TOLERANCE = 0.30
PROTECTED = {"main", "master", media_store.STORE_BRANCH, "tiktok-state", "tiktok-auth-state", "outreach-state"}
ARTIFACT_PATTERN = re.compile(r"^ringshift-tiktok-\d{8}-[a-z0-9-]+$")


class OutputError(RuntimeError):
    pass


# ================================================================================================ the bundle

def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_bundle(directory):
    """{"video", "metadata", "preview"} paths of the one video in `directory` (searched two levels deep, the way
    actions/download-artifact lays files out)."""
    root = Path(directory)
    if not root.is_dir():
        raise OutputError(f"{root} does not exist (was the artifact downloaded?)")
    videos = sorted(p for p in root.rglob("*.mp4") if p.is_file())
    if len(videos) != 1:
        raise OutputError(f"expected exactly one MP4 in {root}, found {len(videos)}")
    video = videos[0]
    metadata = video.with_suffix(".json")
    preview = video.with_name(f"{video.stem}_preview.jpg")
    for label, path in (("metadata JSON", metadata), ("preview JPG", preview)):
        if not path.is_file():
            raise OutputError(f"the {label} for {video.name} is missing (expected {path.name})")
    return {"video": video, "metadata": metadata, "preview": preview}


def _probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,pix_fmt",
         "-of", "json", str(path)], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise OutputError(f"ffprobe cannot read {Path(path).name}: {result.stderr.strip()[:300]}")
    return json.loads(result.stdout or "{}")


def _decode_errors(path):
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"],
                            capture_output=True, text=True, timeout=600)
    text = (result.stderr or "").strip()
    if result.returncode != 0 or text:
        return text[:400] or f"ffmpeg exited with {result.returncode}"
    return ""


def verify_bundle(bundle, probe=_probe, decode=_decode_errors):
    """Every reason the bundle must not be saved or uploaded (an empty list means it is good). Returns
    (problems, metadata)."""
    problems = []
    try:
        metadata = json.loads(Path(bundle["metadata"]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"metadata cannot be read: {error}"], {}
    if metadata.get("status") != "produced":
        problems.append(f"metadata status is {metadata.get('status')!r}, not 'produced'")
    validation = metadata.get("validation") or {}
    failed = [name for name, check in (validation.get("checks") or {}).items() if not (check or {}).get("ok")]
    if not validation.get("ok") or failed or not validation.get("checks"):
        problems.append("the video did not pass every quality check" + (f" ({', '.join(failed)})" if failed else ""))
    output = metadata.get("output") or {}
    video = Path(bundle["video"])
    size = video.stat().st_size if video.is_file() else 0
    if output.get("file") != video.name:
        problems.append(f"metadata describes {output.get('file')!r}, not {video.name}")
    if size < MIN_VIDEO_BYTES:
        problems.append(f"the MP4 is only {size} bytes (at least {MIN_VIDEO_BYTES} expected)")
    if size > MAX_VIDEO_BYTES:
        problems.append(f"the MP4 is {size} bytes, over TikTok's {MAX_VIDEO_BYTES}-byte limit")
    if output.get("bytes") is not None and output.get("bytes") != size:
        problems.append(f"the MP4 is {size} bytes but the metadata recorded {output.get('bytes')}")
    if not output.get("sha256"):
        problems.append("metadata has no SHA-256 for the MP4")
    elif size and sha256_file(video) != output["sha256"]:
        problems.append("the MP4's SHA-256 does not match the metadata (changed or corrupted after validation)")
    preview = Path(bundle["preview"])
    head = preview.read_bytes()[:3] if preview.is_file() else b""
    if head != b"\xff\xd8\xff":
        problems.append("the preview is missing or not a JPEG")
    if problems or not size:
        return problems, metadata

    try:
        info = probe(video)
    except (OutputError, subprocess.SubprocessError, ValueError) as error:
        return problems + [str(error)], metadata
    streams = info.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    if len(videos) != 1:
        problems.append(f"{len(videos)} video streams (expected 1)")
    else:
        stream = videos[0]
        if (stream.get("width"), stream.get("height")) != (WIDTH, HEIGHT):
            problems.append(f"{stream.get('width')}x{stream.get('height')}, not {WIDTH}x{HEIGHT}")
        if stream.get("codec_name") != "h264":
            problems.append(f"video codec {stream.get('codec_name')}, not h264")
    try:
        duration = float((info.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    expected = output.get("duration")
    if duration is None or duration <= 0:
        problems.append("ffprobe reports no duration")
    elif expected is not None and abs(duration - float(expected)) > DURATION_TOLERANCE:
        problems.append(f"duration {duration:.2f}s, metadata says {float(expected):.2f}s")
    if not problems:
        errors = decode(video)
        if errors:
            problems.append(f"the MP4 does not decode cleanly: {errors}")
    return problems, metadata


# ================================================================================================ the branch

def entry_name(metadata):
    """The file stem on the branch: <created-at UTC>_<artifact stem>. Sorting by name sorts by age."""
    stem = Path((metadata.get("output") or {}).get("file") or "").stem
    if not stem:
        raise OutputError("metadata has no output file name")
    try:
        created = datetime.fromisoformat(str(metadata.get("created_at"))).astimezone(timezone.utc)
    except ValueError:
        created = datetime.now(timezone.utc)
    return f"{created:%Y%m%dT%H%M%SZ}_{stem}"


def _check_branch(branch):
    if not branch or branch in PROTECTED or branch.startswith("refs/") or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        raise OutputError(f"refusing to save TikTok output on branch {branch!r}")


def _fetch(work, url, branch):
    """Fetch `branch` into the empty repository `work`. Returns the fetched commit, or "" if the branch does not
    exist yet. Any other failure (credentials, network) is an error, never mistaken for "no branch"."""
    media_store._git(work, "init", "-q")
    media_store._git(work, "remote", "add", "origin", url)
    listed = media_store._git(work, "ls-remote", "--heads", "origin", f"refs/heads/{branch}", check=False)
    if listed.returncode != 0:
        raise OutputError(f"could not read the {branch} branch: {_explain((listed.stderr or listed.stdout).strip())}")
    if not listed.stdout.strip():
        return ""
    fetched = media_store._git(work, "fetch", "-q", "--depth", "1", "origin", f"refs/heads/{branch}", check=False)
    if fetched.returncode != 0:
        raise OutputError(f"could not fetch the {branch} branch: {_explain((fetched.stderr or fetched.stdout).strip())}")
    media_store._git(work, "checkout", "-q", "--detach", "FETCH_HEAD")
    return media_store._git(work, "rev-parse", "HEAD").stdout.strip()


def _explain(text):
    """Git's error plus what to do about it."""
    lowered = text.lower()
    hint = ""
    if "duplicate header" in lowered:
        hint = " (two Authorization headers were sent: run git outside the actions/checkout working copy)"
    elif "403" in lowered or "permission" in lowered or "denied" in lowered:
        hint = (" (the workflow token may not write: the job needs `permissions: contents: write`, and Settings > "
                "Actions > General > Workflow permissions must allow it)")
    elif "401" in lowered or "authentication" in lowered or "could not read username" in lowered:
        hint = " (no credential reached git: is RINGSHIFT_STORE_AUTH_HEADER set from actions/checkout?)"
    return text[:400] + hint


def _load_index(folder):
    try:
        data = json.loads((folder / INDEX).read_text(encoding="utf-8"))
        return [e for e in data.get("videos") or [] if isinstance(e, dict) and e.get("name")]
    except (OSError, json.JSONDecodeError, AttributeError):
        return []


def _existing_by_sha(folder, sha):
    for entry in _load_index(folder):
        if entry.get("sha256") == sha and (folder / f"{entry['name']}.mp4").is_file():
            return entry["name"]
    for meta_path in folder.glob("*.json"):
        if meta_path.name == INDEX:
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (meta.get("output") or {}).get("sha256") == sha and meta_path.with_suffix(".mp4").is_file():
            return meta_path.stem
    return None


README = """# Ringshift TikTok output

Finished, validated 1080x1920 TikTok videos made by the **Ringshift TikTok video** workflow.

Each video is three files with the same name:

- `<name>.mp4` - the video, re-checked before it was saved (SHA-256, 1080x1920 H.264, full decode)
- `<name>.json` - what it shows, every quality check, and the caption suggestion
- `<name>_preview.jpg` - a still frame

`index.json` lists them newest first, with the workflow run that made each one. Only the newest {keep} videos are
kept; older ones are removed (each is also an Actions artifact for 30 days).

Nothing here is public. When the TikTok inbox upload is set up, each new video is also sent to the TikTok inbox
as an editable draft (see docs/TIKTOK_UPLOAD.md on main); it goes live only when you tap Post in TikTok.
"""


def _write_snapshot(folder, bundle, name, metadata, source_run_id, keep, repository):
    folder.mkdir(parents=True, exist_ok=True)
    for key, suffix in (("video", ".mp4"), ("metadata", ".json"), ("preview", "_preview.jpg")):
        (folder / f"{name}{suffix}").write_bytes(Path(bundle[key]).read_bytes())
    entries = [e for e in _load_index(folder) if e.get("name") != name]
    output = metadata.get("output") or {}
    entries.append({
        "name": name, "artifact": Path(output.get("file", "")).stem, "sha256": output.get("sha256"),
        "bytes": output.get("bytes"), "duration": output.get("duration"), "format": metadata.get("format"),
        "created_at": metadata.get("created_at"), "caption_suggestion": metadata.get("caption_suggestion"),
        "source_run_id": str(source_run_id or metadata.get("run_id") or ""),
        "source_run_url": (f"https://github.com/{repository}/actions/runs/{source_run_id or metadata.get('run_id')}"
                           if repository and (source_run_id or metadata.get("run_id")) else None),
    })
    # Rolling retention: keep the newest `keep` videos (by name = by creation time) and their companions.
    videos = sorted(p.stem for p in folder.glob("*.mp4"))
    removed = videos[:-keep] if keep and len(videos) > keep else []
    for old in removed:
        for suffix in (".mp4", ".json", "_preview.jpg"):
            (folder / f"{old}{suffix}").unlink(missing_ok=True)
    kept = set(videos) - set(removed)
    entries = sorted((e for e in entries if e["name"] in kept), key=lambda e: e["name"], reverse=True)
    (folder / INDEX).write_text(json.dumps({"videos": entries}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (folder / "README.md").write_text(README.format(keep=keep), encoding="utf-8")
    return removed


def save(bundle, metadata, *, repo_dir=BASE_DIR, remote="origin", branch=OUTPUT_BRANCH, keep=KEEP,
         source_run_id=None, repository=None, attempts=4):
    """Put the (already verified) bundle on `branch` under TikTok-Output/. Returns {"status": "saved"|"already-saved",
    "name", "removed"}."""
    _check_branch(branch)
    url = media_store.remote_url(repo_dir, remote)
    sha = (metadata.get("output") or {}).get("sha256")
    name = entry_name(metadata)
    last = ""
    for _ in range(attempts):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            head = _fetch(work, url, branch)
            folder = work / FOLDER
            existing = _existing_by_sha(folder, sha) if head else None
            if existing:
                return {"status": "already-saved", "name": existing, "removed": []}
            removed = _write_snapshot(folder, bundle, name, metadata, source_run_id, keep, repository)
            # One fresh root commit holding the current snapshot: the branch history never grows.
            media_store._git(work, "checkout", "-q", "--orphan", "snapshot")
            media_store._git(work, "add", "-A", FOLDER)
            media_store._git(work, "commit", "-q", "-m", f"TikTok output: {name}")
            lease = f"--force-with-lease=refs/heads/{branch}:{head}"
            pushed = media_store._git(work, "push", "-q", lease, "origin", f"HEAD:refs/heads/{branch}", check=False)
            if pushed.returncode == 0:
                return {"status": "saved", "name": name, "removed": removed}
            last = _explain((pushed.stderr or pushed.stdout or "").strip())
            if "stale info" not in last and "rejected" not in last and "fetch first" not in last:
                break                                       # not a race with another save: retrying will not help
    raise OutputError(f"could not save to the {branch} branch: {last}")


# ================================================================================================ CLI

def _outputs(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def _summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def _verify_or_raise(directory):
    bundle = find_bundle(directory)
    problems, metadata = verify_bundle(bundle)
    if problems:
        raise OutputError("the video was NOT saved, it failed the re-check: " + "; ".join(problems))
    return bundle, metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description="Re-check a TikTok video bundle and save it to TikTok-Output/.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_verify = sub.add_parser("verify", help="only re-check the bundle")
    p_verify.add_argument("--dir", required=True)
    p_save = sub.add_parser("save", help="re-check the bundle, then save it on the output branch")
    p_save.add_argument("--dir", required=True)
    p_save.add_argument("--branch", default=OUTPUT_BRANCH)
    p_save.add_argument("--keep", type=int, default=KEEP)
    p_save.add_argument("--source-run-id", default=None)
    args = parser.parse_args(argv)
    try:
        bundle, metadata = _verify_or_raise(args.dir)
        output = metadata["output"]
        if args.command == "verify":
            print(f"OK: {bundle['video'].name} ({output['bytes']} bytes, {output['duration']:.1f}s, 1080x1920, decodes)")
            _outputs({"video_file": bundle["video"], "sha256": output["sha256"]})
            return 0
        repository = os.environ.get("GITHUB_REPOSITORY")
        result = save(bundle, metadata, branch=args.branch, keep=args.keep, source_run_id=args.source_run_id,
                      repository=repository)
    except (OutputError, media_store.StoreError) as error:
        print(f"::error::TikTok output: {error}", file=sys.stderr)
        _summary(f"### TikTok-Output: NOT SAVED\n\n{error}\n\nThe video is still in this run's artifact. Fix the cause, "
                 f"then use **Re-run failed jobs** (or run the workflow with `recover_run_id`).")
        return 1
    where = f"{args.branch}:{FOLDER}/{result['name']}.mp4"
    link = (f"https://github.com/{repository}/blob/{args.branch}/{FOLDER}/{result['name']}.mp4" if repository else where)
    if result["status"] == "saved":
        message = f"Saved {result['name']}.mp4 with its metadata and preview to {where}."
        if result["removed"]:
            message += f" Removed {len(result['removed'])} older video(s) (keeping {args.keep})."
    else:
        message = f"Already saved earlier as {result['name']}.mp4 (same SHA-256); nothing changed."
    print(message)
    _summary(f"### TikTok-Output: {'SAVED' if result['status'] == 'saved' else 'already saved'}\n\n- [{result['name']}.mp4]({link})"
             f"\n- {message}\n")
    _outputs({"saved": result["status"], "name": result["name"], "sha256": output["sha256"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
