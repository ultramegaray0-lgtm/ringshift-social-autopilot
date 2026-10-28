"""Outreach state: prospects, drafts, suppression list, send ledger, run reports.

One JSON document. In GitHub Actions it lives on the `outreach-state` branch as `state.json.enc`, encrypted with
AES-256-CBC (openssl, PBKDF2) under the secret OUTREACH_STATE_KEY and authenticated with HMAC-SHA256, because this
repository is public and the state holds creators' contact addresses. Locally you may use plain `state.json`
(only when not running in GitHub Actions).

The key is handed to openssl through the child's environment, never on a command line, and is never printed.
"""

import base64
import hashlib
import hmac
import json
import os
import subprocess
from pathlib import Path

from . import core

SCHEMA = "ringshift-outreach-state/1"
ENC_FORMAT = "ringshift-outreach-enc/1"
ENC_FILE = "state.json.enc"
PLAIN_FILE = "state.json"
KEY_ENV = "OUTREACH_STATE_KEY"
MIN_KEY_LENGTH = 20
PBKDF2_ITER = 300000
KEEP_RUNS = 60
KEEP_REJECTED = 300
KEEP_EVENTS = 2000


class StateError(core.OutreachError):
    pass


def empty_state():
    return {"schema": SCHEMA, "prospects": {}, "drafts": {}, "suppression": {"emails": {}, "urls": {}},
            "ledger": [], "rejected": [], "events": [], "runs": [], "sync": {}}


# ---- encryption -----------------------------------------------------------------------------------------------

def _key(env=None):
    raw = (env if env is not None else os.environ).get(KEY_ENV, "")
    raw = raw.strip()
    if not raw:
        return None
    if len(raw) < MIN_KEY_LENGTH or "\n" in raw:
        raise StateError(f"{KEY_ENV} must be one line of at least {MIN_KEY_LENGTH} characters (value not shown)")
    return raw


def _openssl(args, data, key):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "RS_OUTREACH_PASS": key}
    try:
        result = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(PBKDF2_ITER), "-md",
                                 "sha256", *args, "-pass", "env:RS_OUTREACH_PASS"],
                                input=data, capture_output=True, env=env, timeout=60)
    except FileNotFoundError:
        raise StateError("openssl is not installed; cannot read or write encrypted state") from None
    if result.returncode != 0:
        raise StateError("openssl could not process the state (wrong key or damaged file)")
    return result.stdout


def _mac(key, blob):
    return hmac.new(hashlib.sha256(b"ringshift-outreach-hmac:" + key.encode()).digest(), blob, hashlib.sha256).hexdigest()


def encrypt(plaintext, key):
    blob = _openssl(["-e", "-salt"], plaintext, key)
    return json.dumps({"format": ENC_FORMAT, "kdf": f"pbkdf2-sha256-{PBKDF2_ITER}", "cipher": "aes-256-cbc",
                       "hmac_sha256": _mac(key, blob), "data": base64.b64encode(blob).decode()}, indent=1).encode()


def decrypt(document, key):
    try:
        wrapper = json.loads(document)
        blob = base64.b64decode(wrapper["data"], validate=True)
        expected = wrapper["hmac_sha256"]
    except (ValueError, KeyError, TypeError):
        raise StateError("encrypted state file is malformed") from None
    if wrapper.get("format") != ENC_FORMAT:
        raise StateError("encrypted state file has an unknown format")
    if not hmac.compare_digest(_mac(key, blob), str(expected)):
        raise StateError(f"encrypted state did not authenticate: wrong {KEY_ENV} or the file was changed")
    return _openssl(["-d"], blob, key)


# ---- load / save ----------------------------------------------------------------------------------------------

def _in_actions(env):
    return str(env.get("GITHUB_ACTIONS", "")).lower() == "true"


def load(state_dir, env=None):
    env = env if env is not None else os.environ
    state_dir = Path(state_dir)
    key = _key(env)
    enc, plain = state_dir / ENC_FILE, state_dir / PLAIN_FILE
    if enc.exists():
        if not key:
            raise StateError(f"{ENC_FILE} exists but {KEY_ENV} is not set")
        data = decrypt(enc.read_bytes(), key)
    elif plain.exists():
        if _in_actions(env):
            raise StateError(f"refusing plain {PLAIN_FILE} in GitHub Actions; state must be encrypted")
        data = plain.read_bytes()
    else:
        if _in_actions(env) and not key:
            raise StateError(f"{KEY_ENV} is not set. Add it as a repository secret (any random 32+ characters).")
        return empty_state()
    try:
        state = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StateError("state file is not valid JSON") from None
    return validate_shape(state)


def save(state, state_dir, env=None):
    env = env if env is not None else os.environ
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    validate_shape(state)
    _trim(state)
    data = json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False).encode("utf-8")
    key = _key(env)
    if key:
        target, payload, other = state_dir / ENC_FILE, encrypt(data, key), state_dir / PLAIN_FILE
    elif _in_actions(env):
        raise StateError(f"{KEY_ENV} is not set; refusing to write unencrypted state in GitHub Actions")
    else:
        target, payload, other = state_dir / PLAIN_FILE, data, None
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, target)
    if other is not None and other.exists():
        other.unlink()                     # never leave a plaintext copy next to the encrypted one
    return target


def validate_shape(state):
    if not isinstance(state, dict) or state.get("schema") != SCHEMA:
        raise StateError("state has an unknown schema")
    base = empty_state()
    for field, default in base.items():
        if not isinstance(state.get(field), type(default)):
            raise StateError(f"state field {field!r} is missing or has the wrong type")
    for field in ("emails", "urls"):
        if not isinstance(state["suppression"].get(field), dict):
            raise StateError("state suppression list is malformed")
    return state


def _trim(state):
    state["runs"] = state["runs"][-KEEP_RUNS:]
    state["rejected"] = state["rejected"][-KEEP_REJECTED:]
    state["events"] = state["events"][-KEEP_EVENTS:]


# ---- helpers on a loaded state --------------------------------------------------------------------------------

def event(state, kind, now, **fields):
    state["events"].append({"at": core.iso(now), "kind": kind, **fields})


def is_suppressed(state, *, email=None, canonical=None):
    if email and core.normalize_email(email) in state["suppression"]["emails"]:
        return True
    return bool(canonical and canonical in state["suppression"]["urls"])


def suppress(state, now, *, email=None, canonical=None, reason, by, status=None):
    """Add to the suppression list (never contacted or re-added again). Matching prospects get `status`
    (SUPPRESSED by default; BOUNCED or DECLINED when that is what happened)."""
    status = status or core.SUPPRESSED
    if not email and not canonical:
        raise core.OutreachError("suppress needs an email or a URL")
    record = {"at": core.iso(now), "reason": core.clean_text(reason, 200), "by": by}
    affected = []
    if email:
        email = core.normalize_email(email)
        state["suppression"]["emails"][email] = record
    if canonical:
        state["suppression"]["urls"][canonical] = record
    for prospect in state["prospects"].values():
        contact = (prospect.get("contact") or {}).get("email")
        if (email and contact == email) or (canonical and canonical in prospect_keys(prospect)):
            if prospect["status"] != core.SUPPRESSED:
                set_status(prospect, status, now, reason)
            affected.append(prospect["id"])
            for draft in state["drafts"].values():
                if draft["prospect_id"] == prospect["id"] and draft["status"] == core.D_AWAITING:
                    draft["status"] = core.D_SUPERSEDED
                    draft["status_reason"] = "prospect suppressed"
    event(state, "suppressed", now, prospects=affected, reason=record["reason"], by=by)
    return affected


def prospect_keys(prospect):
    return {prospect["canonical"], *prospect.get("aliases", [])}


def find_duplicate(state, canonical_keys, email=None):
    """The prospect that already owns one of these canonical keys, or this contact address."""
    canonical_keys = set(canonical_keys)
    email = core.normalize_email(email) if email else None
    for prospect in state["prospects"].values():
        if prospect_keys(prospect) & canonical_keys:
            return prospect
        if email and (prospect.get("contact") or {}).get("email") == email:
            return prospect
    return None


def set_status(prospect, status, now, reason=""):
    if status not in core.PROSPECT_STATUSES:
        raise core.OutreachError(f"unknown status {status}")
    if prospect["status"] == core.SUPPRESSED and status != core.SUPPRESSED:
        raise core.OutreachError(f"{prospect['id']} is suppressed and stays suppressed")
    if prospect["status"] in (core.BOUNCED, core.DECLINED) and status not in (core.SUPPRESSED, prospect["status"]):
        raise core.OutreachError(f"{prospect['id']} is {prospect['status']}; only suppression can follow")
    if prospect["status"] != status:
        prospect.setdefault("history", []).append(
            {"at": core.iso(now), "from": prospect["status"], "to": status, "reason": core.clean_text(reason, 160)})
        prospect["history"] = prospect["history"][-30:]
        prospect["status"] = status
    prospect["updated"] = core.iso(now)


def reject(state, now, *, name, url, reason, source):
    state["rejected"].append({"at": core.iso(now), "name": core.clean_text(name, 80), "url": str(url)[:200],
                              "reason": core.clean_text(reason, 200), "source": source})


def ledger_for(state, email):
    email = core.normalize_email(email)
    return [entry for entry in state["ledger"] if entry.get("recipient") == email]


# ---- outcomes (shared by mailer, sync and your manual marks) ----------------------------------------------

def ledger_add(state, draft, now, *, mode, outcome, confirmation, actor):
    state["ledger"].append({"at": core.iso(now), "draft_id": draft["draft_id"], "prospect_id": draft["prospect_id"],
                            "recipient": draft["recipient"],
                            "kind": draft["kind"], "channel": draft["channel"], "mode": mode, "outcome": outcome,
                            "confirmation": confirmation, "actor": actor})


def mark_sent(state, draft, now, confirmation, actor, mode, sent_at=None):
    draft["status"] = core.D_SENT
    draft["sent_at"] = core.iso(sent_at or now)
    draft["confirmation"] = confirmation
    prospect = state["prospects"].get(draft["prospect_id"])
    if prospect and prospect["status"] not in core.TERMINAL and prospect["status"] not in (core.REPLIED,):
        set_status(prospect, core.SENT, now, confirmation)
    if prospect:
        prospect["last_contact"] = draft["sent_at"]
    ledger_add(state, draft, now, mode=mode, outcome="sent", confirmation=confirmation, actor=actor)


def requalify(state, draft, now, reason):
    prospect = state["prospects"].get(draft["prospect_id"])
    if prospect and draft["kind"] == "initial" and prospect["status"] in (core.APPROVED, core.DRAFTED, core.NEEDS_REVIEW):
        set_status(prospect, core.QUALIFIED, now, reason)
