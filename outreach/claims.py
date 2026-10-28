"""Single-use claims so one draft can leave this system at most once, even across re-runs and failed state pushes.

In GitHub Actions a claim is the git tag `outreach-claim-<draft id>`, created through the REST API: creating an existing
tag fails, so only one run can ever claim a draft. Locally (and in tests) a claim is a file. Claims are never released
by code.
"""

import json
import re
from pathlib import Path

from . import core

PREFIX = "outreach-claim-"
API = "https://api.github.com"


class AlreadyClaimed(core.OutreachError):
    pass


def _check_id(draft_id):
    if not re.fullmatch(r"od-[0-9a-f]{8}", str(draft_id)):
        raise core.OutreachError("malformed draft id")


class FileClaims:
    def __init__(self, directory):
        self.directory = Path(directory)

    def claim(self, draft_id, info=None):
        _check_id(draft_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.directory / f"{PREFIX}{draft_id}", "x", encoding="utf-8") as handle:
                json.dump(info or {}, handle)
        except FileExistsError:
            raise AlreadyClaimed(f"draft {draft_id} was already claimed; it can never be delivered twice") from None

    def is_claimed(self, draft_id):
        return (self.directory / f"{PREFIX}{draft_id}").exists()


class GitHubTagClaims:
    def __init__(self, repository, token, sha, *, post=None, get=None):
        if not (repository and token and sha):
            raise core.OutreachError("GitHub claims need GITHUB_REPOSITORY, GITHUB_TOKEN and GITHUB_SHA")
        self.repository, self.token, self.sha = repository, token, sha
        self._post, self._get = post, get

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _call(self, method, url, **kwargs):
        import requests
        function = (self._post if method == "post" else self._get) or getattr(requests, method)
        try:
            return function(url, headers=self._headers(), timeout=30, **kwargs)
        except Exception as error:
            raise core.OutreachError(f"could not reach GitHub to record the claim ({type(error).__name__}); "
                                     "nothing was sent") from None

    def is_claimed(self, draft_id):
        _check_id(draft_id)
        response = self._call("get", f"{API}/repos/{self.repository}/git/ref/tags/{PREFIX}{draft_id}")
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise core.OutreachError(f"GitHub did not say whether the draft was claimed (HTTP {response.status_code})")

    def claim(self, draft_id, info=None):
        _check_id(draft_id)
        response = self._call("post", f"{API}/repos/{self.repository}/git/refs",
                              json={"ref": f"refs/tags/{PREFIX}{draft_id}", "sha": self.sha})
        if response.status_code == 201:
            return
        if response.status_code == 422 and "already exists" in str(getattr(response, "text", "")).lower():
            raise AlreadyClaimed(f"draft {draft_id} was already claimed (tag {PREFIX}{draft_id} exists)")
        raise core.OutreachError(f"GitHub refused to record the claim (HTTP {response.status_code}); nothing was sent")
