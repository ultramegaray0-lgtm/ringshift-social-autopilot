import argparse
import hashlib
import json
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests
from google import genai

import post_rules
from env_check import require_env
from media_selector import describe_media

MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
]

GEMINI_RETRIES = 3
CANDIDATE_COUNT = 3
RECENT_POSTS_IN_PROMPT = 40
ANALYTICS_POSTS_IN_PROMPT = 12
NEAR_DUPLICATE_THRESHOLD = 0.7

BUFFER_API_URL = "https://api.buffer.com"
BUFFER_CHANNEL_ID = "6ac7b4286a5c39ccb6523f7c"
BUFFER_RETRY_ATTEMPTS = 3        # only ever used for HTTP 429, which never creates a post
BUFFER_MAX_RETRY_WAIT = 120

BASE_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_FILE = BASE_DIR / "RINGSHIFT_KNOWLEDGE.md"
POSTS_FILE = BASE_DIR / "content" / "generated_posts.md"
ANALYTICS_FILE = BASE_DIR / "data" / "analytics.md"
COUNTER_FILE = BASE_DIR / "data" / "post_counter.json"
POST_LOG_FILE = BASE_DIR / "data" / "post_log.json"
PROJECT_FILE = BASE_DIR / "data" / "project.json"

knowledge = KNOWLEDGE_FILE.read_text(encoding="utf-8")
posts_history = POSTS_FILE.read_text(encoding="utf-8") if POSTS_FILE.exists() else ""
analytics = ANALYTICS_FILE.read_text(encoding="utf-8") if ANALYTICS_FILE.exists() else ""

# The kinds of post the autopilot rotates through, so the feed does not become
# one format repeated. Chosen by the number of posts already made.
POST_TYPES = [
    {"id": "challenge", "label": "Puzzle challenge",
     "brief": "Pose a question or challenge about the puzzle shown that invites a reply. Do not give the solution away."},
    {"id": "hook", "label": "Clip hook",
     "brief": "Tease what the viewer will see in a few words so they press play or look closely. No spoilers."},
    {"id": "mechanic", "label": "Mechanic spotlight",
     "brief": "Spotlight ONE mechanic that is on screen (see 'Mechanics on screen'). Only say HOW it works if the knowledge "
              "or the media description says so; otherwise react to it without explaining it."},
    {"id": "observation", "label": "Observation",
     "brief": "A short, curious observation about something visible in the media, in a natural developer voice. "
              "Do not claim a personal experience the knowledge does not support."},
    {"id": "visual", "label": "Visual spotlight",
     "brief": "React to how the media looks (colors, glow, theme, atmosphere). Visuals only: a theme never changes gameplay."},
    {"id": "invite", "label": "Invitation", "needs_url": True,
     "brief": "Invite people to take a look at the project page. Include the project URL exactly once, written exactly as given."},
]

_client = None


def get_client():
    """Create the Gemini client on first use, after the key has been validated."""
    global _client

    if _client is None:
        key = require_env("GEMINI_API_KEY")["GEMINI_API_KEY"]
        _client = genai.Client(api_key=key)

    return _client


def _squash(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def text_hash(text):
    return hashlib.sha256(_squash(text).encode("utf-8")).hexdigest()[:16]


def is_duplicate_post(post, *existing_texts):
    """True if the post text already appears in any of the given texts.

    Compared after lowercasing and removing punctuation, so trivial formatting
    differences do not hide a duplicate. Used with the previously generated
    posts and with the Buffer analytics export (which lists posts already
    queued or sent), so a re-run cannot queue the same text twice.
    """
    needle = _squash(post)

    if not needle:
        return False

    return any(needle in _squash(text) for text in existing_texts if text)


# ---- project settings, counter and post log ----------------------------------

def load_project():
    """data/project.json: {"project_url": "https://..." or null}. Never invented."""
    try:
        data = json.loads(PROJECT_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"project_url": None}

    url = data.get("project_url") if isinstance(data, dict) else None

    if isinstance(url, str) and re.match(r"^https://\S+$", url.strip()):
        return {"project_url": url.strip()}

    return {"project_url": None}


def read_counter():
    try:
        counter = json.loads(COUNTER_FILE.read_text(encoding="utf-8"))
        return int(counter.get("successful_posts", 0))
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        return 0


def pick_post_type(successful_posts, project):
    types = [t for t in POST_TYPES if not t.get("needs_url") or project.get("project_url")]
    return types[successful_posts % len(types)]


def read_post_log():
    try:
        log = json.loads(POST_LOG_FILE.read_text(encoding="utf-8"))
        return log if isinstance(log, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def append_post_log(entry):
    POST_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = read_post_log()
    log.append(entry)
    POST_LOG_FILE.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---- prompt context ------------------------------------------------------------

def clean_for_prompt(text, limit=280):
    """Strip stacked combining marks (one past post was 'glitch text') and cap the length."""
    # NFC first so real accents stay single characters; any combining marks left over
    # are stacked "glitch" marks and are dropped.
    text = unicodedata.normalize("NFC", str(text))
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("M"))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def recent_post_texts(history_md=None, analytics_md=None, limit=RECENT_POSTS_IN_PROMPT):
    """Most recent post texts from our own history and from Buffer, oldest dropped first."""
    generated = post_rules.extract_generated_posts(posts_history if history_md is None else history_md)
    queued = post_rules.extract_analytics_posts(analytics if analytics_md is None else analytics_md)
    combined = generated[-limit:] + queued[:limit]
    seen, unique = set(), []
    for text in combined:
        key = _squash(text)
        if key and key not in seen:
            seen.add(key)
            unique.append(text)
    return unique


def analytics_summary(analytics_md=None, limit=ANALYTICS_POSTS_IN_PROMPT):
    """The newest posts with their real metrics, as compact text for the prompt."""
    markdown = analytics if analytics_md is None else analytics_md
    blocks = re.split(r"(?m)^## Post\s*$", str(markdown or ""))[1:]
    lines = []

    for block in blocks[:limit]:
        text_match = re.search(r"(?ms)^### Post\s*\n(.*?)(?=^### Metrics)", block)
        if not text_match:
            continue
        text = clean_for_prompt(text_match.group(1))
        metrics = re.findall(r"(?m)^- ([^:\n]+): ([\d.]+)", block)
        status = re.search(r"(?m)^Status: (\w+)", block)
        shown = ", ".join(f"{name.strip()} {value}" for name, value in metrics) or "no metrics yet"
        lines.append(f'- [{status.group(1) if status else "?"}] "{text}" -> {shown}')

    return "\n".join(lines) if lines else "(no posts yet)"


# ---- Gemini ---------------------------------------------------------------------

def generate_with_retry(prompt):
    last_error = None

    for model in MODELS:
        print(f"\nTrying Gemini model: {model}")

        for attempt in range(1, GEMINI_RETRIES + 1):
            try:
                return get_client().models.generate_content(
                    model=model,
                    contents=prompt
                )
            except Exception as e:
                last_error = e

                print(
                    f"\nGemini failed using {model} "
                    f"(attempt {attempt}/{GEMINI_RETRIES})."
                )
                print(f"Error: {e}")

                if attempt < GEMINI_RETRIES:
                    print("Retrying in 5 seconds...")
                    time.sleep(5)

        print(f"\nModel {model} failed. Trying the next model...")

    raise last_error


def link_rule(post_type, project):
    url = project.get("project_url")

    if url and post_type and post_type.get("id") == "invite":
        return f"The post MUST contain this exact link once and no other link: {url}"

    return "Do NOT include any link or URL."


def generate_candidates(media=None, post_type=None, project=None):
    project = project or {"project_url": None}
    post_type = post_type or POST_TYPES[0]
    recent = "\n".join(f"- {clean_for_prompt(t)}" for t in recent_post_texts()) or "(none yet)"
    media_block = describe_media(media) if media else "Attached media: not chosen yet."

    prompt = f"""
You are the social-media writer for the indie puzzle game Ringshift.

--- KNOWLEDGE ---
{knowledge}
--- END KNOWLEDGE ---

--- THE MEDIA THIS POST WILL BE ATTACHED TO ---
{media_block}
--- END MEDIA ---

--- RECENT POSTS (already published or queued) ---
{recent}
--- END RECENT POSTS ---

--- X PERFORMANCE DATA (newest posts, real metrics) ---
{analytics_summary()}
--- END X PERFORMANCE DATA ---

Use the performance data to learn which kinds of posts appear to perform
better.

IMPORTANT:
- Only draw conclusions from posts that have actual non-zero performance data.
- Scheduled posts with zero metrics provide NO evidence about what works.
- Do not invent trends from insufficient data.
- Performance data is guidance, not permission to invent facts.
- Never claim that a post performed well or poorly in the actual X post.
- Use the data internally to improve candidate selection.

POST TYPE FOR THIS POST: {post_type['label']}
{post_type['brief']}

Generate exactly {CANDIDATE_COUNT} DIFFERENT X posts about Ringshift, all of the
post type above and all written for the attached media.

Each candidate must use a different angle.

Rules:
- Natural human tone.
- Curious and slightly playful.
- Do NOT sound corporate.
- Do NOT use generic marketing language.
- Do NOT invent ANY facts.
- Do NOT invent numbers, time durations, player reactions,
  achievements, release dates, sales, wishlists, or features.
- Only use information explicitly present in the knowledge or in the media description.
- The post must agree with the media description. Never describe anything the
  media does not show, and never say how the level is solved.
- Themes are visual only unless the knowledge explicitly says otherwise.
- Do not exaggerate mechanics.
- Do not explain mechanics like documentation.
- Avoid starting with "Equip", "Try", "Check out", or "Ringshift is".
- Do NOT reuse hooks, opening sentences, jokes, questions,
  or core ideas from the recent posts.
- The three candidates must be meaningfully different.
- Do NOT use @mentions.
- {link_rule(post_type, project)}
- Maximum 280 characters per candidate.
- Maximum 2 hashtags per candidate.

Return ONLY this format:

CANDIDATE 1:
[post]

CANDIDATE 2:
[post]

CANDIDATE 3:
[post]
"""

    response = generate_with_retry(prompt)
    return parse_candidates(response.text)


def parse_candidates(text):
    candidates = []
    current = None

    for line in str(text).strip().splitlines():
        line = line.strip()

        if line.startswith("CANDIDATE 1:"):
            current = []
        elif line.startswith("CANDIDATE 2:"):
            if current:
                candidates.append("\n".join(current).strip())
            current = []
        elif line.startswith("CANDIDATE 3:"):
            if current:
                candidates.append("\n".join(current).strip())
            current = []
        elif current is not None:
            current.append(line)

    if current:
        candidates.append("\n".join(current).strip())

    cleaned = []
    for candidate in candidates:
        candidate = candidate.strip().strip("`").strip()
        candidate = re.sub(r"^\[(.*)\]$", r"\1", candidate, flags=re.DOTALL).strip()
        if candidate:
            cleaned.append(candidate)

    return cleaned


def critique_post(post, media=None, post_type=None):
    media_block = describe_media(media) if media else "Attached media: unknown."
    type_line = f"{post_type['label']}: {post_type['brief']}" if post_type else "not specified"

    prompt = f"""
You are the senior social-media editor for the indie puzzle game Ringshift.

--- KNOWLEDGE ---
{knowledge}
--- END KNOWLEDGE ---

--- THE MEDIA THIS POST WILL BE ATTACHED TO ---
{media_block}
--- END MEDIA ---

--- INTENDED POST TYPE ---
{type_line}
--- END POST TYPE ---

--- X PERFORMANCE DATA ---
{analytics_summary()}
--- END X PERFORMANCE DATA ---

--- POST ---
{post}
--- END POST ---

Score each category from 1 to 10:

Hook:
Curiosity:
Natural tone:
Ringshift relevance:
Originality:
Viral potential:

Then provide:

KEEP: YES or NO

MAIN PROBLEM:
Give the single biggest problem if there is one.

IMPROVED VERSION:
If KEEP is NO, rewrite the post.
If KEEP is YES, write "Not needed."

STRICT FACT RULES:
- Treat every factual claim in the post as unverified unless explicitly
  supported by the knowledge, the media description, or clearly presented as
  subjective opinion.
- If the post says something about what the media shows, it must match the
  media description. Otherwise KEEP must be NO.
- NEVER allow invented numbers.
- NEVER allow invented time durations.
- NEVER allow invented player reactions, reviews, popularity, sales,
  wishlists, achievements, awards, release dates, or milestones.
- NEVER allow invented gameplay behavior or mechanics.
- NEVER allow the post to imply a mechanic works differently from the knowledge.
- NEVER allow visual themes to be described as changing gameplay, difficulty,
  strategy, perception, or mechanics unless the knowledge explicitly says so.
- If a post says something happened to the developer personally, only accept it
  if the original post itself provides that personal experience.
- Questions are NOT automatically factual, but reject questions containing
  unsupported factual premises.
- If ANY unsupported factual claim appears, KEEP must be NO.
- When KEEP is NO because of an unsupported claim, the improved version must
  remove the unsupported claim rather than inventing a replacement fact.
- Do not insult the writer.
- Do not use generic marketing language.
- Maximum 280 characters.
- Maximum 2 hashtags.
"""

    response = generate_with_retry(prompt)
    return response.text.strip()


def extract_keep(critique):
    for line in critique.splitlines():
        if line.strip().upper().startswith("KEEP:"):
            return "YES" in line.upper()
    return False


def get_score(critique):
    scores = []

    score_names = [
        "Hook",
        "Curiosity",
        "Natural tone",
        "Ringshift relevance",
        "Originality",
        "Viral potential",
    ]

    for line in critique.splitlines():
        for name in score_names:
            if line.strip().lower().startswith(name.lower() + ":"):
                try:
                    value = line.split(":", 1)[1].strip()
                    value = value.split("/")[0].strip()
                    scores.append(int(value))
                except (ValueError, IndexError):
                    pass

    return sum(scores)


def save_post(post, critique):
    POSTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing = (
        POSTS_FILE.read_text(encoding="utf-8")
        if POSTS_FILE.exists()
        else ""
    )

    post_number = existing.count("## Post ") + 1

    with POSTS_FILE.open("a", encoding="utf-8") as file:
        file.write(f"\n## Post {post_number:03d}\n\n")
        file.write(post + "\n\n")
        file.write("### Critic\n\n")
        file.write(critique + "\n")


# ---- candidate screening (no AI calls) -----------------------------------------

def screen_candidate(candidate, recent, project):
    """Why `candidate` must not be posted, or None if it passes the deterministic checks."""
    if is_duplicate_post(candidate, posts_history, analytics):
        return "duplicate of an existing post"

    near, score, other = post_rules.is_near_duplicate(candidate, recent, NEAR_DUPLICATE_THRESHOLD)
    if near:
        return f"too similar to an earlier post (score {score:.2f}): {clean_for_prompt(other, 80)!r}"

    problems = post_rules.validate_post(candidate, allow_urls=bool(project.get("project_url")))

    if project.get("project_url") and post_rules.URL_RE.search(candidate):
        urls = post_rules.URL_RE.findall(candidate)
        if any(u.rstrip(".,!?") != project["project_url"] for u in urls):
            problems.append("contains a link other than the configured project URL")

    if problems:
        return "failed post rules: " + "; ".join(problems)

    return None


# ---- Buffer ----------------------------------------------------------------------

class MediaUnavailableError(RuntimeError):
    pass


def verify_media_url(media, timeout=20):
    """Make sure Buffer will be able to fetch the media BEFORE a post is created.

    A HEAD request must answer 200 directly (Buffer does not follow redirects), with
    a file type (raw.githubusercontent.com serves .mp4 as application/octet-stream,
    so that is accepted) and the size we expect. Set RINGSHIFT_VERIFY_MEDIA=0 to skip."""
    if os.environ.get("RINGSHIFT_VERIFY_MEDIA", "1") == "0":
        print("Media URL check skipped (RINGSHIFT_VERIFY_MEDIA=0).")
        return

    url = media["url"]

    try:
        response = requests.head(url, allow_redirects=False, timeout=timeout)

        if response.status_code in (405, 501):
            response = requests.get(url, headers={"Range": "bytes=0-0"}, stream=True,
                                    allow_redirects=False, timeout=timeout)
            response.close()
    except requests.RequestException as error:
        raise MediaUnavailableError(f"could not reach the media URL ({type(error).__name__}); no post was created") from error

    if response.status_code not in (200, 206):
        raise MediaUnavailableError(
            f"media URL answered HTTP {response.status_code} (is the file pushed to GitHub?); no post was created: {url}")

    kind = (response.headers.get("content-type") or "").split(";")[0].strip().lower()

    if not (kind.startswith(("image/", "video/")) or kind == "application/octet-stream"):
        raise MediaUnavailableError(f"media URL returned {kind or 'no content type'}, not an image or video; no post was created")

    length = response.headers.get("content-length")

    if response.status_code == 200 and length and str(length).isdigit() and media.get("bytes"):
        if int(length) != int(media["bytes"]):
            raise MediaUnavailableError(
                f"media URL serves {length} bytes but the file is {media['bytes']} bytes; "
                "push the latest media to GitHub first; no post was created")

    print(f"Media URL OK ({response.status_code}, {kind or 'unknown type'}).")


def _retry_after_seconds(response):
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None

    try:
        wait = int(float(raw))
    except (TypeError, ValueError):
        wait = 30

    return max(1, min(wait, BUFFER_MAX_RETRY_WAIT))


def post_to_buffer(url, headers, payload):
    """POST to Buffer. ONLY an HTTP 429 is retried: Buffer rejects those before doing anything,
    so a retry cannot create a duplicate. Timeouts, 5xx and unconfirmed responses are never
    retried because the post may already exist."""
    response = None

    for attempt in range(1, BUFFER_RETRY_ATTEMPTS + 1):
        response = requests.post(url, headers=headers, json=payload, timeout=60)

        if response.status_code != 429 or attempt == BUFFER_RETRY_ATTEMPTS:
            return response

        wait = _retry_after_seconds(response)
        print(f"Buffer rate limit (HTTP 429). No post was created. Waiting {wait}s, then retrying "
              f"({attempt}/{BUFFER_RETRY_ATTEMPTS - 1}).")
        time.sleep(wait + random.uniform(0, 2))

    return response


def send_to_buffer(post, media=None, post_type=None):
    from media_selector import select_media, record_media

    # Validate the key before touching media, counters or the network.
    api_key = require_env("BUFFER_API_KEY")["BUFFER_API_KEY"]

    counter_file = COUNTER_FILE
    counter_file.parent.mkdir(parents=True, exist_ok=True)

    successful_posts = read_counter()
    next_post_number = successful_posts + 1

    # Refuse to send text that an earlier run already queued (e.g. a re-run after a partial failure).
    digest = text_hash(post)
    if any(entry.get("text_hash") == digest for entry in read_post_log()):
        raise RuntimeError("This exact post text is already in data/post_log.json; refusing to queue it twice.")

    # Every post must include a selected image or video.
    if media is None:
        media = select_media(post)

    assets = []

    if media["type"] == "video":
        assets.append({"video": {"url": media["url"]}})
    else:
        assets.append({"image": {"url": media["url"]}})

    print(f"Selected media: {media['file']}")
    print(f"Media type: {media['type']}")
    print(f"Selection: {media.get('match')}")
    print(f"Detected topics: {media.get('topics')}")

    verify_media_url(media)

    query = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess {
          post {
            id
            text
            dueAt
            assets {
              id
              mimeType
            }
          }
        }
        ... on MutationError {
          message
        }
      }
    }
    """

    post_input = {
        "text": post,
        "channelId": "6ac7b4286a5c39ccb6523f7c",
        "schedulingType": "automatic",
        "mode": "addToQueue",
        "assets": assets,
    }

    response = post_to_buffer(
        os.environ.get("BUFFER_API_URL", "https://api.buffer.com"),
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        {
            "query": query,
            "variables": {"input": post_input},
        },
    )

    print("\n--- BUFFER ---\n")
    print(f"HTTP status: {response.status_code}")
    print(response.text)

    if response.status_code != 200:
        raise RuntimeError(
            f"Buffer API request failed (HTTP {response.status_code}); "
            "post status is unconfirmed."
        )

    data = response.json()

    if data.get("errors"):
        raise RuntimeError(f"Buffer GraphQL error: {data['errors']}")

    result = data.get("data", {}).get("createPost", {})

    if "message" in result:
        raise RuntimeError(f"Buffer rejected the post: {result['message']}")

    if "post" not in result:
        raise RuntimeError(f"Unexpected Buffer response: {data}")

    queued_post = result["post"]
    attached_assets = queued_post.get("assets") or []

    print("\nBUFFER ACCEPTED THE POST")
    print(f"Buffer post ID: {queued_post['id']}")
    print(f"Scheduled time: {queued_post.get('dueAt')}")

    # Do not count it as a successful media post unless Buffer
    # confirms that at least one asset is attached.
    if not attached_assets:
        raise RuntimeError(
            "Buffer returned a queued post but did not confirm attached media. "
            "Check the post in Buffer before retrying to avoid duplicates."
        )

    print(f"Confirmed attached assets: {len(attached_assets)}")

    # Record media and increment the counter only after attachment confirmation.
    record_media(media)
    counter_file.write_text(
        json.dumps({"successful_posts": next_post_number}, indent=2),
        encoding="utf-8",
    )
    append_post_log({
        "number": next_post_number,
        "buffer_id": queued_post["id"],
        "text": post,
        "text_hash": digest,
        "post_type": post_type["id"] if post_type else None,
        "media_file": media["file"],
        "media_type": media["type"],
        "media_tags": media.get("tags", []),
        "weighted_length": post_rules.weighted_length(post),
        "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "due_at": queued_post.get("dueAt"),
        "confirmed_assets": len(attached_assets),
    })

    print("POST QUEUED WITH MEDIA CONFIRMED")
    return True


# ---- preview ------------------------------------------------------------------------

def format_preview(post, media, post_type, score, critique):
    lines = [
        "## Ringshift post preview (nothing was saved or sent)",
        "",
        f"**Post type:** {post_type['label']}",
        f"**Length:** {post_rules.weighted_length(post)}/{post_rules.X_MAX_WEIGHTED_LENGTH} (X-weighted)",
        f"**Critic score:** {score}/60",
        f"**Media:** `{media['file']}` ({media['type']}) - {media.get('match')}",
        f"**Media shows:** {media.get('description') or '(no verified description)'}",
        f"**Media URL:** {media['url']}",
        "",
        "```",
        post,
        "```",
    ]
    return "\n".join(lines)


def write_step_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")

    if not path:
        return

    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        pass


# ---- main ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(description="Generate and queue one Ringshift post.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="generate and review a post and show it, but save nothing and send nothing "
             "(needs GEMINI_API_KEY only)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else [])

    # Fail fast, before spending any Gemini calls, if a key is missing or malformed.
    if args.dry_run:
        require_env("GEMINI_API_KEY")
        print("DRY RUN: nothing will be saved or sent to Buffer.")
    else:
        require_env("GEMINI_API_KEY", "BUFFER_API_KEY")

    from media_selector import select_media

    project = load_project()
    post_type = pick_post_type(read_counter(), project)

    # Choose the media FIRST so the text is written about what it really shows.
    media = select_media(
        "", require_description=True, require_tags=post_type["id"] == "mechanic")

    print(f"Post type: {post_type['label']}")
    print(f"Media: {media['file']} ({media['type']}, {media.get('match')})")
    print(f"Media shows: {media.get('description') or '(no verified description)'}")

    candidates = generate_candidates(media, post_type, project)

    print("\n--- GENERATED CANDIDATES ---\n")

    for i, candidate in enumerate(candidates, 1):
        print(f"--- CANDIDATE {i} ---")
        print(candidate)
        print()

    if not candidates:
        print("No candidates were generated.")
        return

    recent = recent_post_texts()

    best_post = None
    best_critique = None
    best_score = -1

    for i, candidate in enumerate(candidates, 1):
        # Never queue text that already exists, repeats an earlier idea, or breaks the post rules.
        reason = screen_candidate(candidate, recent, project)

        if reason:
            print(f"\n--- SKIPPED CANDIDATE {i}: {reason} ---\n")
            continue

        print(f"\n--- CRITIC CANDIDATE {i} ---\n")

        critique = critique_post(candidate, media, post_type)
        print(critique)

        if extract_keep(critique):
            total_score = get_score(critique)

            if total_score > best_score:
                best_score = total_score
                best_post = candidate
                best_critique = critique

    if best_post:
        print("\n--- WINNER ---\n")
        print(best_post)
        print(f"\nScore: {best_score}/60")

        if args.dry_run:
            preview = format_preview(best_post, media, post_type, best_score, best_critique)
            print("\n" + preview)
            write_step_summary(preview)
            print("\n--- RESULT ---\n")
            print("DRY RUN COMPLETE: nothing was saved or sent.")
            return

        save_post(best_post, best_critique)

        print("\n--- SENDING TO BUFFER ---")

        try:
            send_to_buffer(best_post, media, post_type)
        except Exception as e:
            print("\nBUFFER ERROR:")
            print(e)
            print(
                "\nThe post was NOT confirmed as queued. The post counter and "
                "media history were not advanced. Check Buffer before re-running "
                "so the same post is not queued twice."
            )
            # Exit non-zero so the workflow fails visibly and its commit step is skipped.
            raise SystemExit(1) from e

        print("\n--- RESULT ---\n")
        print("BEST CANDIDATE ACCEPTED")
    else:
        print("\n--- RESULT ---\n")
        print("NO CANDIDATE PASSED")


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
