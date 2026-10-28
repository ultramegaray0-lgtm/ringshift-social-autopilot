"""Approval and delivery: exact draft only, once, to a clean address; SENT only on provider confirmation."""

import imaplib
import smtplib
import socket
from datetime import timedelta
from pathlib import Path

from outreach import approval, claims, core, drafting, mailer, store
from outreach.tests.support import NOW, FakeIMAP, FakeSMTP, OutreachTestCase
from outreach.tests.test_drafting import DEMO_OK, DraftTestCase

ADDRESS, PASSWORD = "me.ringshift@gmail.com", "abcd efgh ijkl mnop"


class DeliveryTestCase(DraftTestCase):
    def setUp(self):
        super().setUp()
        self.state, self.prospect = self.qualified_state()
        draft_id = drafting.make_drafts(self.state, self.config(), NOW, demo=DEMO_OK)["created"][0]
        self.draft = self.state["drafts"][draft_id]
        self.claims = claims.FileClaims(Path(self.tmp) / "claims")
        FakeSMTP.instances = []

    def gmail(self, smtp=None, imap=None):
        return mailer.Gmail(ADDRESS, PASSWORD, smtp_factory=lambda: smtp or FakeSMTP(),
                            imap_factory=lambda: imap or FakeIMAP())

    def deliver(self, mode="smtp", code=None, recipient=None, gmail=None, now=NOW, demo_ok=True, draft=None):
        draft = draft or self.draft
        return mailer.deliver(self.state, self.config(), draft["draft_id"], code or draft["approval_code"],
                              recipient or draft["recipient"], mode, claims=self.claims, now=now, demo_ok=demo_ok,
                              gmail=gmail if gmail is not None else (None if mode == "manual" else self.gmail()))


class ApprovalTests(DeliveryTestCase):
    def assert_refused(self, pattern, **kwargs):
        smtp = FakeSMTP()
        ledger_before = list(self.state["ledger"])
        with self.assertRaisesRegex(core.OutreachError, pattern):
            self.deliver(gmail=self.gmail(smtp=smtp), **kwargs)
        self.assertEqual(smtp.sent, [])
        self.assertFalse(smtp.logged_in)                     # refused before Gmail was even contacted
        self.assertFalse(self.claims.is_claimed(self.draft["draft_id"]))
        self.assertEqual(self.state["ledger"], ledger_before)

    def test_wrong_code_wrong_recipient_or_edited_draft_is_refused(self):
        self.assert_refused("approval code", code="0" * 12)
        self.assert_refused("recipient you typed", recipient="someone.else@gmail.com")
        self.draft["body"] = self.draft["body"].replace("Thanks for reading", "Thanks a lot")
        self.assert_refused("approval code")

    def test_expired_suppressed_or_unverified_demo_is_refused(self):
        self.assert_refused("expired", now=NOW + timedelta(days=15))
        self.assert_refused("demo link", demo_ok=False)
        store.suppress(self.state, NOW, email=self.draft["recipient"], reason="opt-out", by="test")
        self.assert_refused("not awaiting approval|suppress")

    def test_a_draft_with_blocking_problems_can_never_be_approved(self):
        self.draft["body"] += "\nI can send you a free Steam key."
        self.draft["approval_code"] = approval.code_for(self.draft)
        self.assert_refused("message rules")

    def test_daily_send_limit(self):
        self.state["ledger"] = [{"at": core.iso(NOW), "draft_id": f"od-0000000{i}", "outcome": "sent",
                                 "recipient": f"x{i}@studio.com", "kind": "initial"} for i in range(5)]
        self.assert_refused("daily limit")


class SmtpTests(DeliveryTestCase):
    def test_success_is_recorded_only_after_gmail_accepts(self):
        smtp = FakeSMTP()
        result = self.deliver(gmail=self.gmail(smtp=smtp))
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(len(smtp.sent), 1)
        msg = smtp.sent[0]
        self.assertEqual(msg["To"], "pal.business@gmail.com")
        self.assertEqual(msg["Subject"], self.draft["subject"])
        self.assertEqual(msg.get_content().strip(), self.draft["body"].strip())
        self.assertEqual(self.draft["status"], core.D_SENT)
        self.assertEqual(self.prospect["status"], core.SENT)
        self.assertEqual(self.prospect["last_contact"], core.iso(NOW))
        self.assertEqual(self.state["ledger"][-1]["outcome"], "sent")
        self.assertIn("250", self.state["ledger"][-1]["confirmation"])

    def test_duplicate_send_is_impossible(self):
        self.deliver()
        with self.assertRaisesRegex(core.OutreachError, "not awaiting approval"):
            self.deliver()
        # Even with the state rolled back (e.g. the state push failed), the claim tag blocks a second send.
        self.draft["status"] = core.D_AWAITING
        self.state["ledger"] = []
        smtp = FakeSMTP()
        with self.assertRaises(claims.AlreadyClaimed):
            self.deliver(gmail=self.gmail(smtp=smtp))
        self.assertEqual(smtp.sent, [])

    def test_already_contacted_address_is_refused_for_a_new_draft(self):
        self.deliver()
        self.prospect["status"] = core.QUALIFIED          # pretend discovery found them again
        new_id = drafting.make_drafts(self.state, self.config(), NOW, demo=DEMO_OK)
        self.assertEqual(new_id["created"], [])           # contacted addresses are not even drafted
        twin = dict(self.draft, draft_id="od-12345678", status=core.D_AWAITING)
        twin["approval_code"] = approval.code_for(twin)
        self.state["drafts"][twin["draft_id"]] = twin
        with self.assertRaisesRegex(core.OutreachError, "already contacted"):
            self.deliver(draft=twin)

    def test_login_failure_claims_nothing(self):
        smtp = FakeSMTP(login_error=smtplib.SMTPAuthenticationError(535, b"bad"))
        with self.assertRaises(core.AuthError):
            self.deliver(gmail=self.gmail(smtp=smtp))
        self.assertFalse(self.claims.is_claimed(self.draft["draft_id"]))
        self.assertEqual(self.draft["status"], core.D_AWAITING)

    def test_refusal_is_not_sent_and_the_prospect_gets_a_fresh_draft_later(self):
        smtp = FakeSMTP(send_error=smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded"))
        result = self.deliver(gmail=self.gmail(smtp=smtp))
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(self.draft["status"], core.D_SEND_FAILED)
        self.assertNotEqual(self.prospect["status"], core.SENT)
        self.assertEqual(self.prospect["status"], core.QUALIFIED)
        self.assertEqual(self.state["ledger"][-1]["outcome"], "failed")

    def test_broken_connection_is_unknown_and_blocks_the_address(self):
        smtp = FakeSMTP(send_error=smtplib.SMTPServerDisconnected("gone"))
        result = self.deliver(gmail=self.gmail(smtp=smtp))
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(self.draft["status"], core.D_SEND_UNKNOWN)
        self.assertEqual(self.prospect["status"], core.DRAFTED)
        self.assertIn("already contacted", approval.recipient_block(
            self.state, dict(self.draft, draft_id="od-99999999", kind="initial")))

    def test_timeout_is_unknown_too(self):
        result = self.deliver(gmail=self.gmail(smtp=FakeSMTP(send_error=socket.timeout())))
        self.assertEqual(result["outcome"], "unknown")

    def test_missing_or_bad_credentials(self):
        with self.assertRaises(core.AuthError):
            mailer.Gmail("", "")
        with self.assertRaises(core.AuthError):
            mailer.Gmail(ADDRESS, "short")
        with self.assertRaisesRegex(core.OutreachError, "manual"):
            mailer.deliver(self.state, self.config(), self.draft["draft_id"], self.draft["approval_code"],
                           self.draft["recipient"], "smtp", claims=self.claims, now=NOW, demo_ok=True, gmail=None)


class GmailDraftAndManualTests(DeliveryTestCase):
    def test_gmail_draft_saves_exact_message_and_sends_nothing(self):
        imap = FakeIMAP()
        result = self.deliver(mode="gmail-draft", gmail=self.gmail(imap=imap))
        self.assertEqual(result["outcome"], "gmail-draft")
        self.assertEqual(FakeSMTP.instances, [])
        folder, flags, raw = imap.appended[0]
        self.assertEqual(folder, '"[Gmail]/Drafts"')
        self.assertIn("\\Draft", flags)
        self.assertIn(b"pal.business@gmail.com", raw)
        self.assertEqual(self.draft["status"], core.D_APPROVED)
        self.assertEqual(self.prospect["status"], core.APPROVED)      # NOT sent until Gmail shows it in Sent Mail

    def test_gmail_draft_login_failure_and_refusal(self):
        with self.assertRaises(core.AuthError):
            self.deliver(mode="gmail-draft", gmail=self.gmail(imap=FakeIMAP(login_error=imaplib.IMAP4.error("x"))))
        self.assertFalse(self.claims.is_claimed(self.draft["draft_id"]))
        result = self.deliver(mode="gmail-draft", gmail=self.gmail(imap=FakeIMAP(append_status="NO")))
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(self.prospect["status"], core.QUALIFIED)

    def test_manual_mode_needs_no_gmail_and_is_not_sent_until_you_say_so(self):
        result = self.deliver(mode="manual", gmail=None)
        self.assertEqual(result["outcome"], "manual")
        self.assertEqual(self.draft["status"], core.D_APPROVED)
        self.assertNotEqual(self.prospect["status"], core.SENT)
        self.assertEqual(FakeSMTP.instances, [])

    def test_form_drafts_are_manual_only(self):
        self.draft["channel"], self.draft["recipient"] = "form", "https://puzzlepicks.net/submit"
        self.draft["require_evidence"] = False
        self.draft["approval_code"] = approval.code_for(self.draft)
        with self.assertRaisesRegex(core.OutreachError, "manual"):
            self.deliver(mode="smtp", recipient="https://puzzlepicks.net/submit")


class SyncTests(DeliveryTestCase):
    def sent_message(self):
        return {"from": ADDRESS, "to": self.draft["recipient"], "subject": self.draft["subject"],
                "date": "Wed, 28 Oct 2026 08:00:00 +0000"}

    def test_sync_confirms_a_gmail_draft_you_sent(self):
        self.deliver(mode="gmail-draft", gmail=self.gmail(imap=FakeIMAP()))
        imap = FakeIMAP(sent=[self.sent_message()])
        summary = mailer.sync(self.state, self.config(), self.gmail(imap=imap), NOW + timedelta(hours=3))
        self.assertEqual(summary["confirmed_sent"], [self.draft["draft_id"]])
        self.assertEqual(self.draft["status"], core.D_SENT)
        self.assertEqual(self.prospect["status"], core.SENT)
        self.assertTrue(all(imap.readonly_selects))
        self.assertEqual(imap.writes, [])

    def test_unsent_gmail_draft_is_not_marked_sent(self):
        self.deliver(mode="gmail-draft", gmail=self.gmail(imap=FakeIMAP()))
        other = dict(self.sent_message(), subject="Something else")
        mailer.sync(self.state, self.config(), self.gmail(imap=FakeIMAP(sent=[other])), NOW)
        self.assertEqual(self.draft["status"], core.D_APPROVED)

    def test_reply_opt_out_and_bounce(self):
        cases = [
            ({"from": "Pal <pal.business@gmail.com>", "to": ADDRESS, "subject": "Re: x", "text": "Sounds fun, will try"},
             core.REPLIED, "replied"),
            ({"from": "pal.business@gmail.com", "to": ADDRESS, "subject": "Re: x", "text": "No thanks, please stop."},
             core.SUPPRESSED, "opted_out"),
            ({"from": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "to": ADDRESS, "subject": "Delivery",
              "text": "Address not found: pal.business@gmail.com"}, core.BOUNCED, "bounced"),
        ]
        for message, status, key in cases:
            with self.subTest(key):
                self.setUp()
                self.deliver()
                imap = FakeIMAP(all_mail=[message])
                summary = mailer.sync(self.state, self.config(), self.gmail(imap=imap), NOW + timedelta(days=1))
                self.assertEqual(summary[key], [self.prospect["id"]])
                self.assertEqual(self.prospect["status"], status)
                self.assertEqual(imap.writes, [])
                self.assertEqual(FakeSMTP.instances[-1].sent and len(FakeSMTP.instances[-1].sent), 1)  # only the original
                self.assertNotIn("text", str(self.state["events"][-1]))           # reply text is never stored
                if key != "replied":
                    self.assertTrue(store.is_suppressed(self.state, email="pal.business@gmail.com"))

    def test_reply_cancels_a_queued_follow_up(self):
        self.deliver(now=NOW - timedelta(days=15))
        followup = drafting.make_drafts(self.state, self.config(), NOW, demo=DEMO_OK)["followups"][0]
        reply = {"from": "pal.business@gmail.com", "to": ADDRESS, "subject": "Re", "text": "Thanks, I'll look"}
        mailer.sync(self.state, self.config(), self.gmail(imap=FakeIMAP(all_mail=[reply])), NOW)
        self.assertEqual(self.state["drafts"][followup]["status"], core.D_SUPERSEDED)

    def test_follow_up_cannot_go_twice_or_without_a_confirmed_first_email(self):
        self.deliver(mode="manual", gmail=None, now=NOW - timedelta(days=15))
        followup = dict(self.draft, draft_id="od-abcdef01", kind="followup", status=core.D_AWAITING)
        self.assertIn("confirmed first email", approval.recipient_block(self.state, followup))
        store.mark_sent(self.state, self.draft, NOW - timedelta(days=15), "owner-confirmed", "owner", "manual")
        self.assertIsNone(approval.recipient_block(self.state, followup))
        self.state["ledger"].append({"at": core.iso(NOW), "draft_id": "od-abcdef02", "kind": "followup",
                                     "outcome": "sent", "recipient": self.draft["recipient"]})
        self.assertIn("never a second", approval.recipient_block(self.state, followup))
