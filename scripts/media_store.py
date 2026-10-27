"""Host new clips on a separate `media-store` branch instead of on `main`.

Why: every video committed to `main` stays in the repository history forever,
and the repo already holds ~190 MB of media. The `media-store` branch is
rewritten as a single commit on every publish and holds only the newest clips
(default 60), so it never grows without bound, and `main` stays small.

Buffer fetches media from a public URL. Files on the branch are served by
raw.githubusercontent.com, the same host the existing posts already use:

    https://raw.githubusercontent.com/<owner>/<repo>/media-store/store/<file>

This module never touches `main`, your working tree, or any other branch. It
builds the branch in a throwaway clone and force-pushes only `media-store`.

    python scripts/media_store.py publish media/captured/*.mp4 [--keep 60]
    python scripts/media_store.py list
    python scripts/media_store.py verify

Publishing needs git push access (on your PC, the credentials you already use).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import media_tools as tools

BASE_DIR = Path(__file__).resolve().parent.parent
TAGS_FILE = BASE_DIR / "media" / "media_tags.json"

STORE_BRANCH = "media-store"
STORE_DIR = "store"
MANIFEST = "manifest.json"
DEFAULT_KEEP = 60
PROTECTED_BRANCHES = {"main", "master"}


class StoreError(RuntimeError):
    pass


AUTH_HEADER_ENV = "RINGSHIFT_STORE_AUTH_HEADER"


def _git_env():
    """In GitHub Actions the temporary clone has no credentials. The workflow passes the Authorization header that
    actions/checkout configured for this repository in RINGSHIFT_STORE_AUTH_HEADER; it is handed to these git
    processes only (through git's GIT_CONFIG_* variables, never on the command line or in a config file) and is
    never printed."""
    header = os.environ.get(AUTH_HEADER_ENV, "").strip()
    if not header:
        return None
    env = dict(os.environ)
    env.pop(AUTH_HEADER_ENV, None)
    count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    env[f"GIT_CONFIG_KEY_{count}"] = "http.https://github.com/.extraheader"
    env[f"GIT_CONFIG_VALUE_{count}"] = header
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env


def _git(cwd, *args, check=True):
    result = subprocess.run(
        ["git", "-c", "user.name=ringshift-media-store", "-c", "user.email=media-store@users.noreply.github.com",
         "-c", "core.autocrlf=false", *args],
        cwd=str(cwd), capture_output=True, text=True, env=_git_env())
    if check and result.returncode != 0:
        raise StoreError(f"git {' '.join(args[:2])} failed: {(result.stderr or result.stdout).strip()[:400]}")
    return result


def remote_url(repo_dir=BASE_DIR, remote="origin"):
    result = _git(repo_dir, "remote", "get-url", remote, check=False)
    if result.returncode != 0:
        raise StoreError(f"no git remote named {remote!r} in {repo_dir}")
    return result.stdout.strip()


def owner_repo(url):
    """('owner', 'repo') from an https or ssh GitHub URL."""
    match = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    if not match:
        raise StoreError(f"cannot read owner/repo from remote URL {url!r}")
    return match.group(1), match.group(2)


def raw_url(owner, repo, name, branch=STORE_BRANCH):
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{STORE_DIR}/{quote(name)}"


def safe_name(name):
    """File names are kept URL-safe so Buffer can fetch them without escaping surprises."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._")
    if not cleaned:
        raise StoreError(f"cannot build a safe file name from {name!r}")
    return cleaned


def publish(files, *, repo_dir=BASE_DIR, remote="origin", branch=STORE_BRANCH, keep=DEFAULT_KEEP,
            validate=True, push=True, now=None):
    """Upload `files` to the store branch. Returns [{'name', 'file', 'url', 'type', 'bytes'}]."""
    if branch in PROTECTED_BRANCHES:
        raise StoreError(f"refusing to write to protected branch {branch!r}")

    files = [Path(f) for f in files]
    now = now or datetime.now(timezone.utc)

    for path in files:
        if not path.is_file():
            raise StoreError(f"file not found: {path}")
        if validate:
            problems = tools.validate_media(path)
            if problems:
                raise StoreError(f"{path.name} failed validation, not publishing: " + "; ".join(problems))

    url = remote_url(repo_dir, remote)
    owner, repo = owner_repo(url) if "github.com" in url else ("local", "local")

    published = []

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "store-clone"
        work.mkdir()
        _git(work, "init", "-q")
        _git(work, "remote", "add", "origin", url)

        fetched = _git(work, "fetch", "-q", "--depth", "1", "origin", branch, check=False)
        if fetched.returncode == 0:
            _git(work, "checkout", "-q", "-B", branch, "FETCH_HEAD")
        else:
            _git(work, "checkout", "-q", "--orphan", branch)

        store = work / STORE_DIR
        store.mkdir(exist_ok=True)

        manifest_path = work / MANIFEST
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, list):
                manifest = []
        except (OSError, json.JSONDecodeError):
            manifest = []

        for path in files:
            name = safe_name(path.name)
            shutil.copy2(path, store / name)
            manifest = [m for m in manifest if m.get("name") != name]
            manifest.append({"name": name, "bytes": path.stat().st_size, "added_at": now.isoformat(timespec="seconds")})
            published.append({
                "name": name, "file": str(path), "url": raw_url(owner, repo, name, branch),
                "type": "video" if path.suffix.lower() in tools.VIDEO_EXTENSIONS else "image",
                "bytes": path.stat().st_size,
            })

        # Retention: drop the oldest clips beyond `keep`.
        manifest.sort(key=lambda m: m.get("added_at", ""))
        while len(manifest) > keep:
            oldest = manifest.pop(0)
            (store / oldest["name"]).unlink(missing_ok=True)

        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        (work / "README.md").write_text(
            "# media-store\n\nGenerated by scripts/media_store.py. Holds the newest Ringshift clips for Buffer to fetch.\n"
            "This branch is rewritten as one commit on every publish; do not build on it.\n", encoding="utf-8")

        # Squash to a single commit so history never accumulates old videos.
        _git(work, "checkout", "-q", "--orphan", "squashed")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", f"media-store: {len(manifest)} clip(s), updated {now:%Y-%m-%d %H:%M} UTC")
        _git(work, "branch", "-q", "-M", branch)

        if push:
            _git(work, "push", "-q", "--force", "origin", f"{branch}:{branch}")

    return published


def list_store(repo_dir=BASE_DIR, remote="origin", branch=STORE_BRANCH):
    """Manifest entries currently on the remote store branch."""
    url = remote_url(repo_dir, remote)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        _git(work, "init", "-q")
        _git(work, "remote", "add", "origin", url)
        if _git(work, "fetch", "-q", "--depth", "1", "origin", branch, check=False).returncode != 0:
            return []
        _git(work, "checkout", "-q", "FETCH_HEAD")
        try:
            return json.loads((work / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []


def register_in_catalog(published, tags_file=TAGS_FILE):
    """Point media_tags.json at the hosted URLs.

    Each local entry (e.g. media/captured/x.mp4) is replaced by a hosted entry
    (store/x.mp4) that carries its tags and description plus the public URL."""
    try:
        catalog = json.loads(Path(tags_file).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        catalog = {}
    if not isinstance(catalog, dict):
        catalog = {}

    for item in published:
        local = Path(item["file"])
        old_key = next((k for k in catalog if Path(k).name == local.name and not k.startswith(STORE_DIR + "/")), None)
        entry = dict(catalog.pop(old_key)) if old_key and isinstance(catalog.get(old_key), dict) else {}
        entry.update({"url": item["url"], "type": item["type"], "bytes": item["bytes"], "ref": STORE_BRANCH,
                      "hosted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        catalog[f"{STORE_DIR}/{item['name']}"] = entry

    Path(tags_file).write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return catalog


def verify_urls(urls, timeout=30):
    """HEAD every URL; returns [(url, ok, detail)]. Uses the network, so tests mock it."""
    import requests

    results = []
    for url in urls:
        try:
            response = requests.head(url, timeout=timeout, allow_redirects=False)
            ok = response.status_code == 200
            results.append((url, ok, f"HTTP {response.status_code}, {response.headers.get('content-type', '?')}"))
        except requests.RequestException as error:
            results.append((url, False, f"{type(error).__name__}"))
    return results


# ---- command line ------------------------------------------------------------

def _cmd_publish(args):
    published = publish(args.files, keep=args.keep, validate=not args.no_validate, push=not args.no_push)
    if args.no_push:
        for item in published:
            print(f"would host  {item['name']}  ->  {item['url']}")
        print("(--no-push: nothing was uploaded and the catalog was not changed)")
    else:
        register_in_catalog(published)
        for item in published:
            print(f"hosted  {item['name']}  ->  {item['url']}")
        print("\nCommit media/media_tags.json so the scheduled workflow knows about these clips.")
    if args.delete_local and not args.no_push:
        for item in published:
            Path(item["file"]).unlink(missing_ok=True)
        print("Deleted the local copies.")
    return 0


def _cmd_list(args):
    entries = list_store()
    for entry in entries:
        print(f"{entry['name']:60s} {entry.get('bytes', 0) / 1048576:6.1f} MB  {entry.get('added_at', '')}")
    print(f"\n{len(entries)} clip(s) on the {STORE_BRANCH} branch")
    return 0


def _cmd_verify(args):
    catalog = json.loads(TAGS_FILE.read_text(encoding="utf-8-sig"))
    urls = [e["url"] for e in catalog.values() if isinstance(e, dict) and e.get("url")]
    bad = 0
    for url, ok, detail in verify_urls(urls):
        print(("OK    " if ok else "FAIL  ") + f"{url}  {detail}")
        bad += 0 if ok else 1
    return 1 if bad else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Host clips on the media-store branch for Buffer.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("publish", help="validate, upload and register clips")
    p.add_argument("files", nargs="+")
    p.add_argument("--keep", type=int, default=DEFAULT_KEEP, help=f"clips to retain (default {DEFAULT_KEEP})")
    p.add_argument("--no-validate", action="store_true")
    p.add_argument("--no-push", action="store_true", help="build locally and register, but do not upload")
    p.add_argument("--delete-local", action="store_true", help="remove local copies after a successful upload")
    p.set_defaults(func=_cmd_publish)

    sub.add_parser("list", help="show clips on the branch").set_defaults(func=_cmd_list)
    sub.add_parser("verify", help="check every hosted URL answers 200").set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)

    try:
        return args.func(args)
    except (StoreError, tools.MediaToolError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
