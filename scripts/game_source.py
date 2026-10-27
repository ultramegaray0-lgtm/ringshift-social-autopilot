"""Where the real Ringshift game comes from: the public game repository, checked out next to this one.

The game is one self-contained HTML file in https://github.com/ultramegaray0-lgtm/ringshift-game (branch `main` is the
stable build that the live demo publishes). GitHub Actions checks that repository out read-only (it is public, so no
token is needed) and every capture records the exact commit it was made from.

    python scripts/game_source.py game-src        # prints the game file, its commit and build id
"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

GAME_REPO = "ultramegaray0-lgtm/ringshift-game"
STABLE_BRANCH = "main"
GAME_FILE_RE = re.compile(r"^ringshift(?:[ _-]v?(\d+(?:\.\d+)*))?\.html$", re.IGNORECASE)


class GameSourceError(RuntimeError):
    pass


def _version_key(path):
    match = GAME_FILE_RE.match(Path(path).name)
    numbers = match.group(1) if match else None
    return tuple(int(part) for part in numbers.split(".")) if numbers else ()


def find_game_file(directory):
    """The game HTML in a checkout of the game repository: `ringshift vX.Y.html` with the highest version.

    Raises GameSourceError with a plain explanation when the directory or the file is missing, so an unavailable game
    repository stops the run honestly instead of falling back to anything else."""
    directory = Path(directory)
    if not directory.is_dir():
        raise GameSourceError(f"the game repository is not checked out at {directory} (expected {GAME_REPO})")
    candidates = sorted((p for p in directory.iterdir() if p.is_file() and GAME_FILE_RE.match(p.name)), key=_version_key)
    if not candidates:
        raise GameSourceError(f"no 'ringshift vX.Y.html' game file in {directory}; is it a checkout of {GAME_REPO}?")
    game = candidates[-1]
    if game.stat().st_size < 10_000:
        raise GameSourceError(f"{game.name} is only {game.stat().st_size} bytes; that is not the game")
    return game


def game_commit(directory):
    """The commit of the game checkout (full SHA), or None when the directory is not a git checkout."""
    try:
        result = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def build_id(path):
    """The same 12-character build id the capture tool has always recorded (SHA-256 prefix of the file)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def describe(directory):
    game = find_game_file(directory)
    return {"repo": GAME_REPO, "file": game.name, "path": str(game), "commit": game_commit(directory), "build": build_id(game)}


if __name__ == "__main__":
    try:
        print(json.dumps(describe(sys.argv[1] if len(sys.argv) > 1 else "game-src"), indent=2))
    except GameSourceError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
