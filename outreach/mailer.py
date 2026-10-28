"""Gmail: the ONLY outreach module that can reach a mailbox. Used only by the manually started `mail` job.

Delivery of one approved draft (see approval.verify for the checks), in one of three modes:
  gmail-draft  the exact message is placed in your Gmail Drafts (IMAP APPEND). You open Gmail and press Send.
               The next `sync` finds it in Sent Mail and records SENT. Nothing is sent by this code.
  smtp         sent now through smtp.gmail.com. SENT is recorded only when Gmail's server accepted it. A connection
               that breaks mid-send is SEND_UNKNOWN and is never retried automatically.
  manual       no Gmail access at all: the message is printed for you to send yourself; `mark sent` records it.

Order for gmail-draft / smtp: verify -> log in (proves the credentials) -> claim the draft (git tag) -> deliver.
A failed login claims nothing. Reply sync is read-only (EXAMINE, BODY.PEEK): it never sends, replies, moves,
deletes or marks anything, and it never stores reply text.

Credentials come from GMAIL_ADDRESS / GMAIL_APP_PASSWORD (a Google app password; 2-Step Verification required).
They are never printed or stored.
"""

import imaplib
import re
import smtplib
import socket
from datetime import timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as default_policy
from email.utils import formataddr, formatdate, make_msgid, parsedate_to_datetime

from . import approval, core, store

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
IMAP_HOST = "imap.gmail.com"
TIMEOUT = 30
OPT_OUT = re.compile(r"\b(no,? thanks|no thank you|unsubscribe|stop (emailing|writing|contacting)|remove me|"
                     r"take me off|(don'?t|do not) (contact|email|write)|not interested|please stop)\b", re.I)


def build_message(draft, sender_address, sender_name):
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_address))
    msg["To"] = draft["recipient"]
    msg["Subject"] = draft["subject"]
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(idstring=draft["draft_id"], domain=sender_address.split("@")[-1])
    msg["X-Ringshift-Outreach"] = draft["draft_id"]
    msg["List-Unsubscribe"] = f"<mailto:{sender_address}?subject=no%20thanks>"
    msg.set_content(draft["body"])
    return msg


class Gmail:
    def __init__(self, address, app_password, *, smtp_factory=None, imap_factory=None):
        self.address = core.validate_email(address) if address else None
        self._password = re.sub(r"\s+", "", str(app_password or ""))
        if not self.address or len(self._password) < 16:
            raise core.AuthError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD are missing or malformed (values not shown)")
        self._smtp_factory = smtp_factory or (lambda: smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT))
        self._imap_factory = imap_factory or (lambda: imaplib.IMAP4_SSL(IMAP_HOST, timeout=TIMEOUT))

    # ---- SMTP ----
    def smtp_login(self):
        try:
            server = self._smtp_factory()
            server.login(self.address, self._password)
            return server
        except smtplib.SMTPAuthenticationError:
            raise core.AuthError("Gmail rejected the app password (SMTP). Check GMAIL_APP_PASSWORD.") from None
        except (smtplib.SMTPException, OSError) as error:
            raise core.OutreachError(f"could not connect to Gmail SMTP ({type(error).__name__}); nothing was sent") from None

    # ---- IMAP ----
    def imap_login(self):
        try:
            imap = self._imap_factory()
            imap.login(self.address, self._password)
            return imap
        except imaplib.IMAP4.error:
            raise core.AuthError("Gmail rejected the app password (IMAP). Check GMAIL_APP_PASSWORD and that IMAP "
                                 "is enabled in Gmail settings.") from None
        except OSError as error:
            raise core.OutreachError(f"could not connect to Gmail IMAP ({type(error).__name__})") from None

    @staticmethod
    def special_folder(imap, flag):
        status, lines = imap.list()
        if status != "OK":
            raise core.OutreachError("Gmail did not list its folders")
        for line in lines or []:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            match = re.match(r'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', text)
            if match and flag.lower() in match.group("flags").lower().split():
                name = match.group("name").strip()
                return name if name.startswith('"') else f'"{name}"'
        raise core.OutreachError(f"Gmail folder with {flag} not found")


def deliver(state, config, draft_id, approval_code, recipient_confirmation, mode, *, claims, now, demo_ok,
            gmail=None, actor="owner"):
    """Deliver one approved draft. Returns a dict describing exactly what happened."""
    if mode not in ("gmail-draft", "smtp", "manual"):
        raise core.OutreachError("delivery must be gmail-draft, smtp or manual")
    draft = approval.verify(state, draft_id, approval_code, recipient_confirmation, config, now, demo_ok=demo_ok)
    if draft["channel"] != "email" and mode != "manual":
        raise core.OutreachError("a submission-form draft can only be delivered with mode manual (you paste it)")
    if mode != "manual" and gmail is None:
        raise core.OutreachError(f"mode {mode} needs GMAIL_ADDRESS and GMAIL_APP_PASSWORD (environment outreach-send). "
                                 "Use mode manual to send it yourself instead.")
    prospect = state["prospects"][draft["prospect_id"]]
    info = {"mode": mode, "actor": actor, "at": core.iso(now)}

    if mode == "manual":
        claims.claim(draft["draft_id"], info)
        draft["status"], draft["approved_at"], draft["delivery"] = core.D_APPROVED, core.iso(now), "manual"
        if prospect["status"] not in (core.SENT, core.REPLIED):
            store.set_status(prospect, core.APPROVED, now, f"approved {draft['draft_id']} for manual sending")
        store.ledger_add(state, draft, now, mode=mode, outcome="manual", confirmation="handed to owner", actor=actor)
        return {"outcome": "manual", "draft": draft}

    msg = build_message(draft, gmail.address, config["sender"]["name"])
    if mode == "gmail-draft":
        imap = gmail.imap_login()                         # proves the credentials before anything is claimed
        try:
            folder = gmail.special_folder(imap, "\\Drafts")
            claims.claim(draft["draft_id"], info)
            try:
                status, _ = imap.append(folder, "(\\Draft)", imaplib.Time2Internaldate(now.timestamp()),
                                        msg.as_bytes())
            except (imaplib.IMAP4.error, OSError) as error:
                draft["status"], draft["status_reason"] = core.D_SEND_UNKNOWN, f"Gmail Drafts: {type(error).__name__}"
                store.ledger_add(state, draft, now, mode=mode, outcome="unknown", confirmation=draft["status_reason"], actor=actor)
                return {"outcome": "unknown", "draft": draft}
            if status != "OK":
                draft["status"], draft["status_reason"] = core.D_SEND_FAILED, "Gmail refused to save the draft"
                store.ledger_add(state, draft, now, mode=mode, outcome="failed", confirmation=draft["status_reason"], actor=actor)
                store.requalify(state, draft, now, "Gmail draft could not be created; a new draft will be made")
                return {"outcome": "failed", "draft": draft}
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        draft["status"], draft["approved_at"], draft["delivery"] = core.D_APPROVED, core.iso(now), "gmail-draft"
        draft["message_id"] = msg["Message-ID"]
        if prospect["status"] not in (core.SENT, core.REPLIED):
            store.set_status(prospect, core.APPROVED, now, f"{draft['draft_id']} placed in Gmail Drafts")
        store.ledger_add(state, draft, now, mode=mode, outcome="gmail-draft", confirmation="saved in Gmail Drafts (IMAP OK)",
                actor=actor)
        return {"outcome": "gmail-draft", "draft": draft}

    server = gmail.smtp_login()                           # mode smtp
    try:
        claims.claim(draft["draft_id"], info)
        try:
            refused = server.send_message(msg)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError,
                smtplib.SMTPNotSupportedError) as error:
            code = getattr(error, "smtp_code", None)
            draft["status"] = core.D_SEND_FAILED
            draft["status_reason"] = f"Gmail refused the message ({type(error).__name__}{f' {code}' if code else ''})"
            store.ledger_add(state, draft, now, mode=mode, outcome="failed", confirmation=draft["status_reason"], actor=actor)
            store.requalify(state, draft, now, "not sent; a new draft will be made")
            return {"outcome": "failed", "draft": draft}
        except (smtplib.SMTPServerDisconnected, socket.timeout, OSError, smtplib.SMTPException) as error:
            draft["status"], draft["status_reason"] = core.D_SEND_UNKNOWN, f"connection broke mid-send ({type(error).__name__})"
            store.ledger_add(state, draft, now, mode=mode, outcome="unknown", confirmation=draft["status_reason"], actor=actor)
            return {"outcome": "unknown", "draft": draft}
        if refused:
            draft["status"], draft["status_reason"] = core.D_SEND_FAILED, "Gmail refused the recipient"
            store.ledger_add(state, draft, now, mode=mode, outcome="failed", confirmation=draft["status_reason"], actor=actor)
            store.requalify(state, draft, now, "recipient refused")
            return {"outcome": "failed", "draft": draft}
        draft["message_id"] = msg["Message-ID"]
        store.mark_sent(state, draft, now, "accepted by smtp.gmail.com (250)", actor, mode)
        return {"outcome": "sent", "draft": draft}
    finally:
        try:
            server.quit()
        except Exception:
            pass


# ---- read-only sync --------------------------------------------------------------------------------------------

def _imap_date(moment):
    return moment.strftime("%d-%b-%Y")


def _search(imap, *criteria):
    status, data = imap.search(None, *criteria)
    if status != "OK":
        raise core.OutreachError("Gmail search failed")
    return (data[0] or b"").split() if data else []


def _headers(imap, number):
    status, data = imap.fetch(number, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return BytesParser(policy=default_policy).parsebytes(data[0][1], headersonly=True)


def _text_start(imap, number):
    status, data = imap.fetch(number, "(BODY.PEEK[TEXT]<0.4000>)")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        return ""
    return data[0][1].decode("utf-8", "replace")


def _subject(message):
    try:
        return str(make_header(decode_header(str(message.get("Subject", ""))))).strip()
    except Exception:
        return str(message.get("Subject", "")).strip()


def sync(state, config, gmail, now):
    """Confirm sends, record replies, bounces and opt-outs. Returns a summary dict. Sends nothing."""
    summary = {"confirmed_sent": [], "replied": [], "opted_out": [], "bounced": [], "errors": []}
    imap = gmail.imap_login()
    try:
        sent_folder = gmail.special_folder(imap, "\\Sent")
        all_folder = gmail.special_folder(imap, "\\All")

        # 1. Drafts you sent from Gmail yourself (gmail-draft / manual) or whose SMTP result was unclear.
        imap.select(sent_folder, readonly=True)
        for draft in state["drafts"].values():
            if draft["status"] not in (core.D_APPROVED, core.D_SEND_UNKNOWN) or draft["channel"] != "email":
                continue
            since = core.parse_time(draft.get("approved_at") or draft["created_at"]) - timedelta(days=1)
            for number in _search(imap, "TO", draft["recipient"], "SINCE", _imap_date(since)):
                message = _headers(imap, number)
                if message is not None and _subject(message) == draft["subject"]:
                    try:
                        sent_at = parsedate_to_datetime(message["Date"])
                    except (TypeError, ValueError):
                        sent_at = now
                    store.mark_sent(state, draft, now, "found in Gmail Sent Mail", "gmail-sync", "sync", sent_at=sent_at)
                    summary["confirmed_sent"].append(draft["draft_id"])
                    break

        # 2. Replies, opt-outs and bounces for everyone we have written to.
        imap.select(all_folder, readonly=True)
        contacted = {}
        for draft in state["drafts"].values():
            if draft["status"] == core.D_SENT and draft["channel"] == "email":
                first = contacted.get(draft["recipient"])
                if first is None or draft["sent_at"] < first["sent_at"]:
                    contacted[draft["recipient"]] = draft
        for email, draft in contacted.items():
            prospect = state["prospects"].get(draft["prospect_id"])
            if prospect is None or prospect["status"] in (core.SUPPRESSED, core.BOUNCED):
                continue
            since = _imap_date(core.parse_time(draft["sent_at"]) - timedelta(days=1))
            try:
                bounced = _search(imap, "FROM", "mailer-daemon", "SINCE", since, "BODY", email)
                if bounced:
                    store.suppress(state, now, email=email, reason="message bounced", by="gmail-sync",
                                   status=core.BOUNCED)
                    summary["bounced"].append(prospect["id"])
                    continue
                replies = _search(imap, "FROM", email, "SINCE", since)
            except core.OutreachError as error:
                summary["errors"].append(f"{prospect['id']}: {error}")
                continue
            if not replies:
                continue
            opted_out = any(OPT_OUT.search(_text_start(imap, n)) for n in replies[-5:])
            if opted_out:
                store.suppress(state, now, email=email, canonical=prospect["canonical"],
                               reason="asked not to be contacted (reply)", by="gmail-sync")
                summary["opted_out"].append(prospect["id"])
            elif prospect["status"] not in (core.REPLIED, core.DECLINED):
                store.set_status(prospect, core.REPLIED, now, "reply found in Gmail")
                prospect["replied_at"] = core.iso(now)
                summary["replied"].append(prospect["id"])
            for d in state["drafts"].values():           # no follow-up to someone who answered
                if d["prospect_id"] == prospect["id"] and d["kind"] == "followup" and d["status"] == core.D_AWAITING:
                    d["status"], d["status_reason"] = core.D_SUPERSEDED, "they replied"
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    state["sync"] = {"last_ok_at": core.iso(now), "last_summary": {k: len(v) for k, v in summary.items()}}
    store.event(state, "sync", now, **{k: len(v) for k, v in summary.items()})
    return summary
