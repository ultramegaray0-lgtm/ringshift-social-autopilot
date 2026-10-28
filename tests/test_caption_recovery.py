"""Regression tests for scheduled runs #46 and #47 (2026-10-28): every caption rejected, the workflow exited 1.

Run #47 (Level 20, Easy, Mirror/Block/Portal, Autumn Millstone theme, solved clip) rejected:
  1. "Does the mirror angle line up with the portal here in Autumn Millstone? ..."   repeat of the 27 Oct post
  2. "Level 20 wrapping up ... with a block sitting right beside the portal. ..."      invented position
  3. "Watching the block shift positions right before the portal catches the beam ..." invented movement/behaviour
  4. "Level 20 in the Autumn Millstone theme. ..." (the critic's rewrite)               true but plain: 29/60 < 30
Run #46 rejected two candidates that were one sentence, and a third whose (true) mechanics claim cited the
"Mechanics on screen" line, which was shown to the critic but missing from the evidence sources.

What these tests hold the fix to:
* repeats and invented positions/behaviour are still rejected, before the critic;
* a near-threshold (29/60) result leads to a second, better-aimed writing round, never to a lower minimum;
* when nothing specific can be verified, a truthful caption about the game's verified identity can still pass;
* the critic and its evidence check are never bypassed: every draft was reviewed and scored at least the minimum;
* when nothing passes, there is no draft, no post, a failed run and a readable diagnosis.
"""

import contextlib
import copy
import io
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import support
from support import OfflineTestCase
import autopilot
import caption_plan as cp
import caption_quality as cq
import content_matching as cm
import game_terms
import media_manifest as mm
import media_selector
import post_rules

KNOWLEDGE = (Path(__file__).resolve().parent.parent / "RINGSHIFT_KNOWLEDGE.md").read_text(encoding="utf-8")

RUN47_DESC = ("Level 20 (Easy) with Mirror, Block, Portal gimmicks in the 'Autumn Millstone' visual theme. Real in-game play: "
              "the solution is entered with the keyboard in 9 moves and the clip ends on the BREACHED level-complete screen.")
RUN47_MEDIA = {"type": "video", "level": 20, "mode": "easy", "theme": "Autumn Millstone",
               "mechanisms": ["Mirror", "Block", "Portal"], "puzzle_state": "solved", "shows_action": True,
               "description": RUN47_DESC, "file": "store/rs_easy_L20_mirror-block-portal_clip_20261028.mp4"}
RUN46_MEDIA = {"type": "image", "level": 18, "mode": "hard", "theme": "Circuit Board", "mechanisms": ["Echo", "Mirror", "Prism"],
               "puzzle_state": "unsolved", "shows_action": False,
               "description": "Level 18 (Hard) with Echo, Mirror, Prism gimmicks in the 'Circuit Board' visual theme."}

EARLIER_POST = "Does the mirror angle line up with the portal on Level 8? #Ringshift #indiegames"
RUN47_CANDIDATES = [
    "Does the mirror angle line up with the portal here in Autumn Millstone? #Ringshift #indiegames",
    "Level 20 wrapping up inside the Autumn Millstone theme with a block sitting right beside the portal. #Ringshift #indiegames",
    "Watching the block shift positions right before the portal catches the beam on Level 20. #Ringshift #indiegames",
]
PLAIN_29 = "Level 20 in the Autumn Millstone theme. #Ringshift #indiegames"
RUN46_CANDIDATES = [
    "Where do the mirror, prism, and echo fit together on Level 18 in this circuit board theme? #Ringshift #indiegames",
    "Level 18 sits in the circuit board theme with mirror, prism, and echo waiting at the start. #Ringshift #indiegames",
    "Staring at the initial layout of Level 18 with mirror, prism, and echo in the circuit board theme. #Ringshift",
]


def scores(total):
    """Six critic score lines adding up to `total`."""
    base, extra = divmod(total, 6)
    values = [base + (1 if i < extra else 0) for i in range(6)]
    names = ["Hook", "Curiosity", "Natural tone", "Ringshift relevance", "Originality", "Viral potential"]
    return "\n".join(f"{n}: {v}" for n, v in zip(names, values)) + "\n"


def critique(claims, total, keep, improved="Not needed."):
    return (f"CLAIMS:\n" + "\n".join(claims) + f"\n\n{scores(total)}\nKEEP: {keep}\n\nMAIN PROBLEM:\nx\n\n"
            f"IMPROVED VERSION:\n{improved}\n")


def evidence_line(media):
    """A real verified line for the fake critic to quote."""
    return cm.media_facts(media)[0]


class FakeCritic:
    """Stands in for Gemini's critic. It follows the critic's real contract (claims with labels and evidence, six scores,
    KEEP) and is strict: the caller decides per caption what it says. Every caption it sees is recorded."""

    def __init__(self, verdicts=None, default=None):
        self.verdicts = verdicts or {}
        self.default = default
        self.seen = []

    def __call__(self, text, media=None, post_type=None, facts=None):
        self.seen.append(text)
        for key, verdict in self.verdicts.items():
            if key in text:
                return verdict(media) if callable(verdict) else verdict
        if self.default is None:
            raise AssertionError(f"the fake critic has no verdict for {text!r}")
        return self.default(media) if callable(self.default) else self.default


def approve(total=36):
    """A KEEP: YES critique whose only claim quotes a real verified media line."""
    return lambda media: critique([f'- "stated media fact" => MEDIA evidence: "{evidence_line(media)}"'], total, "YES")


def reject_unsupported(statement="the caption", total=30):
    return critique([f'- "{statement}" => UNSUPPORTED'], total, "NO")


# ----------------------------------------------------------------------------------------------------------------------
# Deterministic pieces
# ----------------------------------------------------------------------------------------------------------------------

class GroundingFixTests(unittest.TestCase):
    def test_run46_true_mechanics_claim_citing_the_mechanics_line_is_now_verifiable(self):
        # Run #46's critic quoted this line (cut at 80 characters in the annotation) for "mirror, prism, and echo".
        quote = "Mechanics on screen (use exactly these names, and no other mechanic names): Mirror, Prism, Echo"
        sources = cm.evidence_sources(RUN46_MEDIA, KNOWLEDGE)
        self.assertTrue(autopilot.evidence_found(quote, sources))
        keep, reasons = autopilot.critique_verdict(
            critique([f'- "mirror, prism, and echo" => MEDIA evidence: "{quote}"'], 36, "YES"), sources=sources)
        self.assertTrue(keep, reasons)

    def test_the_line_shown_to_writer_and_critic_is_the_line_evidence_is_checked_against(self):
        line = cm.mechanics_line(RUN47_MEDIA)
        self.assertIn(line, media_selector.describe_media(RUN47_MEDIA))
        self.assertIn(line, cm.evidence_sources(RUN47_MEDIA, KNOWLEDGE))
        self.assertIsNone(cm.mechanics_line({"description": "A purple theme."}))

    def test_a_made_up_quote_is_still_rejected(self):
        sources = cm.evidence_sources(RUN47_MEDIA, KNOWLEDGE)
        keep, reasons = autopilot.critique_verdict(
            critique(['- "block beside the portal" => MEDIA evidence: "the Block sits beside the Portal"'], 40, "YES"),
            sources=sources)
        self.assertFalse(keep)
        self.assertTrue(any("was not found" in r for r in reasons))

    def test_every_safe_claim_shown_to_the_writer_is_checkable_evidence(self):
        sources = cm.evidence_sources(RUN47_MEDIA, KNOWLEDGE)
        for claim in cp.verified_claims(RUN47_MEDIA, KNOWLEDGE):
            text = claim.replace("What it shows (verified): ", "")
            with self.subTest(claim=claim):
                self.assertTrue(autopilot.evidence_found(text, sources), claim)


class InventedDetailTests(unittest.TestCase):
    def test_run47_position_and_movement_captions_are_rejected_without_the_critic(self):
        self.assertTrue(any("positions are not" in p for p in cq.quality_problems(RUN47_CANDIDATES[1], RUN47_MEDIA, "solution")))
        problems = cq.quality_problems(RUN47_CANDIDATES[2], RUN47_MEDIA, "solution")
        self.assertTrue(any("behaviour and movement" in p for p in problems), problems)
        self.assertTrue(cq.invented_detail_problems(EARLIER_POST))

    def test_plain_true_captions_and_visual_words_are_not_flagged(self):
        for text in (PLAIN_29, "A Portal in a purple level.", "Purple light behind the Portal.", "Purple glow around the Portal.",
                     "Level 11: the Echo and the Portal share one rim.", "Two Portals and one purple lock.",
                     "Level 7 has a Portal and an Echo. Where does the beam go?"):
            with self.subTest(text=text):
                self.assertEqual(cq.invented_detail_problems(text), [])

    def test_the_rule_is_applied_at_publish_time_too(self):
        # prepare_approved re-runs screen_candidate with the draft's media facts: an approved draft that states a position
        # is refused, so no path publishes one.
        with mock.patch.object(autopilot, "posts_history", ""), mock.patch.object(autopilot, "analytics", ""):
            reason = autopilot.screen_candidate(RUN47_CANDIDATES[1], [], {"project_url": None}, RUN47_MEDIA)
        self.assertIn("positions are not", reason)


class RepetitionTests(unittest.TestCase):
    def test_run47_candidate_1_still_repeats_the_earlier_post(self):
        kept, dropped = cq.distinct_candidates([RUN47_CANDIDATES[0]], [EARLIER_POST])
        self.assertEqual(kept, [])
        self.assertIn("an earlier post", dropped[0][1])

    def test_run46_batch_repeat_is_still_dropped(self):
        kept, dropped = cq.distinct_candidates(RUN46_CANDIDATES)
        self.assertEqual(kept, RUN46_CANDIDATES[:1])
        self.assertEqual([why for _, why in dropped], ["repeats another candidate in this batch (62% of the same words)"] * 2)


class IdentityAnchorTests(unittest.TestCase):
    def test_every_identity_spelling_is_word_for_word_in_the_knowledge_file(self):
        knowledge = " " + cq.normalize(KNOWLEDGE) + " "
        for label, spellings in cq.IDENTITY_ANCHORS.items():
            for spelling in spellings:
                with self.subTest(spelling=spelling):
                    self.assertIn(" " + cq.normalize(spelling) + " ", knowledge, f"{label}: {spelling!r} is not in the knowledge")

    def test_an_identity_caption_passes_when_nothing_about_the_media_is_verified(self):
        bare = {"type": "image", "description": "A purple-toned screenshot."}
        caption = "An indie puzzle game where you manipulate rings and beams around a circular puzzle. #Ringshift"
        self.assertEqual(cq.quality_problems(caption, bare, "visual"), [])
        self.assertTrue(cp.is_identity_only(caption, bare))

    def test_identity_does_not_rescue_filler_or_unanchored_text(self):
        bare = {"type": "image"}
        self.assertTrue(cq.quality_problems("Rings and beams.", bare, "visual"))                      # too generic
        self.assertTrue(cq.quality_problems("Brand new idea about a circular puzzle.", bare, "visual"))   # filler
        problems = cq.anchor_problems("A calm evening and a hot drink.", bare, "visual")
        self.assertTrue(any("no verified facts" in p for p in problems), problems)

    def test_identity_never_replaces_a_progress_fact(self):
        facts = [{"id": "f", "kind": "progress", "statement": "Ringshift now has seventy five levels.", "source": "list"}]
        self.assertTrue(cq.anchor_problems("Rings and beams around a circular puzzle.", RUN46_MEDIA, "progress", facts))


class AngleSlotTests(unittest.TestCase):
    def test_the_three_candidates_get_three_different_verified_subjects(self):
        for media in (RUN47_MEDIA, RUN46_MEDIA, {"type": "image", "description": "x"}):
            slots = cp.angle_slots(media, "solution", seed=0, count=3)
            with self.subTest(media=media.get("level")):
                self.assertEqual(len(slots), 3)
                self.assertEqual(len({s["id"] for s in slots}), 3)
                self.assertEqual(len({s["shape"] for s in slots}), 3)
                self.assertTrue(slots[-1]["id"].startswith("experience"))

    def test_slots_only_name_what_is_verified(self):
        slots = cp.angle_slots(RUN47_MEDIA, "mechanic", seed=4)
        text = " ".join(s["focus"] + " " + s["instruction"] for s in slots)
        self.assertTrue(set(game_terms.mechanics_mentioned(text)) <= set(RUN47_MEDIA["mechanisms"]))
        self.assertEqual(slots[0]["id"], "one_mechanic")
        slots = cp.angle_slots({"type": "image", "description": "A purple theme."}, "visual")
        self.assertEqual(game_terms.mechanics_mentioned(" ".join(s["instruction"] for s in slots)), [])

    def test_the_writer_prompt_gives_each_candidate_its_own_subject_and_the_safe_claims(self):
        captured = {}
        with mock.patch.object(autopilot, "generate_with_retry",
                               side_effect=lambda p: captured.update(prompt=p) or mock.Mock(text="CANDIDATE 1:\na")):
            autopilot.generate_candidates(RUN47_MEDIA, autopilot.POST_TYPES[0], {"project_url": None})
        prompt = captured["prompt"]
        self.assertEqual(len(re.findall(r"SUBJECT: ", prompt)), autopilot.CANDIDATE_COUNT)
        self.assertIn("SAFE CLAIMS", prompt)
        self.assertIn(cm.mechanics_line(RUN47_MEDIA), prompt)
        self.assertIn("Never say where anything sits", prompt)
        self.assertNotIn("FIRST ROUND", prompt)


class FactCaptionTests(unittest.TestCase):
    def numbers(self, text):
        return set(re.findall(r"\d+", text))

    def check(self, media, post_type="solution"):
        captions = cp.fact_captions(media)
        self.assertTrue(captions)
        verified_numbers = self.numbers(" ".join(cm.media_facts(media)) + " " + str(media.get("description") or ""))
        for caption in captions:
            with self.subTest(caption=caption):
                self.assertEqual(cq.quality_problems(caption, media, post_type), [])
                self.assertEqual(game_terms.check_mechanics(caption, media), [])
                self.assertEqual(cm.text_problems(caption, media), [])
                self.assertEqual(post_rules.validate_post(caption), [])
                self.assertEqual(cq.invented_detail_problems(caption), [])
                self.assertTrue(self.numbers(caption) <= verified_numbers, "a number that is not in the verified facts")
                if not media.get("theme"):
                    self.assertNotIn("theme,", caption)
        kept, dropped = cq.distinct_candidates(captions)
        self.assertEqual(dropped, [], "the verified-fact captions must differ from each other in substance")
        return captions

    def test_run47_media(self):
        captions = self.check(RUN47_MEDIA)
        self.assertTrue(any(c.startswith("9 moves, then BREACHED") for c in captions))

    def test_unsolved_still(self):
        captions = self.check(RUN46_MEDIA, "challenge")
        self.assertFalse(any("BREACHED" in c for c in captions))

    def test_no_moves_claim_unless_the_description_states_it(self):
        media = dict(RUN47_MEDIA, description="Level 20 (Easy) with Mirror, Block, Portal gimmicks.")
        self.assertFalse(any("moves" in c for c in self.check(media)))

    def test_with_nothing_specific_verified_only_the_identity_caption_is_offered(self):
        bare = {"type": "image", "description": "A purple-toned screenshot."}
        captions = self.check(bare, "visual")
        self.assertEqual(len(captions), 1)
        self.assertTrue(cp.is_identity_only(captions[0], bare))

    def test_rotation_changes_which_one_leads(self):
        self.assertNotEqual(cp.fact_captions(RUN47_MEDIA, 0)[0], cp.fact_captions(RUN47_MEDIA, 1)[0])


class FeedbackTests(unittest.TestCase):
    ATTEMPTS = [
        {"candidate": "repeat", "text": RUN47_CANDIDATES[0], "outcome": "dropped before screening",
         "reason": "repeats an earlier post (same opening and the same words after it)"},
        {"candidate": "candidate 1", "text": RUN47_CANDIDATES[2], "outcome": "rejected before the critic",
         "reason": "failed post rules: states what a game object does or how it moves ('block shift')"},
        {"candidate": "revision", "text": PLAIN_29, "outcome": "critic rejected", "score": 29,
         "reasons": ["critic said KEEP: NO", "score 29/60 is below the minimum 30"]},
    ]

    def test_a_29_out_of_60_rejection_is_a_near_miss_and_far_lower_is_not(self):
        self.assertEqual(cp.near_miss(self.ATTEMPTS, 30)["text"], PLAIN_29)
        far = [dict(self.ATTEMPTS[2], score=18)]
        self.assertIsNone(cp.near_miss(far, 30))

    def test_the_second_round_is_told_what_failed_and_how_to_aim_better(self):
        feedback = cp.retry_feedback(self.ATTEMPTS, 30)
        for text in (RUN47_CANDIDATES[0], RUN47_CANDIDATES[2], PLAIN_29):
            self.assertIn(text, feedback)
        self.assertIn("score 29/60, minimum 30", feedback)
        self.assertIn("Do not add facts", feedback)
        self.assertIn("none of the recent posts", feedback)

    def test_diagnosis_is_plain_and_says_nothing_was_published(self):
        diagnosis = cp.diagnose(self.ATTEMPTS, 30, rounds=2, critic_calls=1)
        self.assertIn("Nothing was published", diagnosis["summary"])
        self.assertIn("Best critic score 29/60 (minimum 30)", diagnosis["summary"])
        self.assertEqual(diagnosis["by_reason"]["repeat"], 1)
        self.assertTrue(diagnosis["near_miss"])

    def test_the_minimum_score_is_unchanged(self):
        self.assertEqual(autopilot.MIN_TOTAL_SCORE, 30)
        keep, reasons = autopilot.critique_verdict(critique(['- "Level 20" => MEDIA evidence: "It is Level 20 (Easy)."'], 29, "YES"),
                                                   sources=cm.evidence_sources(RUN47_MEDIA, KNOWLEDGE))
        self.assertFalse(keep)
        self.assertTrue(any("below the minimum 30" in r for r in reasons))


# ----------------------------------------------------------------------------------------------------------------------
# The whole preview pipeline, replaying run #47
# ----------------------------------------------------------------------------------------------------------------------

class Run47ReplayTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        mock.patch.object(autopilot, "posts_history", f"## Post 027\n\n{EARLIER_POST}\n\n### Critic\n\nok\n").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.candidates = mock.patch.object(autopilot, "generate_candidates").start()
        self.critic = FakeCritic()
        mock.patch.object(autopilot, "critique_post", side_effect=self.critic).start()
        self.tags_file.write_text(json.dumps({self.video_rel: {"description": RUN47_DESC, "type": "video"}}), encoding="utf-8")
        item = mm.make_item(self.video_rel, base_dir=self.tmp, media_type="video", description=RUN47_DESC, mode="easy",
                            level=20, theme="Autumn Millstone", mechanisms=["Mirror", "Block", "Portal"],
                            mechanisms_source="game-data", puzzle_state="solved", shows_action=True, verification="verified",
                            technical={"width": 1080, "height": 1350, "aspect": "4:5", "bytes": 6000, "duration": 10.2,
                                       "sha256": None},
                            validation={"status": "ok", "checked_at": None, "problems": []}, hashes=[])
        mm.save_manifest({"schema": mm.SCHEMA, "items": [item]}, self.tmp / "media" / "manifest.json")
        self.draft_path = self.tmp / "d.json"

    def run_dry(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            autopilot.main(["--dry-run", "--draft-file", str(self.draft_path)])
        return out.getvalue()

    def report(self):
        return json.loads(self.draft_path.read_text(encoding="utf-8"))

    def test_the_media_facts_match_run47(self):
        self.candidates.return_value = []
        self.critic.default = reject_unsupported()
        self.run_dry()
        media = self.report()["media"]
        self.assertEqual(media["file"], self.video_rel)
        self.assertIn("Autumn Millstone", media["description"])

    def test_run47_recovers_with_a_grounded_caption_that_the_critic_approved(self):
        # The writer makes the same mistakes in both rounds; the verified-fact captions are the floor.
        self.candidates.side_effect = [list(RUN47_CANDIDATES), list(RUN47_CANDIDATES)]
        self.critic.default = approve(36)
        output = self.run_dry()
        self.assertIn("DRAFT COMPLETE", output)
        draft = self.report()
        # None of the three rejected captions reached the critic or the draft.
        self.assertFalse(set(RUN47_CANDIDATES) & set(self.critic.seen))
        self.assertNotIn(draft["text"], RUN47_CANDIDATES)
        # The winner was reviewed by the critic, scored at least the minimum, and states only verified facts.
        self.assertIn(draft["text"], self.critic.seen)
        self.assertGreaterEqual(draft["critic_score"], autopilot.MIN_TOTAL_SCORE)
        self.assertEqual(cq.invented_detail_problems(draft["text"]), [])
        self.assertTrue(set(game_terms.mechanics_mentioned(draft["text"])) <= {"Mirror", "Block", "Portal"})
        self.assertEqual(self.candidates.call_count, 2)
        self.assertTrue(any(a.get("round") == 2 and a["outcome"] == "critic KEEP" for a in draft["attempts"]))

    def test_a_29_out_of_60_near_miss_triggers_a_better_second_round_not_a_lower_bar(self):
        better = ("Autumn Millstone is only the paint; Level 20 on Easy is the puzzle under it. "
                  "Ringshift's visual themes only change the look. #Ringshift")
        self.candidates.side_effect = [[PLAIN_29], [better]]
        self.critic.verdicts = {
            PLAIN_29: critique(['- "Level 20 in the Autumn Millstone theme" => MEDIA evidence: "Visual theme: Autumn Millstone."'],
                               29, "NO"),
            "only the paint": approve(38),
        }
        self.critic.default = reject_unsupported()
        output = self.run_dry()
        self.assertIn("Near miss", output)
        self.assertIn("DRAFT COMPLETE", output)
        draft = self.report()
        self.assertEqual(draft["text"], better)
        self.assertEqual(draft["critic_score"], 38)
        self.assertEqual(self.critic.seen.count(PLAIN_29), 1)              # never re-reviewed, never drafted
        feedback = self.candidates.call_args_list[1].kwargs["feedback"]
        self.assertIn("score 29/60, minimum 30", feedback)
        self.assertIn(PLAIN_29, feedback)

    def test_a_second_round_that_repeats_the_first_costs_no_critic_calls(self):
        self.candidates.side_effect = [[PLAIN_29], [PLAIN_29, PLAIN_29.replace("#indiegames", "")]]
        self.critic.verdicts = {PLAIN_29.split(" #")[0]: critique(['- "x" => MEDIA evidence: "Visual theme: Autumn Millstone."'], 29, "NO")}
        self.critic.default = reject_unsupported()
        self.run_dry()
        self.assertEqual(self.critic.seen.count(PLAIN_29), 1)
        reasons = [a.get("reason") or "" for a in self.report()["attempts"] if a.get("round") == 2]
        self.assertTrue(any("already tried" in r or "repeat" in r for r in reasons), reasons)

    def test_a_writer_outage_in_the_second_round_still_reviews_the_verified_fact_captions(self):
        self.candidates.side_effect = [list(RUN47_CANDIDATES), RuntimeError("Gemini 503")]
        self.critic.default = approve(34)
        output = self.run_dry()
        self.assertIn("second writing round failed", output)
        self.assertIn("DRAFT COMPLETE", output)
        self.assertIn(self.report()["text"], cp.fact_captions(dict(RUN47_MEDIA), autopilot.read_counter()))

    def test_endless_rejection_ends_in_a_clear_diagnosis_with_bounded_work(self):
        self.candidates.side_effect = [[PLAIN_29], ["Autumn Millstone on Level 20, plainly. #Ringshift"]]
        self.critic.default = critique(['- "x" => MEDIA evidence: "Visual theme: Autumn Millstone."'], 29, "NO",
                                       improved="Level 20, Easy, Autumn Millstone. #Ringshift")
        output = self.run_dry()
        self.assertIn("NO CANDIDATE PASSED", output)
        self.assertIn("--- DIAGNOSIS ---", output)
        report = self.report()
        self.assertEqual(report["result"], "no_candidate_passed")
        self.assertNotIn("draft_id", report)
        self.assertIn("Nothing was published", report["diagnosis"]["summary"])
        self.assertIn("Best critic score 29/60 (minimum 30)", report["diagnosis"]["summary"])
        self.assertEqual(report["diagnosis"]["rounds"], 2)
        self.assertEqual(self.candidates.call_count, autopilot.MAX_GENERATION_ROUNDS)
        self.assertLessEqual(len(self.critic.seen), autopilot.MAX_CRITIC_CALLS)
        self.assertEqual(self.post.call_count, 0)


class SpecificFactsUnavailableTests(OfflineTestCase):
    """Media with no level, theme or mechanic verified: only the game's identity can be said, and that is allowed."""

    def setUp(self):
        super().setUp()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.candidates = mock.patch.object(autopilot, "generate_candidates").start()
        self.critic = FakeCritic()
        mock.patch.object(autopilot, "critique_post", side_effect=self.critic).start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"description": "A purple-toned screenshot of the game."}}),
                                  encoding="utf-8")
        self.draft_path = self.tmp / "d.json"

    def test_a_truthful_identity_caption_passes_when_nothing_specific_can_be_verified(self):
        identity = "Rings and beams around a circular puzzle, in purple this time. #Ringshift"
        self.candidates.side_effect = [["Level 3 hides a Prism behind the lock."], [identity]]
        self.critic.verdicts = {identity: critique(
            ['- "rings and beams around a circular puzzle" => KNOWLEDGE evidence: "rings and beams around a circular puzzle"',
             '- "purple" => MEDIA evidence: "purple-toned screenshot"'], 34, "YES")}
        self.critic.default = reject_unsupported()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            autopilot.main(["--dry-run", "--draft-file", str(self.draft_path)])
        draft = json.loads(self.draft_path.read_text(encoding="utf-8"))
        self.assertEqual(draft.get("text"), identity, out.getvalue()[-2000:])
        self.assertNotIn("Level 3 hides a Prism behind the lock.", self.critic.seen)
        media = {"type": "image", "description": "A purple-toned screenshot of the game."}
        self.assertTrue(cp.is_identity_only(identity, media))
        # The same text passes the publish-time re-check (prepare_approved runs screen_candidate again).
        self.assertIsNone(autopilot.screen_candidate(identity, [], {"project_url": None}, media))

    def test_an_identity_caption_the_critic_rejects_is_still_not_drafted(self):
        self.candidates.side_effect = [["A Prism next to a Mirror."], ["Rings and beams around a circular puzzle. #Ringshift"]]
        self.critic.default = reject_unsupported(total=33)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            autopilot.main(["--dry-run", "--draft-file", str(self.draft_path)])
        self.assertIn("NO CANDIDATE PASSED", out.getvalue())


# ----------------------------------------------------------------------------------------------------------------------
# The unattended daily run (strict media mode), end to end
# ----------------------------------------------------------------------------------------------------------------------

import approvals  # noqa: E402
import os  # noqa: E402
import test_approvals as approval_fixture  # noqa: E402  (a module, so none of its test classes run twice from here)


class UnattendedRecoveryTests(approval_fixture.DraftTestCase):
    """The same fixture as test_auto_publish.AutoPublishTests: strict media checks, a fake raw GitHub, Buffer mocked."""

    def setUp(self):
        super().setUp()
        self.candidates = mock.patch.object(autopilot, "generate_candidates").start()
        self.critic_fake = FakeCritic()
        mock.patch.object(autopilot, "critique_post", side_effect=self.critic_fake).start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"tags": ["black hole"], "description": approval_fixture.DESCRIPTION}}),
                                  encoding="utf-8")
        self.days_dir = self.tmp / "data" / "used_days"
        mock.patch.object(approvals, "default_claimer", lambda *_: approvals.LocalFileClaim(self.claims_dir)).start()
        mock.patch.object(approvals, "day_claimer", lambda *_: approvals.LocalFileClaim(self.days_dir)).start()
        mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "777"}).start()
        os.environ.pop("RINGSHIFT_AUTOPUBLISH", None)
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

    def result(self):
        return json.loads(self.result_file.read_text(encoding="utf-8"))

    def test_after_two_rejected_rounds_a_verified_caption_is_published_once_through_every_gate(self):
        bad = ["Watching the Block shift right before the Portal catches the beam on Level 11. #Ringshift",
               "The Echo sits right beside the Portal on Level 11. #Ringshift"]
        self.candidates.side_effect = [list(bad), list(bad)]
        self.critic_fake.default = approve(36)
        error, output = self.auto()
        self.assertIsNone(error, output[-3000:])
        self.assertEqual(self.post.call_count, 1)
        sent = self.sent()["text"]
        self.assertNotIn(sent, bad)
        self.assertIn(sent, self.critic_fake.seen)
        self.assertEqual(cq.invented_detail_problems(sent), [])
        self.assertEqual(self.result()["status"], "queued")

    def test_no_acceptable_caption_means_no_post_a_failed_run_and_a_diagnosis(self):
        self.candidates.side_effect = [["Level 11 on Hard, in purple. #Ringshift"], ["Level 11 on Hard. #Ringshift"]]
        self.critic_fake.default = critique(['- "x" => MEDIA evidence: "It is Level 11 (Hard)."'], 29, "NO")
        error, output = self.auto()
        self.assertEqual(error.code, 1)
        self.assertEqual(self.post.call_count, 0)
        result = self.result()
        self.assertEqual(result["status"], "no_draft")
        self.assertIn("Nothing was published", result["reason"])
        self.assertIn("29/60", result["reason"])
        self.assertEqual(result["diagnosis"]["rounds"], 2)
        self.assertIn("NOT PUBLISHED", output)
        self.assertFalse(list(self.days_dir.glob("*")) if self.days_dir.exists() else [], "no day may be claimed")


if __name__ == "__main__":
    unittest.main()
