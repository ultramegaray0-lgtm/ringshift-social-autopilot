"""Media manifest, post-type/media matching, evidence-checked claims, repetition, failure reports and capture helpers.

All offline: no browser, no network, no API keys. Records are built with explicit technical/validation data so no
FFmpeg is needed; real browser capture is covered by test_capture.py when a game file is available."""

import contextlib
import copy
import io
import json
import os
import random
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import support
from support import OfflineTestCase
import autopilot
import capture_gameplay as cg
import content_matching
import game_terms
import media_manifest as mm
import media_selector
import media_tools as tools

OK = {"status": "ok", "checked_at": None, "problems": []}
TECH = {"width": 1080, "height": 1350, "aspect": "4:5", "bytes": 3, "duration": None, "sha256": None}

SOLVED_DESC = "Level 15 (Hard) with Mirror, Block gimmicks. The level is solved: it ends on the BREACHED screen."
OPEN_DESC = "Level 7 (Hard) with Portal and Echo gimmicks. The puzzle is shown unsolved."


def record(relative, media_type="image", description=OPEN_DESC, state="unsolved", action=False, **kw):
    kw.setdefault("verification", "reviewed")
    technical = dict(TECH, duration=6.0 if media_type == "video" else None)
    return mm.make_item(relative, base_dir=kw.pop("base_dir", Path("/nonexistent")), media_type=media_type,
                        description=description, puzzle_state=state, shows_action=action, mode="hard", level=7,
                        technical=technical, validation=copy.deepcopy(OK), hashes=[], **kw)


class CategoryTests(unittest.TestCase):
    def cats(self, **kw):
        return record("media/a.png", **kw)["post_categories"]

    def test_unsolved_media_can_carry_a_challenge_but_not_a_solution(self):
        cats = self.cats()
        self.assertIn("puzzle_challenge", cats)
        self.assertNotIn("solution_reveal", cats)

    def test_solved_media_never_carries_a_challenge(self):
        cats = self.cats(description=SOLVED_DESC, state="solved")
        self.assertIn("solution_reveal", cats)
        self.assertNotIn("puzzle_challenge", cats)

    def test_unknown_state_never_carries_a_challenge_or_a_solution(self):
        cats = self.cats(state="unknown")
        self.assertNotIn("puzzle_challenge", cats)
        self.assertNotIn("solution_reveal", cats)
        self.assertIn("visual_showcase", cats)

    def test_a_video_without_visible_action_is_not_a_gameplay_moment_or_challenge(self):
        cats = self.cats(media_type="video", action=False)
        self.assertNotIn("gameplay_moment", cats)
        self.assertNotIn("puzzle_challenge", cats)
        cats = self.cats(media_type="video", action=True)
        self.assertIn("gameplay_moment", cats)
        self.assertIn("puzzle_challenge", cats)

    def test_excluded_special_mode_undescribed_and_failed_media_supports_nothing(self):
        self.assertEqual(self.cats(exclude=True), [])
        self.assertEqual(self.cats(special_mode=True), [])
        self.assertEqual(self.cats(description=""), [])
        failed = record("media/a.png")
        failed["validation"] = {"status": "failed", "problems": ["x"]}
        self.assertEqual(mm.categories_for(failed), [])


class MatchingTests(unittest.TestCase):
    def test_a_challenge_never_gets_media_that_shows_the_solution(self):
        solved = record("media/s.mp4", "video", SOLVED_DESC, "solved", True)
        self.assertIsNotNone(content_matching.why_not(solved, "challenge"))
        # A hook may use a clip with visible play even when it ends solved ("gameplay moment"); the text check
        # (TextVersusMediaTests) then forbids asking the viewer to solve what the clip shows solved.
        self.assertIsNone(content_matching.why_not(solved, "hook"))
        self.assertIsNone(content_matching.why_not(solved, "solution"))
        self.assertIsNone(content_matching.why_not(solved, "moment"))

    def test_hook_and_moment_need_a_video(self):
        still = record("media/a.png")
        self.assertIn("needs a video", content_matching.why_not(still, "hook"))
        self.assertIn("needs a video", content_matching.why_not(still, "moment"))

    def test_mechanic_posts_need_a_verified_mechanism(self):
        no_mech = record("media/a.png", description="A purple level, unsolved.")
        self.assertIsNotNone(content_matching.why_not(no_mech, "mechanic"))
        self.assertIsNone(content_matching.why_not(record("media/a.png"), "mechanic"))

    def test_progress_posts_need_verified_facts(self):
        t = {"id": "progress"}
        self.assertFalse(content_matching.post_type_available(t, {}, []))
        fact = {"id": "f", "kind": "progress", "statement": "Ten levels were added.", "source": "changelog"}
        self.assertTrue(content_matching.post_type_available(t, {}, [fact]))
        self.assertFalse(content_matching.post_type_available({"id": "behind_scenes"}, {}, [fact]))

    def test_malformed_project_facts_are_ignored_not_repaired(self):
        tmp = Path(support.tempfile.mkdtemp())
        path = tmp / "f.json"
        path.write_text(json.dumps({"facts": [
            {"id": "a", "kind": "progress", "statement": "ok", "source": "s"},
            {"id": "b", "kind": "rumour", "statement": "bad kind", "source": "s"},
            {"id": "c", "kind": "progress", "statement": "", "source": "s"},
            {"id": "d", "kind": "progress", "statement": "no source"},
            "junk"]}), encoding="utf-8")
        self.assertEqual([f["id"] for f in content_matching.load_project_facts(path)], ["a"])
        path.write_text("not json", encoding="utf-8")
        self.assertEqual(content_matching.load_project_facts(path), [])
        self.assertEqual(content_matching.load_project_facts(tmp / "missing.json"), [])


class TextVersusMediaTests(unittest.TestCase):
    solved = {"puzzle_state": "solved", "type": "video"}
    unsolved = {"puzzle_state": "unsolved", "type": "image"}

    def test_asking_to_solve_what_the_media_shows_solved_is_rejected(self):
        for text in ("Can you solve this one?", "Find the solution before the clip ends.", "How would you solve it?"):
            self.assertTrue(content_matching.text_problems(text, self.solved), text)

    def test_claiming_solved_over_an_unsolved_puzzle_is_rejected(self):
        self.assertTrue(content_matching.text_problems("Cleared it on the first try.", self.unsolved))

    def test_motion_words_over_a_still_are_rejected(self):
        self.assertTrue(content_matching.text_problems("Watch the rings turn.", self.unsolved))
        self.assertEqual(content_matching.text_problems("Which ring turns first?", self.unsolved), [])

    def test_a_question_about_an_unsolved_puzzle_is_fine(self):
        self.assertEqual(content_matching.text_problems("Which ring would you turn first?", self.unsolved), [])

    def test_opening_repeat_detection(self):
        recent = ["Which ring would you turn first to light it?"]
        self.assertIsNotNone(content_matching.repeats_opening("Which ring would you rotate?", recent))
        self.assertIsNone(content_matching.repeats_opening("Purple glow around the Portal.", recent))


class OrdinaryWordsTests(unittest.TestCase):
    media = {"description": "A purple level with Portal gimmicks.", "mechanisms": ["Portal"]}

    def test_ordinary_use_of_game_words_is_not_flagged(self):
        self.assertEqual(game_terms.check_mechanics("An echo of last week, still purple", self.media), [])

    def test_game_names_for_unshown_mechanics_are_still_flagged(self):
        self.assertTrue(game_terms.check_mechanics("That Prism bounce is lovely", self.media))

    def test_names_the_game_does_not_use_are_always_flagged(self):
        self.assertTrue(game_terms.check_mechanics("A black hole appears", self.media))


class ManifestValidationTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.manifest = {"schema": mm.SCHEMA, "items": [
            record(self.image_rel, base_dir=self.tmp),
            record(self.video_rel, "video", base_dir=self.tmp, action=True)]}

    def validate(self, manifest=None, **kw):
        kw.setdefault("decode", False)
        kw.setdefault("check_hashes", False)
        return mm.validate_manifest(manifest or self.manifest, self.tmp, **kw)

    def errors(self, **kw):
        return [p for p in self.validate(**kw) if p["level"] == "error"]

    def test_a_consistent_manifest_has_no_errors(self):
        self.assertEqual(self.errors(), [])

    def test_a_missing_file_is_an_error(self):
        (self.tmp / self.image_rel).unlink()
        self.assertTrue(any("missing" in e["message"] for e in self.errors()))

    def test_duplicate_ids_and_paths_are_errors(self):
        self.manifest["items"].append(copy.deepcopy(self.manifest["items"][0]))
        messages = " ".join(e["message"] for e in self.errors())
        self.assertIn("duplicate id", messages)
        self.assertIn("duplicate path", messages)

    def test_a_stale_url_is_an_error(self):
        self.manifest["items"][0]["url"] = "https://example.com/old.png"
        self.assertTrue(any("stale url" in e["message"] for e in self.errors()))

    def test_changed_file_is_detected_by_hash(self):
        self.manifest["items"][0]["sha256"] = "0" * 64
        self.assertTrue(any("sha256" in e["message"] for e in self.errors(check_hashes=True)))

    def test_a_corrupt_file_is_reported_not_crashed_on(self):
        # The fixture bytes are not a real PNG, so decoding must fail cleanly as an error.
        errors = self.errors(decode=True)
        self.assertTrue(any("cannot be used" in e["message"] for e in errors))

    def test_invalid_enum_values_are_errors(self):
        self.manifest["items"][0]["puzzle_state"] = "maybe"
        self.assertTrue(any("puzzle_state" in e["message"] for e in self.errors()))

    def test_non_canonical_mechanism_names_are_errors(self):
        self.manifest["items"][0]["mechanisms"] = ["Black Hole"]
        self.assertTrue(any("canonical" in e["message"] for e in self.errors()))

    def test_descriptions_may_not_use_names_the_game_does_not_use(self):
        self.manifest["items"][0]["description"] = "A satellite orbits."
        self.assertTrue(any("does not use" in e["message"] for e in self.errors()))

    def test_verified_requires_the_capture_tool_and_game_data(self):
        item = self.manifest["items"][0]
        item["verification"]["status"] = "verified"
        messages = " ".join(e["message"] for e in self.errors())
        self.assertIn("not produced by the capture tool", messages)
        self.assertIn("not from game data", messages)

    def test_categories_must_match_the_evidence(self):
        self.manifest["items"][0]["post_categories"] = ["solution_reveal"]
        self.assertTrue(any("post_categories" in e["message"] for e in self.errors()))

    def test_legacy_tag_entries_may_not_be_lost_or_altered(self):
        tags = {self.image_rel: {"tags": ["x"], "description": "orig"}}
        self.assertTrue(any("altered or dropped" in e["message"] for e in self.errors(tags=tags)))
        self.manifest["items"][0]["legacy"] = tags[self.image_rel]
        self.assertEqual(self.errors(tags=tags), [])
        self.assertTrue(any("no manifest record" in e["message"]
                            for e in self.errors(tags={**tags, "media/gone.png": {"tags": []}})))

    def test_unindexed_media_is_only_a_warning(self):
        (self.tmp / "media" / "gameplay" / "new.png").write_bytes(b"x")
        warnings = [p for p in self.validate() if p["level"] == "warning"]
        self.assertTrue(any("not in the manifest" in w["message"] for w in warnings))

    def test_url_check_reports_dead_links_with_an_injected_stub(self):
        problems = mm.check_urls(self.manifest, lambda url: 404)
        self.assertTrue(problems)
        self.assertFalse(mm.check_urls(self.manifest, lambda url: 200))


class MigrationTests(OfflineTestCase):
    def test_sync_keeps_the_original_entry_and_never_drops_a_file(self):
        original = {"tags": ["black hole"], "description": "A portal level with two black holes."}
        self.tags_file.write_text(json.dumps({self.image_rel: original}), encoding="utf-8")
        manifest_file = self.tmp / "media" / "manifest.json"
        with mock.patch.object(mm, "check_decodes", return_value=("ok", [])), \
                mock.patch.object(mm, "probe_file", return_value=dict(TECH, sha256=mm.sha256_file(self.tmp / self.image_rel))), \
                mock.patch.object(mm, "fingerprints", return_value=[]):
            mm.sync(self.tmp, self.tags_file, manifest_file, validate=True, log=lambda *_: None)
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        by = {i["path"]: i for i in manifest["items"]}
        self.assertEqual(by[self.image_rel]["legacy"]["description"], original["description"])
        self.assertEqual(json.loads(self.tags_file.read_text(encoding="utf-8")), {self.image_rel: original})
        self.assertIn(self.video_rel, by)          # an untagged file is indexed too, just with no post categories
        self.assertEqual(by[self.video_rel]["post_categories"], [])


class SelectionTests(OfflineTestCase):
    """Media is chosen by what the post type needs, and a type with no fitting media raises, never falls back."""

    def write_manifest(self, *items):
        mm.save_manifest({"schema": mm.SCHEMA, "items": list(items)}, self.tmp / "media" / "manifest.json")

    def setUp(self):
        super().setUp()
        self.tags_file.write_text(json.dumps({
            self.image_rel: {"tags": ["portal"], "description": OPEN_DESC},
            self.video_rel: {"tags": ["mirror"], "description": SOLVED_DESC}}), encoding="utf-8")
        self.unsolved_still = record(self.image_rel, base_dir=self.tmp)
        self.solved_clip = record(self.video_rel, "video", SOLVED_DESC, "solved", True, base_dir=self.tmp)

    def select(self, post_type_id):
        return media_selector.select_media("", require_description=True, post_type={"id": post_type_id},
                                            rng=random.Random(1))

    def test_each_post_type_gets_only_media_that_supports_it(self):
        self.write_manifest(self.unsolved_still, self.solved_clip)
        self.assertEqual(self.select("challenge")["file"], self.image_rel)
        self.assertEqual(self.select("solution")["file"], self.video_rel)
        self.assertEqual(self.select("moment")["file"], self.video_rel)

    def test_no_fitting_media_raises_instead_of_using_the_wrong_media(self):
        self.write_manifest(self.solved_clip)                # only a solved clip exists
        with self.assertRaises(media_selector.NoSuitableMediaError):
            self.select("challenge")
        self.write_manifest(self.unsolved_still)             # and with only an unsolved still, nothing shows a solution
        for post_type in ("solution", "moment", "hook"):
            with self.assertRaises(media_selector.NoSuitableMediaError, msg=post_type):
                self.select(post_type)

    def test_media_not_in_the_manifest_is_never_auto_selected_for_a_post_type(self):
        self.write_manifest()
        with self.assertRaises(media_selector.NoSuitableMediaError):
            self.select("visual")

    def test_selection_exposes_the_structured_facts(self):
        self.write_manifest(self.unsolved_still, self.solved_clip)
        media = self.select("challenge")
        self.assertEqual(media["puzzle_state"], "unsolved")
        self.assertEqual(media["level"], 7)
        self.assertIn("The puzzle is NOT solved", media_selector.describe_media(media))

    def test_without_a_manifest_the_legacy_selection_still_works(self):
        media = media_selector.select_media("", require_description=True, post_type={"id": "challenge"})
        self.assertIn(media["file"], (self.image_rel, self.video_rel))


class ChoosePostAndMediaTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.tags_file.write_text(json.dumps({
            self.video_rel: {"tags": ["mirror"], "description": SOLVED_DESC}}), encoding="utf-8")
        mm.save_manifest({"schema": mm.SCHEMA, "items": [
            record(self.video_rel, "video", SOLVED_DESC, "solved", True, base_dir=self.tmp)]},
            self.tmp / "media" / "manifest.json")

    def test_types_without_fitting_media_are_skipped_and_reported(self):
        # counter 0 would be 'challenge', but only a solved clip exists, so it must move on to a type that fits.
        post_type, media, skipped = autopilot.choose_post_and_media({"project_url": None}, [], counter=0)
        self.assertIn(post_type["id"], ("hook", "solution", "moment", "visual", "observation", "feedback", "discovery", "mechanic"))
        self.assertNotEqual(post_type["id"], "challenge")
        self.assertTrue(any(s["post_type"] == "challenge" for s in skipped))

    def test_an_unreadable_manifest_stops_the_draft_instead_of_falling_back(self):
        (self.tmp / "media" / "manifest.json").write_text("{ not json", encoding="utf-8")
        with self.assertRaises(autopilot.NoSuitableMediaError) as raised:
            autopilot.choose_post_and_media({"project_url": None}, [], counter=0)
        self.assertTrue(raised.exception.skipped)

    def test_nothing_fits_raises_with_every_reason(self):
        (self.tmp / "media" / "manifest.json").write_text(json.dumps({"schema": mm.SCHEMA, "items": []}), encoding="utf-8")
        with self.assertRaises(autopilot.NoSuitableMediaError) as raised:
            autopilot.choose_post_and_media({"project_url": None}, [], counter=0)
        self.assertTrue(raised.exception.skipped)


class PinnedMediaTests(OfflineTestCase):
    """A person can pin a preview to one committed file. The pin only narrows the pool: it never widens it and never
    skips a check, and a pinned file that cannot be used means no draft, never a different file."""

    def setUp(self):
        super().setUp()
        self.tags_file.write_text(json.dumps({
            self.image_rel: {"tags": ["portal"], "description": OPEN_DESC},
            self.video_rel: {"tags": ["mirror"], "description": SOLVED_DESC}}), encoding="utf-8")
        mm.save_manifest({"schema": mm.SCHEMA, "items": [
            record(self.image_rel, base_dir=self.tmp),
            record(self.video_rel, "video", SOLVED_DESC, "solved", True, base_dir=self.tmp)]},
            self.tmp / "media" / "manifest.json")

    def select(self, post_type_id, only):
        return media_selector.select_media("", require_description=True, post_type={"id": post_type_id},
                                            rng=random.Random(1), only_files=only)

    def test_a_pin_restricts_the_pool_to_that_file(self):
        self.assertEqual(self.select("solution", {self.video_rel})["file"], self.video_rel)
        self.assertEqual(self.select("challenge", {self.image_rel})["file"], self.image_rel)

    def test_a_pin_never_falls_back_to_another_file_that_would_have_fitted(self):
        # Unpinned, "solution" picks the clip. Pinned to the still (which shows no solution) it must raise, not use the clip.
        self.assertEqual(self.select("solution", None)["file"], self.video_rel)
        with self.assertRaises(media_selector.NoSuitableMediaError):
            self.select("solution", {self.image_rel})

    def test_a_pinned_file_that_does_not_exist_raises(self):
        with self.assertRaises(media_selector.NoSuitableMediaError) as raised:
            self.select("challenge", {"media/gameplay/nope.png"})
        self.assertIn("pinned media", str(raised.exception))

    def test_a_pinned_file_that_fails_inspection_raises_with_the_reason(self):
        (self.tmp / "media" / "gameplay" / "broken.png").write_bytes(b"abc")      # three bytes named .png
        with self.assertRaises(media_selector.NoSuitableMediaError) as raised:
            self.select("challenge", {"media/gameplay/broken.png"})
        self.assertIn("not a valid", str(raised.exception))

    def test_a_pinned_file_marked_exclude_is_refused(self):
        self.tags_file.write_text(json.dumps({
            self.image_rel: {"tags": ["portal"], "description": OPEN_DESC, "exclude": True},
            self.video_rel: {"tags": ["mirror"], "description": SOLVED_DESC}}), encoding="utf-8")
        with self.assertRaises(media_selector.NoSuitableMediaError) as raised:
            self.select("challenge", {self.image_rel})
        self.assertIn("exclude", str(raised.exception))

    def test_choose_post_and_media_only_ever_returns_the_pinned_file(self):
        for counter in range(8):                      # whatever the rotation says, the pinned file is the only one
            _, media, _ = autopilot.choose_post_and_media({"project_url": None}, [], counter=counter, only_file=self.video_rel)
            self.assertEqual(media["file"], self.video_rel, counter)

    def test_the_mandatory_media_check_still_runs_on_the_pinned_file(self):
        seen = []

        def failing_check(candidate):
            seen.append(candidate["file"])
            raise autopilot.MediaUnavailableError("simulated: public bytes differ from the repository file")

        with self.assertRaises(autopilot.NoSuitableMediaError) as raised:
            autopilot.choose_post_and_media({"project_url": None}, [], counter=0, check=failing_check, only_file=self.image_rel)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {self.image_rel})                   # never tried the other file
        self.assertTrue(any("media check failed" in s["reason"] for s in raised.exception.skipped))

    def test_an_unusable_pin_means_no_draft_not_a_different_file(self):
        with self.assertRaises(autopilot.NoSuitableMediaError) as raised:
            autopilot.choose_post_and_media({"project_url": None}, [], counter=0, only_file="media/gameplay/missing.png")
        self.assertTrue(raised.exception.skipped)


class PinnedMediaPathTests(unittest.TestCase):
    def test_plain_repository_paths_are_accepted(self):
        for good in ("media/gameplay/06_hard15_five_mechanisms.png", "media/gameplay/2026-10-09 16-03-44.mp4",
                     "media/captured/clip_1.mp4", " media/gameplay/a.PNG "):
            self.assertEqual(autopilot.validate_pinned_media(good), good.strip(), good)

    def test_anything_else_is_refused(self):
        bad = ["", None, "   ", "gameplay/a.png", "/etc/passwd", "/home/x/media/a.png", "media/../data/post_log.json",
               "media/gameplay/../../x.png", "https://example.com/media/a.png", "media//a.png", "media/gameplay/",
               "media/gameplay/a.txt", "media/gameplay/a.png; rm -rf /", "media/gameplay/a$(id).png",
               "media\\gameplay\\a.png", "media/gameplay/a.png\nmedia/b.png", "media/.hidden.png", "data/a.png"]
        for value in bad:
            with self.assertRaises(SystemExit, msg=repr(value)):
                autopilot.validate_pinned_media(value)


class PinnedMediaCommandLineTests(OfflineTestCase):
    def test_a_pin_is_refused_with_publish_verify_rehearse_or_without_dry_run(self):
        for argv in (["--media-file", "media/gameplay/black_hole.png", "--publish-approved", "d.json", "--approval-code", "d-00000000-0000"],
                     ["--media-file", "media/gameplay/black_hole.png", "--verify-approved", "d.json"],
                     ["--media-file", "media/gameplay/black_hole.png", "--rehearse-claims"],
                     ["--media-file", "media/gameplay/black_hole.png"]):
            with self.assertRaises(SystemExit, msg=argv), contextlib.redirect_stdout(io.StringIO()):
                autopilot.main(argv)

    def test_a_bad_pin_stops_the_draft_before_any_gemini_call(self):
        with mock.patch.object(autopilot, "genai") as genai, self.assertRaises(SystemExit):
            autopilot.main(["--dry-run", "--media-file", "../../etc/passwd"])
        genai.Client.assert_not_called()


class EvidenceTests(unittest.TestCase):
    SOURCES = "Level 11 in a purple-toned theme. Portals teleport a beam. The puzzle is NOT solved in this media."

    def verdict(self, claims, sources=SOURCES):
        text = "CLAIMS:\n" + "\n".join(claims) + "\n\nHook: 8\nCuriosity: 8\nNatural tone: 8\nRingshift relevance: 8\nOriginality: 8\nViral potential: 8\n\nKEEP: YES\n"
        return autopilot.critique_verdict(text, sources=sources)

    def test_a_claim_with_a_real_quote_passes(self):
        keep, _ = self.verdict(['- "purple" => MEDIA evidence: "purple-toned theme"'])
        self.assertTrue(keep)

    def test_a_quote_that_is_not_in_the_sources_fails(self):
        keep, reasons = self.verdict(['- "neon" => MEDIA evidence: "neon rings everywhere"'])
        self.assertFalse(keep)
        self.assertIn("not found", " ".join(reasons))

    def test_a_missing_quote_fails_for_knowledge_and_media(self):
        for label in ("MEDIA", "KNOWLEDGE"):
            keep, _ = self.verdict([f'- "x" => {label}'])
            self.assertFalse(keep, label)

    def test_partial_quotes_joined_with_ellipsis_must_each_exist(self):
        keep, _ = self.verdict(['- "x" => KNOWLEDGE evidence: "Portals teleport ... a beam"'])
        self.assertTrue(keep)
        keep, _ = self.verdict(['- "x" => KNOWLEDGE evidence: "Portals teleport ... a ship"'])
        self.assertFalse(keep)

    def test_opinions_and_questions_need_no_evidence(self):
        keep, _ = self.verdict(['- "looks lovely" => OPINION', '- "Which ring first?" => QUESTION'])
        self.assertTrue(keep)

    def test_unsupported_claims_always_fail(self):
        keep, _ = self.verdict(['- "10,000 players" => UNSUPPORTED'])
        self.assertFalse(keep)

    def test_unknown_labels_and_unreadable_lines_fail(self):
        self.assertFalse(self.verdict(['- "x" => PROBABLY'])[0])
        self.assertFalse(self.verdict(['- some line without an arrow'])[0])

    def test_evidence_is_only_enforced_when_sources_are_given(self):
        text = 'CLAIMS:\n- "x" => MEDIA\n\nHook: 8\nCuriosity: 8\nNatural tone: 8\nRingshift relevance: 8\nOriginality: 8\nViral potential: 8\n\nKEEP: YES\n'
        self.assertTrue(autopilot.critique_verdict(text)[0])


class PipelineExtraTests(OfflineTestCase):
    """Whole dry-run flows with mocked Gemini calls: failure reports, screening and the no-publish guarantee."""

    def setUp(self):
        super().setUp()
        self.candidates = mock.patch.object(autopilot, "generate_candidates").start()
        # Rejects by default, so a test that expects no draft does not get one from the second round's captions.
        self.critic = mock.patch.object(autopilot, "critique_post", return_value=support.REJECTING_CRITIQUE).start()
        mock.patch.object(autopilot, "posts_history", "").start()
        mock.patch.object(autopilot, "analytics", "").start()
        self.tags_file.write_text(json.dumps({
            self.image_rel: {"tags": ["portal"], "description": OPEN_DESC}}), encoding="utf-8")
        mm.save_manifest({"schema": mm.SCHEMA, "items": [record(self.image_rel, base_dir=self.tmp)]},
                         self.tmp / "media" / "manifest.json")
        self.draft = self.tmp / "draft.json"

    def run_dry(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            autopilot.main(["--dry-run", "--draft-file", str(self.draft)])
        return out.getvalue()

    def report(self):
        return json.loads(self.draft.read_text(encoding="utf-8"))

    def test_no_media_for_any_type_writes_a_failure_report_and_calls_nothing(self):
        mm.save_manifest({"schema": mm.SCHEMA, "items": []}, self.tmp / "media" / "manifest.json")
        output = self.run_dry()
        self.assertIn("NO CANDIDATE PASSED", output)
        report = self.report()
        self.assertEqual(report["result"], "no_candidate_passed")
        self.assertIsNone(report["media"])
        self.candidates.assert_not_called()
        self.assertEqual(self.post.call_count, 0)

    def test_text_that_contradicts_the_media_never_reaches_the_critic(self):
        self.candidates.return_value = ["Cleared it first try."]      # media is an unsolved still
        output = self.run_dry()
        self.assertIn("NO CANDIDATE PASSED", output)
        self.assertNotIn("Cleared it first try.", support.critic_texts(self.critic))
        self.assertIn("unsolved", json.dumps(self.report()["attempts"]))

    def test_a_good_candidate_produces_a_draft_with_the_media_facts_and_sends_nothing(self):
        self.counter_file.write_text(json.dumps({"successful_posts": 0}), encoding="utf-8")      # rotation starts at 'challenge'
        self.candidates.return_value = ["Level 7 has a Portal and an Echo. Where does the beam go?"]
        self.critic.return_value = (
            'CLAIMS:\n- "Where does the beam go?" => QUESTION\n\n'
            "Hook: 8\nCuriosity: 8\nNatural tone: 8\nRingshift relevance: 8\nOriginality: 8\nViral potential: 8\n\nKEEP: YES\n")
        before = self.state()
        output = self.run_dry()
        self.assertIn("DRAFT COMPLETE", output)
        draft = self.report()
        self.assertEqual(draft["media_facts"]["puzzle_state"], "unsolved")
        self.assertEqual(draft["post_type"], "challenge")
        self.assertEqual(self.post.call_count, 0)
        self.assertEqual(self.state(), before)

    def test_the_critic_is_given_the_evidence_sources_and_a_made_up_quote_is_rejected(self):
        self.candidates.return_value = ["Portal glow in violet."]
        self.critic.return_value = (
            'CLAIMS:\n- "violet" => MEDIA evidence: "violet neon sky"\n\n'
            "Hook: 8\nCuriosity: 8\nNatural tone: 8\nRingshift relevance: 8\nOriginality: 8\nViral potential: 8\n\nKEEP: YES\n")
        self.assertIn("NO CANDIDATE PASSED", self.run_dry())

    def test_a_repeated_opening_is_rejected_before_the_critic(self):
        mock.patch.object(autopilot, "recent_post_texts", return_value=["Which ring would you turn first to light it?"]).start()
        self.candidates.return_value = ["Which ring would you rotate to finish it?"]
        self.assertIn("NO CANDIDATE PASSED", self.run_dry())
        self.assertNotIn("Which ring would you rotate to finish it?", support.critic_texts(self.critic))


class CaptureHelperTests(unittest.TestCase):
    scene = {"mode": "hard", "index": 6, "id": "hard:6", "labels": ["Portal", "Echo"], "solution_cost": 5}
    when = datetime(2026, 10, 26, tzinfo=timezone.utc)

    def test_filenames_are_aspect_aware_so_shapes_never_overwrite_each_other(self):
        names = {cg.clip_filename(self.scene, "clip", self.when, a) for a in ("4:5", "9:16", "16:9")}
        self.assertEqual(len(names), 3)
        self.assertEqual(cg.clip_filename(self.scene, "clip", self.when), "rs_hard_L07_portal-echo_clip_20261026")

    def test_passes_cover_the_requested_kinds(self):
        self.assertEqual(cg.passes_for("teaser"), [("teaser",)])
        self.assertEqual(cg.passes_for("all"), [("video", "screenshot"), ("teaser",)])
        self.assertEqual(cg.passes_for("video"), [("video",)])

    def test_doctor_reports_each_missing_dependency_without_raising(self):
        def no_playwright():
            raise cg.CaptureError("missing")
        checks = cg.check_dependencies(which=lambda tool: None, import_playwright=no_playwright,
                                       out_dir=Path(support.tempfile.mkdtemp()))
        by = {c["name"]: c for c in checks}
        self.assertFalse(by["Playwright (Python package)"]["ok"])
        self.assertFalse(by["ffmpeg"]["ok"])
        self.assertFalse(by["ffprobe"]["ok"])
        self.assertTrue(by["Output folder writable"]["ok"])
        self.assertIn("pip install", by["Playwright (Python package)"]["detail"])

    def test_doctor_flags_a_missing_game_file(self):
        def no_playwright():
            raise cg.CaptureError("missing")
        checks = cg.check_dependencies(game="/nonexistent/game.html", which=lambda tool: None,
                                       import_playwright=no_playwright, out_dir=Path(support.tempfile.mkdtemp()))
        self.assertFalse({c["name"]: c for c in checks}["Game file"]["ok"])

    def test_a_browser_that_keeps_failing_ends_in_a_clear_capture_error(self):
        args = mock.Mock(game="g.html", headed=False, out="o", zoom=None, scene_timeout=5)
        with mock.patch.object(cg, "GameSession", side_effect=RuntimeError("browser killed")):
            with self.assertRaises(cg.CaptureError) as raised:
                cg._capture_with_retry(args, self.scene, ("video",), log=lambda *_: None)
        self.assertIn("failed 2 times", str(raised.exception))

    def test_a_game_reported_failure_is_final_and_not_retried(self):
        args = mock.Mock(game="g.html", headed=False, out="o", zoom=None, scene_timeout=5)
        session = mock.MagicMock()
        session.__enter__.return_value = session
        with mock.patch.object(cg, "GameSession", return_value=session) as game, \
                mock.patch.object(cg, "capture_scene", side_effect=cg.CaptureError("no win")):
            with self.assertRaises(cg.CaptureError):
                cg._capture_with_retry(args, self.scene, ("video",), log=lambda *_: None)
        self.assertEqual(game.call_count, 1)

    def test_capture_never_needs_api_keys_or_the_network(self):
        source = Path(cg.__file__).read_text(encoding="utf-8")
        for needle in ("GEMINI_API_KEY", "BUFFER_API_KEY", "requests.", "createPost"):
            self.assertNotIn(needle, source)


class RepositoryManifestTests(unittest.TestCase):
    """The committed manifest must agree with the committed media (this is what CI checks on every push)."""

    def test_the_committed_manifest_matches_the_committed_media(self):
        root = support.ROOT
        manifest = mm.load_manifest(root / "media" / "manifest.json")
        self.assertTrue(manifest["items"], "media/manifest.json is missing or empty")
        tags = json.loads((root / "media" / "media_tags.json").read_text(encoding="utf-8"))
        errors = [str(p) for p in mm.validate_manifest(manifest, root, tags=tags, decode=False, check_hashes=True)
                  if p["level"] == "error"]
        self.assertEqual(errors, [], "run: python scripts/media_manifest.py sync")

    def test_every_committed_media_file_marked_valid_really_decodes(self):
        """Headers are not enough: a PNG can have a perfect header and unreadable pixels. Every file the manifest does not
        already mark as failed is decoded in full with FFmpeg. CI installs FFmpeg and sets RINGSHIFT_REQUIRE_FFMPEG=1, so
        there it can never be skipped; where FFmpeg is absent (a bare local checkout) it is skipped, not passed."""
        if not tools.have_ffmpeg():
            if os.environ.get("RINGSHIFT_REQUIRE_FFMPEG") == "1":
                self.fail("RINGSHIFT_REQUIRE_FFMPEG=1 but ffmpeg/ffprobe are not on PATH: full decoding cannot be skipped here")
            self.skipTest("ffmpeg/ffprobe not installed: committed media was not fully decoded")
        root = support.ROOT
        manifest = mm.load_manifest(root / "media" / "manifest.json")
        tags = json.loads((root / "media" / "media_tags.json").read_text(encoding="utf-8"))
        errors = [str(p) for p in mm.validate_manifest(manifest, root, tags=tags, decode=True, check_hashes=True)
                  if p["level"] == "error"]
        self.assertEqual(errors, [], "a file marked valid does not decode: fix or re-capture it, then run media_manifest.py sync")
        decoded = [i for i in manifest["items"] if (i.get("validation") or {}).get("status") == "ok"]
        self.assertTrue(decoded, "no manifest record is marked valid, so nothing was decoded")

    def test_no_teaser_is_ever_marked_solved(self):
        manifest = mm.load_manifest(support.ROOT / "media" / "manifest.json")
        for item in manifest["items"]:
            if "_teaser_" in item["path"]:
                self.assertNotEqual(item["puzzle_state"], "solved", item["path"])
                self.assertNotIn("solution_reveal", item["post_categories"], item["path"])


class NoFfmpegTests(unittest.TestCase):
    """Root cause of the PR #4 CI failure: the runner has no FFmpeg, and fingerprinting raised instead of degrading."""

    def test_fingerprints_degrade_to_nothing_without_ffmpeg(self):
        with mock.patch.object(mm.tools, "have_ffmpeg", return_value=False), \
                mock.patch("shutil.which", return_value=None):
            self.assertIsNone(mm.dhash("anything.png"))
            self.assertEqual(mm.fingerprints("anything.mp4", "video", 6.0), [])

    def test_capture_bookkeeping_works_without_ffmpeg(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(cg.find_near_duplicate({"items": []}, "x.mp4", "video", "9:16", 5.0))


class CiFailureSummaryTests(unittest.TestCase):
    LOG = ("=" * 70 + "\nERROR: test_y (a.B.test_y)\n" + "-" * 70 + "\nValueError: boom\n\n" + "-" * 70 +
           "\nRan 2 tests in 0.1s\n\nFAILED (errors=1)\n")

    def test_failures_become_annotations_with_the_traceback_tail(self):
        import ci_failure_summary as ci
        annotations, markdown = ci.render(self.LOG)
        self.assertEqual(len(annotations), 1)
        self.assertIn("test_y", annotations[0])
        self.assertIn("ValueError: boom", annotations[0])
        self.assertNotIn("Ran 2 tests", annotations[0])
        self.assertIn("FAILED (errors=1)", markdown)

    def test_a_run_that_died_before_finishing_is_still_reported(self):
        import ci_failure_summary as ci
        annotations, _ = ci.render("Traceback (most recent call last):\nImportError: nope\n")
        self.assertIn("Offline tests failed", annotations[0])
        self.assertIn("ImportError: nope", annotations[0])


class NoPublishingTests(unittest.TestCase):
    def test_new_modules_cannot_publish(self):
        for name in ("content_matching.py", "media_manifest.py", "capture_gameplay.py", "media_selector.py"):
            source = (support.SCRIPTS / name).read_text(encoding="utf-8")
            for needle in ("createPost", "api.buffer.com", "BUFFER_API_KEY", "requests.post"):
                self.assertNotIn(needle, source, f"{name} mentions {needle}")


if __name__ == "__main__":
    unittest.main()
