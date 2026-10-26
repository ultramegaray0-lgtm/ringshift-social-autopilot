"""Approved-draft publishing: what makes "publish exactly this, once" enforceable.

Flow
1. `autopilot.py --dry-run --draft-file ...` (preview) writes a DRAFT: the exact text, media and target channel,
   with a unique draft ID, a content hash, and an expiry. It is uploaded as a run artifact. Nothing is sent.
2. You read it, then type its draft ID as the approval code when you start a publish run.
3. The publish run downloads that exact artifact and verifies, before touching Buffer:
   - the content hash matches the content (any edit to text, media or channel is rejected),
   - the code you typed equals the draft ID (you approved THIS draft),
   - the draft has not expired and does not come from the future,
   - it came from the preview run you named,
   - the target channel is the configured one and the media URL is inside this repository's raw URLs,
   - the media file is unchanged (SHA-256) and its URL is reachable with the expected size,
   - the draft ID has never been used (post log) and can be claimed (below).
4. The draft ID is CLAIMED before Buffer is called. In GitHub Actions the claim is a git tag
   `ringshift-used-<id>`: creating it is atomic, so a second run, a restart, or a re-run of a failed job
   cannot claim it again. A claim is never released automatically: if the run fails after claiming, the
   outcome in Buffer is unknown, and publishing the same draft again could double-post.

Nothing in this module talks to Buffer.
"""

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = "ringshift-draft/1"
DRAFT_TTL_HOURS = 48
MAX_TTL_HOURS = 168
CLOCK_SKEW = timedelta(minutes=5)
ID_RE = re.compile(r"^d-([0-9a-f]{8})-([0-9a-f]{4})$")
MEDIA_FILE_RE = re.compile(r"^(media|store)/[^\0]+$")
MEDIA_TYPES = ("image", "video")
CLAIM_TAG_PREFIX = "ringshift-used-"


class ApprovalError(RuntimeError):
    """The approval cannot be used. `reason` is a short stable code (tests and logs rely on it)."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class AlreadyUsedError(ApprovalError):
    def __init__(self, message):
        super().__init__("already_used", message)


class ClaimUnavailableError(ApprovalError):
    def __init__(self, message):
        super().__init__("claim_unavailable", message)


# ---- content hash and draft ------------------------------------------------------

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(draft):
    media = draft.get("media") or {}
    target = draft.get("target") or {}
    return {
        "schema": draft.get("schema"),
        "created_at": draft.get("created_at"),
        "expires_at": draft.get("expires_at"),
        "text": draft.get("text"),
        "media": {key: media.get(key) for key in ("file", "type", "url", "bytes", "sha256", "description")},
        "target": {key: target.get(key) for key in ("buffer_channel_id", "mode", "scheduling")},
    }


def content_hash(draft):
    """SHA-256 of what would be published and when the approval stops being valid: text, media identity and
    bytes, target channel, created and expiry times. Editing any of them changes the hash."""
    blob = json.dumps(_canonical(draft), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def create_draft(text, media, channel_id, *, post_type=None, run_id=None, now=None, ttl_hours=DRAFT_TTL_HOURS,
                 repo_root=None, mode="addToQueue", scheduling="automatic", extra=None):
    """Build the draft record for `text` + `media`. Nothing is sent or saved."""
    now = now or datetime.now(timezone.utc)
    file = media["file"]
    local = Path(repo_root) / file if repo_root else None

    draft = {
        "schema": SCHEMA,
        "sent": False,
        "text": text,
        "media": {
            "file": file,
            "type": media["type"],
            "url": media["url"],
            "bytes": local.stat().st_size if local and local.is_file() else media.get("bytes"),
            "sha256": sha256_file(local) if local and local.is_file() else None,
            "description": media.get("description"),
            "tags": media.get("tags", []),
        },
        "target": {"service": "X (via Buffer)", "buffer_channel_id": channel_id, "mode": mode, "scheduling": scheduling},
        "post_type": post_type,
        "run_id": str(run_id) if run_id else None,
        "created_at": _iso(now),
        "expires_at": _iso(now + timedelta(hours=ttl_hours)),
    }
    digest = content_hash(draft)
    draft["content_hash"] = digest
    draft["draft_id"] = f"d-{digest[:8]}-{secrets.token_hex(2)}"
    draft.update(extra or {})
    return draft


def load_draft(path):
    try:
        draft = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ApprovalError("missing", f"no draft file at {path}; was the artifact downloaded from the preview run?") from None
    except (OSError, json.JSONDecodeError) as error:
        raise ApprovalError("malformed", f"the draft file is not valid JSON ({type(error).__name__})") from None

    if not isinstance(draft, dict):
        raise ApprovalError("malformed", "the draft file must contain a JSON object")

    return draft


# ---- verification ----------------------------------------------------------------

def verify_draft(draft, approval_code, *, now=None, channel_id, expected_run_id=None, repo_root, raw_prefix):
    """Validate everything about a draft that does not need the network. Returns the media dict to publish.

    Raises ApprovalError (with a stable `reason`) for anything wrong. Never edits the draft."""
    now = now or datetime.now(timezone.utc)

    # -- shape
    if draft.get("schema") != SCHEMA:
        raise ApprovalError("malformed", f"unknown draft format {draft.get('schema')!r}")

    text = draft.get("text")
    media = draft.get("media")
    target = draft.get("target")

    if not isinstance(text, str) or not text.strip():
        raise ApprovalError("malformed", "the draft has no post text")
    if not isinstance(media, dict) or not isinstance(target, dict):
        raise ApprovalError("malformed", "the draft is missing its media or target")
    if media.get("type") not in MEDIA_TYPES:
        raise ApprovalError("bad_media", f"media type must be image or video, not {media.get('type')!r}")

    draft_id = draft.get("draft_id")
    match = ID_RE.match(str(draft_id or ""))
    if not match:
        raise ApprovalError("malformed", "the draft has no valid draft ID")

    # -- integrity: any edit to text, media or channel changes the hash
    recomputed = content_hash(draft)
    if draft.get("content_hash") != recomputed:
        raise ApprovalError("tampered", "the draft content does not match its hash: it was changed after the preview")
    if match.group(1) != recomputed[:8]:
        raise ApprovalError("tampered", "the draft ID does not belong to this content")

    # -- explicit approval: the typed code must be this draft's ID
    if str(approval_code or "").strip() != draft_id:
        raise ApprovalError("wrong_code", "the approval code does not match this draft's ID")

    # -- time window
    created = _parse(draft.get("created_at"))
    expires = _parse(draft.get("expires_at"))
    if not created or not expires:
        raise ApprovalError("malformed", "the draft has no valid created/expiry time")
    if expires <= created or expires - created > timedelta(hours=MAX_TTL_HOURS):
        raise ApprovalError("malformed", "the draft's expiry window is not valid")
    if created > now + CLOCK_SKEW:
        raise ApprovalError("not_yet_valid", "the draft is dated in the future")
    if now >= expires:
        raise ApprovalError("expired", f"the draft expired at {draft.get('expires_at')}; run a new preview")

    # -- provenance
    if expected_run_id and str(draft.get("run_id") or "") != str(expected_run_id):
        raise ApprovalError("wrong_run", "the draft was not produced by the preview run you named")

    # -- target and media identity
    if target.get("buffer_channel_id") != channel_id:
        raise ApprovalError("wrong_channel", "the draft targets a different Buffer channel than the one configured")

    url = str(media.get("url") or "")
    if not url.startswith(raw_prefix.rstrip("/") + "/"):
        raise ApprovalError("bad_media", "the media URL is not inside this repository's raw.githubusercontent.com address")

    file = str(media.get("file") or "")
    if not MEDIA_FILE_RE.match(file) or ".." in Path(file).parts:
        raise ApprovalError("bad_media", f"the media path {file!r} is not a media/ or store/ path")

    local = Path(repo_root) / file
    if local.is_file():
        expected_hash = media.get("sha256")
        if not expected_hash or sha256_file(local) != expected_hash:
            raise ApprovalError("media_changed", "the media file changed since the preview; run a new preview")
        if media.get("bytes") is not None and local.stat().st_size != media["bytes"]:
            raise ApprovalError("media_changed", "the media file size changed since the preview")

    return {
        "file": file,
        "url": url,
        "type": media["type"],
        "bytes": media.get("bytes"),
        "description": media.get("description") or "",
        "tags": media.get("tags") or [],
        "match": "approved draft",
        "topics": [],
    }


# ---- single use: claims ----------------------------------------------------------

REHEARSAL_TAG_PREFIX = "ringshift-rehearsal-"


class LocalFileClaim:
    """Local/offline claims: one file per draft ID, created exclusively (atomic on the same disk)."""

    rehearsal = False

    def __init__(self, directory):
        self.directory = Path(directory)

    def _path(self, draft_id):
        return self.directory / f"{draft_id}.json"

    def is_used(self, draft_id):
        return self._path(draft_id).exists()

    def claim(self, draft_id, info=None):
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            handle = os.open(str(self._path(draft_id)), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise AlreadyUsedError(f"draft {draft_id} was already used (claim file exists)") from None
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump({"draft_id": draft_id, "claimed_at": _iso(datetime.now(timezone.utc)), **(info or {})}, file)

    def for_rehearsal(self):
        other = LocalFileClaim(self.directory / "rehearsal")
        other.rehearsal = True
        return other

    def release(self, draft_id):
        """Only for throwaway rehearsal claims. A real claim is never released by code."""
        if not self.rehearsal:
            raise RuntimeError("real claims are never released by code")
        try:
            self._path(draft_id).unlink()
        except FileNotFoundError:
            pass


class GitHubRefClaim:
    """Claims in GitHub Actions: create the tag `ringshift-used-<id>` through the REST API.

    Creating a ref that already exists fails, so exactly one run can ever claim an ID, even across
    restarts and re-runs. The claim lives in GitHub, not in the run's files, so it survives a failed push."""

    API = "https://api.github.com"

    def __init__(self, repository, token, sha, post=None, get=None, delete=None, prefix=CLAIM_TAG_PREFIX):
        self.repository, self.token, self.sha = repository, token, sha
        self._post, self._get, self._delete = post, get, delete
        self.prefix = prefix

    @property
    def rehearsal(self):
        return self.prefix != CLAIM_TAG_PREFIX

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _call(self, kind, url, **kwargs):
        import requests
        function = getattr(self, "_" + kind) or getattr(requests, kind)
        try:
            return function(url, headers=self._headers(), timeout=30, **kwargs)
        except Exception as error:
            raise ClaimUnavailableError(
                f"could not reach GitHub to check or record the claim ({type(error).__name__}); nothing was published") from None

    def is_used(self, draft_id):
        """True if the claim tag exists. Anything other than a clear yes/no fails closed."""
        response = self._call("get", f"{self.API}/repos/{self.repository}/git/ref/tags/{self.prefix}{draft_id}")
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise ClaimUnavailableError(f"GitHub did not say whether the draft was used (HTTP {response.status_code}); nothing was published")

    def claim(self, draft_id, info=None):
        response = self._call("post", f"{self.API}/repos/{self.repository}/git/refs",
                              json={"ref": f"refs/tags/{self.prefix}{draft_id}", "sha": self.sha})
        if response.status_code == 201:
            return
        if response.status_code == 422 and "already exists" in str(getattr(response, "text", "")).lower():
            raise AlreadyUsedError(f"draft {draft_id} was already used (tag {self.prefix}{draft_id} exists)")
        raise ClaimUnavailableError(
            f"GitHub refused to record the claim (HTTP {response.status_code}); nothing was published")

    def for_rehearsal(self):
        return GitHubRefClaim(self.repository, self.token, self.sha, self._post, self._get, self._delete,
                              prefix=REHEARSAL_TAG_PREFIX)

    def release(self, draft_id):
        """Only for throwaway rehearsal tags. A real claim tag is never deleted by code."""
        if not self.rehearsal:
            raise RuntimeError("real claims are never released by code")
        response = self._call("delete", f"{self.API}/repos/{self.repository}/git/refs/tags/{self.prefix}{draft_id}")
        if response.status_code not in (204, 404):
            raise ClaimUnavailableError(f"could not remove the rehearsal tag (HTTP {response.status_code})")


def rehearse_claims(claimer):
    """Run the claim mechanism end to end on a throwaway ID. Returns [(step, outcome)] or raises.

    Uses the rehearsal namespace, so it can never touch (or use up) a real draft's claim."""
    rehearsal = claimer.for_rehearsal()
    draft_id = "r-" + secrets.token_hex(6)
    steps = []

    if rehearsal.is_used(draft_id):
        raise ClaimUnavailableError("a brand-new rehearsal ID already looks used")
    steps.append(("fresh ID reads as unused", "ok"))

    try:
        rehearsal.claim(draft_id, {"purpose": "rehearsal"})
        steps.append(("first claim", "created"))

        if not rehearsal.is_used(draft_id):
            raise ClaimUnavailableError("the claim was created but cannot be read back")
        steps.append(("claim reads back as used", "ok"))

        try:
            rehearsal.claim(draft_id)
        except AlreadyUsedError:
            steps.append(("second claim of the same ID", "refused (AlreadyUsed), as required"))
        else:
            raise ClaimUnavailableError("a second claim of the same ID was accepted: double publishing would be possible")
    finally:
        rehearsal.release(draft_id)

    if rehearsal.is_used(draft_id):
        raise ClaimUnavailableError("the rehearsal claim could not be cleaned up")
    steps.append(("throwaway claim removed", "ok"))
    return steps


def default_claimer(data_dir):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        repository = os.environ.get("GITHUB_REPOSITORY")
        token = os.environ.get("GITHUB_TOKEN")
        sha = os.environ.get("GITHUB_SHA")
        if not (repository and token and sha):
            raise ClaimUnavailableError("GITHUB_REPOSITORY, GITHUB_TOKEN and GITHUB_SHA are required to claim a draft in Actions")
        return GitHubRefClaim(repository, token, sha)
    return LocalFileClaim(Path(data_dir) / "used_approvals")
