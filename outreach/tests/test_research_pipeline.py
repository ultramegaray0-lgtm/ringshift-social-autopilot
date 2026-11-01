"""Researched creators: re-checked through the YouTube Data API (never RSS or page scraping), scored on relevance,
activity, engagement and earlier coverage, personalised with a checked angle, deduplicated against search results,
and clearly marked when the API could not be asked. All offline."""

from datetime import timedelta

from outreach import checker, core, discovery, drafting, report, settings, store, web
from outreach.tests.support import (CHANNEL_ID, CHANNEL_ID_2, DEMO, NOW, FakeGemini, FakeWeb, OutreachTestCase,
                                    channel_item, video_item, with_record, youtube_routes)

DEMO_OK = {"ok": True, "url": DEMO, "checked_at": core.iso(NOW)}
NO_SEEDS = {"feeds": [], "youtube_channels": [], "curators": [], "excluded": []}
ANGLE = ("Your puzzle round-ups lean towards rule-based logic games, which is the space a ring-rotation beam puzzle "
         "sits in.")


def researched(**overrides):
    seed = with_record({
        "name": "Puzzle Pal", "handle": "@puzzlepal", "source_type": "youtube-creator", "last_checked": "2026-10-27",
        "fit": "Plays indie logic puzzle games every week.",
        "eligibility": {"status": "likely", "verified": ["Recent videos are indie puzzle games."],
                        "uncertain": ["No submission policy published."]},
        "evidence": {"url": "https://www.youtube.com/watch?v=zzzzzzzzzzz", "title": "Logic puzzle games to watch",
                     "date": "2026-10-10"},
        "similar_coverage": [{"title": "Laser puzzles in a box", "url": "https://www.youtube.com/watch?v=yyyyyyyyyyy",
                              "date": "2026-09-01", "game": "Laser Box"}],
        "angle": ANGLE})
    seed.pop("route_kind", None)
    seed.update(overrides)
    return seed


UPLOADS = {CHANNEL_ID: [("abcdefghijk", "Two new indie puzzle games", 5, "Puzzles! Business inquiries: pal.work@gmail.com"),
                        ("bcdefghijkl", "Logic puzzle roundup", 12, "Business inquiries: pal.work@gmail.com"),
                        ("cdefghijklm", "Cooking stream", 30, "")]}
VIEWS = {"abcdefghijk": (3000, 40), "bcdefghijkl": (2400, 31), "cdefghijklm": (1800, 12)}
QUIET_CHANNEL = channel_item(description="Indie puzzle games every week.")


class ResearchedChannelTests(OutreachTestCase):
    def run_discovery(self, state, seeds, routes, key="TEST", config=None):
        fake = FakeWeb(routes)
        found = discovery.run(state, config or self.config(), {**NO_SEEDS, "youtube_channels": seeds}, NOW,
                              fetcher=web.Fetcher(get=fake), youtube_key=key)
        return found, fake

    def test_handle_is_resolved_and_the_channel_is_rechecked_through_the_api(self):
        state = store.empty_state()
        found, fake = self.run_discovery(state, [researched()], youtube_routes(
            channels=[QUIET_CHANNEL], uploads=UPLOADS, views=VIEWS, search_items=[]))
        prospect = state["prospects"][found.new[0]]
        self.assertEqual(fake.api_calls("channels")[0][1].get("forHandle"), "@puzzlepal")
        self.assertEqual(prospect["canonical"], f"youtube:channel:{CHANNEL_ID}")
        self.assertIn("youtube:handle:@puzzlepal", prospect["aliases"])
        # newest relevant upload replaces the research evidence; descriptions are never kept as evidence
        self.assertEqual(prospect["evidence"]["url"], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertNotIn("text", prospect["evidence"])
        self.assertEqual(prospect["contact"]["email"], "pal.work@gmail.com")
        self.assertEqual(prospect["contact"]["source_kind"], "youtube-video-description")
        stats = prospect["stats"]
        self.assertEqual(stats["recent_uploads"], 3)
        self.assertEqual(stats["median_views"], 2400)
        self.assertAlmostEqual(stats["engagement_rate"], 0.2)
        reasons = " ".join(prospect["score_reasons"])
        self.assertIn("already covered similar puzzle games: Laser Box", reasons)
        self.assertIn("engaged audience", reasons)
        self.assertEqual(prospect["status"], core.QUALIFIED)
        self.assertEqual(prospect["research"]["verification"], "youtube-api")
        self.assertFalse([c for c in fake.calls if "googleapis" not in c[0] and "youtube.com" in c[0]],
                         "no YouTube page or feed is fetched")

    def test_engagement_ignores_videos_younger_than_three_days(self):
        routes = youtube_routes(channels=[QUIET_CHANNEL], views={"aaaaaaaaaaa": (99, 1), "bbbbbbbbbbb": (500, 5)},
                                uploads={CHANNEL_ID: [("aaaaaaaaaaa", "Indie puzzle", 1, ""),
                                                      ("bbbbbbbbbbb", "Indie puzzle", 6, "")]}, search_items=[])
        state = store.empty_state()
        _, fake = self.run_discovery(state, [researched()], routes)
        self.assertEqual(fake.api_calls("/videos")[0][1]["id"], "bbbbbbbbbbb")
        self.assertEqual(next(iter(state["prospects"].values()))["stats"]["median_views"], 500)

    def test_missing_channel_is_reported_not_invented(self):
        state = store.empty_state()
        found, _ = self.run_discovery(state, [researched(handle="@gone")], youtube_routes(
            channels=[QUIET_CHANNEL], search_items=[]))
        self.assertEqual(state["prospects"], {})
        self.assertTrue(any("not found through the YouTube API" in e for e in found.errors))

    def test_inactive_channel_is_not_qualified(self):
        routes = youtube_routes(channels=[QUIET_CHANNEL], search_items=[], uploads={CHANNEL_ID: [
            ("abcdefghijk", "Indie puzzle game", 200, "Business inquiries: pal.work@gmail.com"),
            ("bcdefghijkl", "Logic puzzle", 230, "Business inquiries: pal.work@gmail.com")]})
        state = store.empty_state()
        found, _ = self.run_discovery(state, [researched(evidence=None)], routes)
        self.assertEqual(state["prospects"], {})
        self.assertTrue(any("inactive" in r["reason"] or "days old" in r["reason"] for r in state["rejected"]))

    def test_seed_and_search_result_for_the_same_channel_are_one_prospect(self):
        routes = youtube_routes(channels=[QUIET_CHANNEL], uploads=UPLOADS, views=VIEWS,
                                search_items=[video_item(), video_item(video_id="bcdefghijkl")])
        state = store.empty_state()
        self.run_discovery(state, [researched(channel_id=CHANNEL_ID)], routes)
        self.assertEqual(len(state["prospects"]), 1)
        prospect = next(iter(state["prospects"].values()))
        self.assertEqual(prospect["research"]["angle"], ANGLE)          # the researched record wins
        self.assertTrue(prospect["source_meta"])

    def test_without_a_key_the_research_record_is_used_and_flagged(self):
        seed = researched(channel_id=CHANNEL_ID, contact_email="pal.work@gmail.com",
                          contact_source_url="https://www.youtube.com/watch?v=abcdefghijk",
                          contact_excerpt="Business inquiries: pal.work@gmail.com")
        state = store.empty_state()
        found, fake = self.run_discovery(state, [seed], {}, key=None)
        self.assertEqual(fake.calls, [])                                 # nothing fetched: YouTube can't be read
        prospect = state["prospects"][found.new[0]]
        self.assertEqual(prospect["evidence"]["url"], "https://www.youtube.com/watch?v=zzzzzzzzzzz")
        self.assertIn("research record", prospect["evidence"]["source"])
        self.assertEqual(prospect["contact"]["source_kind"], "research")
        self.assertEqual(prospect["research"]["verification"], "research-record")
        self.assertEqual(prospect["status"], core.QUALIFIED)
        self.assertEqual(found.sources["youtube_seeds_not_rechecked"], 1)

        config = self.config()
        result = drafting.make_drafts(state, config, NOW, demo=DEMO_OK)
        draft = state["drafts"][result["created"][0]]
        self.assertEqual(draft["blocking"], [])
        self.assertEqual(prospect["status"], core.NEEDS_REVIEW)
        joined = " ".join(draft["warnings"])
        self.assertIn("not re-checked this run", joined)
        self.assertIn("not re-checked through the YouTube API", joined)
        self.assertEqual(draft["personalization"], "researched angle (sources.json)")
        self.assertIn(ANGLE, draft["body"])

    def test_old_research_contacts_expire_without_the_api(self):
        seed = researched(channel_id=CHANNEL_ID, contact_email="pal.work@gmail.com", last_checked="2026-08-01",
                          contact_source_url="https://www.youtube.com/watch?v=abcdefghijk")
        state = store.empty_state()
        found, _ = self.run_discovery(state, [seed], {}, key=None)
        prospect = next(iter(state["prospects"].values()), None)
        self.assertTrue(prospect is None or prospect["contact"] == {})
        self.assertTrue(any("older than" in e for e in found.errors))

    def test_seed_without_evidence_and_no_key_is_skipped_with_a_reason(self):
        state = store.empty_state()
        found, _ = self.run_discovery(state, [researched(evidence=None, channel_id=CHANNEL_ID)], {}, key=None)
        self.assertEqual(state["prospects"], {})
        self.assertTrue(any("YOUTUBE_API_KEY" in e for e in found.errors))


class ResearchRecordRules(OutreachTestCase):
    def test_angle_must_pass_the_sentence_rules(self):
        self.assertEqual(settings.research_problems(researched()), [])
        for bad in ("I watched all your videos and loved them.", "It has 120 levels and a story mode.",
                    "Check https://example.org now."):
            with self.subTest(bad):
                self.assertTrue(settings.research_problems(researched(angle=bad)))

    def test_youtube_records_need_an_id_or_a_handle(self):
        sources = {**NO_SEEDS, "youtube_channels": [researched(handle="not a handle")]}
        self.assertTrue(settings.source_problems(sources, today=NOW.date()))
        sources = {**NO_SEEDS, "youtube_channels": [researched(), researched(name="Again")]}
        problems = settings.source_problems(sources, today=NOW.date())
        self.assertTrue(any("one organisation, one prospect" in p for ps in problems.values() for p in ps))

    def test_every_committed_angle_and_coverage_record_is_valid(self):
        for seed in settings.load_sources()["youtube_channels"]:
            with self.subTest(seed["name"]):
                self.assertEqual(settings.research_problems(seed), [])
                if seed.get("angle"):
                    self.assertEqual(checker.check_sentence(seed["angle"]), [])


class DraftQualityTests(OutreachTestCase):
    def qualified(self, **seed):
        state = store.empty_state()
        routes = youtube_routes(channels=[QUIET_CHANNEL], uploads=UPLOADS, views=VIEWS, search_items=[])
        discovery.run(state, self.config(), {**NO_SEEDS, "youtube_channels": [researched(**seed)]}, NOW,
                      fetcher=web.Fetcher(get=FakeWeb(routes)), youtube_key="TEST")
        return state, next(iter(state["prospects"].values()))

    def test_pitch_states_only_verified_mechanics_and_no_generic_openers(self):
        state, _ = self.qualified()
        draft = state["drafts"][drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK)["created"][0]]
        body = draft["body"]
        knowledge = settings.KNOWLEDGE_FILE.read_text(encoding="utf-8").replace("\\", "")
        for claim in ("through the centre", "outer rim", "own beam", "Prisms split a beam", "one source can light two locks",
                      "hand-author the Easy, Normal and Hard", "engine logic"):
            self.assertIn(claim, body)
        for phrase in ("straight through the centre", "never affects that ring's own beam", "splits a beam into two",
                       "one source can light two locks", "hand-author the Easy, Normal and Hard",
                       "check every level against the game's own engine logic"):
            self.assertIn(phrase, knowledge)                       # every claim above is backed by the knowledge file
        for generic in ("I hope this email finds you well", "I'm reaching out", "I came across your amazing",
                        "huge fan", "game-changer", "revolutionary", "Dear Sir"):
            self.assertNotIn(generic.lower(), body.lower())
        self.assertIn("“Two new indie puzzle games”", body)
        self.assertLessEqual(len(body), 1600)
        self.assertEqual(draft["blocking"], [])

    def test_gemini_is_not_asked_about_submission_pages(self):
        gemini = FakeGemini(["This should never be used for a form."])
        writer = drafting.GeminiWriter("KEY", ["m"], client_factory=lambda key: gemini)
        prospect = {"category": "curator", "evidence": {"title": "Submit a game"}, "research": {}}
        sentence, how, _ = drafting.personal_sentence(prospect, self.config(), writer)
        self.assertEqual(gemini.prompts, [])
        self.assertTrue(how.startswith("template"))
        self.assertIn("invites game suggestions", sentence)

    def test_coverage_template_mentions_the_game_they_covered(self):
        state, prospect = self.qualified(angle=None)
        sentence = drafting.template_sentence(prospect, self.config()["keywords"])
        self.assertIn("Laser Box", sentence)
        self.assertEqual(checker.check(f"Ringshift demo for {prospect['name']}", "x" * 10, demo_url=DEMO,
                                       channel="form", require_evidence=False)[:0], [])

    def test_non_english_channel_is_flagged(self):
        state, prospect = self.qualified(language="es")
        draft = state["drafts"][drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK)["created"][0]]
        self.assertTrue(any("'es'" in w for w in draft["warnings"]))
        self.assertEqual(prospect["status"], core.NEEDS_REVIEW)


class ReportTests(OutreachTestCase):
    def test_priority_table_explains_scores_and_masks_addresses(self):
        state = store.empty_state()
        routes = youtube_routes(channels=[QUIET_CHANNEL], uploads=UPLOADS, views=VIEWS, search_items=[])
        found = discovery.run(state, self.config(), {**NO_SEEDS, "youtube_channels": [researched()]}, NOW,
                              fetcher=web.Fetcher(get=FakeWeb(routes)), youtube_key="TEST")
        markdown, _ = report.build(state, NOW, discovery=found.as_dict(), min_score=45)
        self.assertIn("Prospects by priority (1 open: 1 high", markdown)
        self.assertIn("already covered similar puzzle games", markdown)
        self.assertNotIn("pal.work@gmail.com", markdown)

    def test_missing_youtube_key_is_called_out(self):
        state = store.empty_state()
        found = discovery.run(state, self.config(), NO_SEEDS, NOW, fetcher=web.Fetcher(get=FakeWeb({})))
        markdown, _ = report.build(state, NOW, discovery=found.as_dict())
        self.assertIn("YouTube discovery is off", markdown)


class GreetingTests(OutreachTestCase):
    def test_greeting_reads_naturally(self):
        self.assertEqual(drafting.greeting("Olexa"), "Hi Olexa,")
        self.assertEqual(drafting.greeting("Near Tragedy"), "Hi Near Tragedy,")
        self.assertEqual(drafting.greeting("I Dream of Indie Games"), "Hello,")


class ActiveButOffTopicLatelyTests(OutreachTestCase):
    def test_active_channel_with_old_research_evidence_uses_its_newest_upload(self):
        routes = youtube_routes(channels=[QUIET_CHANNEL], search_items=[], views=VIEWS, uploads={CHANNEL_ID: [
            ("abcdefghijk", "Cooking stream", 4, ""), ("bcdefghijkl", "Q&A", 12, "")]})
        state = store.empty_state()
        old = researched(evidence={"url": "https://www.youtube.com/watch?v=zzzzzzzzzzz", "title": "Logic puzzles",
                                   "date": "2025-01-10"})
        discovery.run(state, self.config(), {**NO_SEEDS, "youtube_channels": [old]}, NOW,
                      fetcher=web.Fetcher(get=FakeWeb(routes)), youtube_key="TEST")
        prospect = next(iter(state["prospects"].values()))
        self.assertEqual(prospect["evidence"]["url"], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertNotIn("evidence is not about puzzle or indie games", prospect["disqualifiers"])
