"""Nothing publishes except through the approved-draft checks.

Two paths can post, and both go through autopilot.publish_approved (integrity hash, expiry, run, channel, media
re-download and decode, provenance, post rules, Buffer queue check, 12 h spacing, single-use claim):
  1. the UNATTENDED DAILY run (schedule or mode "daily"): only with media captured fresh in the same run, only after the
     read-only post-window check, at most once per Riyadh day (day claim), and switchable off (RINGSHIFT_AUTOPUBLISH);
  2. the MANUAL publish-approved run: exactly the draft whose ID a person typed.

These are STATIC checks of the workflow files and the code (text/AST based). They prove the files say what we
intend. They do not prove GitHub's behaviour (see docs/TESTING.md).
"""

import ast
import re
import unittest
from pathlib import Path

import support  # noqa: F401  (puts scripts/ on the path)

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPTS = ROOT / "scripts"
TEXT = (WORKFLOWS / "autopilot.yml").read_text(encoding="utf-8")
AUTOPILOT_JOB = TEXT.split("\n  autopilot:\n", 1)[1]
STEPS = re.split(r"(?m)^      - ", AUTOPILOT_JOB)[1:]


def step_name(step):
    return re.match(r"(?:name: )?(.*)", step.splitlines()[0]).group(1)


def position(name):
    return [step_name(s) for s in STEPS].index(name)


def step(name):
    return next(s for s in STEPS if step_name(s) == name)


def condition(s):
    match = re.search(r"(?m)^        if: (.*)$", s)
    return match.group(1) if match else ""


def run_block(s):
    return s.split("run:", 1)[1] if "run:" in s else ""


DAILY = "Write and publish today's post"
MANUAL = "Publish the approved draft"


class SchedulePublishesOnlyThroughTheChecksTests(unittest.TestCase):
    def test_the_daily_schedule_and_the_catch_up_exist(self):
        self.assertIn('- cron: "0 6 * * *"', TEXT)              # 09:00 Riyadh, as before
        self.assertIn('- cron: "37 13 * * *"', TEXT)            # catch-up; does nothing if today already has a post

    def test_exactly_two_steps_can_publish(self):
        publishing = [step_name(s) for s in STEPS if "--publish-approved" in run_block(s) or "--auto-publish" in run_block(s)]
        self.assertEqual(publishing, [DAILY, MANUAL])

    def test_the_manual_step_is_manual_and_approved_only(self):
        gate = condition(step(MANUAL))
        self.assertEqual(gate, "github.event_name == 'workflow_dispatch' && inputs.mode == 'publish-approved'")
        self.assertIn("--publish-approved", run_block(step(MANUAL)))

    def test_the_daily_step_needs_fresh_media_from_this_run(self):
        daily = step(DAILY)
        self.assertEqual(condition(daily), "steps.plan.outputs.daily == 'true' && steps.media.outputs.status == 'captured'")
        self.assertIn("--auto-publish", run_block(daily))
        self.assertIn('--media-file "$DAILY_MEDIA"', run_block(daily))
        self.assertIn("DAILY_MEDIA: ${{ steps.media.outputs.media_file }}", daily)
        self.assertIn("RINGSHIFT_AUTOPUBLISH: ${{ steps.plan.outputs.autopublish }}", daily)
        self.assertLess(position("Capture fresh gameplay media from the real game"), position(DAILY))

    def test_the_post_window_is_checked_read_only_before_capturing_or_posting(self):
        window = step("Check today's post window")
        self.assertIn("--check-publish-window", run_block(window))
        self.assertNotIn("GEMINI_API_KEY", window)
        for later in ("Install Chromium for the real game", "Capture fresh gameplay media from the real game", DAILY):
            self.assertLess(position("Check today's post window"), position(later))
        self.assertIn("steps.window.outputs.should_post == 'true'", condition(step("Capture fresh gameplay media from the real game")))

    def test_the_daily_dry_run_can_never_publish(self):
        plan = step("Decide what this run does")
        self.assertIn('if [ "$MODE" = "daily-dry-run" ]; then auto=off; fi', plan)
        self.assertLess(plan.index("auto=on"), plan.index('if [ "$MODE" = "daily-dry-run" ]; then auto=off; fi'))

    def test_the_daily_path_is_the_schedule_or_mode_daily_and_off_switch_is_respected(self):
        plan = step("Decide what this run does")
        self.assertIn('[ "$EVENT" = "schedule" ] || [ "$MODE" = "daily" ]', plan)
        self.assertIn("AUTOPUBLISH: ${{ vars.RINGSHIFT_AUTOPUBLISH }}", plan)
        self.assertIn("off|0|false|no|disabled|draft-only) auto=off", plan)

    def test_the_preview_draft_step_cannot_publish(self):
        draft = step("Generate a draft (nothing is sent)")
        self.assertEqual(condition(draft), "github.event_name == 'workflow_dispatch' && inputs.mode == 'preview'")
        self.assertIn("--dry-run --draft-file", draft)
        self.assertNotIn("BUFFER_API_KEY", draft)

    def test_every_autopilot_invocation_is_a_known_mode(self):
        invocations = []
        for s in STEPS:
            for match in re.finditer(r"scripts/autopilot\.py([^\n]*(?:\n\s+--[^\n]*)*)", s):
                invocations.append((step_name(s), " ".join(match.group(1).split())))
        self.assertTrue(invocations)
        for name, args in invocations:
            self.assertRegex(args, r"^(--dry-run|--verify-approved|--rehearse-claims|--publish-approved|--auto-publish|--check-publish-window)", name)

    def test_the_buffer_key_is_only_where_it_is_needed(self):
        holders = sorted(step_name(s) for s in STEPS if "BUFFER_API_KEY" in s)
        self.assertEqual(holders, sorted(["Check today's post window", DAILY, "Update X analytics", MANUAL]))

    def test_the_capture_step_has_no_api_keys(self):
        capture = step("Capture fresh gameplay media from the real game")
        for key in ("BUFFER_API_KEY", "GEMINI_API_KEY", "secrets."):
            self.assertNotIn(key, capture)

    def test_the_game_is_checked_out_read_only_from_its_stable_branch(self):
        game = step("Check out the game (stable branch)")
        self.assertIn("repository: ${{ env.GAME_REPOSITORY }}", game)
        self.assertIn("ref: ${{ env.GAME_REF }}", game)
        self.assertIn("persist-credentials: false", game)
        self.assertIn("GAME_REF: main", TEXT)

    def test_the_rehearsal_has_neither_buffer_nor_gemini_keys(self):
        rehearsal = step("Rehearse the approval (nothing is published)")
        self.assertNotIn("BUFFER_API_KEY", rehearsal)
        self.assertNotIn("GEMINI_API_KEY", rehearsal)
        self.assertNotIn("--publish-approved", rehearsal)
        self.assertIn("inputs.mode == 'rehearse-approval'", condition(rehearsal))

    def test_the_modes_are_exactly_these_and_the_default_publishes_nothing(self):
        self.assertRegex(TEXT, r"options:\s+- test\s+- preview\s+- daily\s+- daily-dry-run\s+- rehearse-approval\s+- publish-approved\s+default: test")
        self.assertNotRegex(TEXT, r"mode == 'live'|- live")

    def test_inputs_reach_the_shell_only_through_environment_variables(self):
        for s in STEPS:
            self.assertNotIn("${{ inputs.", run_block(s), step_name(s))
            self.assertNotIn("${{ steps.", run_block(s), step_name(s))
            self.assertNotIn("${{ vars.", run_block(s), step_name(s))


class MediaPinContractTests(unittest.TestCase):
    """The optional media_file input only narrows which committed file a DRAFT is written about."""

    def test_the_pin_reaches_the_script_only_through_an_environment_variable(self):
        for s in STEPS:
            self.assertNotIn("inputs.media_file", run_block(s), step_name(s))      # never interpolated into shell text
        draft = step("Generate a draft (nothing is sent)")
        self.assertIn("MEDIA_FILE: ${{ inputs.media_file }}", draft)
        self.assertIn('--media-file "$MEDIA_FILE"', run_block(draft))

    def test_only_the_draft_step_uses_the_pin_and_it_still_has_no_buffer_key(self):
        # Steps that read the MEDIA_FILE variable or pass --media-file: exactly the draft step (the guard step only
        # inspects inputs.media_file in its condition and never touches the variable).
        users = [step_name(s) for s in STEPS if "MEDIA_FILE" in s]
        self.assertEqual(users, ["Generate a draft (nothing is sent)"])
        # The daily step pins the post to the file captured in the same run (never to a person's input).
        self.assertNotIn("inputs.media_file", step(DAILY))
        draft = step("Generate a draft (nothing is sent)")
        self.assertNotIn("BUFFER_API_KEY", draft)
        for name in ("Rehearse the approval (nothing is published)", MANUAL):
            self.assertNotIn("MEDIA_FILE", step(name), name)
            self.assertNotIn("--media-file", step(name), name)

    def test_the_pin_is_refused_for_every_mode_except_preview_before_any_key_is_used(self):
        guard = step("Check the media pin is only used for a preview")
        self.assertIn("inputs.media_file != ''", condition(guard))
        self.assertIn("inputs.mode != 'preview'", condition(guard))
        self.assertIn("exit 1", run_block(guard))
        self.assertNotIn("secrets.", guard)
        self.assertLess(position("Check the media pin is only used for a preview"), position("Generate a draft (nothing is sent)"))
        self.assertLess(position("Check the media pin is only used for a preview"), position("Find the approved draft"))

    def test_the_input_is_optional_and_a_string(self):
        block = TEXT.split("      media_file:\n", 1)[1].split("      allow_extra_post:", 1)[0]
        self.assertIn("required: false", block)
        self.assertIn("type: string", block)


class OnlyOneWorkflowCanReachBufferTests(unittest.TestCase):
    def test_only_these_workflow_files_exist(self):
        self.assertEqual(sorted(p.name for p in WORKFLOWS.iterdir()), ["autopilot.yml", "ci.yml", "game-lab.yml", "tiktok-video.yml"])

    def test_the_tiktok_workflow_can_never_post_to_x_or_write_main(self):
        tiktok = (WORKFLOWS / "tiktok-video.yml").read_text(encoding="utf-8")
        for forbidden in ("BUFFER", "GEMINI", "autopilot.py", "secrets.", "HEAD:main", "origin main", "media_store.py publish"):
            self.assertNotIn(forbidden, tiktok)
        self.assertRegex(tiktok, r"(?m)^permissions:\s+contents: read")
        self.assertRegex(tiktok, r"concurrency:\s+group: ringshift-tiktok\s+cancel-in-progress: false")
        self.assertNotIn("group: ringshift-autopilot", tiktok)                 # never queues behind or blocks the X runs
        self.assertIn("persist-credentials: false", tiktok)                    # the game checkout itself cannot push
        self.assertIn("STATE_BRANCH: tiktok-state", tiktok)
        self.assertNotRegex(tiktok, r"(?m)^  push:")

    def test_the_game_lab_can_never_post_and_never_writes_the_games_main_branch(self):
        lab = (WORKFLOWS / "game-lab.yml").read_text(encoding="utf-8")
        self.assertNotIn("BUFFER", lab)
        self.assertNotIn("autopilot.py", lab)
        self.assertIn("persist-credentials: false", lab)          # the game checkout itself cannot push
        self.assertRegex(lab, r"(?m)^permissions:\s+contents: read")
        self.assertRegex(lab, r"concurrency:\s+group: ringshift-game-lab\s+cancel-in-progress: false")

    def test_ci_has_no_secrets_and_read_only_permissions(self):
        ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("secrets.", ci)
        self.assertNotIn("BUFFER", ci)
        self.assertRegex(ci, r"(?m)^permissions:\s+contents: read")
        self.assertIn("- main", ci)                           # never runs on pushes to main
        self.assertNotIn("schedule", ci)
        self.assertNotIn("workflow_dispatch", ci)

    def test_the_buffer_secret_is_referenced_only_in_autopilot_yml(self):
        for path in ROOT.rglob("*"):
            if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts and path.suffix in {".yml", ".yaml", ".py", ".sh"}:
                if path.name in {"autopilot.yml"} or "tests" in path.parts:
                    continue
                self.assertNotIn("secrets.BUFFER_API_KEY", path.read_text(encoding="utf-8", errors="ignore"), str(path))

    def test_runs_never_overlap_and_are_never_cancelled(self):
        self.assertRegex(TEXT, r"concurrency:\s+group: ringshift-autopilot\s+cancel-in-progress: false")

    def test_publishing_waits_for_the_tests_job_and_runs_only_from_main(self):
        self.assertRegex(TEXT, r"autopilot:\s+needs: tests")
        guard = step("Publishing only runs from main")
        self.assertIn("(inputs.mode == 'publish-approved' || inputs.mode == 'daily' || inputs.mode == 'daily-dry-run')) && github.ref != 'refs/heads/main'", guard)
        self.assertIn("exit 1", guard)
        self.assertLess(position("Publishing only runs from main"), position(MANUAL))
        self.assertLess(position("Publishing only runs from main"), position(DAILY))

    def test_every_path_that_commits_to_main_runs_only_from_main(self):
        # The state commit pushes HEAD:main; from another branch it would carry that branch's commits onto main.
        committing = condition(step("Save generated content and analytics"))
        self.assertIn("steps.plan.outputs.daily == 'true'", committing)
        guard = condition(step("Publishing only runs from main"))
        for mode in ("publish-approved", "daily", "daily-dry-run"):
            self.assertIn(f"inputs.mode == '{mode}'", guard)
        self.assertLess(position("Publishing only runs from main"), position("Save generated content and analytics"))

    def test_least_privilege_permissions(self):
        self.assertRegex(TEXT, r"(?m)^permissions:\s+contents: read")
        self.assertRegex(TEXT, r"permissions:\s+contents: write[^\n]*\n\s+actions: read")

    def test_no_workflow_can_be_triggered_by_the_commit_it_makes(self):
        self.assertNotRegex(TEXT, r"(?m)^  push:")


class ApprovalPlumbingContractTests(unittest.TestCase):
    """The cross-run steps, as written. Whether GitHub honours them is shown only by an actual rehearsal run."""

    def test_a_malformed_code_is_refused_before_any_lookup_download_or_key(self):
        check = step("Check the approval code")
        self.assertIn("^d-[0-9a-f]{8}-[0-9a-f]{4}$", check)
        for later in ("Find the approved draft", "Download the approved draft",
                      "Rehearse the approval (nothing is published)", "Publish the approved draft"):
            self.assertLess(position("Check the approval code"), position(later))
        self.assertLess(position("Find the approved draft"), position("Download the approved draft"))
        self.assertLess(position("Download the approved draft"), position("Publish the approved draft"))

    def test_both_approval_modes_share_the_same_find_and_download_steps(self):
        for name in ("Check the approval code", "Find the approved draft", "Download the approved draft"):
            gate = condition(step(name))
            self.assertIn("inputs.mode == 'publish-approved'", gate, name)
            self.assertIn("inputs.mode == 'rehearse-approval'", gate, name)

    def test_exactly_one_unexpired_draft_must_match_the_code(self):
        find = step("Find the approved draft")
        self.assertIn(".expired == false", find)
        self.assertIn('[ "$count" != "1" ]', find)
        self.assertIn("ringshift-draft-${APPROVAL_CODE}", find)

    def test_the_download_names_the_artifact_after_the_code_and_the_run_found(self):
        download = step("Download the approved draft")
        self.assertIn("name: ringshift-draft-${{ inputs.approval_code }}", download)
        self.assertIn("run-id: ${{ steps.find.outputs.run_id }}", download)
        self.assertIn("github-token: ${{ github.token }}", download)
        self.assertIn("path: approved", download)

    def test_the_publish_step_pins_the_draft_to_the_run_it_came_from(self):
        publish = step("Publish the approved draft")
        self.assertIn("DRAFT_RUN_ID: ${{ steps.find.outputs.run_id }}", publish)
        self.assertIn('--expected-run-id "$DRAFT_RUN_ID"', publish)
        self.assertIn('--approval-code "$APPROVAL_CODE"', publish)
        self.assertNotIn("GEMINI_API_KEY", publish)

    def test_drafts_are_saved_as_artifacts_named_after_their_id_for_schedule_and_preview(self):
        self.assertIn("name: ringshift-draft-${{ steps.draft.outputs.draft_id }}", TEXT)
        self.assertIn("path: preview_draft.json", TEXT)
        self.assertIn("steps.plan.outputs.daily == 'true'", condition(step("Read the draft ID")))      # schedule + mode daily
        self.assertIn("steps.draft.outputs.draft_id != ''", condition(step("Save the draft as a downloadable artifact")))

    def test_state_is_saved_as_an_artifact_even_if_the_push_fails(self):
        self.assertLess(position("Keep a copy of the state files"), position("Save generated content and analytics"))
        self.assertIn("if: always()", step("Keep a copy of the state files"))


def _calls(tree, wanted):
    """(enclosing function name, called name) for every call of a name in `wanted`."""
    found = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name in wanted:
                found.append((self.stack[-1] if self.stack else "<module>", name))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


class CodeReachesBufferOnlyThroughApprovalTests(unittest.TestCase):
    autopilot_tree = ast.parse((SCRIPTS / "autopilot.py").read_text(encoding="utf-8"))

    def test_send_to_buffer_is_called_from_exactly_one_place_publish_approved(self):
        self.assertEqual(_calls(self.autopilot_tree, {"send_to_buffer"}), [("publish_approved", "send_to_buffer")])

    def test_post_to_buffer_is_only_called_by_send_to_buffer(self):
        self.assertEqual(_calls(self.autopilot_tree, {"post_to_buffer"}), [("send_to_buffer", "post_to_buffer")])

    def _source(self, name):
        return ast.get_source_segment((SCRIPTS / "autopilot.py").read_text(encoding="utf-8"),
                                      next(n for n in self.autopilot_tree.body if isinstance(n, ast.FunctionDef) and n.name == name))

    def test_publish_approved_is_reached_only_from_the_two_flags(self):
        self.assertEqual(sorted(_calls(self.autopilot_tree, {"publish_approved"})),
                         [("auto_publish", "publish_approved"), ("main", "publish_approved")])
        self.assertEqual(_calls(self.autopilot_tree, {"auto_publish"}), [("main", "auto_publish")])
        main_source = self._source("main")
        self.assertIn("if args.publish_approved:", main_source)
        self.assertIn("if not args.auto_publish:", main_source)          # without the flag: a draft, nothing more

    def test_the_unattended_path_claims_the_day_before_the_draft_and_uses_the_same_checks(self):
        auto = self._source("auto_publish")
        self.assertIn("before_claim=claim_day", auto)
        self.assertIn("is_approvable_draft(draft)", auto)
        publish = self._source("publish_approved")
        self.assertLess(publish.index("prepare_approved("), publish.index("before_claim(draft)"))
        self.assertLess(publish.index("ensure_safe_to_publish()"), publish.index("before_claim(draft)"))
        self.assertLess(publish.index("final_publish_checks("), publish.index("before_claim(draft)"))
        self.assertLess(publish.index("before_claim(draft)"), publish.index("claimer.claim("))
        self.assertLess(publish.index("claimer.claim("), publish.index("send_to_buffer("))

    def test_no_other_script_can_create_a_post(self):
        for path in SCRIPTS.glob("*.py"):
            if path.name == "autopilot.py":
                continue
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("createPost", source, path.name)
            self.assertNotIn("mutation", source.lower().replace("mutation of", ""), path.name)

    def test_analytics_only_reads(self):
        source = (SCRIPTS / "analytics.py").read_text(encoding="utf-8")
        self.assertNotIn("mutation", source.lower())
        self.assertNotIn("createPost", source)
        self.assertEqual(len(re.findall(r"requests\.(post|put|patch|delete)\(", source)), len(re.findall(r"requests\.post\(", source)))
        # Buffer's GraphQL endpoint is POST-only, so a POST is expected; the body must be a query.
        for query in re.findall(r'"""(.*?)"""', source, re.S):
            self.assertNotIn("mutation", query.lower())

    def test_the_publish_path_never_releases_a_claim(self):
        text = (SCRIPTS / "autopilot.py").read_text(encoding="utf-8")
        self.assertNotIn(".release(", text)
        self.assertNotIn("delete(", text.replace("def delete", ""))

    def test_the_main_function_has_no_branch_that_generates_and_posts(self):
        calls = [name for _, name in _calls(self.autopilot_tree, {"send_to_buffer", "post_to_buffer", "save_post"})]
        self.assertEqual(sorted(calls), ["post_to_buffer", "save_post", "send_to_buffer"])      # one each, all in publish_approved/send_to_buffer
        self.assertEqual([f for f, n in _calls(self.autopilot_tree, {"save_post"})], ["publish_approved"])


if __name__ == "__main__":
    unittest.main()
