"""Normalisation, validation, dedup keys, masking, encrypted state."""

import json
import os
from pathlib import Path
from unittest import mock

from outreach import core, store
from outreach.tests.support import NOW, OutreachTestCase

KEY = "k" * 32


class UrlAndEmailTests(OutreachTestCase):
    def test_canonical_urls_dedupe_formatting_differences(self):
        same = ["https://www.youtube.com/channel/UC" + "a" * 22, "http://youtube.com/channel/UC" + "a" * 22 + "/videos",
                "https://m.youtube.com/channel/UC" + "a" * 22 + "?si=xyz"]
        self.assertEqual(len({core.canonical_url(u) for u in same}), 1)
        self.assertEqual(core.canonical_url("https://WWW.Blog.com/Posts/?utm_source=x"), "blog.com/Posts")
        self.assertEqual(core.canonical_url("https://youtube.com/@Puzzle.Pal"), "youtube:handle:@puzzle.pal")

    def test_invalid_urls_are_refused(self):
        for url in ("", "ftp://site.com", "javascript:alert(1)", "https://localhost/x", "https://127.0.0.1/",
                    "https://example.com/x", "https://user:pw@site.com/", "not a url", "https://site.invalid/"):
            with self.subTest(url=url), self.assertRaises(core.OutreachError):
                core.validate_url(url)

    def test_fabricated_or_unusable_emails_are_refused(self):
        for value in ("", "name", "a@b", "x@example.com", "noreply@studio.com", "no-reply@studio.com",
                      "dmca@studio.com", "a..b@studio.com", "a@studio.invalid", "two@@studio.com"):
            with self.subTest(value=value), self.assertRaises(core.OutreachError):
                core.validate_email(value)
        self.assertEqual(core.validate_email(" <Mailto:Pal.Business@Gmail.com> "), "pal.business@gmail.com")

    def test_only_literally_written_addresses_are_found(self):
        text = "Contact: pal [at] gmail [dot] com or business@puzzlepal.tv, noreply@puzzlepal.tv"
        self.assertEqual([e for e, _ in core.find_emails(text)], ["business@puzzlepal.tv"])

    def test_masking_hides_addresses(self):
        self.assertEqual(core.mask_email("pal.business@gmail.com"), "pa***@gm***.com")
        self.assertNotIn("business", core.mask_emails_in("write to pal.business@gmail.com"))

    def test_prospect_ids_are_stable(self):
        key = core.canonical_url("https://youtube.com/channel/UC" + "a" * 22)
        self.assertEqual(core.prospect_id(key), core.prospect_id(key))
        self.assertRegex(core.prospect_id(key), r"^p-[0-9a-f]{10}$")


class StateTests(OutreachTestCase):
    def test_encrypted_round_trip_and_no_plaintext_on_disk(self):
        state = store.empty_state()
        state["prospects"]["p-1"] = {"secret": "pal.business@gmail.com"}
        with mock.patch.dict(os.environ, {store.KEY_ENV: KEY}):
            target = store.save(state, self.tmp)
            self.assertEqual(target.name, "state.json.enc")
            self.assertNotIn(b"pal.business", target.read_bytes())
            self.assertFalse((Path(self.tmp) / "state.json").exists())
            self.assertEqual(store.load(self.tmp)["prospects"]["p-1"]["secret"], "pal.business@gmail.com")

    def test_wrong_key_or_tampering_is_detected(self):
        with mock.patch.dict(os.environ, {store.KEY_ENV: KEY}):
            target = store.save(store.empty_state(), self.tmp)
        with mock.patch.dict(os.environ, {store.KEY_ENV: "z" * 32}), self.assertRaisesRegex(store.StateError, "authenticate"):
            store.load(self.tmp)
        wrapper = json.loads(target.read_text())
        wrapper["data"] = wrapper["data"][:-8] + "AAAAAAA="
        target.write_text(json.dumps(wrapper))
        with mock.patch.dict(os.environ, {store.KEY_ENV: KEY}), self.assertRaises(store.StateError):
            store.load(self.tmp)

    def test_actions_requires_encryption(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with self.assertRaisesRegex(store.StateError, "OUTREACH_STATE_KEY"):
                store.load(self.tmp)
            with self.assertRaisesRegex(store.StateError, "unencrypted"):
                store.save(store.empty_state(), self.tmp)
        (Path(self.tmp) / "state.json").write_text(json.dumps(store.empty_state()))
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", store.KEY_ENV: KEY}), \
                self.assertRaisesRegex(store.StateError, "plain"):
            store.load(self.tmp)

    def test_short_key_is_refused_without_showing_it(self):
        with mock.patch.dict(os.environ, {store.KEY_ENV: "short-secret"}):
            with self.assertRaises(store.StateError) as caught:
                store.save(store.empty_state(), self.tmp)
        self.assertNotIn("short-secret", str(caught.exception))

    def test_malformed_state_is_refused(self):
        (Path(self.tmp) / "state.json").write_text(json.dumps({"schema": "other"}))
        with self.assertRaises(store.StateError):
            store.load(self.tmp)

    def test_suppression_marks_prospects_and_supersedes_their_drafts(self):
        state = store.empty_state()
        state["prospects"]["p-1"] = {"id": "p-1", "canonical": "youtube:channel:x", "aliases": [], "status": core.QUALIFIED,
                                     "contact": {"email": "a@studio.com"}}
        state["drafts"]["od-00000001"] = {"draft_id": "od-00000001", "prospect_id": "p-1", "status": core.D_AWAITING}
        store.suppress(state, NOW, email="A@Studio.com", reason="asked", by="test")
        self.assertTrue(store.is_suppressed(state, email="a@studio.com"))
        self.assertEqual(state["prospects"]["p-1"]["status"], core.SUPPRESSED)
        self.assertEqual(state["drafts"]["od-00000001"]["status"], core.D_SUPERSEDED)
        with self.assertRaises(core.OutreachError):
            store.set_status(state["prospects"]["p-1"], core.QUALIFIED, NOW)
