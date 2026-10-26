import os
import unittest
from unittest import mock

import support  # noqa: F401  (sets up sys.path and stubs)
from env_check import require_env


class RequireEnvTests(unittest.TestCase):
    def run_with(self, env, *names):
        with mock.patch.dict(os.environ, env, clear=True):
            return require_env(*names)

    def exit_message(self, env, *names):
        with self.assertRaises(SystemExit) as ctx:
            self.run_with(env, *names)
        return str(ctx.exception)

    def test_valid_value_is_returned_and_stripped(self):
        values = self.run_with({"KEY": "  abc123  "}, "KEY")
        self.assertEqual(values, {"KEY": "abc123"})

    def test_missing_variable_fails_and_names_it(self):
        message = self.exit_message({}, "BUFFER_API_KEY")
        self.assertIn("BUFFER_API_KEY", message)
        self.assertIn("missing or empty", message)

    def test_empty_and_whitespace_only_fail(self):
        # An unset GitHub secret resolves to an empty string.
        for value in ("", "   ", "\t"):
            with self.subTest(value=repr(value)):
                self.assertIn("missing or empty", self.exit_message({"KEY": value}, "KEY"))

    def test_all_problems_are_reported_together(self):
        message = self.exit_message({}, "BUFFER_API_KEY", "GEMINI_API_KEY")
        self.assertIn("BUFFER_API_KEY", message)
        self.assertIn("GEMINI_API_KEY", message)

    def test_bearer_prefix_is_rejected_without_echoing_the_value(self):
        secret = "SUPERSECRETVALUE123"
        message = self.exit_message({"KEY": f"Bearer {secret}"}, "KEY")
        self.assertIn("Bearer", message)
        self.assertNotIn(secret, message)

    def test_line_break_inside_value_is_rejected_without_echoing_it(self):
        secret = "PARTONE\nPARTTWO"
        message = self.exit_message({"KEY": secret}, "KEY")
        self.assertIn("line break", message)
        self.assertNotIn("PARTONE", message)
        self.assertNotIn("PARTTWO", message)

    def test_trailing_newline_alone_is_tolerated(self):
        # A single trailing newline is harmless once stripped.
        self.assertEqual(self.run_with({"KEY": "abc\n"}, "KEY"), {"KEY": "abc"})


if __name__ == "__main__":
    unittest.main()
