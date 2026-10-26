"""outreach.py: a tracker and draft writer only. Nothing is ever sent."""

import inspect
import os
import unittest
from datetime import date
from unittest import mock

import support
from support import FakeResponse, OfflineTestCase
import outreach

D0 = date(2026, 10, 26)


class OutreachTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.leads = self.tmp / "data" / "outreach.csv"
        self.drafts = self.tmp / "drafts"

    def add(self, name="Puzzle Fan", url="https://www.youtube.com/@puzzlefan", **kw):
        return outreach.add_lead(name, "youtube", url, path=self.leads, today=D0, **kw)

    # ---- tracker ----
    def test_add_and_list(self):
        lead = self.add(why="plays logic puzzles")
        self.assertEqual((lead["id"], lead["status"]), ("L001", "found"))
        self.assertEqual(self.add("Second", "https://example.com/two")["id"], "L002")
        self.assertEqual(len(outreach.list_leads(path=self.leads)), 2)
        self.assertEqual(outreach.list_leads("drafted", self.leads), [])

    def test_duplicate_urls_are_refused_even_with_different_formatting(self):
        self.add()
        with self.assertRaisesRegex(outreach.OutreachError, "already tracked"):
            self.add("Same person", "https://youtube.com/@PuzzleFan/")

    def test_bad_input_is_refused(self):
        for kwargs in ({"name": " "}, {"url": "http://insecure"}, {"url": "not a url"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(outreach.OutreachError):
                self.add(**kwargs)
        with self.assertRaises(outreach.OutreachError):
            outreach.add_lead("x", "myspace", "https://x.example", path=self.leads)

    # ---- the human gate ----
    def test_cannot_jump_to_contacted_without_a_reviewed_draft(self):
        lead = self.add()
        with self.assertRaisesRegex(outreach.OutreachError, "approved"):
            outreach.set_status(lead["id"], "contacted", path=self.leads)
        with self.assertRaisesRegex(outreach.OutreachError, "no draft"):
            outreach.set_status(lead["id"], "approved", path=self.leads)

    def test_full_flow(self):
        lead = self.add()
        outreach.write_draft(lead["id"], path=self.leads, drafts_dir=self.drafts, link="https://x.example/r", today=D0)
        self.assertEqual(outreach.list_leads(path=self.leads)[0]["status"], "drafted")
        outreach.set_status(lead["id"], "approved", path=self.leads, today=D0)
        outreach.set_status(lead["id"], "contacted", "sent via YouTube about page", path=self.leads, today=D0)
        self.assertEqual(outreach.read_leads(self.leads)[0]["note"], "sent via YouTube about page")

    # ---- drafts ----
    def test_draft_is_clearly_unsent_and_has_placeholders(self):
        lead = self.add()
        path = outreach.write_draft(lead["id"], path=self.leads, drafts_dir=self.drafts, link=None, today=D0)
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("DRAFT - NOT SENT"))
        self.assertIn("[WRITE ONE SPECIFIC SENTENCE", text)
        self.assertIn("[ADD A LINK", text)               # no project URL configured -> never invented
        self.assertIn("ignore this", text)               # an easy way to say no

    def test_draft_contains_no_invented_claims(self):
        import post_rules
        text = outreach.build_draft({"name": "A", "category": "creator", "why": "", "platform": "x", "url": "https://a"}, None)
        self.assertEqual(post_rules.find_unsupported_claims(text), [])

    def test_declined_and_skipped_leads_get_no_draft(self):
        lead = self.add()
        outreach.set_status(lead["id"], "declined", path=self.leads, today=D0)
        with self.assertRaisesRegex(outreach.OutreachError, "said no"):
            outreach.write_draft(lead["id"], path=self.leads, drafts_dir=self.drafts, today=D0)

    def test_recently_contacted_leads_are_not_redrafted(self):
        lead = self.add()
        outreach.write_draft(lead["id"], path=self.leads, drafts_dir=self.drafts, today=D0)
        outreach.set_status(lead["id"], "approved", path=self.leads, today=D0)
        outreach.set_status(lead["id"], "contacted", path=self.leads, today=D0)
        with self.assertRaisesRegex(outreach.OutreachError, "already contacted"):
            outreach.write_draft(lead["id"], path=self.leads, drafts_dir=self.drafts, today=date(2026, 11, 20))
        # after the block window a draft is allowed again
        outreach.write_draft(lead["id"], path=self.leads, drafts_dir=self.drafts, today=date(2027, 2, 10))

    def test_due_lists_old_contacted_leads_only(self):
        lead = self.add()
        outreach.write_draft(lead["id"], path=self.leads, drafts_dir=self.drafts, today=D0)
        outreach.set_status(lead["id"], "approved", path=self.leads, today=D0)
        outreach.set_status(lead["id"], "contacted", path=self.leads, today=D0)
        self.assertEqual(outreach.due_leads(path=self.leads, today=date(2026, 11, 1)), [])
        self.assertEqual(len(outreach.due_leads(path=self.leads, today=date(2026, 11, 20))), 1)

    # ---- discovery (mocked network) ----
    def test_discover_adds_found_leads_and_skips_known_ones(self):
        self.add("Known", "https://www.youtube.com/channel/UC1")
        body = {"items": [
            {"id": {"channelId": "UC1"}, "snippet": {"title": "Known", "description": "d"}},
            {"id": {"channelId": "UC2"}, "snippet": {"title": "New one", "description": "Logic puzzles daily"}},
            {"id": {}, "snippet": {"title": "broken"}},
        ]}
        get = mock.Mock(return_value=FakeResponse(200, body))
        with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "TEST-YT-KEY"}):
            added, skipped = outreach.discover("puzzle", 5, path=self.leads, today=D0, get=get)
        self.assertEqual([l["name"] for l in added], ["New one"])
        self.assertEqual(skipped, 1)
        self.assertEqual(added[0]["status"], "found")
        self.assertEqual(get.call_args.kwargs["params"]["type"], "channel")

    def test_discover_failure_adds_nothing_and_hides_the_key(self):
        get = mock.Mock(return_value=FakeResponse(403, text="quota"))
        with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "TEST-YT-KEY"}):
            with self.assertRaises(outreach.OutreachError) as ctx:
                outreach.discover("puzzle", path=self.leads, get=get)
        self.assertNotIn("TEST-YT-KEY", str(ctx.exception))
        self.assertEqual(outreach.read_leads(self.leads), [])

    def test_discover_without_a_key_stops_before_any_request(self):
        get = mock.Mock()
        with mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": ""}):
            with self.assertRaises(SystemExit):
                outreach.discover("puzzle", path=self.leads, get=get)
        get.assert_not_called()

    # ---- the promise in the module docstring ----
    def test_module_has_no_way_to_send_anything(self):
        source = inspect.getsource(outreach)
        for forbidden in ("requests.post", "smtplib", "sendmail", "/follow", "reply(", "comments.insert"):
            self.assertNotIn(forbidden, source)
        self.assertEqual(self.post.call_count, 0)

    def test_command_line_reports_errors_without_a_traceback(self):
        with mock.patch.object(outreach, "LEADS_FILE", self.leads):
            self.assertEqual(outreach.main(["mark", "L999", "skip"]), 2)
            self.assertEqual(outreach.main(["add", "N", "--platform", "x", "--url", "https://x.example/n"]), 0)
            self.assertEqual(outreach.main(["list"]), 0)


if __name__ == "__main__":
    unittest.main()
