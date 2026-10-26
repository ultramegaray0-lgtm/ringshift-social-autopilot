"""The content critic: game vocabulary, grounded prompts, claim-based verdicts, one revision round."""

import contextlib
import io
import json
import unittest
from unittest import mock

import support
from support import OfflineTestCase
import autopilot
import game_terms
import media_selector

DESC = "Level 11 in a purple-toned theme, with Echo, Portal and Block gimmicks."
SCORES = ("Hook: 7\nCuriosity: 7\nNatural tone: 8\nRingshift relevance: 8\nOriginality: 7\nViral potential: 5\n")


def critique(claims="NONE", keep="YES", scores=SCORES, improved="Not needed."):
    claim_block = claims if claims == "NONE" else "\n".join(claims)
    return f"CLAIMS:\n{claim_block}\n\n{scores}\nKEEP: {keep}\n\nMAIN PROBLEM:\nNot needed.\n\nIMPROVED VERSION:\n{improved}\n"


class VocabularyTests(unittest.TestCase):
    def test_mechanic_names_are_found_in_every_written_form(self):
        found = game_terms.mechanics_mentioned("Mirrors, a Half-Block, one Block, the prisms and Echoes; portal!")
        self.assertEqual(found, ["Mirror", "Portal", "Block", "Half-Block", "Prism", "Echo"])

    def test_half_block_is_not_also_counted_as_block(self):
        self.assertEqual(game_terms.mechanics_mentioned("a half block"), ["Half-Block"])
        self.assertEqual(game_terms.mechanics_mentioned("half-blocks"), ["Half-Block"])

    def test_ordinary_words_are_not_mechanics(self):
        self.assertEqual(game_terms.mechanics_mentioned("blockbuster, mirrored? echoing mirrorball, prismatic"), ["Echo"])

    def test_names_the_game_does_not_use_are_flagged(self):
        self.assertEqual(game_terms.unknown_terms("Black holes and a Satellite, plus a wormhole"),
                         ["black hole", "satellite", "wormhole"])
        self.assertEqual(game_terms.unknown_terms("Portal and Echo"), [])

    def test_a_post_may_only_name_mechanics_the_media_shows(self):
        media = {"description": DESC}
        self.assertEqual(game_terms.check_mechanics("Portal and Echo in purple", media), [])
        self.assertEqual(game_terms.check_mechanics("No mechanic names here at all", media), [])
        problems = game_terms.check_mechanics("That Prism and Mirror combo", media)
        self.assertIn("Prism, Mirror", problems[0].replace("Mirror, Prism", "Prism, Mirror"))
        self.assertIn("Portal, Block, Echo", problems[0])

    def test_unknown_names_are_rejected_even_when_the_real_mechanic_is_shown(self):
        problems = game_terms.check_mechanics("That black hole is something", {"description": DESC})
        self.assertTrue(any("black hole" in p for p in problems))

    def test_media_without_a_verified_mechanic_list_allows_no_mechanic_names(self):
        problems = game_terms.check_mechanics("Nice Portal", {"description": "A purple theme."})
        self.assertTrue(any("no verified mechanic list" in p for p in problems))
        self.assertEqual(game_terms.check_mechanics("Nice purple glow", {"description": "A purple theme."}), [])


class MediaBlockTests(unittest.TestCase):
    def test_internal_search_tags_are_never_shown_to_the_writer(self):
        media = {"type": "video", "description": DESC, "tags": ["black hole", "satellite", "mechanism"]}
        block = media_selector.describe_media(media)
        for leaked in ("black hole", "satellite", "mechanism"):
            self.assertNotIn(leaked, block)
        self.assertIn("Echo", block)
        self.assertIn("use exactly these names", block)
        self.assertIn(DESC, block)

    def test_media_without_mechanics_says_to_name_none(self):
        block = media_selector.describe_media({"type": "image", "description": "A purple theme."})
        self.assertIn("do not name any mechanic", block)

    def test_undescribed_media_is_not_described(self):
        block = media_selector.describe_media({"type": "image"})
        self.assertIn("not described", block)


class VerdictTests(unittest.TestCase):
    def verdict(self, text):
        return autopilot.critique_verdict(text)

    def test_supported_post_is_kept(self):
        keep, reasons = self.verdict(critique(['- "purple-toned theme" => MEDIA', '- "looks sharp" => OPINION']))
        self.assertTrue(keep, reasons)

    def test_no_factual_claims_is_fine(self):
        self.assertTrue(self.verdict(critique("NONE"))[0])
        self.assertTrue(self.verdict("CLAIMS: NONE\n" + SCORES + "KEEP: YES\n")[0])

    def test_one_unsupported_claim_rejects_even_if_the_model_says_keep_yes(self):
        keep, reasons = self.verdict(critique(['- "purple-toned" => MEDIA', '- "the echo changes everything" => UNSUPPORTED']))
        self.assertFalse(keep)
        self.assertTrue(any("unsupported claim: the echo changes everything" in r for r in reasons))

    def test_a_missing_claim_list_rejects(self):
        keep, reasons = self.verdict(SCORES + "KEEP: YES\n")
        self.assertFalse(keep)
        self.assertTrue(any("did not list" in r for r in reasons))

    def test_unreadable_labels_reject(self):
        self.assertFalse(self.verdict(critique(['- "x" => PROBABLY_FINE']))[0])

    def test_a_claim_line_without_a_label_is_not_silently_ignored(self):
        keep, reasons = self.verdict(critique(['- "it is the hardest level ever"']))
        self.assertFalse(keep)
        self.assertTrue(any("unreadable" in r for r in reasons))

    def test_a_claim_list_labelled_step_1_is_still_read_and_still_rejects(self):
        text = ('STEP 1:\n- "Would you have placed the mirror like this?" => UNSUPPORTED (implies placement)\n'
                '- Game name is Ringshift => KNOWLEDGE\n\nSTEP 2:\n' + SCORES + '\nSTEP 3:\nKEEP: YES\n')
        listed, claims = autopilot.parse_claims(text)
        self.assertTrue(listed)
        self.assertEqual([c[1] for c in claims], ["UNSUPPORTED", "KNOWLEDGE"])
        keep, reasons = autopilot.critique_verdict(text)
        self.assertFalse(keep)
        self.assertTrue(any("unsupported claim" in r for r in reasons))
        self.assertFalse(any("did not list" in r for r in reasons))

    def test_a_mislabelled_but_clean_list_can_be_kept(self):
        text = 'STEP 1:\n- "purple theme" => MEDIA\n\nSTEP 2:\n' + SCORES + '\nKEEP: YES\n'
        self.assertTrue(autopilot.critique_verdict(text)[0])

    def test_keep_no_rejects(self):
        self.assertFalse(self.verdict(critique(keep="NO"))[0])

    def test_dull_posts_are_rejected_even_when_every_claim_is_supported(self):
        low = "Hook: 3\nCuriosity: 3\nNatural tone: 4\nRingshift relevance: 5\nOriginality: 3\nViral potential: 2\n"
        keep, reasons = self.verdict(critique(scores=low))
        self.assertFalse(keep)
        self.assertTrue(any("below the minimum" in r for r in reasons))

    def test_scores_written_as_fractions_are_read(self):
        text = critique(scores=SCORES.replace(": 7", ": 7/10"))
        self.assertEqual(autopilot.get_score(text), 7 + 7 + 8 + 8 + 7 + 5)

    def test_claims_parsing_ignores_later_sections(self):
        listed, claims = autopilot.parse_claims(critique(['- "a" => MEDIA']))
        self.assertTrue(listed)
        self.assertEqual(claims, [("a", "MEDIA")])

    def test_improved_version_is_extracted_and_not_needed_is_none(self):
        self.assertEqual(autopilot.improved_version(critique(keep="NO", improved='"Watch the beam bend. #Ringshift"')),
                         "Watch the beam bend. #Ringshift")
        self.assertIsNone(autopilot.improved_version(critique()))
        self.assertIsNone(autopilot.improved_version("no such section"))


class SolvedStillTests(OfflineTestCase):
    def test_question_posts_do_not_get_a_screenshot_that_already_shows_the_solution(self):
        self.tags_file.write_text(json.dumps({
            self.image_rel: {"description": "Level 15 with Mirror gimmicks. The level has just been solved and the BREACHED screen is showing."},
            self.video_rel: {"description": "Level 15 with Mirror gimmicks. Real in-game play ending on the BREACHED screen.", "type": "video"},
        }), encoding="utf-8")
        for _ in range(5):
            chosen = media_selector.select_media("", require_description=True, avoid_solved_stills=True)
            self.assertEqual(chosen["file"], self.video_rel)
        types = {t["id"]: t.get("avoid_solved_still") for t in autopilot.POST_TYPES}
        self.assertTrue(types["challenge"] and types["hook"])
        self.assertFalse(types["visual"])

    def test_if_only_solved_stills_exist_one_is_still_available(self):
        self.tags_file.write_text(json.dumps({
            self.image_rel: {"description": "just been solved"}, self.video_rel: {"description": "just been solved"}}),
            encoding="utf-8")
        self.assertIn(media_selector.select_media("", avoid_solved_stills=True)["file"], (self.image_rel, self.video_rel))


class PromptTests(OfflineTestCase):
    def prompt_for(self, media):
        captured = {}

        def fake(prompt):
            captured["prompt"] = prompt
            return mock.Mock(text="CANDIDATE 1:\na\n\nCANDIDATE 2:\nb\n\nCANDIDATE 3:\nc")

        with mock.patch.object(autopilot, "generate_with_retry", side_effect=fake):
            autopilot.generate_candidates(media, autopilot.POST_TYPES[0], {"project_url": None})
        return captured["prompt"]

    def test_the_writer_gets_the_facts_and_the_rules_and_no_internal_tags(self):
        prompt = self.prompt_for({"type": "video", "description": DESC, "tags": ["black hole", "satellite"],
                                  "file": "x", "url": "u"})
        self.assertIn(DESC, prompt)
        self.assertIn("Mechanics on screen (use exactly these names", prompt)
        self.assertIn("not \"black hole\", \"satellite\"", prompt)            # only inside the prohibition
        self.assertEqual(prompt.count("black hole"), 1)
        self.assertIn("Never imply a solution exists or is unique", prompt)
        self.assertIn("Never say a theme changes gameplay", prompt)

    def test_the_critic_lists_claims_and_decides_with_the_same_facts(self):
        captured = {}
        with mock.patch.object(autopilot, "generate_with_retry",
                               side_effect=lambda p: captured.update(prompt=p) or mock.Mock(text=" ok ")):
            autopilot.critique_post("A post", {"type": "image", "description": DESC, "tags": ["satellite"]},
                                    autopilot.POST_TYPES[0])
        prompt = captured["prompt"]
        self.assertIn("CLAIMS:", prompt)
        self.assertIn("UNSUPPORTED", prompt)
        self.assertIn("When unsure, choose UNSUPPORTED", prompt)
        self.assertIn(DESC, prompt)
        self.assertNotIn("satellite", prompt)


class PipelineTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.candidates = mock.patch.object(autopilot, "generate_candidates").start()
        self.critic = mock.patch.object(autopilot, "critique_post").start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.tags_file.write_text(json.dumps({self.image_rel: {"tags": ["black hole"], "description": DESC}}),
                                  encoding="utf-8")

    def run_dry(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            autopilot.main(["--dry-run"])
        return out.getvalue()

    def test_a_post_naming_an_unshown_mechanic_never_reaches_the_critic(self):
        self.candidates.return_value = ["That Prism bounce is lovely", "Satellite vibes in purple", "A black hole appears"]
        output = self.run_dry()
        self.assertIn("NO CANDIDATE PASSED", output)
        self.critic.assert_not_called()

    def test_an_unsupported_claim_blocks_a_post_the_model_liked(self):
        self.candidates.return_value = ["Purple glow around the Portal."]
        self.critic.return_value = critique(['- "Portal" => MEDIA', '- "nothing else matters" => UNSUPPORTED'])
        self.assertIn("NO CANDIDATE PASSED", self.run_dry())

    def test_the_critics_rewrite_gets_one_chance_and_is_checked_like_any_candidate(self):
        self.candidates.return_value = ["Purple Portal glow, changes everything."]
        self.critic.side_effect = [
            critique(['- "changes everything" => UNSUPPORTED'], keep="NO", improved="Purple glow around the Portal."),
            critique(['- "purple" => MEDIA evidence: "purple-toned theme"']),
        ]
        output = self.run_dry()
        self.assertIn("critic's rewrite", output)
        self.assertIn("DRAFT COMPLETE", output)
        self.assertIn("Purple glow around the Portal.", output)
        self.assertEqual(self.critic.call_count, 2)

    def test_a_rewrite_that_breaks_the_rules_is_not_used(self):
        self.candidates.return_value = ["Purple Portal glow, changes everything."]
        self.critic.return_value = critique(['- "x" => UNSUPPORTED'], keep="NO", improved="A black hole in purple.")
        output = self.run_dry()
        self.assertIn("NO CANDIDATE PASSED", output)
        self.assertEqual(self.critic.call_count, 1)

    def test_a_rewrite_the_critic_rejects_is_not_used_either(self):
        self.candidates.return_value = ["Purple Portal glow, changes everything."]
        self.critic.return_value = critique(['- "x" => UNSUPPORTED'], keep="NO", improved="Purple calm around the Portal.")
        self.assertIn("NO CANDIDATE PASSED", self.run_dry())
        self.assertEqual(self.critic.call_count, 2)         # original + its one rewrite, then it stops

    def test_critic_calls_stay_bounded(self):
        self.candidates.return_value = ["Purple Portal one.", "Purple Echo two.", "Purple Block three."]
        self.critic.return_value = critique(['- "x" => UNSUPPORTED'], keep="NO", improved="Purple Portal four.")
        self.run_dry()
        self.assertLessEqual(self.critic.call_count, 4)

    def test_a_rejected_post_is_never_turned_into_a_draft_or_sent(self):
        self.candidates.return_value = ["Purple Portal hype."]
        self.critic.return_value = critique(['- "x" => UNSUPPORTED'], keep="NO")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            autopilot.main(["--dry-run", "--draft-file", str(self.tmp / "d.json")])
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(json.loads((self.tmp / "d.json").read_text(encoding="utf-8"))["result"], "no_candidate_passed")


if __name__ == "__main__":
    unittest.main()
