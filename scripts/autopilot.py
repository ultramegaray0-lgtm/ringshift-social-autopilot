import argparse
import hashlib
import json
import os
import random
import re
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google import genai

import analytics as buffer_analytics
import approvals
import game_terms
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
MIN_HOURS_BETWEEN_POSTS = 12     # refuse a second post sooner than this after our last queued one

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
    {"id": "challenge", "label": "Puzzle challenge", "avoid_solved_still": True,
     "brief": "Pose a question or challenge about the puzzle shown that invites a reply. Do not give the solution away."},
    {"id": "hook", "label": "Clip hook", "avoid_solved_still": True,
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


def atomic_write(path, text):
    """Write via a temp file and rename, so a crash never leaves a half-written state file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


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
    """The post log as a list. A missing file is an empty log; a CORRUPT one raises, because
    silently treating it as empty would let the duplicate guard be bypassed and then overwrite it."""
    try:
        text = POST_LOG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []

    try:
        log = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{POST_LOG_FILE.name} is not valid JSON; fix or restore it before posting") from error

    if not isinstance(log, list):
        raise RuntimeError(f"{POST_LOG_FILE.name} must contain a JSON list; fix or restore it before posting")

    return log


def append_post_log(entry):
    log = read_post_log()
    log.append(entry)
    atomic_write(POST_LOG_FILE, json.dumps(log, indent=2, ensure_ascii=False) + "\n")


def check_state_ready():
    """Run BEFORE publishing: every file we must update afterwards has to be readable and writable,
    otherwise a post could be created that we then fail to record."""
    read_post_log()
    for path in (COUNTER_FILE, POST_LOG_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(path.parent, os.W_OK):
            raise RuntimeError(f"{path.parent} is not writable; refusing to post something that cannot be recorded")


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def ensure_safe_to_publish(*, now=None, fetch=None, allow_extra=None):
    """Refuse to queue a second post. Fails CLOSED: if Buffer cannot be checked, nothing is posted.

    Two independent checks, because the repository state can be lost (e.g. a failed push after a post):
      1. Buffer itself: any post with no sent time and a due time still in the future is already waiting.
      2. Our log: a post queued by this tool less than MIN_HOURS_BETWEEN_POSTS ago.
    Set RINGSHIFT_ALLOW_EXTRA_POST=1 (workflow input "allow_extra_post") to deliberately post anyway."""
    if allow_extra is None:
        allow_extra = os.environ.get("RINGSHIFT_ALLOW_EXTRA_POST", "0") == "1"

    now = now or datetime.now(timezone.utc)

    if allow_extra:
        print("Duplicate-post guard OVERRIDDEN (RINGSHIFT_ALLOW_EXTRA_POST=1).")
        return

    for entry in read_post_log():
        queued = _parse_time(entry.get("queued_at"))
        if queued and now - queued < timedelta(hours=MIN_HOURS_BETWEEN_POSTS):
            raise RuntimeError(
                f"A post was already queued {(now - queued).total_seconds() / 3600:.1f}h ago "
                f"(limit {MIN_HOURS_BETWEEN_POSTS}h between posts). Nothing was posted.")

    try:
        edges = (fetch or buffer_analytics.get_posts)()
    except (Exception, SystemExit) as error:
        raise RuntimeError(f"Could not check Buffer for waiting posts ({type(error).__name__}); nothing was posted.") from error

    for edge in edges:
        post = (edge or {}).get("node") or {}
        due = _parse_time(post.get("dueAt"))
        if not post.get("sentAt") and due and due > now - timedelta(minutes=5):
            raise RuntimeError(
                f"Buffer already has a post waiting (id {post.get('id')}, due {post.get('dueAt')}). "
                "Nothing was posted. Delete it in Buffer or set allow_extra_post to override.")


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


ANGLES = [
    "a short question that invites a reply and does not give anything away",
    "a calm observation about something you can literally see in the media",
    "a curiosity hook that makes someone look closely or press play",
]


def generate_candidates(media=None, post_type=None, project=None):
    project = project or {"project_url": None}
    post_type = post_type or POST_TYPES[0]
    recent = "\n".join(f"- {clean_for_prompt(t)}" for t in recent_post_texts()) or "(none yet)"
    media_block = describe_media(media) if media else "Attached media: not chosen yet."
    angles = "\n".join(f"  {i}. {angle}" for i, angle in enumerate(ANGLES[:CANDIDATE_COUNT], 1))

    prompt = f"""
You are the social-media writer for the indie puzzle game Ringshift.

--- KNOWLEDGE (the ONLY source of facts about the game) ---
{knowledge}
--- END KNOWLEDGE ---

--- THE MEDIA THIS POST WILL BE ATTACHED TO (verified facts) ---
{media_block}
--- END MEDIA ---

--- RECENT POSTS (already published or queued) ---
{recent}
--- END RECENT POSTS ---

--- X PERFORMANCE DATA (newest posts, real metrics) ---
{analytics_summary()}
--- END X PERFORMANCE DATA ---

Only draw conclusions from posts that have non-zero performance data. Zero-metric posts are unmeasured, not
failures. Performance data guides style; it never permits inventing facts.

POST TYPE FOR THIS POST: {post_type['label']}
{post_type['brief']}

Write exactly {CANDIDATE_COUNT} DIFFERENT X posts for the attached media, one per angle:
{angles}

What makes a good post here: the media does the explaining, so say something short, specific and a little
curious about what is actually visible (the level, the theme or colours, the shapes and beams, the named
mechanics on screen). A concrete detail beats a vague feeling.

Hard rules (a post that breaks one is rejected by code and wasted):
- Facts come ONLY from the knowledge and the verified media facts above. Nothing else is true.
- Name a mechanic only if it is listed under "Mechanics on screen", using exactly that name. Never use other
  names for mechanics (not "black hole", "satellite", "wormhole" or similar).
- Never say how a mechanic behaves, how the puzzle is solved, or how hard it is, unless the knowledge or the
  verified media facts say so. Never imply a solution exists or is unique unless stated.
- Never say a theme changes gameplay. Themes are visual.
- No numbers, durations, achievements, player reactions, reviews, sales, wishlists, awards, release dates,
  or personal experiences unless they appear in the verified media facts.
- No @mentions. {link_rule(post_type, project)}
- Natural, curious, human tone. Not corporate, no marketing phrases, no "changes everything" style hype.
- Do not start with "Equip", "Try", "Check out", or "Ringshift is".
- Do not reuse hooks, openings, jokes or ideas from the recent posts.
- At most 280 characters and 2 hashtags per post.

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


MIN_TOTAL_SCORE = 30        # of 60; a post below this is dull or off-tone even if every claim is supported
CLAIM_STATUSES = ("KNOWLEDGE", "MEDIA", "OPINION", "UNSUPPORTED")


def critique_post(post, media=None, post_type=None):
    media_block = describe_media(media) if media else "Attached media: unknown."
    type_line = f"{post_type['label']}: {post_type['brief']}" if post_type else "not specified"

    prompt = f"""
You are the strict fact-checking editor for the indie puzzle game Ringshift. You protect the developer from
publishing anything untrue. Be honest: it is better to reject a post than to let one invented fact through.

--- KNOWLEDGE (the ONLY source of facts about the game) ---
{knowledge}
--- END KNOWLEDGE ---

--- THE MEDIA THIS POST WILL BE ATTACHED TO (verified facts) ---
{media_block}
--- END MEDIA ---

--- INTENDED POST TYPE ---
{type_line}
--- END POST TYPE ---

--- POST ---
{post}
--- END POST ---

STEP 1 - list every factual statement or implication the post makes, one per line, and label its support:

CLAIMS:
- "<statement, quoted or paraphrased>" => KNOWLEDGE | MEDIA | OPINION | UNSUPPORTED
(If the post makes no factual statement at all, write the single line: NONE)

The game's name and hashtags are not claims; do not list them.

Labels:
- KNOWLEDGE: stated in the knowledge above.
- MEDIA: stated in the verified media facts above.
- OPINION: a harmless subjective reaction that implies no fact (for example "this looks sharp").
- UNSUPPORTED: anything else. This includes how a mechanic behaves, how the puzzle is solved or how many ways,
  how hard it is, what a player will feel or do, a theme affecting gameplay, any mechanic name not listed on screen,
  any name the knowledge does not use, numbers or experiences not given, and anything about what the media does
  not show. When unsure, choose UNSUPPORTED.

STEP 2 - score from 1 to 10:

Hook:
Curiosity:
Natural tone:
Ringshift relevance:
Originality:
Viral potential:

STEP 3:

KEEP: YES or NO
(NO if any claim is UNSUPPORTED, or the post is dull, hype-y, corporate, or insults the writer.)

MAIN PROBLEM:
The single biggest problem, or "Not needed."

IMPROVED VERSION:
If KEEP is NO, rewrite the post so it keeps the idea but contains ONLY supported claims (no new facts, at most
280 characters and 2 hashtags). If KEEP is YES, write "Not needed."
"""

    response = generate_with_retry(prompt)
    return response.text.strip()


CLAIM_LINE_RE = re.compile(r"^\s*[-*\u2022]\s*(.+?)\s*=>\s*([\w-]+)\b[^\n]*$")      # a trailing explanation is allowed
BULLET_RE = re.compile(r"^\s*[-*\u2022]\s*\S")


def parse_claims(critique):
    """(listed, claims): whether a CLAIMS section exists, and its [(statement, STATUS)] entries."""
    listed = False
    claims = []
    in_section = False

    for line in str(critique).splitlines():
        stripped = line.strip()

        # Models sometimes label the section "STEP 1:" instead of "CLAIMS:". A line shaped like a claim
        # ("- ... => LABEL") counts wherever it appears, so a mislabelled list is still read, not ignored.
        if not in_section and CLAIM_LINE_RE.match(line) and "=>" in line:
            listed = True
            in_section = True

        if stripped.upper().startswith("CLAIMS"):
            listed = True
            in_section = True
            rest = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            if rest.upper() == "NONE":
                in_section = False
            continue

        if in_section:
            match = CLAIM_LINE_RE.match(line)
            if match:
                claims.append((match.group(1).strip().strip('"'), match.group(2).upper()))
            elif BULLET_RE.match(line):
                # A claim line we cannot read is NOT ignored: it counts as an unreadable (rejecting) claim.
                claims.append((stripped.lstrip("-*\u2022 ").strip(), "UNREADABLE"))
            elif stripped.upper() == "NONE":
                in_section = False
            elif re.match(r"^(hook|curiosity|natural tone|ringshift relevance|originality|viral potential|keep|step\s*[23])\b",
                          stripped, re.IGNORECASE):
                in_section = False

    return listed, claims


def critique_verdict(critique):
    """(keep, reasons). Code, not the model, has the last word: any unsupported claim, a missing claim
    list, an unreadable label, a low score, or KEEP: NO all reject the post."""
    reasons = []
    listed, claims = parse_claims(critique)

    if not extract_keep(critique):
        reasons.append("critic said KEEP: NO")

    if not listed:
        reasons.append("critic did not list the post's claims")

    for statement, status in claims:
        if status not in CLAIM_STATUSES:
            reasons.append(f"unreadable claim label {status!r} for: {statement}")
        elif status == "UNSUPPORTED":
            reasons.append(f"unsupported claim: {statement}")

    score = get_score(critique)
    if score < MIN_TOTAL_SCORE:
        reasons.append(f"score {score}/60 is below the minimum {MIN_TOTAL_SCORE}")

    return (not reasons), reasons


def improved_version(critique):
    """The critic's rewrite, or None. It is only a new CANDIDATE: it must pass every check itself."""
    lines = str(critique).splitlines()

    for index, line in enumerate(lines):
        if line.strip().upper().startswith("IMPROVED VERSION"):
            text = line.split(":", 1)[1].strip() if ":" in line else ""
            collected = [text] if text else []
            for following in lines[index + 1:]:
                if not following.strip() and collected:
                    break
                if following.strip():
                    collected.append(following.strip())
            candidate = " ".join(collected).strip().strip('"').strip()
            if not candidate or candidate.lower().startswith("not needed"):
                return None
            return candidate

    return None


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

def screen_candidate(candidate, recent, project, media=None):
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

    if media is not None:
        problems.extend(game_terms.check_mechanics(candidate, media))

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


def send_to_buffer(post, media=None, post_type=None, extra_log=None):
    from media_selector import select_media, record_media

    # Validate the key before touching media, counters or the network.
    api_key = require_env("BUFFER_API_KEY")["BUFFER_API_KEY"]

    counter_file = COUNTER_FILE

    # Every file we must update after posting has to be usable BEFORE we post.
    check_state_ready()

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
        "channelId": BUFFER_CHANNEL_ID,
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
    # The log goes first: it is what stops a repeat. Each write is atomic.
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
        **(extra_log or {}),
    })
    atomic_write(counter_file, json.dumps({"successful_posts": next_post_number}, indent=2))
    record_media(media)

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


def media_check_result(media):
    """Run the same read-only URL check a live post would, and report it instead of raising."""
    if os.environ.get("RINGSHIFT_VERIFY_MEDIA", "1") == "0":
        return "skipped (RINGSHIFT_VERIFY_MEDIA=0)"

    try:
        verify_media_url(media)
        return "ok"
    except MediaUnavailableError as error:
        return f"FAILED: {error}"


def buffer_asset(media):
    """The exact asset object sent to Buffer's createPost `assets` array (typed shape, not the deprecated one)."""
    return {"video": {"url": media["url"]}} if media["type"] == "video" else {"image": {"url": media["url"]}}


def build_draft(post, media, post_type, score, critique, attempts=None):
    """The approvable draft (see approvals.py) plus review details. Contains no secrets."""
    import media_selector

    draft = approvals.create_draft(
        post, media, BUFFER_CHANNEL_ID, post_type=post_type["id"], run_id=os.environ.get("GITHUB_RUN_ID"),
        repo_root=media_selector.BASE_DIR,
        extra={
            "result": "winner",
            "weighted_length": post_rules.weighted_length(post),
            "critic_score": score,
            "buffer_assets_payload": [buffer_asset(media)],
            "text_contains_media_url": media["url"] in post,
            "critique": critique,
            "attempts": attempts or [],
        })
    draft["media"]["selection"] = media.get("match")
    draft["media"]["url_check"] = media_check_result(media)
    return draft


def format_draft_summary(draft):
    """Markdown for the run summary: readable on a phone, with the exact values to approve it."""
    media = draft["media"]
    lines = [
        "## Ringshift draft - NOT sent",
        "",
        f"**Draft ID (the approval code):** `{draft['draft_id']}`",
        f"**Expires:** {draft['expires_at']} (UTC)",
        "",
        "**Post text (exactly as it would be published):**",
        "",
        "> " + draft["text"].replace("\n", "\n> "),
        "",
        f"**Media:** `{media['file']}` ({media['type']}, {media.get('bytes')} bytes) - [open it]({media['url']})",
        f"**Media check:** {media.get('url_check')}",
        f"**Target:** {draft['target']['service']}, Buffer channel `{draft['target']['buffer_channel_id']}`, "
        f"{draft['target']['mode']} / {draft['target']['scheduling']} scheduling",
        f"**Length:** {draft.get('weighted_length')}/{post_rules.X_MAX_WEIGHTED_LENGTH} - **Critic score:** {draft.get('critic_score')}/60",
        f"**Content hash:** `{draft['content_hash'][:16]}...`",
        "",
    ]
    if media["type"] == "image":
        lines += [f"![media preview]({media['url']})", ""]
    lines += [
        "### To publish exactly this",
        "Actions > Ringshift AI Autopilot > Run workflow > branch `main` > mode `publish-approved`, then enter:",
        f"- `approval_code`: `{draft['draft_id']}`",
        "",
        "If you do not approve it, do nothing: it expires and cannot be used after that.",
    ]
    return "\n".join(lines)


def write_draft_file(draft, path):
    atomic_write(path, json.dumps(draft, indent=2, ensure_ascii=False) + "\n")

    # A GitHub Actions annotation, so the draft can be read from the run without downloading anything.
    # Annotations are cut at about 4 KB, so only the essentials go here; the artifact has everything.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        media = draft.get("media") or {}
        compact = {
            "result": draft.get("result"),
            "draft_id": draft.get("draft_id"),
            "text": draft.get("text"),
            "media_file": media.get("file"),
            "media_type": media.get("type"),
            "media_url": media.get("url"),
            "channel_id": (draft.get("target") or {}).get("buffer_channel_id"),
            "expires_at": draft.get("expires_at"),
            "rejected": [{"text": a.get("text"), "why": (a.get("reason") or "; ".join(a.get("reasons") or []))[:160]}
                         for a in (draft.get("attempts") or []) if a.get("outcome") != "critic KEEP"][:4]
            if draft.get("result") != "winner" else None,
        }
        encoded = json.dumps({k: v for k, v in compact.items() if v is not None}, ensure_ascii=False)
        escaped = encoded.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::notice title=Ringshift preview (not sent)::{escaped}")


def build_no_winner_report(media, post_type, attempts, note):
    """What a preview writes when no candidate passed, so the reason is visible instead of an empty run."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sent": False,
        "result": "no_candidate_passed",
        "note": note,
        "post_type": post_type["id"],
        "target": {"service": "X (via Buffer)", "buffer_channel_id": BUFFER_CHANNEL_ID},
        "media": {"file": media["file"], "type": media["type"], "url": media["url"],
                  "description": media.get("description"), "url_check": media_check_result(media)},
        "attempts": attempts,
    }


def write_step_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")

    if not path:
        return

    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        pass


# ---- publishing an approved draft ------------------------------------------------

def publish_approved(draft_path, approval_code, expected_run_id=None, *, now=None, claimer=None):
    """Publish EXACTLY the text and media of an approved draft, once.

    Nothing is regenerated and nothing is edited: the text and media come from the verified draft file.
    Every rejection below happens BEFORE the draft is claimed, so a rejected attempt does not use it up."""
    import media_selector

    api_key_check = require_env("BUFFER_API_KEY")        # no Gemini key is needed or used
    del api_key_check

    draft = approvals.load_draft(draft_path)
    media = approvals.verify_draft(
        draft, approval_code, now=now, channel_id=BUFFER_CHANNEL_ID, expected_run_id=expected_run_id,
        repo_root=media_selector.BASE_DIR, raw_prefix=media_selector.RAW_REPO)

    draft_id = draft["draft_id"]
    text = draft["text"]

    if any(entry.get("draft_id") == draft_id for entry in read_post_log()):
        raise approvals.AlreadyUsedError(f"draft {draft_id} was already published (it is in data/post_log.json)")

    try:
        verify_media_url(media)
    except MediaUnavailableError as error:
        raise approvals.ApprovalError("media_missing", str(error)) from error

    # The same deterministic rules every generated post must pass. Approval does not override them.
    reason = screen_candidate(text, recent_post_texts(), load_project(), media)
    if reason:
        raise approvals.ApprovalError("rules", f"the approved text no longer passes the post rules: {reason}")

    check_state_ready()
    ensure_safe_to_publish()

    # Point of no return. After this the draft ID is spent, whatever happens next.
    claimer = claimer or approvals.default_claimer(COUNTER_FILE.parent)
    claimer.claim(draft_id, {"actor": os.environ.get("GITHUB_ACTOR"), "content_hash": draft["content_hash"]})
    print(f"Draft {draft_id} claimed. It cannot be used again.")

    save_post(text, f"Approved draft {draft_id}, approved by {os.environ.get('GITHUB_ACTOR') or 'a local user'}")

    post_type = next((t for t in POST_TYPES if t["id"] == draft.get("post_type")), None)

    try:
        send_to_buffer(text, media, post_type, extra_log={
            "draft_id": draft_id, "content_hash": draft["content_hash"],
            "approved_by": os.environ.get("GITHUB_ACTOR"), "approved_run_id": draft.get("run_id")})
    except Exception as error:
        print("\nBUFFER ERROR:")
        print(error)
        print(
            f"\nDraft {draft_id} is now CLAIMED and will not be accepted again, because the outcome in Buffer is "
            "unknown. Check Buffer before doing anything else. If Buffer has NO such post, run a new preview and "
            f"approve the new draft (or, to retry this one, delete the tag {approvals.CLAIM_TAG_PREFIX}{draft_id})."
        )
        raise SystemExit(1) from error

    print("\n--- RESULT ---\n")
    print(f"APPROVED DRAFT {draft_id} QUEUED WITH MEDIA CONFIRMED")


# ---- main ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(description="Generate and queue one Ringshift post.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="generate and review a post and show it, but save nothing and send nothing "
             "(needs GEMINI_API_KEY only)")
    parser.add_argument(
        "--draft-file", metavar="PATH",
        help="with --dry-run: also write the full draft (text, media, target channel, Buffer payload) to this JSON file")
    parser.add_argument("--publish-approved", metavar="DRAFT_JSON",
                        help="publish exactly the text and media of this approved preview draft (needs --approval-code)")
    parser.add_argument("--approval-code", help="the draft ID you read in the preview; typing it is your approval")
    parser.add_argument("--expected-run-id", help="the preview run number the draft must come from")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else [])

    if args.publish_approved:
        if args.dry_run:
            raise SystemExit("--publish-approved cannot be combined with --dry-run")
        try:
            publish_approved(args.publish_approved, args.approval_code, args.expected_run_id)
        except approvals.ApprovalError as error:
            print("\n--- RESULT ---\n")
            print(f"NOT PUBLISHING ({error.reason}): {error}")
            raise SystemExit(1) from error
        except RuntimeError as error:                 # state not ready, post waiting, 12 h spacing
            print("\n--- RESULT ---\n")
            print(f"NOT PUBLISHING: {error}")
            raise SystemExit(1) from error
        return

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
        "", require_description=True, require_tags=post_type["id"] == "mechanic",
        allow_special_mode=False, avoid_solved_stills=bool(post_type.get("avoid_solved_still")))

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
        if args.dry_run and args.draft_file:
            write_draft_file(build_no_winner_report(media, post_type, [], "Gemini returned no parsable candidates."), args.draft_file)
        return

    recent = recent_post_texts()

    best_post = None
    best_critique = None
    best_score = -1
    attempts = []
    rejected = []          # (score, index, critique) of critic-rejected candidates, for one revision round

    def consider(text, label, source_index):
        """Screen and critique one candidate. Returns True if it is now the best passing post."""
        nonlocal best_post, best_critique, best_score

        # Never queue text that already exists, repeats an earlier idea, breaks the post rules,
        # or names a mechanic the media does not show.
        reason = screen_candidate(text, recent, project, media)

        if reason:
            print(f"\n--- SKIPPED {label}: {reason} ---\n")
            attempts.append({"candidate": label, "text": text, "outcome": "rejected before the critic", "reason": reason})
            return False

        print(f"\n--- CRITIC {label} ---\n")

        critique = critique_post(text, media, post_type)
        print(critique)

        keep, reasons = critique_verdict(critique)
        score = get_score(critique)
        attempts.append({"candidate": label, "text": text, "outcome": "critic KEEP" if keep else "critic rejected",
                         "score": score, "reasons": reasons, "critique": critique[:1500]})

        if not keep:
            print(f"--- REJECTED {label}: " + "; ".join(reasons) + " ---")
            rejected.append((score, source_index, critique))
            return False

        if score > best_score:
            best_score, best_post, best_critique = score, text, critique
            return True

        return False

    for i, candidate in enumerate(candidates, 1):
        consider(candidate, f"candidate {i}", i)

    # One revision round: the critic's own rewrite of the best rejected candidate is only a NEW candidate.
    # It goes through exactly the same screening and critic; a rejection is never overridden.
    if not best_post:
        for score, source_index, critique in sorted(rejected, key=lambda r: -r[0]):
            revised = improved_version(critique)
            if revised and revised not in candidates:
                print(f"\nTrying the critic's rewrite of candidate {source_index} as a new candidate.")
                consider(revised, f"revision of candidate {source_index}", source_index)
                break

    if best_post:
        print("\n--- WINNER ---\n")
        print(best_post)
        print(f"\nScore: {best_score}/60")

        if args.dry_run:
            draft = build_draft(best_post, media, post_type, best_score, best_critique, attempts)
            print("\n" + format_preview(best_post, media, post_type, best_score, best_critique))
            print(f"\nDRAFT ID (approval code): {draft['draft_id']}   expires {draft['expires_at']}")
            write_step_summary(format_draft_summary(draft))

            if args.draft_file:
                write_draft_file(draft, args.draft_file)
                print(f"Draft written to {args.draft_file}")

            print("\n--- RESULT ---\n")
            print("DRY RUN COMPLETE: nothing was saved or sent.")
            return

        # Live only: refuse before saving or sending if a post is already waiting or was just made.
        try:
            ensure_safe_to_publish()
        except RuntimeError as error:
            print("\n--- RESULT ---\n")
            print(f"NOT PUBLISHING: {error}")
            raise SystemExit(1) from error

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
        if args.dry_run and args.draft_file:
            write_draft_file(build_no_winner_report(
                media, post_type, attempts,
                "Every candidate was rejected. Nothing would be posted. Re-run preview, or see 'attempts' for the reasons."), args.draft_file)
            print(f"Report written to {args.draft_file}")

        print("\n--- RESULT ---\n")
        print("NO CANDIDATE PASSED")


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
