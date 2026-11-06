"""Conversion sprint: drafts name a puzzle video (not an unrelated latest upload); drafts with an outdated demo
link are retired so the prospect is drafted again with the current one."""

from datetime import timedelta

from outreach import core, drafting, store
from outreach.tests.support import NOW, DEMO, OutreachTestCase

DEMO_OK = {"ok": True, "url": DEMO, "checked_at": core.iso(NOW)}


def prospect(evidence_title, coverage=()):
    return {"id": "p-1", "name": "Olexa", "category": "youtube", "status": core.QUALIFIED, "score": 90,
            "first_discovered": core.iso(NOW), "contact": {"email": "creator@example.com"},
            "evidence": {"url": "https://www.youtube.com/watch?v=latestupload", "title": evidence_title,
                         "date": "2026-10-24"},
            "research": {"similar_coverage": list(coverage)}}


COVERAGE = [{"title": "I Love Minimalistic Logic Puzzles Games", "url": "https://www.youtube.com/watch?v=0TsjVSwXDfs",
             "date": "2025-08-26", "game": "Cipher Zero"},
            {"title": "Old puzzle video", "url": "https://www.youtube.com/watch?v=oldpuzzleaa", "date": "2024-01-01"}]


class ReferenceTests(OutreachTestCase):
    def test_unrelated_latest_upload_is_replaced_by_newest_puzzle_coverage(self):
        ref = drafting.reference(prospect("I'm Organizing Orgies.", COVERAGE), self.config())
        self.assertEqual(ref["title"], "I Love Minimalistic Logic Puzzles Games")
        self.assertEqual(ref["date"], "2025-08-26")

    def test_puzzle_evidence_is_kept(self):
        p = prospect("Trying 5 indie puzzle game demos", COVERAGE)
        self.assertEqual(drafting.reference(p, self.config()), p["evidence"])

    def test_without_puzzle_coverage_the_evidence_stays(self):
        p = prospect("I'm Organizing Orgies.", [])
        self.assertEqual(drafting.reference(p, self.config()), p["evidence"])

    def test_composed_body_names_the_puzzle_video_and_passes_checks(self):
        config = self.config()
        p = prospect("I'm Organizing Orgies.", COVERAGE)
        channel, recipient, subject, body, require = drafting.compose(p, config, "Your channel covers logic puzzles.")
        self.assertIn("“I Love Minimalistic Logic Puzzles Games” (August 26, 2025)", body)
        self.assertNotIn("Orgies", body)


class StaleDemoLinkTests(OutreachTestCase):
    def test_waiting_draft_with_old_demo_link_is_retired_and_prospect_requeued(self):
        config = self.config()
        state = store.empty_state()
        p = prospect("Trying 5 indie puzzle game demos")
        p["status"] = core.DRAFTED
        state["prospects"][p["id"]] = p
        old = {"draft_id": "od-old", "prospect_id": p["id"], "kind": "initial", "status": core.D_AWAITING,
               "created_at": core.iso(NOW - timedelta(days=1)),
               "body": "Play: https://ultramegaray0-lgtm.github.io/ringshift-game/"}
        current = dict(old, draft_id="od-cur", prospect_id="p-2", body=f"Play: {DEMO}")
        state["drafts"] = {"od-old": old, "od-cur": current}
        expired = drafting.expire_drafts(state, config, NOW)
        self.assertEqual(expired, ["od-old"])
        self.assertEqual(old["status"], core.D_SUPERSEDED)
        self.assertIn("demo link changed", old["status_reason"])
        self.assertEqual(p["status"], core.QUALIFIED)
        self.assertEqual(current["status"], core.D_AWAITING)
