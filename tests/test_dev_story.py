"""Developer-led posts: the 'dev_story' post type, its media eligibility, the two-thirds rotation, rotating connection
endings, and the screens that still stop invented claims. All offline: Gemini, the critic and Buffer are mocked.

What these tests hold the change to:
* about two thirds of rotation positions are developer-led, and every other post type keeps its turn;
* a developer story is only written about media with a verified level or mechanic list, in the first person, naming
  a verified detail of that media, and never with invented development history, anecdotes or mechanic behaviour;
* the ending rotates between four styles and never repeats a recent post's closing line ("Let's connect" included);
* the critic, its evidence check and its minimum score are unchanged, and the publish path is the same single,
  verified path (one Buffer call, media attached, logged, counted once).
"""

import contextlib
import io
import itertools
import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import support
from support import OfflineTestCase
import approvals
import autopilot
import caption_quality as cq
import content_matching as cm
import dev_story as ds
import game_terms
import media_manifest as mm
import post_rules
import test_approvals as approval_fixture
from test_caption_recovery import FakeCritic, approve, critique, reject_unsupported

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = (ROOT / "RINGSHIFT_KNOWLEDGE.md").read_text(encoding="utf-8")
PROJECT = {"project_url": None}
OTHER_TYPES = {"challenge", "hook", "mechanic", "observation", "visual", "solution", "moment", "feedback", "discovery"}

STILL = {"file": "store/rs_hard_L21_mirror-block-halfblock_puzzle_20261027.jpg", "type": "image", "level": 21,
         "mode": "hard", "theme": "Wind Dunes", "mechanisms": ["Mirror", "Block", "Half-Block", "Portal"],
         "puzzle_state": "unsolved", "shows_action": False,
         "description": "Level 21 (Hard) with Mirror, Block, Half-Block, Portal gimmicks in the 'Wind Dunes' visual theme. "
                        "The puzzle at its starting position; no locks solved yet."}
CLIP = {"file": "store/rs_hard_L24_prism-mirror-halfblock_clip_20261026.mp4", "type": "video", "level": 24, "mode": "hard",
        "theme": "Circuit Board", "mechanisms": ["Mirror", "Half-Block", "Prism"], "puzzle_state": "solved",
        "shows_action": True,
        "description": "Level 24 (Hard) with Prism, Mirror, Half-Block gimmicks in the 'Circuit Board' visual theme. Real "
                       "in-game play: the solution is entered with the keyboard in 12 moves and the clip ends on the "
                       "BREACHED level-complete screen."}
ONE_MECHANIC = {"file": "store/x.jpg", "type": "image", "level": 3, "mode": "easy", "mechanisms": ["Mirror"],
                "puzzle_state": "unsolved", "description": "Level 3 (Easy) with Mirror gimmicks."}
MEDIA_SET = [STILL, CLIP, ONE_MECHANIC]


def dev_type(style):
    found = next(t for t in autopilot.POST_TYPES if t["id"] == "dev_story")
    return dict(found, connection_style=style)


def manifest_record(base, relative, *, level=11, mechanisms=("Echo", "Portal", "Block"), state="unsolved", **extra):
    """A genuine-looking manifest record for a fixture file (see media_manifest.make_item)."""
    item = mm.make_item(relative, base_dir=base, media_type="image", source="capture_gameplay.py", mode="hard",
                        level=level, theme="Wind Dunes", mechanisms=list(mechanisms), mechanisms_source="game-data",
                        puzzle_state=state, shows_action=False, verification="verified",
                        description=extra.pop("description", "Level 11 (Hard) with Echo, Portal, Block gimmicks."),
                        technical={"width": 1080, "height": 1350, "aspect": "4:5", "bytes": 6000, "duration": None,
                                   "sha256": None},
                        validation={"status": "ok", "checked_at": None, "problems": []}, hashes=[])
    item.update(extra)
    return item


# ======================================================================================================================
# 1. The post type and its media eligibility
# ======================================================================================================================

class DevStoryTypeTests(unittest.TestCase):
    def test_the_type_exists_is_developer_led_and_keeps_the_old_type_order(self):
        ids = [t["id"] for t in autopilot.POST_TYPES]
        self.assertEqual(ids[:12], ["challenge", "hook", "mechanic", "observation", "visual", "solution", "moment",
                                    "feedback", "discovery", "progress", "behind_scenes", "invite"])
        self.assertEqual(ids[12], "dev_story")
        self.assertEqual(set(ds.DEVELOPER_LED_TYPES), {"dev_story", "progress", "behind_scenes"})
        self.assertTrue(autopilot.is_developer_led(dev_type("showcase")))
        self.assertFalse(autopilot.is_developer_led({"id": "visual"}))

    def test_it_is_always_available_without_facts_or_a_url(self):
        self.assertIn("dev_story", [t["id"] for t in autopilot.eligible_post_types(PROJECT, [])])

    def test_media_with_a_verified_level_or_mechanic_list_is_eligible(self):
        for record in ({"post_categories": ["visual_showcase"], "type": "image", "game": {"level": 21}},
                       {"post_categories": ["mechanism_demo", "visual_showcase"], "type": "image", "mechanisms": ["Prism"]},
                       {"post_categories": ["solution_reveal", "gameplay_moment", "visual_showcase"], "type": "video",
                        "game": {"level": 24}, "mechanisms": ["Prism"]}):
            with self.subTest(record=record):
                self.assertIsNone(cm.why_not(record, "dev_story"))

    def test_media_without_any_verified_detail_is_not_eligible(self):
        reason = cm.why_not({"post_categories": ["visual_showcase"], "type": "image", "game": {}}, "dev_story")
        self.assertIn("verified level number or mechanic list", reason)

    def test_failed_excluded_or_special_mode_media_supports_no_developer_story(self):
        base = {"description": "Level 9 (Hard) with Prism gimmicks.", "mechanisms": ["Prism"], "type": "image",
                "puzzle_state": "unsolved", "game": {"level": 9}}
        for bad in ({"exclude": True}, {"game": {"level": 9, "special_mode": True}},
                    {"validation": {"status": "failed"}}, {"review": {"status": "rejected"}}, {"description": ""}):
            record = dict(base, **bad)
            record["post_categories"] = mm.categories_for(record)
            with self.subTest(bad=bad):
                self.assertIsNotNone(cm.why_not(record, "dev_story"))


class DevStoryMediaSelectionTests(OfflineTestCase):
    """The rotation picks a developer story at a developer position, on a file that supports one, and falls back to a
    type the file does support otherwise. The media check (strict in production) still decides what can be used."""

    def write(self, *records):
        mm.save_manifest({"schema": mm.SCHEMA, "items": list(records)}, self.tmp / "media" / "manifest.json")

    def test_a_developer_position_chooses_a_developer_story_on_verified_media(self):
        self.write(manifest_record(self.tmp, self.image_rel))
        post_type, media, _ = autopilot.choose_post_and_media(PROJECT, [], counter=4)
        self.assertEqual(post_type["id"], "dev_story")
        self.assertEqual(media["file"], self.image_rel)
        self.assertEqual(media["level"], 11)

    def test_media_with_no_verified_detail_falls_back_to_a_type_it_supports(self):
        record = manifest_record(self.tmp, self.image_rel, level=None, mechanisms=(),
                                 description="A purple-toned screenshot of the game.")
        record["game"]["level"] = None
        record["post_categories"] = mm.categories_for(record)
        self.write(record)
        post_type, media, skipped = autopilot.choose_post_and_media(PROJECT, [], counter=4)
        self.assertNotIn(post_type["id"], ds.DEVELOPER_LED_TYPES)
        self.assertTrue(any(s["post_type"] == "dev_story" and "verified level number" in s["reason"] for s in skipped))

    def test_the_pinned_daily_file_is_the_only_file_a_developer_story_can_use(self):
        self.write(manifest_record(self.tmp, self.image_rel))
        for counter in (1, 2, 4, 5):
            post_type, media, _ = autopilot.choose_post_and_media(PROJECT, [], counter=counter, only_file=self.image_rel)
            self.assertEqual((post_type["id"], media["file"]), ("dev_story", self.image_rel))

    def test_a_failing_media_check_still_stops_a_developer_story(self):
        self.write(manifest_record(self.tmp, self.image_rel))

        def failing(_media):
            raise autopilot.MediaUnavailableError("raw URL answered 404")

        with self.assertRaises(autopilot.NoSuitableMediaError) as raised:
            autopilot.choose_post_and_media(PROJECT, [], counter=4, check=failing, only_file=self.image_rel)
        self.assertTrue(any("404" in s["reason"] for s in raised.exception.skipped))


# ======================================================================================================================
# 2. The intended rotation frequency
# ======================================================================================================================

class RotationShareTests(unittest.TestCase):
    def ids(self, positions, log=(), facts=(), project=PROJECT):
        return [autopilot.pick_post_type(n, project, list(facts), post_log=list(log))["id"] for n in positions]

    def test_the_target_share_is_two_thirds(self):
        self.assertAlmostEqual(ds.TARGET_DEVELOPER_SHARE, 2 / 3)

    def test_every_three_consecutive_positions_hold_exactly_two_developer_stories(self):
        ids = self.ids(range(300))
        for start in range(0, 300, 3):
            window = ids[start:start + 3]
            self.assertEqual(sum(i in ds.DEVELOPER_LED_TYPES for i in window), 2, (start, window))

    def test_daily_publishing_reaches_two_thirds_and_keeps_every_other_type(self):
        # A published day moves the post counter AND the day offset, so the position advances by two.
        log, chosen, counter, day = [], [], 19, 301
        for _ in range(60):
            post_type = autopilot.pick_post_type(counter + day, PROJECT, [], post_log=log)["id"]
            chosen.append(post_type)
            log.append({"post_type": post_type})
            counter, day = counter + 1, day + 1
        share = sum(c in ds.DEVELOPER_LED_TYPES for c in chosen) / len(chosen)
        self.assertAlmostEqual(share, 2 / 3, delta=0.04)
        self.assertEqual(set(chosen) - {"dev_story"}, OTHER_TYPES)
        runs = [len(list(group)) for key, group in itertools.groupby(chosen, lambda c: c in ds.DEVELOPER_LED_TYPES) if key]
        self.assertLessEqual(max(runs), 2, "never three developer stories in a row")

    def test_days_without_a_post_still_reach_two_thirds_and_every_type(self):
        # Draft-only days (kill switch) and failed days move only the day offset: the position advances by one.
        ids = self.ids(range(301, 301 + 81))
        self.assertAlmostEqual(sum(i == "dev_story" for i in ids) / len(ids), 2 / 3, delta=0.01)
        self.assertEqual(set(ids) - {"dev_story"}, OTHER_TYPES)

    def test_mixed_days_stay_close_to_two_thirds(self):
        log, chosen, position = [], [], 300
        for step in [2, 2, 1, 2, 1, 1, 2, 2, 2, 1] * 6:
            post_type = autopilot.pick_post_type(position, PROJECT, [], post_log=log)["id"]
            chosen.append(post_type)
            if step == 2:                           # only a published day is logged
                log.append({"post_type": post_type})
            position += step
        self.assertAlmostEqual(sum(c == "dev_story" for c in chosen) / len(chosen), 2 / 3, delta=0.07)

    def test_other_types_are_chosen_least_recently_published_first(self):
        log = [{"post_type": t} for t in ("challenge", "observation", "moment", "hook", "visual", "feedback", "mechanic",
                                           "solution")]
        self.assertEqual(self.ids([0], log), ["discovery"])         # the only one not published recently

    def test_verified_facts_join_the_developer_share_and_rotate_with_it(self):
        facts = [{"id": "f1", "kind": "progress", "statement": "A level editor ships with the game.", "source": "doc"},
                 {"id": "f2", "kind": "behind_the_scenes", "statement": "Levels are checked by brute force.", "source": "doc"}]
        log, chosen = [], []
        for n in range(1, 31):
            if not ds.is_developer_slot(n):
                continue
            post_type = autopilot.pick_post_type(n, PROJECT, facts, post_log=log)["id"]
            chosen.append(post_type)
            log.append({"post_type": post_type})
        self.assertEqual(set(chosen), {"dev_story", "progress", "behind_scenes"})

    def test_a_project_url_brings_the_invitation_back_into_the_other_slots(self):
        ids = self.ids(range(0, 90, 3), project={"project_url": "https://example.com/r"})
        self.assertIn("invite", ids)

    def test_every_eligible_type_stays_in_the_fallback_order(self):
        for position in range(6):
            order = [t["id"] for t in autopilot.rotation_types(PROJECT, [], position, [])]
            self.assertEqual(set(order), OTHER_TYPES | {"dev_story"})
            self.assertEqual(order[0] == "dev_story", ds.is_developer_slot(position))


# ======================================================================================================================
# 3. Varied connection invitations and non-repetitive wording
# ======================================================================================================================

class ConnectionStyleTests(unittest.TestCase):
    def test_the_four_endings_rotate_and_never_repeat_back_to_back(self):
        log, styles = [], []
        for position in range(1, 25):
            style = ds.choose_connection_style(position, log)
            styles.append(style)
            log.append({"post_type": "dev_story", "connection_style": style})
        self.assertEqual(set(styles), set(ds.STYLE_ORDER))
        self.assertTrue(all(a != b for a, b in zip(styles, styles[1:])), styles)
        for start in range(0, 24, 4):
            self.assertEqual(len(set(styles[start:start + 4])), 4, styles)

    def test_drafts_without_a_log_still_vary_by_day(self):
        self.assertEqual({ds.choose_connection_style(p) for p in range(8)}, set(ds.STYLE_ORDER))

    def test_question_endings_are_skipped_while_most_recent_posts_were_questions(self):
        for position in range(12):
            style = ds.choose_connection_style(position, [], question_share=0.6, recent_count=6)
            self.assertFalse(ds.CONNECTION_STYLES[style]["question"])

    def test_the_verified_floor_captions_pass_every_deterministic_screen_in_every_style(self):
        for media, style in itertools.product(MEDIA_SET, ds.STYLE_ORDER):
            captions = ds.fact_captions(media, style, seed=3)
            self.assertTrue(captions, (media["file"], style))
            for text in captions:
                with self.subTest(media=media["file"], style=style, text=text):
                    self.assertEqual(ds.voice_problems(text, media, style), [])
                    self.assertEqual(cq.quality_problems(text, media, "dev_story"), [])
                    self.assertEqual(game_terms.check_mechanics(text, media), [])
                    self.assertEqual(cm.text_problems(text, media, dev_type(style)), [])
                    self.assertEqual(post_rules.validate_post(text), [])
                    self.assertEqual(ds.bait_problems(text), [])

    def test_each_style_ends_differently(self):
        closings = {style: {ds.closing_sentence(c) for c in ds.fact_captions(STILL, style)} for style in ds.STYLE_ORDER}
        self.assertTrue(all(c.endswith("?") for c in closings["design_question"]))
        self.assertTrue(all(not c.endswith("?") for c in closings["maker_note"] | closings["showcase"]))
        self.assertTrue(any(re.search(r"indie dev", c, re.I) for c in closings["indie_invite"]))
        for a, b in itertools.combinations(ds.STYLE_ORDER, 2):
            self.assertFalse(closings[a] & closings[b], (a, b))

    def test_no_stock_ending_in_the_verified_captions(self):
        for media, style in itertools.product(MEDIA_SET, ds.STYLE_ORDER):
            for text in ds.fact_captions(media, style):
                self.assertNotRegex(text, r"(?i)let'?s connect|what are you working on")

    def test_a_closing_line_already_used_recently_is_rejected(self):
        recent = ["Lets connect. I am working on a puzzle game. What are you working on?",
                  "Level 8 with a Mirror. Fellow indie devs, I'd love to see what you're building. #Ringshift"]
        self.assertTrue(ds.closing_problems("I built Level 21. What are you working on?", recent))
        self.assertTrue(ds.closing_problems("I built Level 21 on Hard. Let's connect!", recent))
        self.assertTrue(ds.closing_problems(
            "I built Level 21 on Hard. Fellow indie devs, I'd love to see what you're building! #indiedev", recent))
        self.assertTrue(ds.closing_problems(
            "I built Level 21 on Hard. Fellow indie devs, I would love to see what you are building.", recent))
        self.assertEqual(ds.closing_problems("I built Level 21 on Hard. Does the Mirror read clearly here?", recent), [])

    def test_a_week_of_developer_stories_never_ends_the_same_way_twice(self):
        # The daily capture shows a level not shown before (see daily_media.choose_scenes), so each day has its own level.
        recent, log = [], []
        for day, media in zip(range(1, 13), itertools.cycle(MEDIA_SET)):
            media = dict(media, level=media["level"] + 30 + day)
            style = ds.choose_connection_style(day, log)
            usable = [c for c in ds.fact_captions(media, style, seed=day) if not ds.closing_problems(c, recent)
                      and not cm.repeats_opening(c, recent)]
            self.assertTrue(usable, (day, style))
            recent.append(usable[0])
            log.append({"post_type": "dev_story", "connection_style": style})
        closings = [ds.closing_sentence(t) for t in recent if ds.closing_sentence(t)]
        tails = [c for c in closings if c.endswith("?") or ds.FOLLOW_RE.search(c) or ds.INDIE_AUDIENCE_RE.search(c)]
        self.assertEqual(len(tails), len(set(tails)), tails)

    def test_each_style_is_enforced(self):
        self.assertTrue(ds.style_problems("I built Level 21. Follow along as it grows?", STILL, "maker_note"))
        self.assertTrue(ds.style_problems("I built Level 21 on Hard.", STILL, "maker_note"))
        self.assertTrue(ds.style_problems("I built Level 21. Fellow indie devs, share yours?", STILL, "showcase"))
        self.assertTrue(ds.style_problems("I built Level 21. I'm sharing the build here.", STILL, "showcase"))
        self.assertTrue(ds.style_problems("I built Level 21. What did you have for lunch?", STILL, "design_question"))
        self.assertTrue(ds.style_problems("I built Level 21. Anyone else?", STILL, "indie_invite"))
        self.assertEqual(ds.style_problems("I built Level 21. Does the Portal read clearly here?", STILL, "design_question"), [])

    def test_engagement_bait_is_rejected_on_every_post_type(self):
        for bait in ("Like and retweet if you'd play Level 21!", "Follow for more puzzles like Level 21.",
                     "Tag a friend who'd solve Level 21.", "RT if Level 21 on Hard looks good"):
            for type_id in ("visual", "observation", "dev_story"):
                with self.subTest(bait=bait, type_id=type_id), mock.patch.object(autopilot, "posts_history", ""), \
                        mock.patch.object(autopilot, "analytics", ""):
                    reason = autopilot.screen_candidate(bait, [], PROJECT, STILL, dev_type("showcase") if type_id == "dev_story"
                                                        else {"id": type_id})
                    self.assertIn("engagement bait", reason or "")


# ======================================================================================================================
# 4. Unsupported claims and invented mechanic behaviour are still rejected
# ======================================================================================================================

class DeveloperClaimsTests(unittest.TestCase):
    def reason(self, text, media=STILL, style="showcase", recent=()):
        with mock.patch.object(autopilot, "posts_history", ""), mock.patch.object(autopilot, "analytics", ""):
            return autopilot.screen_candidate(text, list(recent), PROJECT, media, dev_type(style))

    def test_invented_development_history_and_anecdotes_are_rejected(self):
        for text in ("Level 21 on Hard took me three weeks to get right. #Ringshift",
                     "I almost gave up on Level 21 on Hard. #Ringshift",
                     "My playtesters hated the Mirror in Level 21. #Ringshift",
                     "I rewrote the Portal for Level 21 twice. #Ringshift",
                     "I scrapped the first version of Level 21. #Ringshift",
                     "I built Level 21 last night. #Ringshift",
                     "I'm adding a Mirror to Level 21 in the next update. #Ringshift",
                     "Level 21 is the 40th version I made. #Ringshift",
                     "Players kept asking me for Level 21, so I built it. #Ringshift",
                     "My friend couldn't beat Level 21 so I built it harder. #Ringshift"):
            with self.subTest(text=text):
                reason = self.reason(text)
                self.assertIsNotNone(reason)
                self.assertRegex(reason, r"only a verified project fact can say that|unsupported claim")

    def test_a_developer_story_must_be_first_person_and_about_this_media(self):
        self.assertIn("first-person", self.reason("Level 21 on Hard, with a Mirror. #Ringshift"))
        self.assertIn("does not name the level", self.reason(
            "I'm building an indie puzzle game of rings and beams around a circular puzzle. #Ringshift"))
        self.assertIn("refers to level 7", self.reason("I built Level 7 with a Mirror. #Ringshift"))

    def test_mechanics_the_media_does_not_show_are_rejected(self):
        self.assertIn("names mechanic(s) the media does not show: Prism", self.reason("I built the Prism in Level 21. #Ringshift"))
        self.assertIn("does not use", self.reason("I built the black hole in Level 21. #Ringshift"))

    def test_invented_mechanic_behaviour_is_rejected(self):
        for text in ("I built Level 21 so the Portal catches the beam. #Ringshift",
                     "I designed Level 21 so the Block shifts every turn. #Ringshift",
                     "I built the Mirror in Level 21 to split a beam. The Mirror splits beams into two. #Ringshift",
                     "In Level 21 the Mirror bends the beam straight into the lock. #Ringshift",
                     "I put the Block right beside the Portal in Level 21. #Ringshift",
                     "In Level 21 the Portal teleports the beam into the third lock. #Ringshift"):
            with self.subTest(text=text):
                self.assertRegex(self.reason(text) or "", r"what a game object does|relative to each other")

    def test_verified_behaviour_is_allowed_only_for_a_mechanic_on_screen(self):
        prism = "I built the Prism for Level 24: a Prism splits a beam into two beams at 45 degrees. #Ringshift"
        self.assertEqual(cq.invented_detail_problems(prism, CLIP), [])
        self.assertTrue(cq.invented_detail_problems(prism, STILL))            # no Prism in Level 21
        self.assertTrue(cq.invented_detail_problems(prism))                   # no media: nothing is verified
        mirror = "A Mirror bends a beam 90 degrees, and Level 21 on Hard has one. I built it. #Ringshift"
        self.assertEqual(cq.invented_detail_problems(mirror, STILL), [])
        self.assertTrue(cq.invented_detail_problems("A Mirror bends a beam 45 degrees here.", STILL))

    def test_every_allowed_behaviour_is_written_in_the_knowledge_file(self):
        knowledge = cq.normalize(KNOWLEDGE)
        examples = {"Mirror": "Mirror: bends a beam 90 degrees", "Portal": "Portal: teleports a beam to its paired mouth",
                    "Prism": "Prism: splits a beam into two beams", "Echo": "Echo: sends a copy of a beam"}
        self.assertEqual(set(examples), set(cq.VERIFIED_BEHAVIOURS))
        for name, line in examples.items():
            with self.subTest(name=name):
                self.assertIn(cq.normalize(line), knowledge)
                self.assertTrue(cq._VERIFIED_BEHAVIOUR_RES[name].search(line.replace(":", "")))

    def test_social_proof_and_release_claims_are_still_rejected_in_the_developer_voice(self):
        for text in ("I built Level 21 and 500 players already solved it. #Ringshift",
                     "I'm building Ringshift, out now on Steam. Level 21 on Hard. #Ringshift",
                     "I built Level 21 for my award-winning puzzle game. #Ringshift"):
            with self.subTest(text=text):
                self.assertIn("unsupported claim", self.reason(text) or "")

    def test_the_critic_still_rejects_an_unsupported_developer_claim_and_keeps_its_minimum(self):
        sources = cm.evidence_sources(STILL, KNOWLEDGE)
        anecdote = critique(['- "it took me weeks" => UNSUPPORTED'], 44, "NO")
        self.assertFalse(autopilot.critique_verdict(anecdote, sources)[0])
        invented = critique(['- "I spent three weeks on it" => KNOWLEDGE evidence: "I spent three weeks on Level 21"'], 44, "YES")
        keep, reasons = autopilot.critique_verdict(invented, sources)
        self.assertFalse(keep)
        self.assertTrue(any("was not found in the knowledge" in r for r in reasons))
        real = critique(['- "I hand-author the levels" => KNOWLEDGE evidence: "I hand-author the Easy, Normal and Hard levels"'],
                        30, "YES")
        self.assertTrue(autopilot.critique_verdict(real, sources)[0])
        self.assertFalse(autopilot.critique_verdict(real.replace("Hook: 5", "Hook: 4"), sources)[0])
        self.assertEqual(autopilot.MIN_TOTAL_SCORE, 30)

    def test_the_prompts_carry_the_developer_rules(self):
        captured = {}
        with mock.patch.object(autopilot, "generate_with_retry",
                               side_effect=lambda p: captured.setdefault("prompts", []).append(p) or mock.Mock(text="")):
            autopilot.generate_candidates(STILL, dev_type("maker_note"), PROJECT)
            autopilot.generate_candidates(STILL, next(t for t in autopilot.POST_TYPES if t["id"] == "visual"), PROJECT)
            autopilot.critique_post("I built Level 21.", STILL, dev_type("maker_note"))
        writer_dev, writer_visual, critic = captured["prompts"]
        self.assertIn("in the first person", writer_dev)
        self.assertIn("--- DEVELOPER VOICE (this post type) ---", writer_dev)
        self.assertIn(ds.CONNECTION_STYLES["maker_note"]["instruction"], writer_dev)
        self.assertIn("I hand-author the Easy, Normal and Hard levels as puzzle ladders.", writer_dev)   # safe claim
        self.assertNotIn("--- DEVELOPER VOICE (this post type) ---", writer_visual)
        self.assertIn("You are the social-media writer", writer_visual)
        for prompt in (writer_dev, writer_visual):
            self.assertIn("Never say a theme changes gameplay", prompt)
            self.assertIn("No engagement bait", prompt)
        self.assertIn("DEVELOPER VOICE section says it", critic)
        self.assertIn("When unsure, choose UNSUPPORTED", critic)

    def test_developer_voice_lines_in_the_knowledge_are_what_the_writer_may_restate(self):
        claims = ds.verified_claims(CLIP, KNOWLEDGE)
        self.assertIn("I'm an indie game developer building Ringshift.", claims)
        self.assertTrue(any(c.startswith("Prism:") for c in claims))
        self.assertFalse(any(c.startswith(("Portal:", "Echo:")) for c in claims))      # not on screen in this clip
        for claim in claims:
            self.assertTrue(autopilot.evidence_found(claim, cm.evidence_sources(CLIP, KNOWLEDGE)), claim)


# ======================================================================================================================
# 5. The pipeline end to end: same critic, same publish path, same safeguards
# ======================================================================================================================

class DevStoryDraftTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.candidates = mock.patch.object(autopilot, "generate_candidates").start()
        self.critic = FakeCritic()
        mock.patch.object(autopilot, "critique_post", side_effect=self.critic).start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"description": "Level 11 (Hard) with Echo, Portal, Block gimmicks."}}),
                                  encoding="utf-8")
        mm.save_manifest({"schema": mm.SCHEMA, "items": [manifest_record(self.tmp, self.image_rel)]},
                         self.tmp / "media" / "manifest.json")
        self.counter_file.write_text(json.dumps({"successful_posts": 4}), encoding="utf-8")     # a developer position
        self.draft_path = self.tmp / "d.json"

    def run_dry(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            autopilot.main(["--dry-run", "--draft-file", str(self.draft_path)])
        return out.getvalue()

    def report(self):
        return json.loads(self.draft_path.read_text(encoding="utf-8"))

    def test_screenshot_descriptions_are_refused_and_a_verified_developer_caption_wins(self):
        described = ["The Wind Dunes theme changes how Level 11 looks without altering the gameplay. #Ringshift",
                     "Level 11 took me three weeks, the Echo nearly broke me. #Ringshift",
                     "Lets connect. I am working on a puzzle game. What are you working on?"]
        self.candidates.side_effect = [list(described), list(described)]
        self.critic.default = approve(36)
        output = self.run_dry()
        self.assertIn("DRAFT COMPLETE", output)
        self.assertIn("developer-led; ending:", output)
        draft = self.report()
        self.assertEqual(draft["post_type"], "dev_story")
        self.assertTrue(draft["developer_led"])
        self.assertIn(draft["connection_style"], ds.STYLE_ORDER)
        self.assertNotIn(draft["text"], described)
        self.assertFalse(set(described) & set(self.critic.seen), "none of them may even reach the critic")
        self.assertIn(draft["text"], self.critic.seen)
        self.assertGreaterEqual(draft["critic_score"], autopilot.MIN_TOTAL_SCORE)
        self.assertEqual(ds.voice_problems(draft["text"], dict(STILL, level=11, mechanisms=["Echo", "Portal", "Block"],
                                                               theme="Wind Dunes"), draft["connection_style"]), [])
        self.assertEqual(self.post.call_count, 0)
        self.assertFalse(self.post_log_file.exists())

    def test_the_writer_is_asked_for_the_runs_ending_style(self):
        self.candidates.return_value = []
        self.critic.default = reject_unsupported()
        self.run_dry()
        post_type = self.candidates.call_args_list[0].args[1]
        self.assertEqual(post_type["id"], "dev_story")
        self.assertIn(post_type["connection_style"], ds.STYLE_ORDER)

    def test_a_good_developer_caption_from_the_writer_is_drafted_after_the_critic(self):
        style = ds.choose_connection_style(4, [], 0.0, 0)
        good = {"design_question": "I built Level 11 on Hard around an Echo, a Portal and a Block. Which one does your eye find first? #Ringshift",
                "maker_note": "I hand-author every level, and Level 11 on Hard is one of mine: Echo, Portal and Block. I'm sharing the build here as it grows. #Ringshift",
                "indie_invite": "Level 11 on Hard: Echo, Portal and Block on one board I built. Fellow indie devs, show me what you're making! #Ringshift",
                "showcase": "Level 11 on Hard, a level I built for Ringshift: Echo, Portal and Block on one circular puzzle. #Ringshift"}[style]
        self.candidates.return_value = [good]
        self.critic.default = approve(40)
        self.run_dry()
        self.assertEqual(self.report()["text"], good)
        self.assertEqual(self.critic.seen, [good])

    def test_when_the_critic_rejects_everything_there_is_no_developer_draft(self):
        self.candidates.side_effect = [["I built Level 11 on Hard. #Ringshift"], []]
        self.critic.default = reject_unsupported(total=44)
        output = self.run_dry()
        self.assertIn("NO CANDIDATE PASSED", output)
        self.assertNotIn("draft_id", self.report())
        self.assertLessEqual(len(self.critic.seen), autopilot.MAX_CRITIC_CALLS)


class DevStoryUnattendedTests(approval_fixture.DraftTestCase):
    """The unattended daily run (strict media mode) for a developer story: one post, through publish_approved."""

    def setUp(self):
        super().setUp()
        self.candidates = mock.patch.object(autopilot, "generate_candidates").start()
        self.critic = FakeCritic(default=approve(38))
        mock.patch.object(autopilot, "critique_post", side_effect=self.critic).start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"tags": ["black hole"], "description": approval_fixture.DESCRIPTION}}),
                                  encoding="utf-8")
        self.days_dir = self.tmp / "data" / "used_days"
        mock.patch.object(approvals, "default_claimer", lambda *_: approvals.LocalFileClaim(self.claims_dir)).start()
        mock.patch.object(approvals, "day_claimer", lambda *_: approvals.LocalFileClaim(self.days_dir)).start()
        mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "777"}).start()
        os.environ.pop("RINGSHIFT_AUTOPUBLISH", None)
        self.counter_file.write_text(json.dumps({"successful_posts": 4}), encoding="utf-8")
        self.draft_out = self.tmp / "preview_draft.json"
        self.result_file = self.tmp / "auto_result.json"

    def auto(self):
        argv = ["--auto-publish", "--draft-file", str(self.draft_out), "--result-file", str(self.result_file),
                "--media-file", self.image_rel]
        out, error = io.StringIO(), None
        with contextlib.redirect_stdout(out):
            try:
                autopilot.main(argv)
            except SystemExit as exc:
                error = exc
        return error, out.getvalue()

    def test_a_developer_story_is_published_once_with_media_and_logged_for_the_rotation(self):
        self.candidates.side_effect = [["The purple theme changes nothing about Level 11. #Ringshift"], []]
        error, output = self.auto()
        self.assertIsNone(error, output[-3000:])
        self.assertEqual(self.post.call_count, 1)                                     # exactly one Buffer call
        sent = self.sent()
        self.assertEqual(sent["assets"], [{"image": {"url": support.media_selector.raw_url(self.image_rel)}}])
        self.assertEqual(sent["mode"], "addToQueue")
        self.assertTrue(ds.first_person(sent["text"]), sent["text"])
        self.assertIn(sent["text"], self.critic.seen)
        log = json.loads(self.post_log_file.read_text(encoding="utf-8"))
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["post_type"], "dev_story")
        self.assertTrue(log[0]["developer_led"])
        self.assertIn(log[0]["connection_style"], ds.STYLE_ORDER)
        self.assertEqual(log[0]["approved_by"], "autopilot (unattended daily run)")
        self.assertEqual(json.loads(self.counter_file.read_text())["successful_posts"], 5)
        self.assertEqual(len(list(self.days_dir.iterdir())), 1)

        # The next day's rotation reads that log: a different ending, and no second post the same day.
        self.assertNotEqual(ds.choose_connection_style(6, log), log[0]["connection_style"])
        self.candidates.side_effect = None
        self.candidates.return_value = []
        error, _ = self.auto()
        self.assertEqual(error.code, 1)
        self.assertEqual(self.post.call_count, 1)

    def test_no_acceptable_developer_caption_means_no_post_and_no_claimed_day(self):
        self.candidates.side_effect = [["Level 11, nothing more. #Ringshift"], []]
        self.critic.default = critique(['- "x" => MEDIA evidence: "It is Level 11 (Hard)."'], 29, "NO")
        error, output = self.auto()
        self.assertEqual(error.code, 1)
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.result()["status"], "no_draft")
        self.assertFalse(list(self.days_dir.glob("*")) if self.days_dir.exists() else [])

    def result(self):
        return json.loads(self.result_file.read_text(encoding="utf-8"))

    def test_publishing_rechecks_the_developer_voice_and_refuses_without_sending(self):
        media = self.media(**{"level": 11, "mechanisms": ["Echo", "Portal", "Block"]})
        draft = approvals.create_draft("Level 11 on Hard with Echo, Portal and Block. #Ringshift", media,
                                       autopilot.BUFFER_CHANNEL_ID, post_type="dev_story", run_id="777", repo_root=self.tmp,
                                       media_check=support.ok_check(self.tmp / self.image_rel),
                                       extra={"connection_style": "showcase", "result": "winner",
                                              "media_facts": {"level": 11, "mode": "hard",
                                                              "mechanisms": ["Echo", "Portal", "Block"]}})
        self.store(draft)
        before = self.state()
        error, output = self.publish()
        self.assertIsNotNone(error)
        self.assertIn("first-person", output)
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.claim_files(), [])


class SafeguardConstantsTests(unittest.TestCase):
    """The change adds a post type; it must not loosen any limit around publishing."""

    def test_limits_are_unchanged(self):
        self.assertEqual(autopilot.MIN_TOTAL_SCORE, 30)
        self.assertEqual(autopilot.MAX_CRITIC_CALLS, 8)
        self.assertEqual(autopilot.MAX_DRAFTS_PER_RUN, 1)
        self.assertEqual(autopilot.MIN_HOURS_BETWEEN_POSTS, 12)
        self.assertEqual(autopilot.MEDIA_CHECK_MODE, "strict")
        self.assertEqual(autopilot.NEAR_DUPLICATE_THRESHOLD, 0.7)

    def test_the_schedule_is_unchanged(self):
        workflow = (ROOT / ".github" / "workflows" / "autopilot.yml").read_text(encoding="utf-8")
        self.assertIn('- cron: "0 6 * * *"', workflow)
        self.assertIn('- cron: "37 13 * * *"', workflow)

    def test_the_new_module_cannot_publish(self):
        source = (ROOT / "scripts" / "dev_story.py").read_text(encoding="utf-8")
        for forbidden in ("requests", "send_to_buffer", "post_to_buffer", "createPost", "genai", "subprocess"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
