"""Drafts: honest, specific, verified link, no promises; Gemini guarded; follow-ups only queued."""

from datetime import timedelta

from outreach import approval, checker, core, discovery, drafting, store, web
from outreach.tests.support import NOW, DEMO, FakeGemini, FakeWeb, OutreachTestCase, youtube_routes

DEMO_OK = {"ok": True, "url": DEMO, "checked_at": core.iso(NOW)}


class DraftTestCase(OutreachTestCase):
    def qualified_state(self, **channel):
        state = store.empty_state()
        routes = youtube_routes(**channel)
        discovery.run(state, self.config(), {"feeds": [], "youtube_channels": [], "curators": []}, NOW,
                      fetcher=web.Fetcher(get=FakeWeb(routes)), youtube_key="TEST")
        prospect = next(iter(state["prospects"].values()))
        self.assertEqual(prospect["status"], core.QUALIFIED)
        return state, prospect


class DraftContentTests(DraftTestCase):
    def test_template_draft_is_specific_honest_and_checked(self):
        state, prospect = self.qualified_state()
        result = drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK)
        draft = state["drafts"][result["created"][0]]
        body = draft["body"]
        self.assertEqual(draft["recipient"], "pal.business@gmail.com")
        self.assertEqual(draft["blocking"], [])
        self.assertIn("“Trying 5 indie puzzle game demos” (October 22, 2026)", body)
        self.assertEqual(body.count(DEMO), 1)
        self.assertIn("indie puzzle game", body)
        self.assertIn('reply "no thanks"', body)
        self.assertIn("No reply is needed", body)
        self.assertIn("Test Sender", body)
        self.assertIn("Ringshift", draft["subject"])
        self.assertFalse(draft["subject"].lower().startswith(("re:", "fwd:")))
        for forbidden in ("watched", "loved", "big fan", "key", "paid", "exclusive", "early access", "review copy"):
            self.assertNotIn(forbidden, body.lower())
        self.assertEqual(draft["approval_code"], approval.code_for(draft))
        self.assertEqual(draft["status"], core.D_AWAITING)
        self.assertEqual(prospect["status"], core.DRAFTED)
        self.assertEqual(draft["evidence"]["url"], "https://www.youtube.com/watch?v=abcdefghijk")

    def test_game_description_only_uses_knowledge_file_mechanics(self):
        from outreach import settings
        config = self.config()
        self.assertEqual(settings.check_game_facts(config), [])
        config["game"]["mechanics_line"] = "Puzzles use mirrors and teleporting lasers and gravity wells."
        config["game"]["pitch"] = "Includes lenses."
        self.assertEqual(settings.check_game_facts(config), [])      # unknown words are not mechanics we track...
        config["game"]["mechanics_line"] = "Puzzles use mirrors and magnets."
        settings_words = dict(settings.MECHANIC_WORDS, magnets="Magnets")
        original = settings.MECHANIC_WORDS
        try:
            settings.MECHANIC_WORDS = settings_words
            self.assertTrue(settings.check_game_facts(config))        # ...but a tracked one missing from the file fails
        finally:
            settings.MECHANIC_WORDS = original

    def test_no_drafts_without_verified_demo_or_sign_off(self):
        state, _ = self.qualified_state()
        result = drafting.make_drafts(state, self.config(), NOW, demo={"ok": False, "reason": "HTTP 404"})
        self.assertEqual(result["created"], [])
        self.assertIn("demo", result["notes"][0])
        config = self.config()
        config["sender"]["name"] = ""
        result = drafting.make_drafts(state, config, NOW, demo=DEMO_OK)
        self.assertEqual(result["created"], [])
        self.assertIn("OUTREACH_SENDER_NAME", result["notes"][0])

    def test_daily_draft_limit(self):
        state, prospect = self.qualified_state()
        for index in range(7):
            clone = dict(prospect, id=f"p-{index:010d}", canonical=f"site{index}.com", aliases=[],
                         contact=dict(prospect["contact"], email=f"biz{index}@studio{index}.com"))
            state["prospects"][clone["id"]] = clone
        result = drafting.make_drafts(state, self.config(max_new_drafts_per_run=3), NOW, demo=DEMO_OK)
        self.assertEqual(len(result["created"]), 3)

    def test_submission_route_gets_paste_text_held_for_review(self):
        state, prospect = self.qualified_state()
        prospect["contact"] = {"submission_url": "https://puzzlepicks.net/submit", "verified_at": core.iso(NOW)}
        prospect["category"] = "curator"
        draft = state["drafts"][drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK)["created"][0]]
        self.assertEqual(draft["channel"], "form")
        self.assertEqual(draft["blocking"], [])
        self.assertEqual(prospect["status"], core.NEEDS_REVIEW)

    def test_drafts_expire_and_the_prospect_is_rechecked(self):
        state, prospect = self.qualified_state()
        drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK)
        later = NOW + timedelta(days=20)
        expired = drafting.expire_drafts(state, self.config(), later)
        self.assertEqual(len(expired), 1)
        self.assertEqual(prospect["status"], core.QUALIFIED)


class GeminiTests(DraftTestCase):
    def writer(self, answers):
        client = FakeGemini(answers)
        return drafting.GeminiWriter("TEST-GEMINI", ["m1", "m2"], client_factory=lambda key: client), client

    def test_good_sentence_is_used_and_prompt_has_no_personal_data(self):
        state, prospect = self.qualified_state()
        writer, client = self.writer(["People who enjoy trying odd indie puzzle demos may like a game about "
                                      "steering beams through rotating rings."])
        draft = state["drafts"][drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK, writer=writer)["created"][0]]
        self.assertEqual(draft["personalization"], "gemini:m1")
        self.assertIn("steering beams", draft["body"])
        prompt = client.prompts[0]
        self.assertNotIn("pal.business", prompt)
        self.assertNotIn("Puzzle Pal", prompt)
        self.assertNotIn("http", prompt)

    def test_bad_sentences_fall_back_to_the_template(self):
        bad = ["I watched your video and loved it, your channel is amazing.",
               "We can send you a free key for your review of this puzzle game.",
               "It has 200 levels and a level editor so your viewers will love it.",
               "Check it out at https://evil.example now because it is great."]
        for answer in bad:
            with self.subTest(answer=answer):
                state, _ = self.qualified_state()
                writer, _ = self.writer([answer, answer])
                draft = state["drafts"][drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK,
                                                             writer=writer)["created"][0]]
                self.assertTrue(draft["personalization"].startswith("template"))
                self.assertEqual(draft["blocking"], [])

    def test_quota_and_auth_errors_disable_gemini_for_the_run(self):
        for error, word in ((RuntimeError("429 RESOURCE_EXHAUSTED"), "quota"),
                            (RuntimeError("403 PERMISSION_DENIED: API key not valid"), "key")):
            with self.subTest(word=word):
                state, _ = self.qualified_state()
                writer, client = self.writer([error])
                result = drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK, writer=writer)
                self.assertEqual(len(result["created"]), 1)
                self.assertIn(word, writer.disabled)
                self.assertEqual(len(client.prompts), 1)


class CheckerTests(OutreachTestCase):
    def body(self, extra=""):
        return (f"Hi Pal,\n\nI'm making Ringshift, an indie puzzle game. I found your channel through your video "
                f"“X” (October 1, 2026).\n\n{extra}Rotate rings so beams reach locks.\n\n{DEMO}\n\n"
                f"No reply is needed if it's not for you.\n\nThanks,\nTest Sender\n\n--\nIf you'd prefer not to "
                f"hear from me, reply \"no thanks\" and I won't contact you again." + " " * 20)

    def check(self, subject="Ringshift: an indie puzzle game demo for Pal", body=None):
        return checker.check(subject, body or self.body(), demo_url=DEMO, channel="email", evidence_title="X")

    def test_clean_message_passes(self):
        self.assertEqual(self.check(), [])

    def test_violations_are_blocking(self):
        cases = {
            "subject reply": dict(subject="Re: Ringshift demo for you"),
            "subject no name": dict(subject="A quick question for you"),
            "promise": dict(body=self.body("I can send you a Steam key and pay for the video. ")),
            "familiarity": dict(body=self.body("I'm a big fan and I watched all your videos. ")),
            "invented": dict(body=self.body("It already has 10,000 players and won an award. ")),
            "extra link": dict(body=self.body("See https://other.site/x too. ")),
            "email in body": dict(body=self.body("Write to me at someone@studio.com. ")),
            "placeholder": dict(body=self.body("[ADD LINK] ")),
            "no evidence": dict(body=self.body().replace("“X”", "a video")),
            "no opt-out": dict(body=self.body().replace('reply "no thanks"', "write back")),
            "no demo": dict(body=self.body().replace(DEMO, "the demo")),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                self.assertTrue(self.check(**kwargs))


class FollowUpTests(DraftTestCase):
    def sent_state(self, days_ago):
        state, prospect = self.qualified_state()
        draft = state["drafts"][drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK)["created"][0]]
        store.mark_sent(state, draft, NOW - timedelta(days=days_ago), "test", "test", "smtp",
                        sent_at=NOW - timedelta(days=days_ago))
        return state, prospect, draft

    def test_one_follow_up_is_queued_for_approval_never_sent(self):
        state, prospect, original = self.sent_state(15)
        result = drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK)
        self.assertEqual(len(result["followups"]), 1)
        followup = state["drafts"][result["followups"][0]]
        self.assertEqual(followup["status"], core.D_AWAITING)
        self.assertEqual(followup["kind"], "followup")
        self.assertEqual(followup["recipient"], original["recipient"])
        self.assertEqual(followup["blocking"], [])
        self.assertTrue(followup["warnings"])
        self.assertEqual(drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK)["followups"], [])
        followup["status"] = core.D_REJECTED
        self.assertEqual(drafting.make_drafts(state, self.config(), NOW + timedelta(days=30),
                                              demo=DEMO_OK)["followups"], [])

    def test_no_follow_up_too_early_or_after_a_reply(self):
        state, prospect, _ = self.sent_state(5)
        self.assertEqual(drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK)["followups"], [])
        state, prospect, _ = self.sent_state(20)
        store.set_status(prospect, core.REPLIED, NOW)
        self.assertEqual(drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK)["followups"], [])
