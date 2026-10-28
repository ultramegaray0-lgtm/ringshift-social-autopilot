"""Expanded discovery: the committed source records, held/excluded sources, directories, organisation aliases, YouTube
query/quota handling, and business contacts read from a creator's own video descriptions. All offline."""

from datetime import date, timedelta
from urllib.parse import urlsplit

from outreach import core, discovery, drafting, report, settings, store, web
from outreach.tests.support import (CHANNEL_ID, CHANNEL_ID_2, DEMO, NOW, FakeWeb, OutreachTestCase, Response,
                                    channel_item, video_item, with_record, youtube_routes)

DEMO_OK = {"ok": True, "url": DEMO, "checked_at": core.iso(NOW)}
NO_SEEDS = {"feeds": [], "youtube_channels": [], "curators": [], "excluded": []}
TODAY = NOW.date()


def youtube_feed(channel_id, videos):
    """A YouTube channel Atom feed. videos: [(video_id, title, days_ago, description)]."""
    entries = "".join(
        f"<entry><yt:videoId>{vid}</yt:videoId><title>{title}</title>"
        f"<link rel='alternate' href='https://www.youtube.com/watch?v={vid}'/>"
        f"<published>{core.iso(NOW - timedelta(days=days))}</published>"
        f"<media:group><media:title>{title}</media:title><media:description>{text}</media:description></media:group>"
        f"</entry>" for vid, title, days, text in videos)
    return ("<?xml version='1.0' encoding='UTF-8'?><feed xmlns='http://www.w3.org/2005/Atom' "
            "xmlns:yt='http://www.youtube.com/xml/schemas/2015' xmlns:media='http://search.yahoo.com/mrss/'>"
            f"<title>Channel</title>{entries}</feed>")


def feed_url(channel_id):
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


NO_EMAIL_CHANNEL = channel_item(description="Indie puzzle games every week. Contact via the About page.")
LABELLED = "Weekly thinky puzzle games. Business inquiries: pal.work@gmail.com"


class CommittedSourcesTests(OutreachTestCase):
    """The real outreach/sources.json: complete, honest records, and nothing ineligible switched on."""

    def setUp(self):
        super().setUp()
        self.sources = settings.load_sources()

    def seeds(self):
        for kind in ("feeds", "youtube_channels", "curators"):
            for seed in self.sources[kind]:
                yield kind, seed

    def test_every_record_is_complete_and_consistent(self):
        self.assertEqual(settings.source_problems(self.sources, today=TODAY), {})

    def test_records_carry_type_date_eligibility_and_fit(self):
        for kind, seed in self.seeds():
            with self.subTest(seed=seed["name"]):
                self.assertIn(seed["source_type"], settings.SOURCE_TYPES)
                checked = date.fromisoformat(seed["last_checked"])
                self.assertLessEqual(checked, TODAY + timedelta(days=1))
                self.assertTrue(seed["fit"].strip())
                self.assertTrue(seed["eligibility"]["verified"])
                if seed["eligibility"]["status"] != "meets":
                    self.assertTrue(seed["eligibility"]["uncertain"], "uncertain points must be written down")

    def test_only_meets_or_likely_sources_are_enabled(self):
        for kind, seed in self.seeds():
            if seed["enabled"]:
                self.assertIn(seed["eligibility"]["status"], ("meets", "likely"), seed["name"])

    def test_games_yall_and_playlin_are_never_targets(self):
        targets = " ".join(str(seed).lower() for _, seed in self.seeds())
        for marker in ("gamesyall", "games y'all", "moha.wiki", "playlin"):
            self.assertNotIn(marker, targets)
        excluded = {e["name"] for e in self.sources["excluded"]}
        self.assertTrue({"Games Y'all", "Playlin"} <= excluded)
        for entry in self.sources["excluded"]:
            self.assertTrue(entry["reason"].strip())
            self.assertIsNone(core.EMAIL_RE.search(str(entry)), "no addresses are kept for excluded sources")

    def test_every_contact_route_is_public_and_written_down(self):
        for kind, seed in self.seeds():
            with self.subTest(seed=seed["name"]):
                if seed.get("contact_email"):
                    core.validate_email(seed["contact_email"])
                    self.assertTrue(seed["contact_source_url"].startswith("https://"))
                if kind == "curators":
                    if seed["route_kind"] == "email":
                        self.assertTrue(seed.get("contact_email"))
                    else:
                        self.assertTrue(seed.get("submission_url"))
                    if seed.get("submission_url"):
                        self.assertTrue(seed["must_contain"].strip())

    def test_one_organisation_one_prospect(self):
        keys = []
        for kind, seed in self.seeds():
            keys += settings._seed_keys(kind, seed)
        self.assertEqual(len(keys), len(set(keys)))

    def test_held_source_is_overlooked_until_a_store_page_exists(self):
        held = [seed["name"] for _, seed in self.seeds() if not seed["enabled"]]
        self.assertEqual(held, ["Overlooked.gg"])


class SourceRecordValidationTests(OutreachTestCase):
    def curator(self, **overrides):
        return with_record({"name": "Puzzle Picks", "category": "curator", "site_url": "https://puzzlepicks.net/",
                            "submission_url": "https://puzzlepicks.net/submit"}, **overrides)

    def problems(self, *curators, excluded=()):
        return settings.source_problems({**NO_SEEDS, "curators": list(curators), "excluded": list(excluded)},
                                        today=TODAY)

    def test_a_complete_record_passes(self):
        self.assertEqual(self.problems(self.curator()), {})

    def test_incomplete_or_contradictory_records_are_caught(self):
        cases = {
            "enabled while uncertain": self.curator(eligibility={"status": "uncertain", "verified": ["x"],
                                                                 "uncertain": ["y"]}),
            "not-eligible kept as a source": self.curator(enabled=False, eligibility={
                "status": "not-eligible", "verified": ["x"], "uncertain": []}),
            "nothing verified": self.curator(eligibility={"status": "meets", "verified": [], "uncertain": []}),
            "likely without saying what is unconfirmed": self.curator(eligibility={
                "status": "likely", "verified": ["x"], "uncertain": []}),
            "no fit": self.curator(fit=""),
            "no date": self.curator(last_checked="last week"),
            "future date": self.curator(last_checked=(TODAY + timedelta(days=30)).isoformat()),
            "unknown type": self.curator(source_type="influencer"),
            "address without its page": self.curator(contact_email="press@puzzlepicks.net"),
            "placeholder address": self.curator(contact_email="you@example.com",
                                                contact_source_url="https://puzzlepicks.net/contact"),
            "no route kind": self.curator(route_kind="dm"),
            "route not proven open": self.curator(must_contain=""),
        }
        for name, seed in cases.items():
            with self.subTest(name):
                self.assertTrue(self.problems(seed), name)

    def test_same_organisation_twice_is_caught(self):
        other = self.curator(name="Puzzle Picks YouTube", site_url="https://other.net/",
                             aliases=["https://puzzlepicks.net/"])
        problems = self.problems(self.curator(), other)
        self.assertTrue(any("one organisation" in p for ps in problems.values() for p in ps))

    def test_excluded_source_cannot_also_be_a_target(self):
        excluded = [{"name": "Puzzle Picks", "url": "https://puzzlepicks.net/", "last_checked": "2026-10-28",
                     "reason": "not eligible"}]
        problems = self.problems(self.curator(), excluded=excluded)
        self.assertTrue(any("also listed" in p for ps in problems.values() for p in ps))

    def test_stale_records_are_reported(self):
        old = self.curator(last_checked=(TODAY - timedelta(days=200)).isoformat())
        self.assertEqual(settings.stale_sources({**NO_SEEDS, "curators": [old]}, today=TODAY),
                         ["curators 'Puzzle Picks'"])


class SeedDiscoveryTests(OutreachTestCase):
    def run_discovery(self, state, sources, routes, key=None, config=None):
        fake = FakeWeb(routes)
        found = discovery.run(state, config or self.config(), {**NO_SEEDS, **sources}, NOW,
                              fetcher=web.Fetcher(get=fake), youtube_key=key)
        return found, fake

    def test_held_source_is_never_fetched_or_drafted(self):
        held = with_record({"name": "Overlooked", "site_url": "https://overlooked.net/",
                            "submission_url": "https://overlooked.net/request"}, enabled=False,
                           eligibility={"status": "uncertain", "verified": ["Submissions are open."],
                                        "uncertain": ["They only review games they buy."]})
        state = store.empty_state()
        found, fake = self.run_discovery(state, {"curators": [held]}, {})
        self.assertEqual(state["prospects"], {})
        self.assertFalse([c for c in fake.calls if "overlooked.net" in c[0]])
        self.assertTrue(any("Overlooked" in h and "uncertain" in h for h in found.held))
        markdown, _ = report.build(state, NOW, discovery=found.as_dict())
        self.assertIn("Sources on hold (1)", markdown)

    def test_invalid_record_is_skipped_with_a_reason_and_not_fetched(self):
        broken = with_record({"name": "Half Done", "site_url": "https://halfdone.net/",
                              "submission_url": "https://halfdone.net/submit"}, fit="")
        state = store.empty_state()
        found, fake = self.run_discovery(state, {"curators": [broken]}, {})
        self.assertEqual(state["prospects"], {})
        self.assertFalse([c for c in fake.calls if "halfdone.net" in c[0]])
        self.assertTrue(any("Half Done" in e and "fit" in e for e in found.errors))

    def test_directory_upload_route_qualifies_and_its_draft_needs_review(self):
        directory = with_record({"name": "Web Arcade", "category": "directory", "site_url": "https://webarcade.net/",
                                 "submission_url": "https://webarcade.net/developers", "must_contain": "basic launch"},
                                source_type="browser-game-directory", route_kind="upload",
                                eligibility={"status": "likely", "verified": ["Basic launch needs no SDK."],
                                             "uncertain": ["Acceptance depends on their QA review."]})
        page = Response(200, text="<title>Developers</title><p>Basic Launch: publish your web game.</p>")
        state = store.empty_state()
        found, _ = self.run_discovery(state, {"curators": [directory]}, {"https://webarcade.net/developers": page})
        prospect = state["prospects"][found.new[0]]
        self.assertEqual(prospect["status"], core.QUALIFIED)
        self.assertEqual(prospect["contact"]["route_kind"], "upload")
        self.assertEqual(prospect["source_meta"]["status"], "likely")
        self.assertIn("open submission route", " ".join(prospect["score_reasons"]))

        made = drafting.make_drafts(state, self.config(), NOW, demo=DEMO_OK)
        draft = state["drafts"][made["created"][0]]
        self.assertEqual(draft["channel"], "form")
        self.assertEqual(draft["blocking"], [])
        warnings = " ".join(draft["warnings"])
        self.assertIn("developer-portal upload", warnings)
        self.assertIn("QA review", warnings)                         # what is unconfirmed is shown to you
        self.assertIn("publishes browser games", draft["body"])
        self.assertEqual(prospect["status"], core.NEEDS_REVIEW)

    def test_email_route_never_falls_back_to_a_form_when_the_address_is_gone(self):
        seed = with_record({"name": "Beta Picks", "site_url": "https://betapicks.net/",
                            "submission_url": "https://betapicks.net/contact",
                            "contact_email": "admin@betapicks.net",
                            "contact_source_url": "https://betapicks.net/contact", "must_contain": "in development"},
                           route_kind="email")
        page = Response(200, text="<p>Games must be in development. Write to us via Discord.</p>")
        state = store.empty_state()
        found, _ = self.run_discovery(state, {"curators": [seed]}, {"https://betapicks.net/contact": page})
        prospect = state["prospects"][found.new[0]]
        self.assertEqual(prospect["contact"], {})
        self.assertEqual(prospect["status"], core.DISCOVERED)
        self.assertTrue(any("not written" in e for e in found.errors))

        state = store.empty_state()
        page = Response(200, text="<p>Games must be in development. Email: Admin@betapicks.net</p>")
        found, _ = self.run_discovery(state, {"curators": [seed]}, {"https://betapicks.net/contact": page})
        prospect = state["prospects"][found.new[0]]
        self.assertEqual(prospect["contact"]["email"], "admin@betapicks.net")
        self.assertEqual(prospect["status"], core.QUALIFIED)

    def test_organisation_alias_stops_a_second_prospect_for_its_youtube_channel(self):
        curator = with_record({"name": "Puzzle Picks", "site_url": "https://puzzlepicks.net/",
                               "submission_url": "https://puzzlepicks.net/submit",
                               "aliases": [f"https://www.youtube.com/channel/{CHANNEL_ID}"]})
        routes = {**youtube_routes(), "https://puzzlepicks.net/submit": Response(200, text="suggest a game")}
        state = store.empty_state()
        found, _ = self.run_discovery(state, {"curators": [curator]}, routes, key="TEST")
        self.assertEqual(len(state["prospects"]), 1)
        prospect = next(iter(state["prospects"].values()))
        self.assertEqual(prospect["canonical"], "puzzlepicks.net")
        # The search result found the channel's description email, but your record and its route are kept.
        self.assertEqual(prospect["contact"]["submission_url"], "https://puzzlepicks.net/submit")
        self.assertNotIn("email", prospect["contact"])
        self.assertEqual(prospect["evidence"]["url"], "https://puzzlepicks.net/submit")
        self.assertIn("youtube:handle:@puzzlepal", prospect["aliases"])
        found, _ = self.run_discovery(state, {"curators": [curator]}, routes, key="TEST")   # next day: still one
        self.assertEqual(len(state["prospects"]), 1)
        self.assertEqual(found.new, [])

    def test_seeded_youtube_contact_must_be_labelled_in_the_channels_own_videos(self):
        seed = with_record({"name": "Puzzle Pal", "channel_id": CHANNEL_ID, "contact_email": "pal.work@gmail.com",
                            "contact_source_url": "https://www.youtube.com/watch?v=abcdefghijk"},
                           source_type="youtube-creator")
        good = youtube_feed(CHANNEL_ID, [("abcdefghijk", "Indie puzzle game demos", 3, LABELLED)])
        state = store.empty_state()
        found, fake = self.run_discovery(state, {"youtube_channels": [seed]},
                                         {feed_url(CHANNEL_ID): Response(200, text=good)})
        prospect = state["prospects"][found.new[0]]
        self.assertEqual(prospect["contact"]["email"], "pal.work@gmail.com")
        self.assertEqual(prospect["contact"]["source_kind"], "youtube-video-description")
        self.assertEqual(prospect["contact"]["source_url"], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(prospect["status"], core.QUALIFIED)
        self.assertFalse([c for c in fake.calls if "/watch" in c[0]], "YouTube pages are never scraped")

        unlabelled = youtube_feed(CHANNEL_ID, [("abcdefghijk", "Indie puzzle game demos", 3,
                                                "Game developer: pal.work@gmail.com")])
        state = store.empty_state()
        found, _ = self.run_discovery(state, {"youtube_channels": [seed]},
                                      {feed_url(CHANNEL_ID): Response(200, text=unlabelled)})
        self.assertEqual(next(iter(state["prospects"].values()))["contact"], {})
        self.assertTrue(any("not labelled" in e for e in found.errors))


class YouTubeExpansionTests(OutreachTestCase):
    def run_discovery(self, state, routes, config=None):
        fake = FakeWeb(routes)
        found = discovery.run(state, config or self.config(), NO_SEEDS, NOW, fetcher=web.Fetcher(get=fake),
                              youtube_key="TEST")
        return found, fake

    def test_queries_cover_puzzle_terms_with_newest_first_ordering(self):
        config = self.config()
        queries = discovery._queries(config["youtube"])
        terms = " ".join(q for q, _ in queries)
        for term in ("logic puzzle", "spatial reasoning", "brain teaser", "thinky", "indie puzzle", "browser puzzle"):
            self.assertIn(term, terms)
        self.assertIn("date", {order for _, order in queries})
        state = store.empty_state()
        found, fake = self.run_discovery(state, youtube_routes(), config)
        searches = fake.api_calls("search")
        self.assertEqual(len(searches), len(queries))
        self.assertEqual({c[1]["order"] for c in searches}, {"date", "relevance"})
        self.assertEqual(found.sources["youtube_quota_units"], 100 * len(queries) + 1)
        self.assertEqual(len(state["prospects"]), 1)                 # one channel found by every query: one prospect

    def test_searches_stop_at_the_quota_budget(self):
        config = self.config()
        config["youtube"]["daily_quota_budget"] = 250
        state = store.empty_state()
        found, fake = self.run_discovery(state, youtube_routes(), config)
        self.assertEqual(len(fake.api_calls("search")), 2)
        self.assertIn("quota budget", found.sources["youtube_quota_note"])

    def test_business_contact_from_several_of_the_creators_own_video_descriptions(self):
        feed = youtube_feed(CHANNEL_ID, [("abcdefghijk", "Indie puzzle game demos", 6, LABELLED),
                                         ("bcdefghijkl", "Logic puzzle roundup", 20, LABELLED),
                                         ("cdefghijklm", "Spatial puzzle game", 40, "No links today")])
        routes = {**youtube_routes(channels=[NO_EMAIL_CHANNEL]), feed_url(CHANNEL_ID): Response(200, text=feed)}
        state = store.empty_state()
        found, _ = self.run_discovery(state, routes)
        prospect = state["prospects"][found.new[0]]
        contact = prospect["contact"]
        self.assertEqual(contact["email"], "pal.work@gmail.com")
        self.assertEqual(contact["source_kind"], "youtube-video-description")
        self.assertEqual(contact["source_url"], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertIn("2 of the latest 3", contact["verified_by"])
        self.assertEqual(prospect["status"], core.QUALIFIED)
        self.assertEqual(prospect["stats"]["recent_uploads"], 3)
        self.assertIn("uploads regularly", " ".join(prospect["score_reasons"]))
        self.assertEqual(found.sources["youtube_contacts_from_video_descriptions"], 1)

    def test_one_off_unlabelled_obfuscated_or_third_party_addresses_are_not_used(self):
        cases = {
            "only one video": [("abcdefghijk", "Indie puzzle", 6, LABELLED), ("bcdefghijkl", "Puzzle", 9, "hi")],
            "a game's developer": [(v, "Indie puzzle", d, "Developer contact: studio@puzzledev.com")
                                   for v, d in (("abcdefghijk", 6), ("bcdefghijkl", 9))],
            "publisher's press desk": [(v, "Indie puzzle", d, "Publisher's press contact: pr@bigpub.com")
                                       for v, d in (("abcdefghijk", 6), ("bcdefghijkl", 9))],
            "no label": [(v, "Indie puzzle", d, "pal.work@gmail.com") for v, d in (("abcdefghijk", 6),
                                                                                 ("bcdefghijkl", 9))],
            "obfuscated": [(v, "Indie puzzle", d, "Business: pal [at] gmail [dot] com")
                           for v, d in (("abcdefghijk", 6), ("bcdefghijkl", 9))],
        }
        for name, videos in cases.items():
            with self.subTest(name):
                routes = {**youtube_routes(channels=[NO_EMAIL_CHANNEL]),
                          feed_url(CHANNEL_ID): Response(200, text=youtube_feed(CHANNEL_ID, videos))}
                state = store.empty_state()
                found, _ = self.run_discovery(state, routes)
                prospect = state["prospects"][found.new[0]]
                self.assertEqual(prospect["contact"], {})
                self.assertEqual(prospect["status"], core.DISCOVERED)

    def test_description_email_wins_and_feed_failures_are_harmless(self):
        routes = {**youtube_routes(), feed_url(CHANNEL_ID): Response(500, text="oops")}
        state = store.empty_state()
        found, _ = self.run_discovery(state, routes)
        prospect = state["prospects"][found.new[0]]
        self.assertEqual(prospect["contact"]["source_kind"], "youtube-description")
        self.assertEqual(found.fatal, [])

    def test_feed_checks_are_bounded_and_prefer_small_relevant_channels(self):
        config = self.config()
        config["youtube"]["feed_checks_per_run"] = 1
        big = channel_item(channel_id=CHANNEL_ID_2, title="Big Puzzles", subscribers=400_000, custom="@bigpuzzles",
                           description="Puzzle games.")
        search = [video_item(), video_item(video_id="bcdefghijkl", channel_id=CHANNEL_ID_2)]
        routes = youtube_routes(search_items=search, channels=[NO_EMAIL_CHANNEL, big])
        state = store.empty_state()
        found, fake = self.run_discovery(state, routes, config)
        feeds = [c[0] for c in fake.calls if "feeds/videos.xml" in c[0]]
        self.assertEqual(feeds, [feed_url(CHANNEL_ID)])               # the 12k channel, not the 400k one

        config["youtube"]["feed_checks_per_run"] = 0
        found, fake = self.run_discovery(store.empty_state(), routes, config)
        self.assertFalse([c for c in fake.calls if "feeds/videos.xml" in c[0]])

    def test_suppressed_creator_found_through_video_descriptions_is_not_added(self):
        feed = youtube_feed(CHANNEL_ID, [("abcdefghijk", "Indie puzzle", 6, LABELLED),
                                         ("bcdefghijkl", "Logic puzzle", 9, LABELLED)])
        routes = {**youtube_routes(channels=[NO_EMAIL_CHANNEL]), feed_url(CHANNEL_ID): Response(200, text=feed)}
        state = store.empty_state()
        store.suppress(state, NOW, email="pal.work@gmail.com", reason="opted out", by="test")
        found, _ = self.run_discovery(state, routes)
        self.assertEqual(state["prospects"], {})
        self.assertEqual(found.rejected, 1)

    def test_discovery_requests_only_read_public_pages_and_google_apis(self):
        feed = youtube_feed(CHANNEL_ID, [("abcdefghijk", "Indie puzzle", 6, LABELLED)])
        routes = {**youtube_routes(channels=[NO_EMAIL_CHANNEL]), feed_url(CHANNEL_ID): Response(200, text=feed)}
        _, fake = self.run_discovery(store.empty_state(), routes)
        hosts = {urlsplit(c[0]).hostname for c in fake.calls}
        self.assertLessEqual(hosts, {"www.googleapis.com", "www.youtube.com"})
