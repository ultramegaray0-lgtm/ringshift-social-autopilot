"""Upload a generated Ringshift video to TikTok's inbox as an editable draft.

This never publishes. The creator must open TikTok's inbox notification, review/edit the
video and caption, and tap Publish in TikTok.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://open.tiktokapis.com"
MAX_VIDEO_BYTES = 4 * 1024 * 1024 * 1024
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024


class TikTokUploadError(RuntimeError):
    pass


def chunk_plan(size):
    if not isinstance(size, int) or size <= 0 or size > MAX_VIDEO_BYTES:
        raise TikTokUploadError(f"Video size must be between 1 byte and {MAX_VIDEO_BYTES} bytes")
    if size < MIN_CHUNK:
        return size, 1
    chunk_size = MAX_CHUNK
    # TikTok defines total_chunk_count as floor(video_size / chunk_size); the final chunk
    # absorbs the remainder and may be larger than chunk_size (up to 128 MB).
    chunk_count = size // chunk_size
    if chunk_count < 1 or chunk_count > 1000:
        raise TikTokUploadError("TikTok requires between 1 and 1000 chunks")
    return chunk_size, chunk_count


def _json_request(url, data, headers=None, method="POST"):
    body = json.dumps(data).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json; charset=UTF-8",
        **(headers or {}),
    }, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        raise TikTokUploadError(f"TikTok API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TikTokUploadError(f"Could not reach TikTok: {exc.reason}") from exc
    error = payload.get("error") or {}
    if error.get("code") not in (None, "ok"):
        raise TikTokUploadError(f"TikTok API error {error.get('code')}: {error.get('message', '')}")
    return payload


def _form_request(url, fields):
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Cache-Control": "no-cache",
    }, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        raise TikTokUploadError(f"TikTok OAuth returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TikTokUploadError(f"Could not reach TikTok OAuth: {exc.reason}") from exc
    if payload.get("error"):
        raise TikTokUploadError(f"TikTok OAuth error {payload['error']}: {payload.get('error_description', '')}")
    return payload


def load_refresh_token(state_path, encryption_key, initial_token):
    path = Path(state_path)
    if path.exists():
        try:
            from cryptography.fernet import Fernet, InvalidToken
            return Fernet(encryption_key.encode("ascii")).decrypt(path.read_bytes()).decode("utf-8")
        except (ValueError, UnicodeError, InvalidToken) as exc:
            raise TikTokUploadError("Saved TikTok token state cannot be decrypted; check TIKTOK_TOKEN_ENCRYPTION_KEY") from exc
    if not initial_token:
        raise TikTokUploadError("No saved TikTok token. Set the one-time TIKTOK_REFRESH_TOKEN secret first.")
    return initial_token


def save_refresh_token(state_path, encryption_key, refresh_token):
    from cryptography.fernet import Fernet
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = Fernet(encryption_key.encode("ascii")).encrypt(refresh_token.encode("utf-8"))
    path.write_bytes(encrypted)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def refresh_access_token(client_key, client_secret, refresh_token):
    payload = _form_request(f"{API}/v2/oauth/token/", {
        "client_key": client_key,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    if not payload.get("access_token") or not payload.get("refresh_token"):
        raise TikTokUploadError("TikTok token refresh response was missing access_token or refresh_token")
    scopes = set((payload.get("scope") or "").split(","))
    if "video.upload" not in scopes:
        raise TikTokUploadError("TikTok authorization is missing video.upload; reconnect with that scope enabled")
    return payload["access_token"], payload["refresh_token"]


def upload_file(video_path, access_token):
    path = Path(video_path)
    if not path.is_file():
        raise TikTokUploadError(f"Video file does not exist: {path}")
    size = path.stat().st_size
    chunk_size, chunk_count = chunk_plan(size)
    if path.suffix.lower() != ".mp4":
        raise TikTokUploadError("Only MP4 output is supported by this uploader")
    init = _json_request(f"{API}/v2/post/publish/inbox/video/init/", {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": chunk_count,
        }
    }, {"Authorization": f"Bearer {access_token}"})
    data = init.get("data") or {}
    publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
    if not publish_id or not upload_url:
        raise TikTokUploadError("TikTok did not return publish_id and upload_url")
    # The upload URL is a short-lived, signed credential. Never print it.
    with path.open("rb") as handle:
        offset = 0
        for index in range(chunk_count):
            remaining = size - offset
            # All preceding chunks are chunk_size; the last one absorbs all trailing bytes.
            chunk = handle.read(chunk_size if index < chunk_count - 1 else remaining)
            if not chunk:
                raise TikTokUploadError("Video file ended before all planned chunks were read")
            end = offset + len(chunk) - 1
            request = urllib.request.Request(upload_url, data=chunk, headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{size}",
            }, method="PUT")
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    if response.status not in (201, 206):
                        raise TikTokUploadError(f"TikTok media upload returned HTTP {response.status}")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                raise TikTokUploadError(f"TikTok media upload failed (HTTP {exc.code}): {detail}") from exc
            offset += len(chunk)
    if offset != size:
        raise TikTokUploadError(f"Uploaded {offset} of {size} bytes")
    return publish_id


def fetch_status(publish_id, access_token):
    payload = _json_request(f"{API}/v2/post/publish/status/fetch/", {"publish_id": publish_id},
                            {"Authorization": f"Bearer {access_token}"})
    return (payload.get("data") or {}).get("status", "UNKNOWN")


def verify_artifact(video_path, metadata):
    if metadata.get("status") != "produced" or not (metadata.get("validation") or {}).get("ok"):
        raise TikTokUploadError("Refusing upload: artifact metadata does not confirm a successfully validated video")
    expected = (metadata.get("output") or {}).get("sha256")
    if not expected:
        raise TikTokUploadError("Refusing upload: artifact metadata has no output SHA-256")
    digest = hashlib.sha256()
    with Path(video_path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise TikTokUploadError("Refusing upload: MP4 SHA-256 does not match the validated artifact metadata")


FINAL_STATUSES = ("SEND_TO_USER_INBOX", "FAILED", "PUBLISH_COMPLETE")


def run(video, metadata, state_path, poll_attempts=24, poll_seconds=5, on_accepted=None):
    verify_artifact(video, metadata)
    client_key = os.environ.get("TIKTOK_CLIENT_KEY", "")
    client_secret = os.environ.get("TIKTOK_CLIENT_SECRET", "")
    encryption_key = os.environ.get("TIKTOK_TOKEN_ENCRYPTION_KEY", "")
    if not all((client_key, client_secret, encryption_key)):
        raise TikTokUploadError("Set TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_TOKEN_ENCRYPTION_KEY in GitHub Actions secrets")
    try:
        from cryptography.fernet import Fernet
        Fernet(encryption_key.encode("ascii"))
    except Exception as exc:
        raise TikTokUploadError("TIKTOK_TOKEN_ENCRYPTION_KEY must be a valid Fernet key (generate with Fernet.generate_key())") from exc
    refresh_token = load_refresh_token(state_path, encryption_key, os.environ.get("TIKTOK_REFRESH_TOKEN", ""))
    access_token, rotated_refresh_token = refresh_access_token(client_key, client_secret, refresh_token)
    # TikTok may rotate the refresh token on every refresh. Save it before the media transfer, so a failed upload can
    # never lose the only valid token.
    save_refresh_token(state_path, encryption_key, rotated_refresh_token)
    publish_id = upload_file(video, access_token)
    if on_accepted:
        on_accepted(publish_id, "UPLOADED")         # the bytes are with TikTok: from here on a retry would duplicate
    status = "UNKNOWN"
    for attempt in range(poll_attempts):
        try:
            status = fetch_status(publish_id, access_token)
        except TikTokUploadError as exc:            # a status hiccup does not undo an accepted upload
            print(f"Status check {attempt + 1} failed: {exc}", file=sys.stderr)
        if status in FINAL_STATUSES:
            break
        if attempt < poll_attempts - 1:
            time.sleep(poll_seconds)
    return publish_id, status, metadata.get("caption_suggestion", "")


def _write_json(path, data):
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _summary(text):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(text)


FINISH_STEPS = ("Open the TikTok app → **Inbox** → the notification from the Ringshift app (\"Your video is ready\") → "
                "edit the caption, add a sound if you like → tap **Post**. The video is NOT public until you post it.")


def main():
    parser = argparse.ArgumentParser(description="Upload a Ringshift video to TikTok inbox as a draft; never publishes.")
    parser.add_argument("--video", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--state", required=True, help="encrypted refresh-token file")
    parser.add_argument("--ledger", default=None,
                        help="TikTok state JSON (tiktok_state.py): refuses a video already uploaded, records this one")
    parser.add_argument("--result", default=None, help="write the outcome here as JSON")
    parser.add_argument("--force", action="store_true", help="upload even if this video was already uploaded")
    parser.add_argument("--source-run-id", default=None)
    parser.add_argument("--artifact-name", default=None)
    args = parser.parse_args()
    ledger = state = None
    sha = None
    try:
        metadata = json.loads(Path(args.metadata).read_text(encoding="utf-8"))
        sha = (metadata.get("output") or {}).get("sha256")
        if args.ledger:
            import tiktok_state as ledger
            state = ledger.load(args.ledger)
            earlier = ledger.upload_for(state, sha)
            if earlier and not args.force:
                _write_json(args.result, {"status": "ALREADY_UPLOADED", "publish_id": earlier.get("publish_id"), "sha256": sha})
                _summary("### TikTok inbox upload: already done\n\n"
                         f"This exact video (SHA-256 `{sha[:12]}…`) was sent to the TikTok inbox on "
                         f"{str(earlier.get('at', '?'))[:16]} (publish ID `{earlier.get('publish_id')}`, run "
                         f"{earlier.get('upload_run_id', '?')}). Nothing was uploaded again, so no duplicate draft.\n\n"
                         f"- **Finish it:** {FINISH_STEPS}\n- To send it again anyway, run this workflow by hand with **force** on.\n")
                print(f"Already uploaded (publish ID {earlier.get('publish_id')}); skipping to avoid a duplicate draft.")
                return 0

        def remember(publish_id, status):
            record = {"publish_id": publish_id, "status": status, "artifact": args.artifact_name,
                      "source_run_id": args.source_run_id, "upload_run_id": os.environ.get("GITHUB_RUN_ID")}
            _write_json(args.result, dict(record, sha256=sha))
            if ledger is not None:
                ledger.record_upload(state, sha, record)
                ledger.save(state, args.ledger)

        publish_id, status, caption = run(args.video, metadata, args.state, on_accepted=remember)
        remember(publish_id, status)
        if status == "FAILED":
            raise TikTokUploadError(f"TikTok accepted the upload but processing failed (publish_id {publish_id}); "
                                    "re-running this workflow will try again")
        waiting = status not in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE")
        _summary(
            "### TikTok inbox upload complete\n\n"
            f"- **TikTok status:** {status}"
            + (" (still processing on TikTok's side; the inbox notification usually follows within a few minutes)" if waiting else "")
            + "\n"
            f"- **Publish ID:** `{publish_id}`\n"
            f"- **Video:** `{Path(args.video).name}`" + (f" from run {args.source_run_id}" if args.source_run_id else "") + "\n"
            f"- **Finish the draft:** {FINISH_STEPS}\n"
            f"- **Suggested caption (paste/edit in TikTok):** {caption}\n"
        )
        print(f"TikTok inbox upload accepted. Status: {status}. Publish ID: {publish_id}")
        return 0
    except Exception as exc:
        print(f"TikTok inbox upload failed: {exc}", file=sys.stderr)
        _summary(f"### TikTok inbox upload: FAILED\n\n{exc}\n\nNothing was posted. Fix the cause above, then use "
                 "**Re-run all jobs** on this run (a video TikTok already accepted is never sent twice).\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
