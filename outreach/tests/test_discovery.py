"""Discovery: evidence, verified contacts only, dedup across runs, suppression, quota/auth errors."""

from datetime import timedelta

from outreach import core, discovery, store, web
from outreach.tests.support import (CHANNEL_ID, CHANNEL_ID_2, NOW, VIDEO_ID_2, FakeWeb, OutreachTestCase, Response,
                                    channel_item, video_item, with_record, youtube_routes)

NO_SEEDS = {"feeds": [], "youtube_channels": [], "curators": []}


class YouTubeDiscoveryTests(OutreachTestCase):
    def run_discovery(self, state, routes, sources=None, key="TEST-YT-KEY", config=None):
        fake = FakeWeb(routes)
        result = discovery.run(state, config or self.config(), sources or NO_SEEDS, NOW,
                               fetcher=web.Fetcher(get=fake), youtube_key=key)
        return result, fake

    def test_channel_with_public_business_email_qualifies_with_evidence_and_source(self):
        state = store.empty_state()
        result, fake = self.run_discovery(state, youtube_routes())
        self.assertEqual(len(result.new), 1)
        prospect = state["prospects"][result.new[0]]
        self.assertEqual(prospect["status"], core.QUALIFIED)
        self.assertEqual(prospect["evidence"]["url"], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(prospect["evidence"]["title"], "Trying 5 indie puzzle game demos")
        self.assertTrue(prospect["evidence"]["date"].startswith("2026-10-22"))
        contact = prospect["contact"]
        self.assertEqual(contact["email"], "pal.business@gmail.com")
        self.assertEqual(contact["source_kind"], "youtube-description")
        self.assertEqual(contact["source_url"], f"https://www.youtube.com/channel/{CHANNEL_ID}")
        self.assertIn("pal.business@gmail.com", contact["excerpt"].lower())
        self.assertGreaterEqual(prospect["score"], 45)
        self.assertTrue(prospect["score_reasons"])
        self.assertIn("youtube:handle:@puzzlepal", prospect["aliases"])
        self.assertEqual(fake.api_calls("search")[0][1]["key"], "TEST-YT-KEY")

    def test_no_email_is_ever_guessed(self):
        state = store.empty_state()
        routes = youtube_routes(channels=[channel_item(description="Indie puzzle games! Contact via the About page.")])
        result, _ = self.run_discovery(state, routes)
        prospect = state["prospects"][result.new[0]]
        self.assertEqual(prospect["status"], core.DISCOVERED)
        self.assertEqual(prospect["contact"], {})
        self.assertIn("no public business email or submission route", prospect["not_qualified"])

    def test_obfuscated_or_placeholder_addresses_are_not_used(self):
        state = store.empty_state()
        routes = youtube_routes(channels=[channel_item(
            description="Puzzle games. Email: pal [at] gmail [dot] com. Template: you@example.com")])
        result, _ = self.run_discovery(state, routes)
        self.assertEqual(state["prospects"][result.new[0]]["contact"], {})

    def test_same_channel_found_again_is_not_duplicated(self):
        state = store.empty_state()
        self.run_discovery(state, youtube_routes())
        result, _ = self.run_discovery(state, youtube_routes(search_items=[video_item(video_id=VIDEO_ID_2, days_ago=1)]))
        self.assertEqual(len(state["prospects"]), 1)
        self.assertEqual(result.new, [])
        prospect = next(iter(state["prospects"].values()))
        self.assertEqual(prospect["evidence"]["url"], "https://www.youtube.com/watch?v=bcdefghijkl")   # newer evidence

    def test_same_contact_address_under_another_channel_is_a_duplicate(self):
        state = store.empty_state()
        self.run_discovery(state, youtube_routes())
        routes = youtube_routes(search_items=[video_item(channel_id=CHANNEL_ID_2)],
                                channels=[channel_item(channel_id=CHANNEL_ID_2, title="Pal Clips", custom="@palclips")])
        result, _ = self.run_discovery(state, routes)
        self.assertEqual(result.new, [])
        self.assertEqual(len(state["prospects"]), 1)

    def test_suppressed_contacts_are_never_re_added(self):
        state = store.empty_state()
        store.suppress(state, NOW, email="pal.business@gmail.com", reason="opted out", by="test")
        result, _ = self.run_discovery(state, youtube_routes())
        self.assertEqual(state["prospects"], {})
        self.assertEqual(result.rejected, 1)
        self.assertIn("suppression", state["rejected"][-1]["reason"])
        state2 = store.empty_state()
        store.suppress(state2, NOW, canonical=core.canonical_url(f"https://youtube.com/channel/{CHANNEL_ID}"),
                       reason="blocked", by="test")
        self.run_discovery(state2, youtube_routes())
        self.assertEqual(state2["prospects"], {})

    def test_off_topic_old_or_huge_channels_are_rejected_with_reasons(self):
        cases = {
            "off-topic": youtube_routes(search_items=[video_item(title="Minecraft puzzle map", description="")]),
            "old": youtube_routes(search_items=[video_item(days_ago=400)]),
            "huge": youtube_routes(channels=[channel_item(subscribers=5_000_000)]),
            "unrelated": youtube_routes(search_items=[video_item(title="My day vlog", description="coffee")]),
        }
        for name, routes in cases.items():
            with self.subTest(name):
                state = store.empty_state()
                result, _ = self.run_discovery(state, routes)
                self.assertEqual(state["prospects"], {})
                self.assertEqual(result.rejected, 1)
                self.assertTrue(state["rejected"][-1]["reason"])

    def test_quota_error_is_reported_not_fatal(self):
        state = store.empty_state()
        routes = youtube_routes(search_status=403, search_error={"errors": [{"reason": "quotaExceeded"}]})
        result, fake = self.run_discovery(state, routes)
        self.assertEqual(result.fatal, [])
        self.assertTrue(any("quota" in e for e in result.errors))
        self.assertEqual(len(fake.api_calls("search")), 1)            # stops asking after the first quota answer

    def test_bad_key_is_fatal_and_never_logged(self):
        state = store.empty_state()
        routes = youtube_routes(search_status=400, search_error={"errors": [{"reason": "keyInvalid"}],
                                                                 "status": "INVALID_ARGUMENT"})
        result, _ = self.run_discovery(state, routes, key="SECRET-KEY-VALUE")
        self.assertTrue(result.fatal)
        self.assertNotIn("SECRET-KEY-VALUE", " ".join(result.fatal + result.errors))

    def test_no_key_skips_youtube_without_network(self):
        state = store.empty_state()
        result, fake = self.run_discovery(state, {}, key=None)
        self.assertEqual(fake.calls, [])
        self.assertIn("skipped", result.sources["youtube"])


class SeedTests(OutreachTestCase):
    def run_seeds(self, state, sources, routes):
        fake = FakeWeb(routes)
        return discovery.run(state, self.config(), sources, NOW, fetcher=web.Fetcher(get=fake)), fake

    def test_curator_submission_route_is_checked_on_the_page(self):
        sources = {"feeds": [], "youtube_channels": [], "curators": [with_record({
            "name": "Puzzle Picks", "category": "curator", "site_url": "https://puzzlepicks.net/",
            "submission_url": "https://puzzlepicks.net/submit", "must_contain": "suggest a game"})]}
        page = Response(200, text="<title>Submit</title><p>Want to suggest a game? Use the form below.</p>")
        state = store.empty_state()
        result, _ = self.run_seeds(state, sources, {"https://puzzlepicks.net/submit": page})
        prospect = state["prospects"][result.new[0]]
        self.assertEqual(prospect["status"], core.QUALIFIED)
        self.assertEqual(prospect["contact"]["submission_url"], "https://puzzlepicks.net/submit")

        state = store.empty_state()
        changed = Response(200, text="<p>Submissions are closed.</p>")
        result, _ = self.run_seeds(state, sources, {"https://puzzlepicks.net/submit": changed})
        self.assertEqual(state["prospects"], {})
        self.assertTrue(any("no longer" in e for e in result.errors))

    def test_seed_email_must_be_written_on_its_source_page(self):
        seed = with_record({"name": "Puzzle Picks", "category": "curator", "site_url": "https://puzzlepicks.net/",
                            "contact_email": "press@puzzlepicks.net",
                            "contact_source_url": "https://puzzlepicks.net/contact"})
        sources = {"feeds": [], "youtube_channels": [], "curators": [seed]}
        state = store.empty_state()
        result, _ = self.run_seeds(state, sources, {"https://puzzlepicks.net/contact": Response(
            200, text="<p>Contact us: <a href='mailto:other@puzzlepicks.net'>email</a></p>")})
        prospect = next(iter(state["prospects"].values()))
        self.assertEqual(prospect["contact"], {})                  # the listed address is not on the page: not used
        self.assertTrue(any("not written" in e for e in result.errors))

        state = store.empty_state()
        self.run_seeds(state, sources, {"https://puzzlepicks.net/contact": Response(
            200, text="<p>Press: <a href='mailto:press@puzzlepicks.net'>press@puzzlepicks.net</a></p>")})
        prospect = next(iter(state["prospects"].values()))
        self.assertEqual(prospect["contact"]["email"], "press@puzzlepicks.net")
        self.assertEqual(prospect["contact"]["source_kind"], "web-page")

    def test_robots_txt_is_respected(self):
        sources = {"feeds": [], "youtube_channels": [], "curators": [with_record({
            "name": "Closed", "site_url": "https://closed.net/", "submission_url": "https://closed.net/submit"})]}
        state = store.empty_state()
        result, fake = self.run_seeds(state, sources, {
            "https://closed.net/robots.txt": Response(200, text="User-agent: *\nDisallow: /"),
            "https://closed.net/submit": Response(200, text="suggest a game")})
        self.assertEqual(state["prospects"], {})
        self.assertTrue(any("robots.txt" in e for e in result.errors))
        self.assertNotIn("https://closed.net/submit", [c[0] for c in fake.calls])

    def test_blog_feed_gives_dated_evidence_and_invalid_seed_urls_are_skipped(self):
        feed = (f"<?xml version='1.0'?><rss><channel><item><title>Five thinky puzzle games to try</title>"
                f"<link>https://puzzleblog.net/five-thinky</link><pubDate>{(NOW - timedelta(days=3)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
                f"<description>Logic puzzles galore</description></item>"
                f"<item><title>Cooking</title><link>https://puzzleblog.net/cook</link></item></channel></rss>")
        sources = {"youtube_channels": [], "curators": [], "feeds": [
            with_record({"name": "Puzzle Blog", "category": "blog", "site_url": "https://puzzleblog.net/",
                         "feed_url": "https://puzzleblog.net/feed"}, source_type="blog"),
            with_record({"name": "Broken", "site_url": "notaurl", "feed_url": "https://x.net/feed"},
                        source_type="blog")]}
        state = store.empty_state()
        result, _ = self.run_seeds(state, sources, {"https://puzzleblog.net/feed": Response(200, text=feed)})
        prospect = state["prospects"][result.new[0]]
        self.assertEqual(prospect["evidence"]["url"], "https://puzzleblog.net/five-thinky")
        self.assertEqual(prospect["status"], core.DISCOVERED)      # no contact route given
        self.assertTrue(any("Broken" in e for e in result.errors))

    def test_feed_with_entity_declarations_is_refused(self):
        with self.assertRaises(core.SourceError):
            discovery.parse_feed('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss/>')

    def test_evidence_without_a_date_cannot_qualify_a_creator(self):
        candidate = {"name": "X", "category": "youtube", "url": f"https://www.youtube.com/channel/{CHANNEL_ID}",
                     "canonical": f"youtube:channel:{CHANNEL_ID}", "evidence": {
                         "url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "indie puzzle game",
                         "date": None, "excerpt": "", "source": "test"},
                     "contact": {}, "channel_text": ""}
        state = store.empty_state()
        report = discovery.DiscoveryReport()
        self.assertIsNone(discovery.ingest(state, candidate, self.config(), NOW, report))
        self.assertIn("no date", state["rejected"][-1]["reason"])
