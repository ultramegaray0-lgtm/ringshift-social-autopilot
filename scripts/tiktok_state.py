"""Which funny clips and gameplay were already turned into TikTok videos, kept OFF the main branch.

The state is one small JSON file. In GitHub Actions it lives on its own branch, `tiktok-state` (never `main`, never
`media-store`), so the TikTok workflow never commits to main and can never collide with, block or corrupt the daily
X autopilot, which commits its own state to main. Locally the same file sits at data/tiktok_state.json (git-ignored).

    python scripts/tiktok_state.py pull --to data/tiktok_state.json     # copy the branch's state here (empty if none)
    python scripts/tiktok_state.py push --from data/tiktok_state.json   # merge this state into the branch

A push never overwrites: it fetches the branch, merges the two states (union of videos by clip hash and run, highest
failure counts) and pushes a new commit on top, retrying if another run pushed first. The git work happens in a
throwaway clone with the same credential hand-off as media_store.py (RINGSHIFT_STORE_AUTH_HEADER, never printed).
"""

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import media_store

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "tiktok_state.json"
STATE_BRANCH = "tiktok-state"
BRANCH_FILE = "tiktok_state.json"
SCHEMA = "ringshift-tiktok-state/1"
PROTECTED = {"main", "master", media_store.STORE_BRANCH}
MAX_FAILURES = 3            # a clip whose video failed this many times is no longer picked automatically
KEEP_VIDEOS = 1000


class StateError(RuntimeError):
    pass


def empty_state():
    return {"schema": SCHEMA, "videos": [], "failures": {}, "gameplay_uses": {}, "scenes": []}


def normalise(state):
    """A well-formed state from whatever was read (missing keys filled, wrong types replaced)."""
    if not isinstance(state, dict):
        return empty_state()
    clean = empty_state()
    clean["videos"] = [v for v in state.get("videos") or [] if isinstance(v, dict) and v.get("funny_sha256")]
    clean["failures"] = {k: v for k, v in (state.get("failures") or {}).items() if isinstance(v, dict)}
    clean["gameplay_uses"] = {k: int(v) for k, v in (state.get("gameplay_uses") or {}).items()
                              if isinstance(v, (int, float))}
    clean["scenes"] = [s for s in state.get("scenes") or [] if isinstance(s, dict) and s.get("scene")]
    return clean


def load(path=STATE_FILE):
    try:
        return normalise(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    except (OSError, json.JSONDecodeError):
        return empty_state()


def save(state, path=STATE_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(normalise(state), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


# ---------------------------------------------------------------------------------------------------- queries

def produced(state):
    return [v for v in state["videos"] if v.get("status") == "produced"]


def used_hashes(state):
    return {v["funny_sha256"] for v in produced(state)}


def video_for_hash(state, sha256):
    return next((v for v in reversed(produced(state)) if v["funny_sha256"] == sha256), None)


def video_for_run(state, run_id):
    """The video this workflow run already produced (a re-run of the same run must not make a second one)."""
    if not run_id:
        return None
    return next((v for v in reversed(produced(state)) if str(v.get("run_id")) == str(run_id)), None)


def failure_count(state, sha256):
    return int((state["failures"].get(sha256) or {}).get("count", 0))


# ---------------------------------------------------------------------------------------------------- updates

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_video(state, entry):
    """Add a produced video. `entry` needs funny_sha256 and funny_file; status is set to produced."""
    entry = dict(entry, status="produced")
    entry.setdefault("at", now_iso())
    state["videos"] = (state["videos"] + [entry])[-KEEP_VIDEOS:]
    state["failures"].pop(entry["funny_sha256"], None)
    for sha in entry.get("gameplay_sha256") or []:
        state["gameplay_uses"][sha] = state["gameplay_uses"].get(sha, 0) + 1
    for scene in entry.get("scenes") or []:
        state["scenes"].append({"scene": scene.get("scene"), "labels": scene.get("labels") or [], "at": entry["at"]})
    state["scenes"] = state["scenes"][-200:]
    return state


def record_failure(state, sha256, funny_file, reason, run_id=None):
    previous = state["failures"].get(sha256) or {}
    state["failures"][sha256] = {"file": funny_file, "count": int(previous.get("count", 0)) + 1,
                                 "last_reason": str(reason)[:500], "last_run_id": run_id, "at": now_iso()}
    return state


def merge(a, b):
    """Union of two states (e.g. the branch's and this run's). Videos are keyed by (clip hash, run id, output name);
    failure counts and gameplay uses keep the larger value."""
    a, b = normalise(a), normalise(b)
    merged = empty_state()
    seen = set()
    for video in a["videos"] + b["videos"]:
        key = (video.get("funny_sha256"), str(video.get("run_id")), video.get("output_name"), video.get("status"))
        if key in seen:
            continue
        seen.add(key)
        merged["videos"].append(video)
    merged["videos"].sort(key=lambda v: v.get("at") or "")
    merged["videos"] = merged["videos"][-KEEP_VIDEOS:]
    produced_hashes = {v["funny_sha256"] for v in merged["videos"] if v.get("status") == "produced"}
    for source in (a, b):
        for sha, failure in source["failures"].items():
            if sha in produced_hashes:
                continue
            if failure_count(merged, sha) < int(failure.get("count", 0)):
                merged["failures"][sha] = failure
        for sha, count in source["gameplay_uses"].items():
            merged["gameplay_uses"][sha] = max(merged["gameplay_uses"].get(sha, 0), count)
    scene_keys = set()
    for scene in a["scenes"] + b["scenes"]:
        key = (scene.get("scene"), scene.get("at"))
        if key not in scene_keys:
            scene_keys.add(key)
            merged["scenes"].append(scene)
    merged["scenes"].sort(key=lambda s: s.get("at") or "")
    merged["scenes"] = merged["scenes"][-200:]
    return merged


# ---------------------------------------------------------------------------------------------------- the branch

def _check_branch(branch):
    if not branch or branch in PROTECTED or branch.startswith("refs/"):
        raise StateError(f"refusing to keep TikTok state on branch {branch!r}")


def _clone(work, url, branch):
    """Fetch `branch` into an empty repository at `work`. Returns True if the branch exists."""
    media_store._git(work, "init", "-q")
    media_store._git(work, "remote", "add", "origin", url)
    fetched = media_store._git(work, "fetch", "-q", "--depth", "1", "origin", branch, check=False)
    if fetched.returncode != 0:
        text = (fetched.stderr or "") + (fetched.stdout or "")
        if "couldn't find remote ref" in text or "not found" in text.lower():
            return False
        raise StateError(f"could not fetch the {branch} branch: {text.strip()[:300]}")
    media_store._git(work, "checkout", "-q", "-B", branch, "FETCH_HEAD")
    return True


def pull(repo_dir=BASE_DIR, branch=STATE_BRANCH, remote="origin"):
    """The state stored on the branch, or an empty state if the branch does not exist yet."""
    _check_branch(branch)
    url = media_store.remote_url(repo_dir, remote)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        if not _clone(work, url, branch):
            return empty_state()
        return load(work / BRANCH_FILE)


def push(state, repo_dir=BASE_DIR, branch=STATE_BRANCH, remote="origin", attempts=4, message=None):
    """Merge `state` into the branch's state and push it (fast-forward only, never --force). Returns the merged state."""
    _check_branch(branch)
    url = media_store.remote_url(repo_dir, remote)
    last = None
    for _ in range(attempts):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            exists = _clone(work, url, branch)
            if not exists:
                media_store._git(work, "checkout", "-q", "--orphan", branch)
            merged = merge(load(work / BRANCH_FILE) if exists else empty_state(), state)
            save(merged, work / BRANCH_FILE)
            (work / "README.md").write_text(
                "# tiktok-state\n\nWritten by scripts/tiktok_state.py: which funny clips and gameplay files were already "
                "turned into TikTok videos. Not code; never merge this branch.\n", encoding="utf-8")
            media_store._git(work, "add", BRANCH_FILE, "README.md")
            if media_store._git(work, "diff", "--cached", "--quiet", check=False).returncode == 0:
                return merged                    # nothing new to store
            media_store._git(work, "commit", "-q", "-m", message or f"TikTok state: {len(produced(merged))} video(s)")
            pushed = media_store._git(work, "push", "-q", "origin", f"{branch}:{branch}", check=False)
            if pushed.returncode == 0:
                return merged
            last = (pushed.stderr or pushed.stdout or "").strip()[:300]
    raise StateError(f"could not push the {branch} branch after {attempts} attempts: {last}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pull or push the TikTok video state (kept on its own branch).")
    sub = parser.add_subparsers(dest="command", required=True)
    p_pull = sub.add_parser("pull")
    p_pull.add_argument("--to", default=str(STATE_FILE))
    p_pull.add_argument("--branch", default=STATE_BRANCH)
    p_push = sub.add_parser("push")
    p_push.add_argument("--from", dest="source", default=str(STATE_FILE))
    p_push.add_argument("--branch", default=STATE_BRANCH)
    args = parser.parse_args(argv)
    try:
        if args.command == "pull":
            state = pull(branch=args.branch)
            save(state, args.to)
            print(f"TikTok state: {len(produced(state))} video(s) made so far, {len(state['failures'])} clip(s) with failures.")
        else:
            merged = push(load(args.source), branch=args.branch)
            print(f"TikTok state saved on branch {args.branch}: {len(produced(merged))} video(s) in total.")
    except (StateError, media_store.StoreError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
