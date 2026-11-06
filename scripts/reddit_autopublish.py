"""Approval-gated Reddit publisher for Ringshift. Draft mode is offline.
Publish mode requires explicit API and commercial approval, a reviewed community,
OAuth refresh-token credentials, and a one-post-per-week global cooldown.
Ambiguous submissions are never retried automatically.
"""
import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
try:
    from scripts.reddit_repurpose import RedditDraftError, build_draft, load_community, load_source
except ModuleNotFoundError:
    from reddit_repurpose import RedditDraftError, build_draft, load_community, load_source

ROOT = Path(__file__).resolve().parent.parent
POST_LOG = ROOT / "data" / "post_log.json"
MANIFEST = ROOT / "media" / "manifest.json"
COMMUNITIES = ROOT / "reddit" / "communities.json"
DEFAULT_STATE = ROOT / "reddit" / "state.json"
AUTOMATION_CONFIG = ROOT / "reddit" / "automation.json"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SUBMIT_URL = "https://oauth.reddit.com/api/submit"
COOLDOWN_HOURS = 168


class RedditPublishError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        raise RedditPublishError(f"Cannot read {path}: {exc}") from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def user_agent(username):
    username = str(username or "").strip().removeprefix("u/")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,20}", username):
        raise RedditPublishError("REDDIT_USERNAME must be your actual Reddit username for a truthful User-Agent.")
    return f"python:ringshift-social-autopilot:v1.0 (by /u/{username})"


def validate_publish_gates(community, environ=None, config=None):
    environ = os.environ if environ is None else environ
    config = read_json(AUTOMATION_CONFIG, {}) if config is None else config
    if not config.get("enabled") or not config.get("api_access_approved") or not config.get("commercial_use_approved"):
        raise RedditPublishError("Reddit automation config is disabled or missing written approval flags.")
    if config.get("target_subreddit", "").lower() != community.get("name", "").lower():
        raise RedditPublishError("Automation config target_subreddit must match the reviewed community.")
    flags = ("REDDIT_API_APPROVED", "REDDIT_COMMERCIAL_APPROVED", "REDDIT_AUTOPUBLISH")
    missing = [key for key in flags if str(environ.get(key, "")).strip().lower() not in {"1", "true", "yes", "on"}]
    if missing:
        raise RedditPublishError("Publishing disabled until written approval; missing flags: " + ", ".join(missing))
    if not community.get("rules_reviewed") or not community.get("rules_url"):
        raise RedditPublishError("Subreddit rules have not been reviewed and recorded.")
    if community.get("self_promo_allowed") is not True or community.get("allow_links") is not True:
        raise RedditPublishError("This subreddit is not explicitly approved for self-promotion and links.")
    if community.get("autopost_allowed") is not True:
        raise RedditPublishError("This subreddit has not explicitly permitted automated submissions.")
    approval_ref = str(environ.get("REDDIT_APPROVAL_REFERENCE", "")).strip()
    if not approval_ref or approval_ref != str(config.get("approval_reference", "")).strip():
        raise RedditPublishError("REDDIT_APPROVAL_REFERENCE must match the written approval record in reddit/automation.json.")
    for key in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_REFRESH_TOKEN", "REDDIT_USERNAME"):
        if not str(environ.get(key, "")).strip():
            raise RedditPublishError(f"Missing required GitHub Actions secret/variable: {key}")
    user_agent(environ.get("REDDIT_USERNAME"))


def content_hash(draft):
    raw = (draft["title"].strip() + "\n\n" + draft["body"].strip()).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def select_latest_source(post_log=POST_LOG):
    posts = read_json(post_log, [])
    if not isinstance(posts, list) or not posts:
        raise RedditPublishError("No X source posts exist in data/post_log.json.")
    return sorted(posts, key=lambda item: (str(item.get("queued_at", "")), int(item.get("number", 0))), reverse=True)[0]


def check_state(state, source_number, subreddit, now=None):
    now = now or utc_now()
    entries = state.get("submissions", [])
    for entry in entries:
        if entry.get("status") in {"pending", "unknown"}:
            raise RedditPublishError("A previous Reddit submission has an unresolved outcome; reconcile manually.")
        if str(entry.get("source_x_post_number")) == str(source_number):
            raise RedditPublishError("This X source post has already been used for a Reddit submission.")
    for entry in entries:
        if entry.get("status") != "posted":
            continue
        try:
            posted_at = datetime.fromisoformat(entry["published_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            continue
        if now - posted_at < timedelta(hours=COOLDOWN_HOURS):
            raise RedditPublishError("Global Reddit cooldown active: at most one automated post every 7 days.")
        if entry.get("target_subreddit", "").lower() == subreddit.lower() and now - posted_at < timedelta(days=30):
            raise RedditPublishError("This subreddit is on a 30-day per-community cooldown.")
    return True


def oauth_access_token(environ=None, session=requests):
    environ = os.environ if environ is None else environ
    response = session.post(
        TOKEN_URL,
        auth=(environ["REDDIT_CLIENT_ID"], environ["REDDIT_CLIENT_SECRET"]),
        data={"grant_type": "refresh_token", "refresh_token": environ["REDDIT_REFRESH_TOKEN"]},
        headers={"User-Agent": user_agent(environ["REDDIT_USERNAME"])},
        timeout=20,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RedditPublishError("Reddit OAuth response did not contain an access token.")
    return token


def submit_post(draft, community, environ=None, session=requests):
    environ = os.environ if environ is None else environ
    token = oauth_access_token(environ, session)
    headers = {"Authorization": f"bearer {token}", "User-Agent": user_agent(environ["REDDIT_USERNAME"])}
    payload = {
        "api_type": "json", "kind": "self", "sr": community["name"].removeprefix("r/"),
        "title": draft["title"], "text": draft["body"], "sendreplies": False,
    }
    if community.get("flair_id"):
        payload["flair_id"] = community["flair_id"]
    response = session.post(SUBMIT_URL, data=payload, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json().get("json", {})
    errors = data.get("errors") or []
    if errors:
        raise RedditPublishError("Reddit rejected the submission: " + json.dumps(errors, ensure_ascii=False))
    things = data.get("data", {})
    if not things.get("url") or not things.get("name"):
        raise RedditPublishError("Reddit response did not confirm a post URL and ID; outcome must be checked manually.")
    return {"reddit_id": things["name"], "permalink": things["url"]}


def run(mode, subreddit, post_id=None, state_path=DEFAULT_STATE, post_log=POST_LOG, manifest=MANIFEST,
        communities_path=COMMUNITIES, automation_config_path=AUTOMATION_CONFIG, environ=None, session=requests, now=None):
    environ = os.environ if environ is None else environ
    community = load_community(subreddit, communities_path)
    source = select_latest_source(post_log) if post_id is None else None
    source_id = source.get("number") if source else post_id
    post, media = load_source(source_id, post_log, manifest)
    draft = build_draft(post, media, subreddit, community)
    if media.get("url") and community.get("allow_links") is True and community.get("rules_reviewed") is True:
        draft["body"] += "\n\nGameplay capture: " + media["url"]
    digest = content_hash(draft)
    result = {
        "mode": mode, "status": "DRAFT_ONLY", "target_subreddit": draft["target_subreddit"],
        "source_x_post_number": source_id, "title": draft["title"], "body": draft["body"],
        "content_sha256": digest, "media_sha256": media["sha256"], "rules_url": community.get("rules_url"),
        "draft_review_status": draft["status"], "must_be_submitted_manually": mode != "publish",
    }
    if mode == "draft":
        return result
    if mode != "publish":
        raise RedditPublishError("mode must be draft or publish")
    if draft["status"] != "READY_FOR_HUMAN_REVIEW":
        raise RedditPublishError("Draft is held: " + draft["status"])
    if not community.get("rules_url"):
        raise RedditPublishError("The current official subreddit rules URL must be recorded before use.")
    automation_config = read_json(automation_config_path, {})
    validate_publish_gates(community, environ, automation_config)
    state = read_json(state_path, {"submissions": []})
    check_state(state, source_id, community["name"], now=now)
    record = {
        "source_x_post_number": source_id, "target_subreddit": community["name"],
        "content_sha256": digest, "status": "pending",
        "started_at": (now or utc_now()).isoformat(), "rules_url": community["rules_url"],
    }
    state.setdefault("submissions", []).append(record)
    write_json(state_path, state)
    try:
        posted = submit_post(draft, community, environ, session)
    except requests.RequestException:
        record["status"] = "unknown"
        record["outcome_note"] = "Network/HTTP outcome was ambiguous; reconcile manually before retrying."
        write_json(state_path, state)
        raise
    except RedditPublishError as exc:
        message = str(exc)
        record["status"] = "rejected" if message.startswith("Reddit rejected the submission:") else "unknown"
        record["outcome_note"] = message
        write_json(state_path, state)
        raise
    record.update(posted)
    record["status"] = "posted"
    record["published_at"] = (now or utc_now()).isoformat()
    write_json(state_path, state)
    result.update({"status": "POSTED", **posted, "published_at": record["published_at"]})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create or publish one approved Ringshift Reddit post.")
    parser.add_argument("--mode", choices=("draft", "publish"), default="draft")
    parser.add_argument("--subreddit", default=os.getenv("REDDIT_SUBREDDIT", ""))
    parser.add_argument("--post-id")
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if not args.subreddit:
        raise SystemExit("ERROR: provide --subreddit or REDDIT_SUBREDDIT.")
    try:
        result = run(args.mode, args.subreddit, args.post_id, args.state)
    except (RedditDraftError, RedditPublishError, requests.RequestException) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        write_json(args.output, result)
    print(rendered)


if __name__ == "__main__":
    main()
