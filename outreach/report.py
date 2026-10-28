"""The run summary: what was found, drafted, sent and heard back. Email addresses are always masked here because
GitHub Actions logs and summaries of a public repository are public."""

from . import core, web


def check_demo(fetcher, config, now):
    """Is the demo link live and really Ringshift? Recorded in every draft and re-checked before delivery."""
    url, expect = config["demo"]["url"], config["demo"].get("expect_title_contains", "")
    result = {"url": url, "checked_at": core.iso(now), "ok": False}
    try:
        final, html = fetcher.page(url)
    except core.OutreachError as error:
        result["reason"] = str(error)
        return result
    title = web.page_title(html)
    result["title"] = title
    if expect and expect.lower() not in title.lower():
        result["reason"] = f"page title {title!r} does not contain {expect!r}"
        return result
    if core.canonical_url(final) != core.canonical_url(url):
        result["reason"] = f"the link redirects elsewhere ({core.canonical_url(final)})"
        return result
    result["ok"] = True
    return result


def _counts(state, now):
    day = core.iso(now)[:10]
    prospects = state["prospects"].values()
    drafts = state["drafts"].values()
    return {
        "prospects_total": len(state["prospects"]),
        "qualified_now": sum(p["status"] == core.QUALIFIED for p in prospects),
        "awaiting_approval": sum(d["status"] == core.D_AWAITING for d in drafts),
        "approved_not_confirmed": sum(d["status"] == core.D_APPROVED for d in drafts),
        "send_unknown": sum(d["status"] == core.D_SEND_UNKNOWN for d in drafts),
        "sent_today": sum(d["status"] == core.D_SENT and str(d.get("sent_at", ""))[:10] == day for d in drafts),
        "sent_total": sum(d["status"] == core.D_SENT for d in drafts),
        "replied_total": sum(p["status"] == core.REPLIED for p in prospects),
        "positive_interest": sum(p.get("interest") == "positive" for p in prospects),
        "suppressed": len(state["suppression"]["emails"]) + len(state["suppression"]["urls"]),
        "bounced": sum(p["status"] == core.BOUNCED for p in prospects),
    }


def _fence(text):
    return "~~~~text\n" + str(text).replace("~~~~", "~ ~ ~ ~") + "\n~~~~"


def draft_section(state, draft):
    prospect = state["prospects"].get(draft["prospect_id"], {})
    evidence, contact = draft["evidence"], draft.get("contact") or {}
    recipient = core.mask_email(draft["recipient"]) if draft["channel"] == "email" else draft["recipient"]
    lines = [f"### `{draft['draft_id']}` · {prospect.get('name', '?')} ({prospect.get('category', '?')}) · "
             f"score {prospect.get('score', '?')} · {draft['kind']}",
             f"- **Approval code:** `{draft['approval_code']}`",
             f"- **To:** {recipient} ({draft['channel']})" + (
                 f" — published at {contact.get('source_url')} ({contact.get('source_kind')}, checked "
                 f"{str(contact.get('verified_at', ''))[:10]})" if contact.get("source_url") else ""),
             f"- **Evidence:** [{evidence.get('title')}]({evidence.get('url')})"
             + (f" · {str(evidence.get('date'))[:10]}" if evidence.get("date") else "") + f" · {evidence.get('source')}",
             f"- **Why:** {'; '.join(prospect.get('score_reasons', [])[:4])}",
             f"- **Personal sentence:** {draft.get('personalization')}"]
    for warning in draft.get("warnings", []):
        lines.append(f"- ⚠️ {warning}")
    for problem in draft.get("blocking", []):
        lines.append(f"- ⛔ cannot be approved: {problem}")
    lines += [f"- **Subject:** {draft['subject']}", "", _fence(draft["body"]), ""]
    return "\n".join(lines)


def build(state, now, *, discovery=None, drafting=None, sync=None, notes=(), demo=None):
    counts = _counts(state, now)
    out = [f"# Ringshift outreach · {core.iso(now)[:10]}", ""]
    if demo is not None:
        out.append(f"Demo link {demo['url']}: " + ("verified ✅" if demo.get("ok") else f"**NOT verified** ({demo.get('reason')})"))
        out.append("")
    found = discovery or {}
    out += ["| New prospects | Qualified (new) | Drafts awaiting you | Sent today | Sent (all) | Replies | "
            "Positive interest | Rejected candidates | Errors |",
            "|---|---|---|---|---|---|---|---|---|",
            f"| {len(found.get('new', []))} | {len(found.get('qualified', []))} | {counts['awaiting_approval']} | "
            f"{counts['sent_today']} | {counts['sent_total']} | {counts['replied_total']} | "
            f"{counts['positive_interest']} | {found.get('rejected', 0)} | "
            f"{len(found.get('errors', [])) + len(found.get('fatal', []))} |", ""]
    if counts["approved_not_confirmed"] or counts["send_unknown"]:
        out.append(f"Approved, waiting for Gmail confirmation: {counts['approved_not_confirmed']} · unclear sends to "
                   f"check by hand: {counts['send_unknown']}")
        out.append("")
    last_sync = state["sync"].get("last_ok_at")
    out.append(f"Replies were last checked in Gmail: {last_sync[:16].replace('T', ' ') + ' UTC' if last_sync else 'never'}"
               " (run mode `sync-replies`).")
    out.append("")
    if sync:
        out.append("**Gmail sync:** " + ", ".join(f"{k.replace('_', ' ')} {len(v)}" for k, v in sync.items()))
        out.append("")

    awaiting = sorted((d for d in state["drafts"].values() if d["status"] == core.D_AWAITING),
                      key=lambda d: d["created_at"])
    out.append(f"## Drafts awaiting your approval ({len(awaiting)})")
    out.append("")
    if not awaiting:
        out.append("None.")
    for draft in awaiting:
        out.append(draft_section(state, draft))
    out.append("")

    if drafting:
        for note in drafting.get("notes", []):
            out.append(f"- Drafting note: {core.mask_emails_in(note)}")
        if drafting.get("expired"):
            out.append(f"- Expired unapproved drafts: {', '.join(drafting['expired'])}")
    for note in notes:
        out.append(f"- {core.mask_emails_in(note)}")
    errors = list(found.get("fatal", [])) + list(found.get("errors", []))
    if errors:
        out += ["", "## Errors"] + [f"- {core.mask_emails_in(e)}" for e in errors]
    rejected = [r for r in state["rejected"] if r["at"][:10] == core.iso(now)[:10]]
    if rejected:
        out += ["", f"<details><summary>Rejected candidates today ({len(rejected)})</summary>", ""]
        out += [f"- {r['name']} — {core.mask_emails_in(r['reason'])} ({r['source']})" for r in rejected[-50:]]
        out += ["", "</details>"]
    out += ["", "**To approve one:** Actions → *Ringshift creator outreach* → Run workflow (branch `main`) → "
            "mode `deliver`, the draft id, its approval code, the recipient typed in full, and a delivery mode. "
            "See outreach/README.md."]
    return "\n".join(out), counts
