"""Command line for the outreach pipeline. Run from the repository root: python -m outreach <command> ...

  daily          discover, score, draft, queue follow-ups, report. No Gmail access, cannot send.
  review         print the approval queue (no network).
  deliver        deliver ONE draft you approved: --draft-id --approval-code --recipient --mode gmail-draft|smtp|manual
  sync-replies   read-only Gmail check: sends confirmed, replies, bounces, opt-outs.
  mark           record what happened: --action sent|not-sent|reject|replied|interested|declined|bounced|opt-out
  suppress       never contact an address or site: --email / --url, --reason
  add-contact    add a business address you found yourself: --prospect-id --email --source-url

State: --state-dir (default .outreach-state). Encrypted when OUTREACH_STATE_KEY is set (required in Actions).
Exit codes: 0 ok, 1 something needs your attention (bad key, auth failure), 2 refused (nothing was sent).
"""

import argparse
import os
import sys
from pathlib import Path

from . import approval, core, discovery, drafting, report, settings, store, web

DEFAULT_STATE_DIR = ".outreach-state"


def _in_actions():
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


def _summary(markdown):
    print(markdown)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")


def _annotate(level, message):
    if _in_actions():
        print(f"::{level}::{core.mask_emails_in(message)}")


def _env(name):
    value = (os.environ.get(name) or "").strip()
    return value or None


# ---- commands ---------------------------------------------------------------------------------------------------

def cmd_daily(args, now, fetcher=None, gemini_factory=None):
    config, sources = settings.load_config(), settings.load_sources()
    state = store.load(args.state_dir)
    fetcher = fetcher or web.Fetcher()
    notes, exit_code = [], 0
    fact_problems = settings.check_game_facts(config)
    demo = report.check_demo(fetcher, config, now)
    if fact_problems:
        demo = {**demo, "ok": False, "reason": "game facts failed: " + "; ".join(fact_problems)}
        exit_code = 1
    found = discovery.run(state, config, sources, now, fetcher=fetcher, youtube_key=_env("YOUTUBE_API_KEY"))
    gemini_key = _env("OUTREACH_GEMINI_API_KEY") or _env("GEMINI_API_KEY")
    writer = None
    if config["gemini"].get("enabled") and gemini_key:
        writer = drafting.GeminiWriter(gemini_key, config["gemini"]["models"], client_factory=gemini_factory,
                                       budget=2 * int(config["limits"]["max_new_drafts_per_run"]))
    else:
        notes.append("Gemini not used (no key or disabled); personal sentences come from the template")
    drafted = drafting.make_drafts(state, config, now, demo=demo, writer=writer)
    markdown, counts = report.build(state, now, discovery=found.as_dict(), drafting=drafted, notes=notes, demo=demo)
    state["runs"].append({"at": core.iso(now), "kind": "daily", **counts, "new": len(found.new),
                          "qualified_new": len(found.qualified), "drafts_created": len(drafted["created"]),
                          "followups_queued": len(drafted["followups"]), "errors": len(found.errors) + len(found.fatal)})
    store.save(state, args.state_dir)
    _summary(markdown)
    for message in found.fatal:
        _annotate("error", message)
        exit_code = 1
    for message in found.errors:
        _annotate("warning", message)
    if not demo.get("ok"):
        _annotate("warning", f"demo link not verified: {demo.get('reason')}")
    return exit_code


def cmd_review(args, now):
    state = store.load(args.state_dir)
    markdown, _ = report.build(state, now)
    _summary(markdown)
    return 0


def _gmail():
    from . import mailer
    address, password = _env("GMAIL_ADDRESS"), _env("GMAIL_APP_PASSWORD")
    if not address and not password:
        return None
    return mailer.Gmail(address, password)


def _claims(args):
    from . import claims
    if _in_actions():
        return claims.GitHubTagClaims(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN"),
                                      os.environ.get("GITHUB_SHA"))
    return claims.FileClaims(Path(args.state_dir) / "claims")


def cmd_deliver(args, now, fetcher=None, gmail=None, claimer=None):
    from . import mailer
    config = settings.load_config()
    state = store.load(args.state_dir)
    gmail = gmail if gmail is not None else (_gmail() if args.mode != "manual" else None)
    claimer = claimer or _claims(args)
    demo = report.check_demo(fetcher or web.Fetcher(), config, now)
    pre_sync = None
    try:
        if gmail is not None:
            pre_sync = mailer.sync(state, config, gmail, now)      # a reply that arrived since the last check counts
        result = mailer.deliver(state, config, args.draft_id, args.approval_code, args.recipient, args.mode,
                                claims=claimer, now=now, demo_ok=bool(demo.get("ok")), gmail=gmail,
                                actor=os.environ.get("GITHUB_ACTOR", "owner"))
    finally:
        store.save(state, args.state_dir)                          # a claimed draft is recorded even if anything failed
    draft = result["draft"]
    headline = {
        "sent": "✅ SENT — Gmail's SMTP server accepted the message.",
        "gmail-draft": "📝 Saved in your Gmail **Drafts**. Open Gmail, check it, and press Send. The next "
                       "`sync-replies` run records it as SENT once it is in Sent Mail.",
        "manual": "📋 Approved for manual sending. Copy the message below into Gmail yourself, then run mode `mark` "
                  "with action `sent` and this draft id.",
        "failed": "❌ NOT sent: Gmail refused it. A fresh draft will be made on the next daily run.",
        "unknown": "⚠️ UNCLEAR: the connection broke mid-delivery. Check Gmail Sent Mail / Drafts, then run `mark` "
                   "with `sent` or `not-sent`. It will not be retried automatically.",
    }[result["outcome"]]
    _summary(f"# Outreach delivery · `{draft['draft_id']}`\n\n{headline}\n\n" + report.draft_section(state, draft)
             + (f"\nPre-delivery Gmail check: {', '.join(f'{k} {len(v)}' for k, v in pre_sync.items())}" if pre_sync else ""))
    return 0 if result["outcome"] in ("sent", "gmail-draft", "manual") else 2


def cmd_sync(args, now, gmail=None):
    from . import mailer
    config = settings.load_config()
    state = store.load(args.state_dir)
    gmail = gmail or _gmail()
    if gmail is None:
        raise core.AuthError("sync-replies needs GMAIL_ADDRESS and GMAIL_APP_PASSWORD (environment outreach-send)")
    try:
        summary = mailer.sync(state, config, gmail, now)
    finally:
        store.save(state, args.state_dir)
    markdown, _ = report.build(state, now, sync=summary)
    _summary(markdown)
    return 0


def _find_draft(state, draft_id):
    draft = state["drafts"].get(str(draft_id or "").strip())
    if draft is None:
        raise core.OutreachError("no draft with that id")
    return draft


def _find_prospect(state, prospect_id=None, draft_id=None):
    if draft_id:
        prospect_id = _find_draft(state, draft_id)["prospect_id"]
    prospect = state["prospects"].get(str(prospect_id or "").strip())
    if prospect is None:
        raise core.OutreachError("no prospect with that id (give --prospect-id or --draft-id)")
    return prospect


def cmd_mark(args, now):
    state = store.load(args.state_dir)
    actor = os.environ.get("GITHUB_ACTOR", "owner")
    action = args.action
    if action in ("sent", "not-sent", "reject"):
        draft = _find_draft(state, args.draft_id)
        if action == "sent":
            if draft["status"] not in (core.D_APPROVED, core.D_SEND_UNKNOWN):
                raise core.OutreachError(f"draft is {draft['status']}; only an approved or unclear draft can be marked sent "
                                         "(approve it first with mode deliver)")
            store.mark_sent(state, draft, now, "owner-confirmed" if draft["channel"] == "email" else
                            "owner-confirmed form submission", actor, "manual")
        elif action == "not-sent":
            if draft["status"] not in (core.D_APPROVED, core.D_SEND_UNKNOWN):
                raise core.OutreachError(f"draft is {draft['status']}; nothing to withdraw")
            draft["status"], draft["status_reason"] = core.D_SUPERSEDED, "owner: not sent"
            store.ledger_add(state, draft, now, mode="manual", outcome="withdrawn", confirmation="owner: not sent",
                             actor=actor)
            store.requalify(state, draft, now, "owner said it was not sent; a fresh draft will be made")
        else:
            if draft["status"] != core.D_AWAITING:
                raise core.OutreachError(f"draft is {draft['status']}; only a draft awaiting approval can be rejected")
            draft["status"], draft["status_reason"] = core.D_REJECTED, f"rejected by {actor}"
            prospect = state["prospects"].get(draft["prospect_id"])
            if prospect and draft["kind"] == "initial" and prospect["status"] in (core.DRAFTED, core.NEEDS_REVIEW):
                prospect["owner_skipped"] = True
                store.set_status(prospect, core.DISCOVERED, now, "owner rejected the draft; not drafted again")
        message = f"{draft['draft_id']}: {action} recorded ({draft['status']})"
    else:
        prospect = _find_prospect(state, args.prospect_id, args.draft_id)
        email = (prospect.get("contact") or {}).get("email")
        if action == "replied":
            store.set_status(prospect, core.REPLIED, now, f"owner: replied")
        elif action == "interested":
            store.set_status(prospect, core.REPLIED, now, "owner: positive interest")
            prospect["interest"] = "positive"
        elif action == "declined":
            store.suppress(state, now, email=email, canonical=prospect["canonical"], reason="declined", by=actor,
                           status=core.DECLINED)
        elif action == "bounced":
            if not email:
                raise core.OutreachError("this prospect has no email address to mark as bounced")
            store.suppress(state, now, email=email, reason="bounced (owner)", by=actor, status=core.BOUNCED)
        elif action == "opt-out":
            store.suppress(state, now, email=email, canonical=prospect["canonical"], reason="opted out (owner)", by=actor)
        message = f"{prospect['id']} ({prospect['name']}): {action} recorded ({prospect['status']})"
    store.event(state, f"mark:{action}", now, by=actor)
    store.save(state, args.state_dir)
    _summary(f"# Outreach · mark\n\n{core.mask_emails_in(message)}")
    return 0


def cmd_suppress(args, now):
    state = store.load(args.state_dir)
    email = core.validate_email(args.email) if args.email else None
    canonical = core.canonical_url(args.url) if args.url else None
    affected = store.suppress(state, now, email=email, canonical=canonical, reason=args.reason or "owner request",
                              by=os.environ.get("GITHUB_ACTOR", "owner"))
    store.save(state, args.state_dir)
    _summary(f"# Outreach · suppress\n\nSuppressed {core.mask_email(email) if email else ''} {canonical or ''}. "
             f"Prospects affected: {', '.join(affected) or 'none'}. They will never be drafted, contacted or re-added.")
    return 0


def cmd_add_contact(args, now):
    config = settings.load_config()
    state = store.load(args.state_dir)
    prospect = _find_prospect(state, args.prospect_id)
    email = core.validate_email(args.email)
    source = core.validate_url(args.source_url)
    if store.is_suppressed(state, email=email):
        raise core.OutreachError("that address is on the suppression list")
    other = store.find_duplicate(state, set(), email)
    if other and other["id"] != prospect["id"]:
        raise core.OutreachError(f"that address already belongs to {other['id']} ({other['name']})")
    prospect["contact"] = {"email": email, "source_kind": "owner", "source_url": source,
                           "excerpt": "entered by the owner", "verified_at": core.iso(now),
                           "verified_by": os.environ.get("GITHUB_ACTOR", "owner")}
    prospect.pop("owner_skipped", None)
    qualified = discovery.qualify(prospect, config, now)
    store.save(state, args.state_dir)
    _summary(f"# Outreach · add-contact\n\n{prospect['id']} ({prospect['name']}): contact {core.mask_email(email)} "
             f"recorded from {source}. Status: {prospect['status']}"
             + ("" if qualified else f" (not qualified: {'; '.join(prospect.get('not_qualified', []))})")
             + ". The next daily run drafts qualified prospects.")
    return 0


# ---- entry point ------------------------------------------------------------------------------------------------

def parser():
    p = argparse.ArgumentParser(prog="python -m outreach", description="Ringshift creator outreach (approval-first).")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("daily")
    sub.add_parser("review")
    d = sub.add_parser("deliver")
    d.add_argument("--draft-id", required=True)
    d.add_argument("--approval-code", required=True)
    d.add_argument("--recipient", required=True, help="the recipient typed in full (email or submission URL)")
    d.add_argument("--mode", required=True, choices=["gmail-draft", "smtp", "manual"])
    sub.add_parser("sync-replies")
    m = sub.add_parser("mark")
    m.add_argument("--action", required=True, choices=["sent", "not-sent", "reject", "replied", "interested",
                                                       "declined", "bounced", "opt-out"])
    m.add_argument("--draft-id")
    m.add_argument("--prospect-id")
    s = sub.add_parser("suppress")
    s.add_argument("--email")
    s.add_argument("--url")
    s.add_argument("--reason", default="")
    a = sub.add_parser("add-contact")
    a.add_argument("--prospect-id", required=True)
    a.add_argument("--email", required=True)
    a.add_argument("--source-url", required=True)
    return p


COMMANDS = {"daily": cmd_daily, "review": cmd_review, "deliver": cmd_deliver, "sync-replies": cmd_sync,
            "mark": cmd_mark, "suppress": cmd_suppress, "add-contact": cmd_add_contact}


def main(argv=None, now=None):
    args = parser().parse_args(argv)
    now = now or core.utcnow()
    try:
        return COMMANDS[args.command](args, now)
    except core.AuthError as error:
        _annotate("error", str(error))
        print(f"error: {core.mask_emails_in(error)}", file=sys.stderr)
        return 1
    except core.OutreachError as error:
        _annotate("error", f"refused: {error}")
        print(f"refused (nothing was sent): {core.mask_emails_in(error)}", file=sys.stderr)
        return 2
