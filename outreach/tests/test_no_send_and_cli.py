"""Structural guarantees (discovery and drafting cannot send; only the manual mail job has Gmail secrets) and an
end-to-end run of the command line with plain local state."""

import ast
import io
import json
import os
import re
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from outreach import cli, core, store, web
from outreach.tests.support import (NOW, FakeGemini, FakeIMAP, FakeSMTP, FakeWeb, OutreachTestCase, youtube_routes)

PACKAGE = Path(__file__).resolve().parent.parent
REPO = PACKAGE.parent
WORKFLOW = REPO / ".github" / "workflows" / "outreach.yml"
NO_MAIL_MODULES = ["core", "store", "settings", "web", "scoring", "discovery", "checker", "drafting", "approval",
                   "report", "claims"]


def imported_names(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names |= {a.name for a in node.names}
    return names


def jobs(workflow_text):
    """{job name: its YAML text} for the top-level jobs (two-space indented keys under `jobs:`)."""
    body = workflow_text.split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([a-z][\w-]*):\n", body)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


class StructureTests(OutreachTestCase):
    def test_only_mailer_imports_mail_libraries(self):
        for name in NO_MAIL_MODULES:
            imports = imported_names(PACKAGE / f"{name}.py")
            with self.subTest(module=name):
                self.assertFalse(imports & {"smtplib", "imaplib", "mailer", "outreach.mailer"}, imports)
        self.assertTrue({"smtplib", "imaplib"} <= imported_names(PACKAGE / "mailer.py"))

    def test_cli_loads_the_mailer_only_inside_deliver_and_sync(self):
        tree = ast.parse((PACKAGE / "cli.py").read_text(encoding="utf-8"))
        top = {a.name for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        self.assertNotIn("mailer", top)
        users = {f.name for f in tree.body if isinstance(f, ast.FunctionDef)
                 for n in ast.walk(f) if isinstance(n, ast.ImportFrom) and any(a.name == "mailer" for a in n.names)}
        self.assertEqual(users, {"cmd_deliver", "cmd_sync", "_gmail"})

    def test_gmail_secrets_exist_only_in_the_manual_mail_job(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        found = jobs(text)
        self.assertEqual(set(found), {"tests", "pipeline", "mail"})
        for name in ("tests", "pipeline"):
            self.assertNotIn("GMAIL", found[name], name)
        mail = found["mail"]
        self.assertIn("secrets.GMAIL_APP_PASSWORD", mail)
        self.assertIn("environment: outreach-send", mail)
        self.assertRegex(mail, r"if: github\.event_name == 'workflow_dispatch' && \(inputs\.mode == 'deliver' \|\| "
                               r"inputs\.mode == 'sync-replies'\)")
        self.assertIn('"$ACTOR" != "$OWNER"', mail)
        self.assertIn("refs/heads/main", mail)
        self.assertIn("schedule", found["pipeline"])
        self.assertNotIn("deliver", found["pipeline"].split("run: |", 2)[-1].split("esac")[0].replace(
            "does not run in this job", ""))

    def test_x_autopilot_files_are_not_referenced_or_changed_by_outreach(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        for marker in ("autopilot.py", "BUFFER_API_KEY", "ringshift-autopilot", "ringshift-used-", "ringshift-day-",
                       "media-store", "HEAD:main"):
            self.assertNotIn(marker, text)
        self.assertIn("group: ringshift-outreach", text)

    def test_state_script_only_writes_the_outreach_state_branch(self):
        import subprocess
        script = PACKAGE / "save_state.sh"
        text = script.read_text(encoding="utf-8")
        self.assertNotIn("HEAD:main", text)
        self.assertIn("state.json.enc", text)
        for branch in ("main", "media-store", "tiktok-state"):
            result = subprocess.run(["bash", str(script)], env={**os.environ, "STATE_BRANCH": branch,
                                    "STATE_DIR": self.tmp}, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0, branch)
            self.assertIn("protected branch", result.stdout)

    def test_daily_run_never_touches_mail_even_with_mail_secrets_present(self):
        FakeSMTP.instances = []
        with mock.patch.dict(os.environ, {"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "a" * 16,
                                          "YOUTUBE_API_KEY": "TEST", "OUTREACH_SENDER_NAME": "Test Sender"}):
            fetcher = web.Fetcher(get=FakeWeb(youtube_routes()))
            with redirect_stdout(io.StringIO()):
                code = cli.cmd_daily(cli.parser().parse_args(["--state-dir", self.tmp, "daily"]), NOW, fetcher=fetcher)
        self.assertEqual(code, 0)
        self.assertEqual(FakeSMTP.instances, [])     # smtplib/imaplib constructors are patched to fail the test


class CliEndToEndTests(OutreachTestCase):
    def run_cli(self, *argv, fetcher=None, **kwargs):
        args = cli.parser().parse_args(["--state-dir", self.tmp, *argv])
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                code = cli.COMMANDS[args.command](args, NOW, **({"fetcher": fetcher} if fetcher else {}), **kwargs)
            except core.OutreachError as error:
                return 2, str(error)
        return code, out.getvalue()

    def setUp(self):
        super().setUp()
        os.environ.update({"YOUTUBE_API_KEY": "TEST", "OUTREACH_SENDER_NAME": "Test Sender"})
        self.summary = Path(self.tmp) / "summary.md"
        os.environ["GITHUB_STEP_SUMMARY"] = str(self.summary)

    def fetcher(self):
        return web.Fetcher(get=FakeWeb(youtube_routes()))

    def test_discover_review_approve_manually_and_mark_sent(self):
        code, out = self.run_cli("daily", fetcher=self.fetcher())
        self.assertEqual(code, 0)
        self.assertIn("Demo link https://ultramegaray0-lgtm.github.io/ringshift-game/: verified", out)
        self.assertNotIn("pal.business@gmail.com", out)               # public logs: masked
        self.assertIn("pa***@gm***.com", out)
        self.assertNotIn("pal.business@gmail.com", self.summary.read_text())
        state = store.load(self.tmp)
        draft = next(iter(state["drafts"].values()))
        self.assertIn(draft["approval_code"], out)
        self.assertIn(draft["body"].split("\n")[2], out)              # the full body is shown for review

        code, out = self.run_cli("deliver", "--draft-id", draft["draft_id"], "--approval-code", draft["approval_code"],
                                 "--recipient", "PAL.business@gmail.com", "--mode", "manual", fetcher=self.fetcher())
        self.assertEqual(code, 0, out)
        self.assertIn("Approved for manual sending", out)
        state = store.load(self.tmp)
        self.assertEqual(state["drafts"][draft["draft_id"]]["status"], core.D_APPROVED)

        code, out = self.run_cli("mark", "--action", "sent", "--draft-id", draft["draft_id"])
        self.assertEqual(code, 0)
        state = store.load(self.tmp)
        self.assertEqual(state["prospects"][draft["prospect_id"]]["status"], core.SENT)
        self.assertEqual(state["ledger"][-1]["confirmation"], "owner-confirmed")

        code, message = self.run_cli("deliver", "--draft-id", draft["draft_id"], "--approval-code",
                                     draft["approval_code"], "--recipient", draft["recipient"], "--mode", "manual",
                                     fetcher=self.fetcher())
        self.assertEqual(code, 2)
        self.assertIn("not awaiting approval", message)

    def test_mark_sent_requires_approval_first(self):
        self.run_cli("daily", fetcher=self.fetcher())
        draft = next(iter(store.load(self.tmp)["drafts"].values()))
        code, message = self.run_cli("mark", "--action", "sent", "--draft-id", draft["draft_id"])
        self.assertEqual(code, 2)
        self.assertIn("approve it first", message)

    def test_reject_suppress_and_add_contact(self):
        self.run_cli("daily", fetcher=self.fetcher())
        state = store.load(self.tmp)
        draft = next(iter(state["drafts"].values()))
        self.assertEqual(self.run_cli("mark", "--action", "reject", "--draft-id", draft["draft_id"])[0], 0)
        self.run_cli("daily", fetcher=self.fetcher())
        state = store.load(self.tmp)
        self.assertEqual(len(state["drafts"]), 1)                     # a rejected prospect is not redrafted

        prospect_id = draft["prospect_id"]
        code, message = self.run_cli("add-contact", "--prospect-id", prospect_id, "--email", "made-up",
                                     "--source-url", "https://youtube.com/@puzzlepal/about")
        self.assertEqual(code, 2)
        self.assertEqual(self.run_cli("suppress", "--email", "pal.business@gmail.com", "--reason", "test")[0], 0)
        state = store.load(self.tmp)
        self.assertEqual(state["prospects"][prospect_id]["status"], core.SUPPRESSED)
        self.run_cli("daily", fetcher=self.fetcher())
        self.assertEqual(len(store.load(self.tmp)["prospects"]), 1)   # not re-added

    def test_demo_failure_means_no_drafts_and_a_warning(self):
        routes = youtube_routes()
        routes.pop("https://ultramegaray0-lgtm.github.io/ringshift-game/")
        code, out = self.run_cli("daily", fetcher=web.Fetcher(get=FakeWeb(routes)))
        self.assertEqual(code, 0)
        self.assertIn("NOT verified", out)
        self.assertEqual(store.load(self.tmp)["drafts"], {})

    def test_youtube_auth_error_exits_1_but_keeps_state(self):
        routes = youtube_routes(search_status=400, search_error={"errors": [{"reason": "keyInvalid"}]})
        code, out = self.run_cli("daily", fetcher=web.Fetcher(get=FakeWeb(routes)))
        self.assertEqual(code, 1)
        self.assertTrue((Path(self.tmp) / "state.json").exists())
        self.assertIn("rejected the key", out)

    def test_sync_without_credentials_is_an_auth_error(self):
        self.run_cli("daily", fetcher=self.fetcher())
        with self.assertRaises(core.AuthError):
            cli.cmd_sync(cli.parser().parse_args(["--state-dir", self.tmp, "sync-replies"]), NOW)

    def test_main_returns_exit_codes(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(cli.main(["--state-dir", self.tmp, "mark", "--action", "sent", "--draft-id",
                                       "od-00000000"], now=NOW), 2)
            self.assertEqual(cli.main(["--state-dir", self.tmp, "sync-replies"], now=NOW), 1)
