"""Offline fixtures for the outreach tests.

Every test runs with: requests.get/post/Session.request, smtplib.SMTP/SMTP_SSL and imaplib.IMAP4_SSL replaced by
functions that fail the test, no real keys in the environment, and a temporary state directory.
Run: python -m unittest discover -s outreach/tests -t .
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from outreach import core, settings

NOW = datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc)
DEMO = "https://ultramegaray0-lgtm.github.io/ringshift-game/"
CHANNEL_ID = "UC" + "a" * 22
CHANNEL_ID_2 = "UC" + "b" * 22
VIDEO_ID = "abcdefghijk"
VIDEO_ID_2 = "bcdefghijkl"
DEMO_HTML = "<html><head><title>RINGSHIFT — Demo</title></head><body>game</body></html>"


class Response:
    def __init__(self, status_code=200, payload=None, text=None, url=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else (json.dumps(payload) if payload is not None else "")
        self.content = self.text.encode("utf-8")
        self.url = url
        self.encoding = "utf-8"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeWeb:
    """Routes GET requests by URL prefix. Unknown URLs answer 404; robots.txt answers 404 (= allowed) unless set."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append((url, dict(params or {})))
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                answer = self.routes[prefix]
                answer = answer(url, params or {}) if callable(answer) else answer
                if answer.url is None:
                    answer.url = url
                return answer
        return Response(404, text="not found", url=url)

    def api_calls(self, needle):
        return [c for c in self.calls if needle in c[0]]


def youtube_routes(*, search_items=None, channels=None, search_status=200, search_error=None):
    def search(url, params):
        if search_error:
            return Response(search_status, {"error": search_error})
        return Response(200, {"items": search_items if search_items is not None else [video_item()]})

    def channel_list(url, params):
        wanted = params.get("id", "").split(",")
        items = [c for c in (channels if channels is not None else [channel_item()]) if c["id"] in wanted]
        return Response(200, {"items": items})

    return {"https://www.googleapis.com/youtube/v3/search": search,
            "https://www.googleapis.com/youtube/v3/channels": channel_list,
            DEMO: Response(200, text=DEMO_HTML)}


def video_item(video_id=VIDEO_ID, channel_id=CHANNEL_ID, title="Trying 5 indie puzzle game demos",
               days_ago=6, description="Strange logic puzzles from Steam Next Fest."):
    return {"id": {"videoId": video_id}, "snippet": {
        "channelId": channel_id, "channelTitle": "Puzzle Pal", "title": title, "description": description,
        "publishedAt": core.iso(NOW - timedelta(days=days_ago))}}


def channel_item(channel_id=CHANNEL_ID, title="Puzzle Pal", subscribers=12000, videos=140,
                 description="I play indie puzzle games every week. Business enquiries: pal.business@gmail.com",
                 custom="@puzzlepal"):
    return {"id": channel_id, "snippet": {"title": title, "description": description, "customUrl": custom},
            "statistics": {"subscriberCount": str(subscribers), "videoCount": str(videos),
                           "hiddenSubscriberCount": False}}


def with_record(seed, **overrides):
    """A seed with the eligibility record every sources.json entry must carry (see settings.source_problems)."""
    record = {"source_type": "puzzle-curator", "enabled": True, "last_checked": "2026-10-27",
              "fit": "Covers puzzle games and invites suggestions.",
              "eligibility": {"status": "meets", "verified": ["Invites game suggestions on its own page."],
                              "uncertain": []}}
    if "submission_url" in seed or "contact_email" in seed:
        record["route_kind"] = "email" if seed.get("contact_email") and not seed.get("submission_url") else "form"
    if "submission_url" in seed:
        record["must_contain"] = seed.get("must_contain") or "suggest a game"
    return {**record, **seed, **overrides}


class FakeSMTP:
    """Records everything; can fail at login or at send in the ways smtplib does."""
    instances = []

    def __init__(self, *, login_error=None, send_error=None, refused=None):
        self.login_error, self.send_error, self.refused = login_error, send_error, refused or {}
        self.sent, self.logged_in, self.quit_called = [], False, False
        FakeSMTP.instances.append(self)

    def login(self, user, password):
        if self.login_error:
            raise self.login_error
        self.logged_in = True

    def send_message(self, msg):
        if self.send_error:
            raise self.send_error
        self.sent.append(msg)
        return self.refused

    def quit(self):
        self.quit_called = True


class FakeIMAP:
    """A tiny Gmail: folders with messages {from, to, subject, date, text}. Tracks writes."""

    def __init__(self, *, login_error=None, sent=(), all_mail=(), append_status="OK", append_error=None):
        self.login_error = login_error
        self.folders = {'"[Gmail]/Sent Mail"': list(sent), '"[Gmail]/All Mail"': list(all_mail),
                        '"[Gmail]/Drafts"': []}
        self.selected, self.readonly_selects, self.appended = None, [], []
        self.append_status, self.append_error = append_status, append_error
        self.writes = []

    def login(self, user, password):
        if self.login_error:
            raise self.login_error

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
                      b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Drafts"',
                      b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']

    def select(self, folder, readonly=False):
        self.readonly_selects.append(readonly)
        if not readonly:
            self.writes.append(("select-rw", folder))
        self.selected = folder
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        crit = list(criteria)
        msgs = self.folders.get(self.selected, [])
        found = []
        for index, message in enumerate(msgs, start=1):
            ok = True
            for key, value in zip(crit[::2], crit[1::2]):
                if key == "TO" and value.lower() != message["to"].lower():
                    ok = False
                if key == "FROM" and value.lower() not in message["from"].lower():
                    ok = False
                if key == "BODY" and value.lower() not in message.get("text", "").lower():
                    ok = False
            if ok:
                found.append(str(index).encode())
        return "OK", [b" ".join(found)]

    def fetch(self, number, what):
        message = self.folders[self.selected][int(number) - 1]
        if "HEADER" in what:
            raw = (f"From: {message['from']}\r\nTo: {message['to']}\r\nSubject: {message['subject']}\r\n"
                   f"Date: {message.get('date', 'Wed, 28 Oct 2026 07:00:00 +0000')}\r\n\r\n").encode()
        else:
            raw = message.get("text", "").encode()
        return "OK", [(b"1 (BODY[] {%d}" % len(raw), raw), b")"]

    def append(self, folder, flags, date_time, message):
        if self.append_error:
            raise self.append_error
        self.writes.append(("append", folder))
        self.appended.append((folder, flags, message))
        return self.append_status, [b"APPEND completed"]

    def logout(self):
        return "BYE", []

    # Anything that would change the mailbox fails loudly in tests that don't expect it.
    def store(self, *a, **k):
        raise AssertionError("IMAP STORE must never be used")

    def copy(self, *a, **k):
        raise AssertionError("IMAP COPY must never be used")

    def expunge(self, *a, **k):
        raise AssertionError("IMAP EXPUNGE must never be used")


class FakeGemini:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []
        self.models = self

    def generate_content(self, model, contents):
        self.prompts.append(contents)
        answer = self.answers.pop(0) if self.answers else "x"
        if isinstance(answer, Exception):
            raise answer
        return type("R", (), {"text": answer})()


class OutreachTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(mock.patch.stopall)
        self.tmp = self._tmp.name
        mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}, clear=False).start()
        for name in ("YOUTUBE_API_KEY", "GEMINI_API_KEY", "OUTREACH_GEMINI_API_KEY", "GMAIL_ADDRESS",
                     "GMAIL_APP_PASSWORD", "OUTREACH_STATE_KEY", "GITHUB_STEP_SUMMARY", "OUTREACH_SENDER_NAME",
                     "GITHUB_TOKEN"):
            os.environ.pop(name, None)

        def blocked(*args, **kwargs):
            raise AssertionError("real network/mail call attempted during an offline test")

        for target in ("requests.get", "requests.post", "requests.head", "requests.Session.request",
                       "smtplib.SMTP", "smtplib.SMTP_SSL", "imaplib.IMAP4_SSL"):
            mock.patch(target, side_effect=blocked).start()

    def config(self, **limits):
        config = settings.load_config(env={"OUTREACH_SENDER_NAME": "Test Sender"})
        config["limits"].update(limits)
        return config
