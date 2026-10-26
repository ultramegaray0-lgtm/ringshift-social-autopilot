"""Turn a failed `python -m unittest ... -v` log into GitHub annotations and a job summary.

Why: GitHub's job log can be hard to open (phone, or tools that may not download logs), while annotations and the
job summary show on the run page and through the check-runs API. Reads the saved test output, prints one
`::error` annotation per failing test (with the last lines of its traceback) and appends the same to
$GITHUB_STEP_SUMMARY. It never fails and never touches anything but its own output.

Usage (CI): python scripts/ci_failure_summary.py test-output.txt
"""

import os
import re
import sys
from pathlib import Path

HEADER_RE = re.compile(r"^(ERROR|FAIL): (\S+) \(([^)]*)\)", re.MULTILINE)
SEPARATOR = "=" * 70
MAX_ANNOTATIONS = 10          # GitHub shows at most 10 error annotations per step
TAIL_LINES = 12


def failing_tests(text):
    """[(kind, test name, last lines of its report)] for each FAIL/ERROR block in unittest output."""
    results = []
    for block in text.split(SEPARATOR)[1:]:
        block = re.split(r"\n-{70}\nRan \d+ tests?", block)[0]      # the last block is followed by the run summary
        match = HEADER_RE.match(block.strip("\n"))
        if not match:
            continue
        lines = [line for line in block.strip().splitlines() if line.strip() and not line.startswith("-" * 20)]
        results.append((match.group(1), f"{match.group(2)} ({match.group(3)})", lines[-TAIL_LINES:]))
    return results


def summary_line(text):
    """The final 'Ran N tests ...' / 'FAILED (...)' lines, or a note when the run never got that far."""
    lines = [line for line in text.splitlines() if line.startswith(("Ran ", "FAILED", "OK"))]
    return " | ".join(lines[-2:]) if lines else "no unittest summary found: the run stopped before finishing (see the last lines below)"


def escape(value):
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def render(text):
    failures = failing_tests(text)
    annotations = [f"::error title={escape(kind + ' ' + name)}::{escape(chr(10).join(tail))}"
                   for kind, name, tail in failures[:MAX_ANNOTATIONS]]
    if not failures:
        tail = text.strip().splitlines()[-TAIL_LINES:]
        annotations.append(f"::error title=Offline tests failed::{escape(summary_line(text) + chr(10) + chr(10).join(tail))}")

    markdown = ["## Offline test failures", "", summary_line(text), ""]
    for kind, name, tail in failures:
        markdown += [f"### {kind}: `{name}`", "```", *tail, "```", ""]
    if not failures:
        markdown += ["```", *text.strip().splitlines()[-30:], "```"]
    return annotations, "\n".join(markdown)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path = Path(argv[0]) if argv else Path("test-output.txt")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        text = f"could not read {path}: {error}"

    annotations, markdown = render(text)
    print("\n".join(annotations))

    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        try:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(markdown + "\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
