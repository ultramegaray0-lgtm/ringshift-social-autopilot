"""Fail-fast validation of required environment variables.

Never prints or returns anything derived from a value except the value itself
to the caller. Error messages name the variable and the problem only, so a
secret can never end up in a log through this module.
"""

import os


def require_env(*names):
    """Return {name: value} for every required variable, or exit with an error.

    A variable is rejected when it is:
    - missing or empty (GitHub resolves a missing secret to an empty string),
    - whitespace only,
    - containing a newline or carriage return (requests rejects these in a
      header, and they are almost always a copy/paste mistake),
    - already prefixed with "Bearer " (the code adds the prefix itself, so the
      request would be sent as "Bearer Bearer ...").

    Leading/trailing spaces are tolerated and stripped.
    """
    problems = []
    values = {}

    for name in names:
        raw = os.environ.get(name)

        if raw is None or not raw.strip():
            problems.append(
                f"{name} is missing or empty "
                "(check the secret name and that it is a repository Actions secret)"
            )
            continue

        if "\n" in raw.strip() or "\r" in raw.strip():
            problems.append(f"{name} contains a line break; re-save it as a single line")
            continue

        value = raw.strip()

        if value.lower().startswith("bearer "):
            problems.append(
                f"{name} already starts with 'Bearer '; store only the key itself"
            )
            continue

        values[name] = value

    if problems:
        raise SystemExit(
            "Configuration error (values are never printed):\n  - "
            + "\n  - ".join(problems)
        )

    return values
