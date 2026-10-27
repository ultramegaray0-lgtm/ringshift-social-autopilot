"""Propose a validated experiment to the game repository: a NEW lab/ branch plus a pull request. Never touches main.

Used by the level lab and the theme lab. Writing to another repository needs a token that GitHub Actions does not
provide by default (the job's GITHUB_TOKEN only covers THIS repository). Add a fine-grained personal access token as
the repository secret RINGSHIFT_GAME_TOKEN with access to ONLY ultramegaray0-lgtm/ringshift-game and the permissions
"Contents: Read and write" and "Pull requests: Read and write" (docs/AUTOMATION.md). Without it the labs still generate
and validate, upload the result as a run artifact, and then fail with a message saying exactly this.

Locally (no token) the `gh` command line is used for the pull request if it is logged in, and `git push` uses your
own credentials.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import game_source

TOKEN_ENV = "RINGSHIFT_GAME_TOKEN"
API = "https://api.github.com"
PROTECTED = {"main", "master", "gh-pages", "pages-setup"}
BRANCH_RE = re.compile(r"^lab/[a-z0-9][a-z0-9._/-]{2,80}$")
PREVIEW_BRANCH = "game-lab-previews"


class GameRepoError(RuntimeError):
    pass


def _token():
    return os.environ.get(TOKEN_ENV, "").strip() or None


def _git_env(token):
    env = dict(os.environ)
    env.pop(TOKEN_ENV, None)
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        env[f"GIT_CONFIG_KEY_{count}"] = "http.https://github.com/.extraheader"
        env[f"GIT_CONFIG_VALUE_{count}"] = f"AUTHORIZATION: basic {basic}"
        env["GIT_CONFIG_COUNT"] = str(count + 1)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(cwd, *args, token=None, check=True):
    result = subprocess.run(["git", "-c", "user.name=ringshift-lab", "-c", "user.email=ringshift-lab@users.noreply.github.com",
                             *args], cwd=str(cwd), capture_output=True, text=True, env=_git_env(token), timeout=600)
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        if token:
            message = message.replace(token, "***")
        raise GameRepoError(f"git {' '.join(args[:2])} failed: {message[:400]}")
    return result


def safe_branch(prefix, now=None):
    now = now or datetime.now(timezone.utc)
    branch = f"{prefix}-{now:%Y%m%d-%H%M%S}"
    check_branch(branch)
    return branch


def check_branch(branch):
    if branch in PROTECTED or not BRANCH_RE.match(branch):
        raise GameRepoError(f"refusing to write to branch {branch!r}: experiments only go to new lab/... branches")
    return branch


def _create_pr_rest(token, repo, branch, base, title, body):
    import requests
    response = requests.post(f"{API}/repos/{repo}/pulls", timeout=60,
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                      "X-GitHub-Api-Version": "2022-11-28"},
                             json={"title": title, "head": branch, "base": base, "body": body, "maintainer_can_modify": True})
    if response.status_code != 201:
        raise GameRepoError(f"GitHub refused to open the pull request (HTTP {response.status_code}): {response.text[:300]}")
    return response.json()["html_url"]


def _create_pr_gh(repo, branch, base, title, body):
    if not shutil.which("gh"):
        raise GameRepoError(f"no {TOKEN_ENV} and no 'gh' command: cannot open a pull request")
    payload = json.dumps({"title": title, "head": branch, "base": base, "body": body, "maintainer_can_modify": True})
    result = subprocess.run(["gh", "api", f"repos/{repo}/pulls", "--method", "POST", "--input", "-"],
                            input=payload, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise GameRepoError(f"gh could not open the pull request: {(result.stderr or result.stdout).strip()[:300]}")
    return json.loads(result.stdout)["html_url"]


def require_write_access(repo=game_source.GAME_REPO):
    """Raise GameRepoError (with the exact fix) when this run cannot write to the game repository."""
    if not _token() and os.environ.get("GITHUB_ACTIONS") == "true":
        raise GameRepoError(
            f"the repository secret {TOKEN_ENV} is not set, so this workflow cannot write to {repo}. The experiment was "
            "validated and is attached to this run as an artifact. To have it proposed automatically, add a fine-grained "
            f"token limited to {repo} (Contents and Pull requests: read and write) as {TOKEN_ENV}; see docs/AUTOMATION.md.")


def propose(patched_file, game, *, branch_prefix, title, body, extra_files=None, repo=game_source.GAME_REPO,
            base=game_source.STABLE_BRANCH, log=print):
    """Push `patched_file` (replacing the game file of the same name) to a new lab/ branch based on the exact commit
    the experiment was built from, and open a pull request against `base`. Returns the pull request URL."""
    require_write_access(repo)
    token = _token()
    branch = safe_branch(branch_prefix)
    patched_file = Path(patched_file)
    if patched_file.name != game["file"]:
        raise GameRepoError(f"the patched file must keep the game's file name {game['file']!r}")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "game"
        work.mkdir()
        _git(work, "init", "-q", token=token)
        _git(work, "remote", "add", "origin", f"https://github.com/{repo}", token=token)
        _git(work, "fetch", "-q", "--depth", "1", "origin", base, token=token)
        _git(work, "checkout", "-q", "-b", branch, "FETCH_HEAD", token=token)
        head = _git(work, "rev-parse", "HEAD", token=token).stdout.strip()
        if game.get("commit") and head != game["commit"]:
            raise GameRepoError(f"{base} moved from {game['commit'][:10]} to {head[:10]} while the experiment ran; "
                                "re-run it on the new build instead of proposing a stale patch")
        shutil.copyfile(patched_file, work / game["file"])
        for name, source in (extra_files or {}).items():
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        _git(work, "add", "-A", token=token)
        if _git(work, "diff", "--cached", "--quiet", token=token, check=False).returncode == 0:
            raise GameRepoError("the patched build is identical to the stable build; nothing to propose")
        _git(work, "commit", "-q", "-m", title, token=token)
        check_branch(branch)
        _git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", token=token)
    log(f"Pushed branch {branch} to {repo}")

    url = _create_pr_rest(token, repo, branch, base, title, body) if token else _create_pr_gh(repo, branch, base, title, body)
    log(f"Opened pull request {url}")
    return url


def host_previews(report, label, *, publish=None, log=print):
    """Upload preview screenshots to the game-lab-previews branch of THIS repository (rewritten as one commit, newest 40
    kept) so a pull request can show them. Returns {scene: [url, ...]}; an upload problem returns {} (previews are a
    convenience: the PR says the checks, not the pictures, are the evidence)."""
    import media_store
    publish = publish or media_store.publish
    paths, owners = [], {}
    for entry in report.get("candidates") or report.get("previews_by") or []:
        for path in entry.get("previews") or []:
            paths.append(Path(path))
            owners[Path(path).name] = entry.get("scene") or entry.get("theme") or label
    for path in report.get("previews") or []:
        paths.append(Path(path))
        owners[Path(path).name] = label
    if not paths:
        return {}
    try:
        published = publish(paths, branch=PREVIEW_BRANCH, keep=40)
    except Exception as error:
        log(f"note: preview upload failed ({type(error).__name__}: {str(error)[:160]}); the PR will have no pictures")
        return {}
    result = {}
    for item in published:
        result.setdefault(owners.get(Path(item["file"]).name, label), []).append(item["url"])
    return result
