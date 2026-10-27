"""Level lab, theme lab and the game-repository helpers. Offline by default; the real-game tests need
RINGSHIFT_GAME_HTML (the stable game file) and Playwright/Chromium."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import support  # noqa: F401
import capture_gameplay as cg
import game_patch
import game_repo
import level_lab
import theme_lab
import theme_metrics

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "theme_rain_window.txt"
GAME = os.environ.get("RINGSHIFT_GAME_HTML")
HAVE_GAME = bool(GAME and Path(GAME).is_file())

TINY_GAME = """<html><script>
const NOTES = "a ] in a string } and a [ too";
const LEVELS = {
  easy: [ {name:'LEVEL 1', slots:8, rings:[{spokeSlots:[1], gimmicks:{2:'block'}, initialRotation:3}], lockSlots:[0], bonusLockSlots:[4]} ],
  // a comment with brackets [1,2] and braces { } that must not confuse the scanner
  hard: [ /* block comment ] */ {name:'LEVEL 1', slots:12, rings:[], lockSlots:[1], bonusLockSlots:[]} ],
};
const AFTER = `template ${'}'} literal`;
(function v2Register(){ })();
</script></html>"""


class GamePatchTests(unittest.TestCase):
    def test_the_object_end_skips_strings_comments_and_templates(self):
        end = game_patch.find_object_end(TINY_GAME, "const LEVELS = {")
        self.assertTrue(TINY_GAME[:end].endswith("};"))
        self.assertTrue(TINY_GAME[end:].lstrip().startswith("const AFTER"))

    def test_insert_after_is_idempotent_and_marked(self):
        once = game_patch.insert_after(TINY_GAME, "const LEVELS = {", "AUTO_X", "LEVELS.hard.push({name:'A'});")
        twice = game_patch.insert_after(once, "const LEVELS = {", "AUTO_X", "LEVELS.hard.push({name:'B'});")
        self.assertEqual(twice.count("AUTO_X_START"), 1)
        self.assertIn("name:'B'", twice)
        self.assertNotIn("name:'A'", twice)
        self.assertEqual(game_patch.remove_block(twice, "AUTO_X"), TINY_GAME)

    def test_insert_before_is_exactly_reversible(self):
        patched = game_patch.insert_before(TINY_GAME, "(function v2Register(){", "AUTO_T", "THEME_V2.x = {};")
        self.assertLess(patched.index("AUTO_T_START"), patched.index("(function v2Register(){"))
        self.assertEqual(game_patch.remove_block(patched, "AUTO_T"), TINY_GAME)

    def test_anchors_must_exist_and_be_unique(self):
        with self.assertRaises(game_patch.PatchError):
            game_patch.insert_before(TINY_GAME, "(function nowhere(){", "AUTO_Y", "x();")
        with self.assertRaises(game_patch.PatchError):
            game_patch.insert_before(TINY_GAME + "(function v2Register(){", "(function v2Register(){", "AUTO_Y", "x();")

    def test_js_literal_matches_the_level_style(self):
        text = game_patch.js_literal({"name": "LEVEL 26", "rings": [{"gimmicks": {"3": {"type": "portal", "id": "P0", "fixed": True}}}],
                                      "lockSlots": [1, 2]})
        self.assertEqual(text, "{name:'LEVEL 26', rings:[{gimmicks:{3:{type:'portal', id:'P0', fixed:true}}}], lockSlots:[1,2]}")


def candidate(kinds=("prism", "portal"), score=10, rings=None):
    return {"level": {"name": "CANDIDATE", "slots": 12, "rings": rings or [{"spokeSlots": [1], "gimmicks": {"2": {"type": "prism"}},
                                                                        "initialRotation": 3}],
                      "lockSlots": [1, 5], "bonusLockSlots": [3]},
            "routeKinds": list(kinds), "score": score, "route": [{"ring": 1, "slot": 2, "kind": k} for k in kinds],
            "target": [0, 6, 0], "start": [6, 8, 5], "cost": 13, "coinSolutions": 1, "requiredOnlySolutions": 0,
            "nearMisses": 40, "rotations": 1728}


class LevelLabTests(unittest.TestCase):
    def test_the_batch_prefers_different_mechanic_mixes(self):
        found = [candidate(score=50, rings=[{"a": 1}]), candidate(score=49, rings=[{"a": 2}]),
                 candidate(("echo_portal", "mirror"), score=30, rings=[{"a": 3}])]
        chosen = level_lab.pick_varied(found, 2)
        self.assertEqual([c["routeKinds"] for c in chosen], [["prism", "portal"], ["echo_portal", "mirror"]])

    def test_identical_layouts_are_offered_once(self):
        found = [candidate(score=50), candidate(score=40)]
        self.assertEqual(len(level_lab.pick_varied(found, 2)), 1)

    def test_candidates_are_appended_after_levels_and_existing_ones_stay_put(self):
        patched = level_lab.patch_source(TINY_GAME, [candidate()], build="abc", today="2026-10-27")
        self.assertIn("LEVELS.hard.push(", patched)
        self.assertIn("VERIFIED (build abc", patched)
        self.assertLess(patched.index("const LEVELS = {"), patched.index("AUTO_LEVEL_CANDIDATES_START"))
        self.assertEqual(game_patch.remove_block(patched, level_lab.BLOCK_NAME), TINY_GAME)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_the_patched_source_runs_and_names_the_new_level(self):
        patched = level_lab.patch_source(TINY_GAME, [candidate()], build="abc", today="2026-10-27")
        script = patched.split("<script>", 1)[1].split("</script>", 1)[0]
        script += "\nconsole.log(JSON.stringify([LEVELS.easy.length, LEVELS.hard.length, LEVELS.hard[1].name, LEVELS.hard[0].name]));"
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(out.stdout), [1, 2, "LEVEL 2", "LEVEL 1"], out.stderr)


class GameRepoTests(unittest.TestCase):
    def test_only_new_lab_branches_can_ever_be_written(self):
        for bad in ("main", "master", "gh-pages", "theme-redesign", "lab/", "../main", "lab/../../main x"):
            with self.subTest(branch=bad), self.assertRaises(game_repo.GameRepoError):
                game_repo.check_branch(bad)
        self.assertTrue(game_repo.safe_branch("lab/levels").startswith("lab/levels-"))

    def test_in_actions_without_the_token_it_says_exactly_what_is_missing(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", game_repo.TOKEN_ENV: ""}):
            with self.assertRaises(game_repo.GameRepoError) as caught:
                game_repo.require_write_access()
        self.assertIn("RINGSHIFT_GAME_TOKEN", str(caught.exception))
        self.assertIn("artifact", str(caught.exception))

    def test_the_token_never_appears_in_an_error(self):
        with mock.patch.object(game_repo.subprocess, "run", return_value=subprocess.CompletedProcess([], 128, "", "fatal: ghp_SECRET denied")):
            with self.assertRaises(game_repo.GameRepoError) as caught:
                game_repo._git(".", "push", token="ghp_SECRET")
        self.assertNotIn("ghp_SECRET", str(caught.exception))

    def test_previews_failing_to_upload_do_not_stop_a_proposal(self):
        report = {"candidates": [{"scene": "hard:26", "previews": ["/tmp/a.jpg"]}]}
        result = game_repo.host_previews(report, "levels", publish=mock.Mock(side_effect=RuntimeError("push refused")), log=lambda *a: None)
        self.assertEqual(result, {})


SHIPPED = {
    "a": {"field_mean": 30, "field_std": 30, "beam_change": 4.0, "dark_background": 0.1, "apply_ms": 40, "env_chars": 100_000,
          "histogram": [1.0] + [0.0] * 63},
    "b": {"field_mean": 60, "field_std": 26, "beam_change": 3.7, "dark_background": 0.9, "apply_ms": 90, "env_chars": 500_000,
          "histogram": [0.0] * 63 + [1.0]},
}


class ThemeJudgeTests(unittest.TestCase):
    def metrics(self, **change):
        good = {"field_mean": 45, "field_std": 28, "beam_change": 4.2, "dark_background": 0.05, "apply_ms": 35, "env_chars": 90_000,
                "histogram": [0.5] + [0.0] * 31 + [0.5] + [0.0] * 31}
        good.update(change)
        return good

    def test_a_theme_inside_every_shipped_range_passes(self):
        problems, nearest, distance = theme_lab.judge(self.metrics(), SHIPPED, theme_lab.limits(SHIPPED))
        self.assertEqual(problems, [])
        self.assertGreater(distance, 0.12)

    def test_each_measured_failure_is_reported_in_plain_words(self):
        bounds = theme_lab.limits(SHIPPED)
        cases = {"field_mean": (90, "brightness"), "field_std": (50, "busy"), "beam_change": (2.0, "harder to see"),
                 "dark_background": (0.8, "near-black"), "apply_ms": (900, "takes"), "env_chars": (5_000_000, "background art")}
        for key, (value, words) in cases.items():
            with self.subTest(key=key):
                problems, _, _ = theme_lab.judge(self.metrics(**{key: value}), SHIPPED, bounds)
                self.assertTrue(any(words in p for p in problems), problems)

    def test_a_copy_of_an_existing_palette_is_rejected(self):
        problems, nearest, _ = theme_lab.judge(self.metrics(histogram=SHIPPED["a"]["histogram"]), SHIPPED, theme_lab.limits(SHIPPED))
        self.assertEqual(nearest, "a")
        self.assertTrue(any("too close" in p for p in problems))

    def test_histogram_distance(self):
        self.assertEqual(theme_metrics.histogram_distance([1, 0], [1, 0]), 0.0)
        self.assertEqual(theme_metrics.histogram_distance([1, 0], [0, 1]), 1.0)


THEMES = [{"id": "vinyl", "name": "Vinyl Record"}, {"id": "lido", "name": "Lido Pool"}]


class ThemeTextTests(unittest.TestCase):
    def spec(self):
        return theme_lab.parse_response(FIXTURE.read_text(encoding="utf-8"))

    def test_the_fixture_parses_and_passes_the_static_checks(self):
        spec = self.spec()
        self.assertEqual((spec["id"], spec["name"]), ("rainglass", "Rain Window"))
        self.assertEqual(theme_lab.static_problems(spec, THEMES), [])

    def test_dangerous_or_off_framework_code_is_rejected_before_it_runs(self):
        bad_bits = {"fetch('https://x')": "fetch", "window.localStorage": "window", "Math.random()": "Math.random",
                    "LEVELS.hard.pop()": "LEVELS", "el('a',{href:'x'})": "href", "setTimeout(()=>1)": "setTimeout",
                    "el('g',{onload:'x'})": "onload"}
        for snippet, word in bad_bits.items():
            spec = self.spec()
            spec["code"] = spec["code"].replace("env(){", "env(){ " + snippet + ";", 1)
            with self.subTest(snippet=snippet):
                self.assertTrue(any(word in p for p in theme_lab.static_problems(spec, THEMES)))

    def test_names_ids_and_extra_statements(self):
        spec = self.spec()
        spec["name"] = "Mirror Lake"
        self.assertTrue(any("mechanic word" in p for p in theme_lab.static_problems(spec, THEMES)))
        spec = self.spec()
        spec["id"] = "vinyl"
        spec["code"] = spec["code"].replace("THEME_V2.rainglass", "THEME_V2.vinyl", 1)
        self.assertTrue(any("already exists" in p for p in theme_lab.static_problems(spec, THEMES)))
        spec = self.spec()
        spec["code"] += "\nLEVELS = {};"
        self.assertTrue(any("more than the one" in p or "not allowed" in p for p in theme_lab.static_problems(spec, THEMES)))

    def test_an_answer_without_the_format_is_refused(self):
        with self.assertRaises(theme_lab.ThemeLabError):
            theme_lab.parse_response("Sure! Here is a theme: THEME_V2.x = {};")

    def test_the_block_registers_the_theme_honestly(self):
        block = theme_lab.theme_block(dict(self.spec(), concept="rain"))
        self.assertIn("THEME_LIST.push({id:'rainglass'", block)
        self.assertIn("UNASSIGNED_THEMES.push('rainglass')", block)
        self.assertIn("RIM_IDS.push('rainglass')", block)

    def test_the_prompt_carries_the_rules_examples_and_feedback(self):
        prompt = theme_lab.build_prompt(theme_lab.CONCEPTS[0], {"vinyl": "THEME_V2.vinyl = {};"}, THEMES, feedback=["too dark"])
        for words in ("READABILITY FIRST", "NOT a space", "THEME_V2.vinyl = {};", "too dark", "Lido Pool", "v2TrackStd"):
            self.assertIn(words, prompt)


class ThemeScheduleTests(unittest.TestCase):
    def test_due_every_two_weeks(self):
        now = datetime(2026, 11, 9, 2, 23, tzinfo=timezone.utc)
        self.assertTrue(theme_lab.is_due({"themes": []}, 13, now))
        recent = {"themes": [{"at": (now - timedelta(days=7)).isoformat()}]}
        self.assertFalse(theme_lab.is_due(recent, 13, now))
        old = {"themes": [{"at": (now - timedelta(days=14)).isoformat()}]}
        self.assertTrue(theme_lab.is_due(old, 13, now))

    def test_knowledge_file_directions_come_first_and_are_not_repeated(self):
        first = theme_lab.choose_concept({"themes": []}, ["Deep Field"])
        self.assertEqual(first["key"], "rain")
        second = theme_lab.choose_concept({"themes": [{"concept": "rain"}]}, ["Deep Field"])
        self.assertEqual(second["key"], "creepy")
        skip_existing = theme_lab.choose_concept({"themes": []}, ["Rain Garden"])
        self.assertNotEqual(skip_existing["key"], "rain")


@unittest.skipUnless(HAVE_GAME, "set RINGSHIFT_GAME_HTML to the stable game file")
class RealGameLabTests(unittest.TestCase):
    """The game's own tracer and a real browser."""

    def test_generated_levels_are_unique_and_survive_full_validation(self):
        found = level_lab.generate(GAME, count=1, budget_s=90, seed=5, log=lambda *a: None)
        self.assertTrue(found, "the generator found no candidate in 90 s")
        with tempfile.TemporaryDirectory() as tmp:
            patched = Path(tmp) / Path(GAME).name
            patched.write_text(level_lab.patch_source(Path(GAME).read_text(encoding="utf-8"), found, build="test", today="2026-10-27"),
                               encoding="utf-8")
            report = level_lab.validate(GAME, patched, found, Path(tmp) / "previews", log=lambda *a: None)
        self.assertTrue(report["ok"], report)
        self.assertTrue(all(c["ok"] for c in report["candidates"]), report)
        self.assertTrue(report["candidates"][0]["playthrough"]["coin"])

    def test_every_shipped_theme_passes_the_static_filter(self):
        source = Path(GAME).read_text(encoding="utf-8")
        import re
        for theme_id in re.findall(r"THEME_V2\.([a-z0-9]+) = \{", source):
            with self.subTest(theme=theme_id):
                self.assertIsNone(theme_lab.FORBIDDEN.search(theme_lab.extract_theme_code(source, theme_id)))

    def test_the_fixture_theme_registers_and_renders_without_touching_anything_else(self):
        spec = theme_lab.parse_response(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            patched = Path(tmp) / Path(GAME).name
            patched.write_text(theme_lab.patch_source(Path(GAME).read_text(encoding="utf-8"), spec), encoding="utf-8")
            with cg.GameSession(GAME) as stable:
                before = stable.page.evaluate(theme_lab.STATE_JS, spec["id"])
            with cg.GameSession(patched) as session:
                after = session.page.evaluate(theme_lab.STATE_JS, spec["id"])
                shown = session.set_theme(spec["id"])
                session.start_level("hard", 8)
                session.page.wait_for_timeout(500)
                screen = session.screen()
                self.assertEqual(session.errors, [])
        self.assertEqual(after["name"], "Rain Window")
        self.assertTrue(after["unassigned"])
        self.assertEqual(before["levels"], after["levels"])
        self.assertEqual(set(after["v2"]) - set(before["v2"]), {"rainglass"})
        self.assertEqual(shown["id"], "rainglass")
        self.assertEqual(screen["problems"], [])


if __name__ == "__main__":
    unittest.main()
