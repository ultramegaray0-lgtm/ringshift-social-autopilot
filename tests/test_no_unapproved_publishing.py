"""Nothing publishes without an approved draft.

These are STATIC checks of the workflow files and the code (text/AST based). They prove the files say what we
intend. They do not prove GitHub's behaviour: that is covered by the `rehearse-approval` workflow run (see
docs/TESTING.md), and nothing here has ever published a real post.
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


class ScheduleCannotPublishTests(unittest.TestCase):
    def test_the_schedule_exists_and_is_daily(self):
        self.assertIn('- cron: "0 6 * * *"', TEXT)

    def test_the_scheduled_run_generates_a_draft_with_no_buffer_key(self):
        draft = step("Generate a draft (nothing is sent)")
        self.assertIn("github.event_name == 'schedule'", condition(draft))
        self.assertIn("--dry-run --draft-file", draft)
        self.assertIn("GEMINI_API_KEY", draft)
        self.assertNotIn("BUFFER_API_KEY", draft)
        self.assertNotIn("--publish-approved", draft)

    def test_every_autopilot_invocation_is_a_draft_a_check_or_the_one_approved_publish(self):
        invocations = []
        for s in STEPS:
            for match in re.finditer(r"scripts/autopilot\.py([^\n]*(?:\n\s+--[^\n]*)*)", s):
                invocations.append((step_name(s), " ".join(match.group(1).split())))
        self.assertTrue(invocations)
        for name, args in invocations:
            self.assertRegex(args, r"^(--dry-run|--verify-approved|--rehearse-claims|--publish-approved)", name)

    def test_exactly_one_step_publishes_and_it_is_manual_approved_only(self):
        publishing = [step_name(s) for s in STEPS if "--publish-approved" in run_block(s)]
        self.assertEqual(publishing, ["Publish the approved draft"])
        gate = condition(step("Publish the approved draft"))
        self.assertEqual(gate, "github.event_name == 'workflow_dispatch' && inputs.mode == 'publish-approved'")
        self.assertNotIn("schedule", gate)

    def test_no_schedule_step_has_the_buffer_key_except_read_only_analytics(self):
        scheduled = [s for s in STEPS if "schedule" in condition(s)]
        self.assertTrue(scheduled)
        holders = sorted(step_name(s) for s in scheduled if "BUFFER_API_KEY" in s)
        self.assertEqual(holders, ["Update X analytics"])

    def test_the_buffer_key_is_only_in_analytics_and_the_publish_step(self):
        holders = sorted(step_name(s) for s in STEPS if "BUFFER_API_KEY" in s)
        self.assertEqual(holders, ["Publish the approved draft", "Update X analytics"])

    def test_the_rehearsal_has_neither_buffer_nor_gemini_keys(self):
        rehearsal = step("Rehearse the approval (nothing is published)")
        self.assertNotIn("BUFFER_API_KEY", rehearsal)
        self.assertNotIn("GEMINI_API_KEY", rehearsal)
        self.assertNotIn("--publish-approved", rehearsal)
        self.assertIn("inputs.mode == 'rehearse-approval'", condition(rehearsal))

    def test_there_is_no_live_mode_and_the_modes_are_exactly_these(self):
        self.assertRegex(TEXT, r"options:\s+- test\s+- preview\s+- rehearse-approval\s+- publish-approved\s+default: test")
        self.assertNotRegex(TEXT, r"(?i)\blive\b(?!\s+(?:run|post))[^\n]*mode|mode == 'live'|- live")

    def test_default_manual_mode_publishes_nothing(self):
        self.assertIn("default: test", TEXT)

    def test_inputs_reach_the_shell_only_through_environment_variables(self):
        for s in STEPS:
            self.assertNotIn("${{ inputs.", run_block(s), step_name(s))
            self.assertNotIn("${{ steps.", run_block(s), step_name(s))


class OnlyOneWorkflowCanReachBufferTests(unittest.TestCase):
    def test_only_two_workflow_files_exist(self):
        self.assertEqual(sorted(p.name for p in WORKFLOWS.iterdir()), ["autopilot.yml", "ci.yml"])

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
        self.assertIn("inputs.mode == 'publish-approved' && github.ref != 'refs/heads/main'", guard)
        self.assertIn("exit 1", guard)
        self.assertLess(position("Publishing only runs from main"), position("Publish the approved draft"))

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
        self.assertIn("github.event_name == 'schedule'", condition(step("Read the draft ID")))
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

    def test_publish_approved_is_only_called_from_main_behind_the_flag(self):
        self.assertEqual(_calls(self.autopilot_tree, {"publish_approved"}), [("main", "publish_approved")])
        main_source = ast.get_source_segment((SCRIPTS / "autopilot.py").read_text(encoding="utf-8"),
                                             next(n for n in self.autopilot_tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"))
        self.assertIn("if args.publish_approved:", main_source)

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
