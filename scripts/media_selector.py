
import json
import random
from pathlib import Path
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent.parent
MEDIA_DIR = BASE_DIR / "media"
HISTORY_FILE = BASE_DIR / "data" / "media_history.json"

GITHUB_RAW_BASE = (
    "https://raw.githubusercontent.com/"
    "ultramegaray0-lgtm/ringshift-social-autopilot/main"
)

SUPPORTED_EXTENSIONS = {
    ".mp4", ".mov", ".webm",
    ".png", ".jpg", ".jpeg", ".webp"
}

RECENT_LIMIT = 5


def select_media():
    files = sorted(
        path for path in MEDIA_DIR.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not files:
        raise RuntimeError("No supported media files found.")

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

    if HISTORY_FILE.exists():
        try:
            history = json.loads(
                HISTORY_FILE.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            history = []
    else:
        history = []

    if not isinstance(history, list):
        history = []

    available = [
        path for path in files
        if path.relative_to(BASE_DIR).as_posix() not in history[-RECENT_LIMIT:]
    ]

    if not available:
        available = files

    chosen = random.choice(available)
    relative_path = chosen.relative_to(BASE_DIR).as_posix()

    history.append(relative_path)
    history = history[-100:]

    HISTORY_FILE.write_text(
        json.dumps(history, indent=2),
        encoding="utf-8"
    )

    encoded_path = quote(relative_path, safe="/")
    public_url = f"{GITHUB_RAW_BASE}/{encoded_path}"

    return {
        "file": relative_path,
        "url": public_url,
        "type": (
            "video"
            if chosen.suffix.lower() in {".mp4", ".mov", ".webm"}
            else "image"
        )
    }


if __name__ == "__main__":
    result = select_media()

    print("Selected media:")
    print(f"File: {result['file']}")
    print(f"Type: {result['type']}")
    print(f"URL: {result['url']}")