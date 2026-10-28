"""Approval codes and the checks that must all pass before a draft may leave this system.

A draft's approval code is derived from its id, exact recipient, subject and body. Typing it (plus the recipient in
full) in a manually started workflow run is the approval. Any change to the message changes the code.
"""

import hashlib
import hmac

from . import checker, core, store


def content_hash(draft_id, recipient, subject, body):
    payload = "\n\x1e".join([draft_id, core.normalize_email(recipient) if "@" in str(recipient) else str(recipient),
                             subject, body])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def code_for(draft):
    return content_hash(draft["draft_id"], draft["recipient"], draft["subject"], draft["body"])[:12]


COUNTS_AS_CONTACT = ("sent", "unknown", "gmail-draft", "manual")


def sends_today(state, now):
    """Drafts released today (each counted once, on the day it first left: sent, Gmail draft or handed to you)."""
    first = {}
    for entry in state["ledger"]:
        if entry.get("outcome") in COUNTS_AS_CONTACT and entry.get("draft_id") not in first:
            first[entry["draft_id"]] = entry.get("at", "")[:10]
    return sum(1 for day in first.values() if day == core.iso(now)[:10])


def latest_outcomes(state, email):
    """{draft_id: last ledger entry} for one address: the current truth about every draft sent to it."""
    latest = {}
    for entry in store.ledger_for(state, email):
        latest[entry["draft_id"]] = entry
    return latest


def recipient_block(state, draft):
    """Why this recipient must not get this draft, or None. Uses the persistent ledger, not the draft's own record."""
    prospect = state["prospects"].get(draft["prospect_id"])
    if prospect is None:
        return "the prospect no longer exists"
    if prospect["status"] in core.TERMINAL:
        return f"the prospect is {prospect['status']}"
    if draft["channel"] != "email":
        return None
    email = draft["recipient"]
    if store.is_suppressed(state, email=email):
        return "the address is on the suppression list"
    try:
        core.validate_email(email)
    except core.OutreachError as error:
        return f"invalid address: {error}"
    history = latest_outcomes(state, email).values()
    initial = [e for e in history if e.get("kind") == "initial" and e.get("outcome") in COUNTS_AS_CONTACT]
    followups = [e for e in history if e.get("kind") == "followup" and e.get("outcome") in COUNTS_AS_CONTACT]
    if draft["kind"] == "initial" and initial:
        return f"this address was already contacted ({initial[-1]['outcome']} on {initial[-1]['at'][:10]})"
    if draft["kind"] == "followup":
        if not any(e["outcome"] == "sent" for e in initial):
            return "a follow-up needs a confirmed first email"
        if followups:
            return "a follow-up was already sent; there is never a second one"
        if prospect["status"] in (core.REPLIED, core.DECLINED):
            return "they already replied"
    return None


def verify(state, draft_id, approval_code, recipient_confirmation, config, now, *, demo_ok):
    """Return the draft if every check passes, else raise OutreachError with the first reason."""
    draft = state["drafts"].get(str(draft_id or "").strip())
    if draft is None:
        raise core.OutreachError("no draft with that id")
    if draft["status"] != core.D_AWAITING:
        raise core.OutreachError(f"draft is {draft['status']}, not awaiting approval; nothing was sent")
    created = core.parse_time(draft["created_at"])
    if created is None or core.days_between(created, now) > config["limits"]["draft_expiry_days"]:
        raise core.OutreachError("draft expired; a fresh one will be made from current evidence")
    expected = code_for(draft)
    if not hmac.compare_digest(expected, str(approval_code or "").strip().lower()):
        raise core.OutreachError("approval code does not match this draft (wrong code, or the draft changed)")
    if draft["channel"] == "email":
        if core.normalize_email(recipient_confirmation) != draft["recipient"]:
            raise core.OutreachError("the recipient you typed is not this draft's recipient")
    elif str(recipient_confirmation or "").strip().rstrip("/") != draft["recipient"].rstrip("/"):
        raise core.OutreachError("the submission URL you typed is not this draft's route")
    problems = checker.check(draft["subject"], draft["body"], demo_url=config["demo"]["url"], channel=draft["channel"],
                             evidence_title=draft["evidence"].get("title"),
                             require_evidence=draft.get("require_evidence", True))
    if problems:
        raise core.OutreachError("the draft no longer passes the message rules: " + problems[0])
    if draft.get("blocking"):
        raise core.OutreachError("the draft has blocking warnings: " + draft["blocking"][0])
    if not demo_ok:
        raise core.OutreachError("the demo link could not be verified just now; nothing was sent")
    reason = recipient_block(state, draft)
    if reason:
        raise core.OutreachError(reason)
    if sends_today(state, now) >= config["limits"]["max_sends_per_day"]:
        raise core.OutreachError(f"daily limit of {config['limits']['max_sends_per_day']} reached; try tomorrow")
    return draft
