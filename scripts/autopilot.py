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
import caption_quality
import content_matching
import game_terms
import media_tools
import post_rules
from env_check import require_env
from media_selector import describe_media

MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
]

GEMINI_RETRIES = 3
CANDIDATE_COUNT = 3              # captions the writer is asked for, and the most that are ever considered, per run
MAX_CRITIC_CALLS = CANDIDATE_COUNT + 1     # one critic call per surviving candidate plus at most one rewrite
MAX_DRAFTS_PER_RUN = 1           # one preview run issues at most ONE approval code
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
FACTS_FILE = BASE_DIR / "data" / "project_facts.json"

knowledge = KNOWLEDGE_FILE.read_text(encoding="utf-8")
posts_history = POSTS_FILE.read_text(encoding="utf-8") if POSTS_FILE.exists() else ""
analytics = ANALYTICS_FILE.read_text(encoding="utf-8") if ANALYTICS_FILE.exists() else ""

# The kinds of post the autopilot rotates through, so the feed does not become one format repeated.
# Every type has one purpose (objective), an audience, and media requirements (content_matching.REQUIREMENTS):
# a type is only written when media that honestly supports it exists. Rotation is by the number of posts made
# plus, for draft-only runs, the day (see draft_rotation_offset).
POST_TYPES = [
    {"id": "challenge", "label": "Puzzle challenge", "avoid_solved_still": True,
     "audience": "puzzle-game players who like to look closely",
     "objective": "Get a reply by posing something about the unsolved puzzle shown.",
     "brief": "Pose a question or challenge about the puzzle shown that invites a reply. Do not give the solution away."},
    {"id": "hook", "label": "Clip hook", "avoid_solved_still": True,
     "audience": "people scrolling who might press play on a short clip",
     "objective": "Make someone press play.",
     "brief": "Tease what the viewer will see in a few words so they press play or look closely. No spoilers."},
    {"id": "mechanic", "label": "Mechanic spotlight",
     "audience": "puzzle fans curious how the game's mechanics look in play",
     "objective": "Make one mechanic on screen interesting.",
     "brief": "Spotlight ONE mechanic that is on screen (see 'Mechanics on screen'). Only say HOW it works if the knowledge "
              "or the media description says so; otherwise react to it without explaining it."},
    {"id": "observation", "label": "Observation",
     "audience": "indie-game followers who like a developer's voice",
     "objective": "Leave a small, specific impression of the game.",
     "brief": "A short, curious observation about something visible in the media, in a natural developer voice. "
              "Do not claim a personal experience the knowledge does not support."},
    {"id": "visual", "label": "Visual spotlight",
     "audience": "people who stop for striking visuals",
     "objective": "Get attention for how the game looks.",
     "brief": "React to how the media looks (colors, glow, theme, atmosphere). Visuals only: a theme never changes gameplay."},
    {"id": "solution", "label": "Satisfying solution",
     "audience": "puzzle fans who enjoy a payoff",
     "objective": "Share the payoff of a solved level.",
     "brief": "The media shows the level solved. Say something short and specific about the moment it comes together. "
              "Do not ask the viewer to solve it, and do not claim how hard it was or how many ways there are to solve it."},
    {"id": "moment", "label": "Gameplay moment",
     "audience": "people who like to see a game actually being played",
     "objective": "Point at one real moment in the clip.",
     "brief": "Point at ONE moment you can see in the clip (a ring turning, a beam changing course). "
              "Describe only what is visible; do not explain rules the verified facts do not state."},
    {"id": "feedback", "label": "Feedback request",
     "audience": "players and fellow developers willing to give an opinion",
     "objective": "Get one useful opinion.",
     "brief": "Ask ONE specific question that anyone can answer from what is visible (for example whether the layout reads "
              "clearly or which part catches the eye first). Never ask the viewer to solve a puzzle the media shows solved."},
    {"id": "discovery", "label": "Indie puzzle discovery",
     "audience": "people who look for new indie puzzle games",
     "objective": "Help the right people find the game.",
     "brief": "A friendly, plain note for people who like indie puzzle games: say what kind of game this is using only the "
              "knowledge, and point at one thing visible. No hype, no promises."},
    {"id": "progress", "label": "Developer progress", "fact_kind": "progress",
     "audience": "followers who want to know how the game is developing",
     "objective": "Share one verified development fact.",
     "brief": "State ONE of the VERIFIED PROJECT FACTS in a plain developer voice, tied to what the media shows. "
              "Add no detail the fact does not contain."},
    {"id": "behind_scenes", "label": "Behind the scenes", "fact_kind": "behind_the_scenes",
     "audience": "people curious how the game is made",
     "objective": "Share one verified behind-the-scenes fact.",
     "brief": "State ONE of the VERIFIED PROJECT FACTS about how the game is made, in a plain developer voice. "
              "Add no detail the fact does not contain."},
    {"id": "invite", "label": "Invitation", "needs_url": True,
     "audience": "people who liked what they saw and may want to look further",
     "objective": "Send interested people to the project page.",
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


def load_facts():
    """Verified project facts (data/project_facts.json). Missing or malformed means none."""
    return content_matching.load_project_facts(FACTS_FILE)


def eligible_post_types(project, facts=None):
    """The post types that can be written right now, in rotation order."""
    return [t for t in POST_TYPES if content_matching.post_type_available(t, project, facts)]


def pick_post_type(successful_posts, project, facts=None):
    types = eligible_post_types(project, facts)
    return types[successful_posts % len(types)]


DRAFT_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def draft_rotation_offset(now=None):
    """Whole days since 2026-01-01 (UTC). Draft-only runs write no state, so the publish counter alone would pick
    the same post type every day; adding the day makes consecutive daily drafts different without committing anything."""
    now = now or datetime.now(timezone.utc)
    return max((now - DRAFT_EPOCH).days, 0)


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
    "a short direct question, ONLY if the media makes it answerable from what is visible",
    "a calm, concrete observation (a statement, no question mark) about something you can literally see",
    "a short statement in the developer's own voice that makes someone look closely; no question mark",
]


def question_share(posts):
    """Fraction of `posts` that end in a question mark (0.0 when there are none)."""
    posts = [p for p in posts if str(p).strip()]
    return sum(1 for p in posts if str(p).strip().endswith("?")) / len(posts) if posts else 0.0


def own_recent_posts(limit=6):
    """Our own most recent generated posts, oldest first (content/generated_posts.md is append-only)."""
    return post_rules.extract_generated_posts(posts_history)[-limit:]


def generate_candidates(media=None, post_type=None, project=None, facts=None):
    project = project or {"project_url": None}
    post_type = post_type or POST_TYPES[0]
    recent = "\n".join(f"- {clean_for_prompt(t)}" for t in recent_post_texts()) or "(none yet)"
    media_block = describe_media(media) if media else "Attached media: not chosen yet."
    brief = content_matching.grounding_block(media or {}, post_type, facts)
    angles = "\n".join(f"  {i}. {angle}" for i, angle in enumerate(ANGLES[:CANDIDATE_COUNT], 1))
    questions = question_share(own_recent_posts())
    variety = (f"{questions:.0%} of our last posts were questions: use a statement unless the post type needs a question."
               if questions >= 0.5 else "Vary sentence shape, length and opening words from the recent posts.")

    prompt = f"""
You are the social-media writer for the indie puzzle game Ringshift.

--- KNOWLEDGE (the ONLY source of facts about the game) ---
{knowledge}
--- END KNOWLEDGE ---

--- THE MEDIA THIS POST WILL BE ATTACHED TO (verified facts) ---
{media_block}
--- END MEDIA ---

{brief}

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
mechanics on screen). A concrete detail beats a vague feeling. {variety}

Hard rules (a post that breaks one is rejected by code and wasted):
- Facts come ONLY from the knowledge and the verified media facts above. Nothing else is true.
- Name a mechanic only if it is listed under "Mechanics on screen", using exactly that name. Never use other
  names for mechanics (not "black hole", "satellite", "wormhole" or similar).
- Never say how a mechanic behaves, how the puzzle is solved, or how hard it is, unless the knowledge or the
  verified media facts say so. Never imply a solution exists or is unique unless stated.
- Never say a theme changes gameplay. Themes are visual.
- No numbers, durations, achievements, player reactions, reviews, sales, wishlists, awards, release dates,
  or personal experiences unless they appear in the verified media facts.
- A question must be answerable by looking at the media. Never ask the viewer to solve a puzzle the media shows solved.
- No @mentions. {link_rule(post_type, project)}
- Natural, curious, human tone. Not corporate, no marketing phrases, no "changes everything" style hype.
- Every post must refer to something concrete and verified about THIS media: a mechanic listed under "Mechanics on
  screen", its level number, or its theme. No filler such as "a brand new idea", "a genuinely new thought",
  "check this out" or "you won't believe", and no post that could be attached to any other picture.
- The posts must be genuinely different from each other (different point, not the same sentence reworded).
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
    return parse_candidates(response.text)[:CANDIDATE_COUNT]


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
CLAIM_STATUSES = ("KNOWLEDGE", "MEDIA", "OPINION", "QUESTION", "UNSUPPORTED")
EVIDENCE_REQUIRED = ("KNOWLEDGE", "MEDIA")      # these two must quote their support, and the quote must really exist


def critique_post(post, media=None, post_type=None, facts=None):
    media_block = describe_media(media) if media else "Attached media: unknown."
    type_line = f"{post_type['label']}: {post_type['brief']}" if post_type else "not specified"
    brief = content_matching.grounding_block(media or {}, post_type or {}, facts)

    prompt = f"""
You are the strict fact-checking editor for the indie puzzle game Ringshift. You protect the developer from
publishing anything untrue. Be honest: it is better to reject a post than to let one invented fact through.
Do not reject harmless text just because it uses an ordinary English word (block, mirror, echo) in its ordinary sense.

--- KNOWLEDGE (the ONLY source of facts about the game) ---
{knowledge}
--- END KNOWLEDGE ---

--- THE MEDIA THIS POST WILL BE ATTACHED TO (verified facts) ---
{media_block}
--- END MEDIA ---

{brief}

--- INTENDED POST TYPE ---
{type_line}
--- END POST TYPE ---

--- POST ---
{post}
--- END POST ---

STEP 1 - list every factual statement or implication the post makes, one per line. Classify it and, for facts,
quote the evidence word for word from the knowledge or the verified media facts above:

CLAIMS:
- "<statement, quoted or paraphrased>" => KNOWLEDGE | MEDIA | OPINION | QUESTION | UNSUPPORTED | evidence: "<exact words copied from the knowledge or media facts>"
(If the post makes no factual statement at all, write the single line: NONE)

The game's name and hashtags are not claims; do not list them.

Labels:
- KNOWLEDGE: stated in the knowledge above. Needs an evidence quote from the knowledge.
- MEDIA: stated in the verified media facts above. Needs an evidence quote from the media facts.
- OPINION: a harmless subjective reaction that implies no fact (for example "this looks sharp"). No evidence needed.
- QUESTION: a question that asserts no unsupported fact AND can be answered by looking at the media. No evidence needed.
  A question that asks the viewer to solve a puzzle the media shows solved is UNSUPPORTED.
- UNSUPPORTED: anything else. This includes how a mechanic behaves, how the puzzle is solved or how many ways,
  how hard it is, what a player will feel or do, a theme affecting gameplay, any mechanic name not listed on screen,
  any name the knowledge does not use, numbers or experiences not given, anything about what the media does
  not show, and a mismatch between the post and the media (for example text about a clip over a still image).
  When unsure, choose UNSUPPORTED.

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


CLAIM_LINE_RE = re.compile(r"^\s*[-*\u2022]\s*(.+?)\s*=>\s*([\w-]+)\b([^\n]*)$")      # a trailing explanation / evidence is allowed
BULLET_RE = re.compile(r"^\s*[-*\u2022]\s*\S")
EVIDENCE_RE = re.compile(r"evidence\s*[:=]\s*(.+)$", re.IGNORECASE)


def _claim_lines(critique):
    """(listed, [(statement, STATUS, evidence_text)]). `evidence_text` is "" when none was given."""
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
                found = EVIDENCE_RE.search(match.group(3) or "")
                evidence = found.group(1).strip().strip("|").strip().strip('"\'').strip() if found else ""
                claims.append((match.group(1).strip().strip('"'), match.group(2).upper(), evidence))
            elif BULLET_RE.match(line):
                # A claim line we cannot read is NOT ignored: it counts as an unreadable (rejecting) claim.
                claims.append((stripped.lstrip("-*\u2022 ").strip(), "UNREADABLE", ""))
            elif stripped.upper() == "NONE":
                in_section = False
            elif re.match(r"^(hook|curiosity|natural tone|ringshift relevance|originality|viral potential|keep|step\s*[23])\b",
                          stripped, re.IGNORECASE):
                in_section = False

    return listed, claims


def parse_claims(critique):
    """(listed, claims): whether a CLAIMS section exists, and its [(statement, STATUS)] entries."""
    listed, claims = _claim_lines(critique)
    return listed, [(statement, status) for statement, status, _ in claims]


def _normalise(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def evidence_found(evidence, sources):
    """True if every fragment of the quoted `evidence` really occurs in `sources` (compared without case or
    punctuation). Fragments are separated by '...'. A critic that cites words that are not there is not believed."""
    haystack = " " + _normalise(sources) + " "
    fragments = [f for f in re.split(r"\.\.\.|\u2026", str(evidence)) if _normalise(f)]

    if not fragments:
        return False

    return all(" " + _normalise(fragment) + " " in haystack for fragment in fragments)


def critique_verdict(critique, sources=None):
    """(keep, reasons). Code, not the model, has the last word: any unsupported claim, a missing claim
    list, an unreadable label, a low score, or KEEP: NO all reject the post.

    With `sources` (the knowledge and verified media text), every KNOWLEDGE or MEDIA claim must also quote
    evidence that really appears in those sources."""
    reasons = []
    listed, claims = _claim_lines(critique)

    if not extract_keep(critique):
        reasons.append("critic said KEEP: NO")

    if not listed:
        reasons.append("critic did not list the post's claims")

    for statement, status, evidence in claims:
        if status not in CLAIM_STATUSES:
            reasons.append(f"unreadable claim label {status!r} for: {statement}")
        elif status == "UNSUPPORTED":
            reasons.append(f"unsupported claim: {statement}")
        elif sources is not None and status in EVIDENCE_REQUIRED:
            if not evidence:
                reasons.append(f"{status} claim has no evidence quote: {statement}")
            elif not evidence_found(evidence, sources):
                reasons.append(f"evidence for '{statement}' was not found in the knowledge or media facts: {evidence[:80]}")

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

def screen_candidate(candidate, recent, project, media=None, post_type=None, facts=None):
    """Why `candidate` must not be posted, or None if it passes the deterministic checks."""
    if is_duplicate_post(candidate, posts_history, analytics):
        return "duplicate of an existing post"

    near, score, other = post_rules.is_near_duplicate(candidate, recent, NEAR_DUPLICATE_THRESHOLD)
    if near:
        return f"too similar to an earlier post (score {score:.2f}): {clean_for_prompt(other, 80)!r}"

    # The same idea in different words is a repeat too (a different content hash does not make a caption different).
    for other in recent:
        same, why = caption_quality.same_idea(candidate, other)
        if same:
            return f"repeats an earlier post ({why}): {clean_for_prompt(other, 80)!r}"

    problems = post_rules.validate_post(candidate, allow_urls=bool(project.get("project_url")))

    if project.get("project_url") and post_rules.URL_RE.search(candidate):
        urls = post_rules.URL_RE.findall(candidate)
        if any(u.rstrip(".,!?") != project["project_url"] for u in urls):
            problems.append("contains a link other than the configured project URL")

    if media is not None:
        problems.extend(game_terms.check_mechanics(candidate, media))
        problems.extend(content_matching.text_problems(candidate, media, post_type))
        problems.extend(caption_quality.quality_problems(
            candidate, media, (post_type or {}).get("id"),
            [f for f in facts or [] if f.get("kind") == (post_type or {}).get("fact_kind")]))

    echoed = content_matching.repeats_opening(candidate, recent)
    if echoed:
        problems.append(f"starts with the same words as an earlier post: {clean_for_prompt(echoed, 60)!r}")

    if candidate.strip().endswith("?") and question_share(own_recent_posts()) >= 0.5 and len(own_recent_posts()) >= 4:
        problems.append("most of our recent posts were already questions")

    if problems:
        return "failed post rules: " + "; ".join(problems)

    return None


# ---- Buffer ----------------------------------------------------------------------

class MediaUnavailableError(RuntimeError):
    pass


# How strictly media is checked. "strict" is the only mode any workflow or command line can reach: it is a constant
# in this file, never read from an environment variable or an option. Tests (and only tests) switch it to "fixture"
# to use throwaway files; a draft made in fixture mode is stamped as such and can NEVER be approved (see approvals).
MEDIA_CHECK_MODE = "strict"
MEDIA_FETCH_LIMIT = media_tools.MAX_VIDEO_BYTES


def _warn_if_bypass_requested():
    """RINGSHIFT_VERIFY_MEDIA=0 used to skip the media checks. It is ignored now; say so instead of silently obeying."""
    if os.environ.get("RINGSHIFT_VERIFY_MEDIA", "1") == "0":
        print("WARNING: RINGSHIFT_VERIFY_MEDIA=0 is ignored. Media checks cannot be switched off.")


def verify_media_url(media, timeout=20):
    """Make sure Buffer will be able to fetch the media BEFORE a post is created.

    A HEAD request must answer 200 directly (Buffer does not follow redirects), with
    a file type (raw.githubusercontent.com serves .mp4 as application/octet-stream,
    so that is accepted) and the size we expect. There is no switch to skip this check."""
    _warn_if_bypass_requested()

    if MEDIA_CHECK_MODE == "fixture":
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


def _download_media(url, suffix, timeout=30):
    """Fetch the media exactly as Buffer would (no redirects). Returns (temp path, sha256, byte count).
    Raises MediaUnavailableError for any non-200 answer, a wrong content type or an oversized file."""
    try:
        response = requests.get(url, stream=True, allow_redirects=False, timeout=timeout)
    except requests.RequestException as error:
        raise MediaUnavailableError(f"could not reach the media URL ({type(error).__name__}): {url}") from error

    try:
        if response.status_code != 200:
            raise MediaUnavailableError(
                f"media URL answered HTTP {response.status_code} (is the file pushed to GitHub, on the branch the URL names?): {url}")

        kind = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not (kind.startswith(("image/", "video/")) or kind == "application/octet-stream"):
            raise MediaUnavailableError(f"media URL returned {kind or 'no content type'}, not an image or video: {url}")

        digest = hashlib.sha256()
        size = 0
        handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            with handle:
                for block in response.iter_content(1 << 20):
                    size += len(block)
                    if size > MEDIA_FETCH_LIMIT:
                        raise MediaUnavailableError(f"media at {url} is larger than {MEDIA_FETCH_LIMIT // 1048576} MB")
                    digest.update(block)
                    handle.write(block)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        return Path(handle.name), digest.hexdigest(), size
    finally:
        response.close()


def manifest_decode_proof(relative, sha256):
    """True only if the committed manifest in THIS checkout records a successful decode of exactly these bytes.

    The evidence is looked up here, by file path, from media/manifest.json in the checkout the run is using (publishing
    only runs from main). It is never taken from the draft or from fields on the media dict, which an editable draft
    could simply claim. A missing, unreadable or stale manifest, a failed record or a different hash is no proof."""
    import media_selector
    try:
        records = media_selector._manifest_records()
    except media_selector.NoSuitableMediaError:
        return False
    record = (records or {}).get(relative)
    if not isinstance(record, dict) or not sha256:
        return False
    validation = record.get("validation") or {}
    return validation.get("status") == "ok" and not validation.get("problems") and record.get("sha256") == sha256


def check_media_for_approval(media, repo_root=None, *, expected_sha256=None):
    """The mandatory precondition for any draft that carries an approval code. Returns a record
    {"status": "ok", ...} or raises MediaUnavailableError. It proves, in this order, that:

    1. the media file exists in this checkout (or, for media hosted elsewhere, at its URL),
    2. it is a real image/video: right signature for its extension, not tiny/empty/truncated, big enough,
    3. its URL answers 200 with exactly the bytes of the repository file (same SHA-256),
    4. it decodes: FFmpeg decodes it now, or (where FFmpeg is missing) the committed manifest in this checkout recorded
       a successful decode of these same bytes (looked up here, never taken from the draft). With neither, it fails.

    In fixture mode (tests only) no network is used and the record says "fixture", which approvals refuse."""
    _warn_if_bypass_requested()
    relative = media["file"]
    local = Path(repo_root) / relative if repo_root else None
    has_local = bool(local and local.is_file())

    if MEDIA_CHECK_MODE == "fixture":
        return {"status": "fixture", "sha256": approvals.sha256_file(local) if has_local else None,
                "bytes": local.stat().st_size if has_local else media.get("bytes"),
                "note": "test fixture: not verified, can never be approved"}

    local_sha = None
    if has_local:
        inspected = media_tools.inspect_file(local)
        if not inspected["ok"]:
            raise MediaUnavailableError(f"{relative} is not a valid {media['type']}: " + "; ".join(inspected["problems"]))
        local_sha = approvals.sha256_file(local)
    elif not media.get("url"):
        raise MediaUnavailableError(f"{relative} does not exist in this checkout and has no URL")

    suffix = Path(relative).suffix or (".mp4" if media["type"] == "video" else ".png")
    downloaded, remote_sha, remote_size = _download_media(media["url"], suffix)

    try:
        served = media_tools.inspect_file(downloaded)
        if not served["ok"]:
            raise MediaUnavailableError(f"the media URL does not serve a valid {media['type']}: " + "; ".join(served["problems"]))

        wanted = expected_sha256 or local_sha
        if wanted and remote_sha != wanted:
            raise MediaUnavailableError(
                "the media URL serves different bytes than the file in the repository (push the latest media, "
                f"then preview again): URL {remote_sha[:12]}..., expected {wanted[:12]}...")

        if media.get("bytes") and not has_local and int(media["bytes"]) != remote_size:
            raise MediaUnavailableError(f"the media URL serves {remote_size} bytes but {media['bytes']} were expected")

        if media_tools.have_ffmpeg():
            problems = media_tools.validate_media(downloaded)
            if problems:
                raise MediaUnavailableError("the served media does not decode cleanly: " + "; ".join(problems))
            proof = "ffprobe"
        elif manifest_decode_proof(relative, remote_sha):
            proof = "manifest"
        else:
            raise MediaUnavailableError(
                "cannot prove the media decodes: FFmpeg is not installed here and the manifest has no successful "
                "check of these exact bytes (run scripts/media_manifest.py sync where FFmpeg is installed)")
    finally:
        downloaded.unlink(missing_ok=True)

    return {
        "status": "ok",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": remote_sha, "bytes": remote_size, "format": served["format"],
        "width": served["width"], "height": served["height"],
        "decode_proof": proof, "source": "repo" if has_local else "url",
    }


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


def media_check_summary(check):
    """One line for the draft summary."""
    if not isinstance(check, dict):
        return "NOT CHECKED"
    if check.get("status") == "ok":
        return f"ok (URL serves the repository bytes; decode proof: {check.get('decode_proof')})"
    return f"{str(check.get('status')).upper()}: {check.get('note') or 'not verified'}"


def buffer_asset(media):
    """The exact asset object sent to Buffer's createPost `assets` array (typed shape, not the deprecated one)."""
    return {"video": {"url": media["url"]}} if media["type"] == "video" else {"image": {"url": media["url"]}}


DRAFTS_THIS_RUN = []      # draft IDs issued by this process; reset at the start of every run (see main)


def build_draft(post, media, post_type, score, critique, attempts=None, media_check=None):
    """The approvable draft (see approvals.py) plus review details. Contains no secrets.

    Fails closed: without a passing media check there is no draft and so no approval code. `media_check` is the
    record from check_media_for_approval (made while choosing the media); if it is missing it is made here."""
    import media_selector

    if len(DRAFTS_THIS_RUN) >= MAX_DRAFTS_PER_RUN:
        raise RuntimeError(f"this run already issued {MAX_DRAFTS_PER_RUN} approval code; a run never issues more")

    if media_check is None:
        media_check = check_media_for_approval(media, media_selector.BASE_DIR)      # raises MediaUnavailableError

    if media_check.get("status") not in ("ok", "fixture"):
        raise MediaUnavailableError("no approval code is created without a passing media check")

    draft = approvals.create_draft(
        post, media, BUFFER_CHANNEL_ID, post_type=post_type["id"], run_id=os.environ.get("GITHUB_RUN_ID"),
        repo_root=media_selector.BASE_DIR, media_check=media_check,
        extra={
            "result": "winner",
            "weighted_length": post_rules.weighted_length(post),
            "critic_score": score,
            "buffer_assets_payload": [buffer_asset(media)],
            "text_contains_media_url": media["url"] in post,
            "critique": critique,
            "attempts": attempts or [],
            "media_facts": {key: media.get(key) for key in (
                "manifest_id", "level", "mode", "puzzle_state", "shows_action", "mechanisms", "theme", "aspect",
                "verification", "review", "game_build", "post_categories")},
            "selection_reason": media.get("reason"),
        })
    draft["media"]["selection"] = media.get("match")
    draft["media"]["url_check"] = media_check_summary(media_check)
    DRAFTS_THIS_RUN.append(draft["draft_id"])
    return draft


def format_draft_summary(draft):
    """Markdown for the run summary: readable on a phone, with the exact values to approve it."""
    media = draft["media"]
    lines = [
        "## Ringshift draft - NOT sent",
        "",
        *(["> **TEST FIXTURE: this draft cannot be approved or published.**", ""]
          if (media.get("check") or {}).get("status") != "ok" else []),
        f"**Draft ID (the approval code):** `{draft['draft_id']}`",
        f"**Expires:** {draft['expires_at']} (UTC)",
        "",
        "**Post text (exactly as it would be published):**",
        "",
        "> " + draft["text"].replace("\n", "\n> "),
        "",
        f"**Media:** `{media['file']}` ({media['type']}, {media.get('bytes')} bytes) - [open it]({media['url']})",
        f"**Media check:** {media.get('url_check')}",
        *([f"**Post type:** {draft.get('post_type')} - **Media facts:** "
           + ", ".join(f"{k}={v}" for k, v in (draft.get("media_facts") or {}).items()
                       if v not in (None, [], "") and k in ("level", "mode", "puzzle_state", "shows_action", "mechanisms", "verification"))]
          if draft.get("media_facts") else []),
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


def build_no_winner_report(media, post_type, attempts, note, skipped_types=None):
    """What a preview writes when nothing could be drafted, so the reason is visible instead of an empty run.
    `media` and `post_type` may be None when no post type had suitable media."""
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sent": False,
        "result": "no_candidate_passed",
        "note": note,
        "post_type": post_type["id"] if post_type else None,
        "target": {"service": "X (via Buffer)", "buffer_channel_id": BUFFER_CHANNEL_ID},
        "media": ({"file": media["file"], "type": media["type"], "url": media["url"],
                   "description": media.get("description"), "url_check": media_check_summary(media.get("media_check"))} if media else None),
        "attempts": attempts,
    }
    if skipped_types:
        report["post_types_skipped"] = skipped_types
    return report


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

def prepare_approved(draft_path, approval_code, expected_run_id=None, *, now=None, claimer=None):
    """Everything that must hold before an approved draft may be published, without claiming or sending.

    Returns (draft, media, claimer). Raises ApprovalError / RuntimeError for any problem."""
    import media_selector

    draft = approvals.load_draft(draft_path)
    media = approvals.verify_draft(
        draft, approval_code, now=now, channel_id=BUFFER_CHANNEL_ID, expected_run_id=expected_run_id,
        repo_root=media_selector.BASE_DIR, raw_prefix=media_selector.RAW_REPO)

    draft_id = draft["draft_id"]

    if any(entry.get("draft_id") == draft_id for entry in read_post_log()):
        raise approvals.AlreadyUsedError(f"draft {draft_id} was already published (it is in data/post_log.json)")

    claimer = claimer or approvals.default_claimer(COUNTER_FILE.parent)
    if claimer.is_used(draft_id):
        raise approvals.AlreadyUsedError(f"draft {draft_id} was already used (its claim exists)")

    try:
        # Download exactly what Buffer would fetch and prove it is the approved file: same bytes, valid, decodable.
        check_media_for_approval(media, media_selector.BASE_DIR, expected_sha256=media.get("sha256"))
    except MediaUnavailableError as error:
        raise approvals.ApprovalError("media_missing", str(error)) from error

    # The same deterministic rules every generated post must pass. Approval does not override them.
    facts = {key: value for key, value in (draft.get("media_facts") or {}).items() if value not in (None, [], "")}
    reason = screen_candidate(draft["text"], recent_post_texts(), load_project(), {**media, **facts})
    if reason:
        raise approvals.ApprovalError("rules", f"the approved text no longer passes the post rules: {reason}")

    check_state_ready()
    return draft, media, claimer


def verify_approved(draft_path, approval_code, expected_run_id=None, *, now=None, claimer=None):
    """Dry check of an approval: needs no Buffer key, claims nothing, sends nothing."""
    draft, media, _ = prepare_approved(draft_path, approval_code, expected_run_id, now=now, claimer=claimer)
    expires = approvals._parse(draft["expires_at"])
    hours = ((expires - (now or datetime.now(timezone.utc))).total_seconds()) / 3600
    print("\n--- RESULT ---\n")
    print(f"VERIFIED draft {draft['draft_id']} (nothing was claimed or sent): content hash matches, approval code matches, "
          f"{hours:.1f} h left before it expires, media file and URL check out, text passes the post rules, not used before.")
    print(f"It would publish: {draft['text']!r} with {media['file']} to channel {draft['target']['buffer_channel_id']}.")


def rehearse_claims():
    """Exercise the real claim mechanism in GitHub Actions on a throwaway tag, then clean it up."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("--rehearse-claims only makes sense inside GitHub Actions")
    results = approvals.rehearse_claims(approvals.default_claimer(COUNTER_FILE.parent))
    print("\n--- CLAIM REHEARSAL (throwaway tag, no Buffer) ---")
    for step, outcome in results:
        print(f"  {step}: {outcome}")
    print("CLAIM MECHANISM OK")


def final_publish_checks(draft, media):
    """Re-prove, right before publishing, that what Buffer will fetch is exactly what was approved: the target channel is
    the configured one, and the media URL serves the approved bytes (same SHA-256), as a valid file of the right
    structure and content type that decodes. Raises ApprovalError; nothing has been claimed or sent yet."""
    import media_selector

    if (draft.get("target") or {}).get("buffer_channel_id") != BUFFER_CHANNEL_ID:
        raise approvals.ApprovalError("wrong_channel", "the draft targets a different Buffer channel than the one configured")

    try:
        record = check_media_for_approval(media, media_selector.BASE_DIR, expected_sha256=media.get("sha256"))
    except MediaUnavailableError as error:
        raise approvals.ApprovalError("media_missing", f"final media check before publishing failed: {error}") from error

    if record.get("sha256") != media.get("sha256"):
        raise approvals.ApprovalError("media_changed", "the media served now is not the approved file")
    print(f"Final media check OK: {record['sha256'][:12]}... ({record['format']}, decode proof: {record['decode_proof']}).")


def publish_approved(draft_path, approval_code, expected_run_id=None, *, now=None, claimer=None):
    """Publish EXACTLY the text and media of an approved draft, once. The only code path that reaches Buffer.

    Nothing is regenerated and nothing is edited: the text and media come from the verified draft file.
    Every rejection below happens BEFORE the draft is claimed, so a rejected attempt does not use it up."""
    require_env("BUFFER_API_KEY")        # no Gemini key is needed or used

    draft, media, claimer = prepare_approved(draft_path, approval_code, expected_run_id, now=now, claimer=claimer)
    draft_id = draft["draft_id"]
    text = draft["text"]

    ensure_safe_to_publish()

    # The last full look at the target and the media, after every slower check (Buffer queue, state, spacing) and
    # immediately before the draft is claimed. Only the claim, the local log write and the size/type check inside
    # send_to_buffer sit between this and the Buffer call. A failure here does NOT use up the draft.
    final_publish_checks(draft, media)

    # Point of no return. After this the draft ID is spent, whatever happens next.
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


class NoSuitableMediaError(RuntimeError):
    """Raised by choose_post_and_media when no post type has media that fits it."""

    skipped = None


MEDIA_TRIES_PER_TYPE = 3          # how many different files may fail the media check for one post type before it is skipped


def choose_post_and_media(project, facts, *, counter, offset=0, rng=None, check=None):
    """Walk the post types in rotation order and return (post_type, media, skipped) for the first one that has
    media supporting it AND passing `check` (the mandatory media verification; see check_media_for_approval).
    `skipped` lists the types and files passed over, and why. Raises NoSuitableMediaError if nothing is usable:
    a post is never built on media that is missing, invalid, unreachable or different from the repository file."""
    import media_selector

    types = eligible_post_types(project, facts)
    start = (counter + offset) % len(types)
    skipped = []
    failed_files = set()

    for step in range(len(types)):
        post_type = types[(start + step) % len(types)]

        for _ in range(MEDIA_TRIES_PER_TYPE):
            try:
                media = media_selector.select_media(
                    "", require_description=True, require_tags=post_type["id"] == "mechanic",
                    allow_special_mode=False, avoid_solved_stills=bool(post_type.get("avoid_solved_still")),
                    post_type=post_type, rng=rng, exclude_files=failed_files)
            except media_selector.NoSuitableMediaError as error:
                skipped.append({"post_type": post_type["id"], "reason": str(error)})
                break

            if check is not None:
                try:
                    media["media_check"] = check(media)
                except MediaUnavailableError as error:
                    failed_files.add(media["file"])
                    skipped.append({"post_type": post_type["id"], "media": media["file"], "reason": f"media check failed: {error}"})
                    continue

            return post_type, media, skipped

    error = NoSuitableMediaError("; ".join(f"{e['post_type']}: {e['reason']}" for e in skipped) or "no post types are available")
    error.skipped = skipped
    raise error


# ---- main ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(description="Generate and queue one Ringshift post.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="generate and review a draft post and show it; saves nothing and sends nothing "
             "(needs GEMINI_API_KEY only). This is the only way to generate text.")
    parser.add_argument(
        "--draft-file", metavar="PATH",
        help="with --dry-run: also write the full draft (text, media, target channel, Buffer payload) to this JSON file")
    parser.add_argument("--publish-approved", metavar="DRAFT_JSON",
                        help="publish exactly the text and media of this approved preview draft (needs --approval-code)")
    parser.add_argument("--verify-approved", metavar="DRAFT_JSON",
                        help="check an approved draft exactly as --publish-approved would, but claim and send NOTHING")
    parser.add_argument("--rehearse-claims", action="store_true",
                        help="in GitHub Actions: exercise the claim mechanism on a throwaway tag (create, repeat, delete); no Buffer")
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

    if args.verify_approved:
        try:
            verify_approved(args.verify_approved, args.approval_code, args.expected_run_id)
        except (approvals.ApprovalError, RuntimeError) as error:
            print("\n--- RESULT ---\n")
            print(f"NOT VERIFIED ({getattr(error, 'reason', 'error')}): {error}")
            raise SystemExit(1) from error
        return

    if args.rehearse_claims:
        rehearse_claims()
        return

    # There is no other way to publish. Generating text is only ever a draft for a human to approve.
    if not args.dry_run:
        raise SystemExit(
            "autopilot.py never publishes on its own. Use --dry-run to write a draft (nothing is sent), or "
            "--publish-approved DRAFT.json --approval-code CODE to publish one approved draft.")

    # Fail fast, before spending any Gemini calls, if a key is missing or malformed.
    require_env("GEMINI_API_KEY")
    print("DRAFT ONLY: nothing will be saved or sent to Buffer.")
    DRAFTS_THIS_RUN.clear()

    project = load_project()
    facts = load_facts()

    # Choose the media FIRST so the text is written about what it really shows. Each post type only accepts media
    # that supports it (a challenge never gets media that shows the solution); a type with no fitting media is skipped.
    try:
        import media_selector
        post_type, media, skipped_types = choose_post_and_media(
            project, facts, counter=read_counter(), offset=draft_rotation_offset(),
            check=lambda candidate: check_media_for_approval(candidate, media_selector.BASE_DIR))
    except NoSuitableMediaError as error:
        print(f"NO DRAFT: {error}")
        report = build_no_winner_report(None, None, [], f"No post type had media that fits it: {error}",
                                        skipped_types=getattr(error, "skipped", None))
        if args.draft_file:
            write_draft_file(report, args.draft_file)
            print(f"Report written to {args.draft_file}")
        print("\n--- RESULT ---\n")
        print("NO CANDIDATE PASSED")
        return

    for entry in skipped_types:
        print(f"Skipped post type '{entry['post_type']}': {entry['reason']}")

    print(f"Post type: {post_type['label']}")
    print(f"Media: {media['file']} ({media['type']}, {media.get('match')})")
    print(f"Media shows: {media.get('description') or '(no verified description)'}")
    if media.get("puzzle_state"):
        print(f"Media facts: state={media.get('puzzle_state')}, action={media.get('shows_action')}, "
              f"mechanisms={', '.join(media.get('mechanisms') or []) or '-'}, verification={media.get('verification')}")

    candidates = generate_candidates(media, post_type, project, facts)

    print("\n--- GENERATED CANDIDATES ---\n")

    for i, candidate in enumerate(candidates, 1):
        print(f"--- CANDIDATE {i} ---")
        print(candidate)
        print()

    if not candidates:
        print("No candidates were generated.")
        if args.draft_file:
            write_draft_file(build_no_winner_report(media, post_type, [], "Gemini returned no parsable candidates.",
                                                    skipped_types=skipped_types), args.draft_file)
        return

    recent = recent_post_texts()
    sources = content_matching.evidence_sources(media, knowledge, [f for f in facts if f.get("kind") == post_type.get("fact_kind")])

    best_post = None
    best_critique = None
    best_score = -1
    attempts = []
    rejected = []          # (score, index, critique) of critic-rejected candidates, for one revision round
    critic_calls = 0

    # Repeats are removed first, by what the captions SAY: the same idea reworded, a repeat of an earlier post, or
    # two candidates that are one sentence with a word swapped. They cost no critic call and can never be drafted.
    candidates, repeats = caption_quality.distinct_candidates(candidates, recent, limit=CANDIDATE_COUNT)
    for text, why in repeats:
        print(f"--- DROPPED (repeat): {why}: {text!r} ---")
        attempts.append({"candidate": "repeat", "text": text, "outcome": "dropped before screening", "reason": why})

    if not candidates:
        if args.draft_file:
            write_draft_file(build_no_winner_report(
                media, post_type, attempts, "Every generated caption repeated an earlier post or another candidate.",
                skipped_types=skipped_types), args.draft_file)
            print(f"Report written to {args.draft_file}")
        print("\n--- RESULT ---\n")
        print("NO CANDIDATE PASSED")
        return

    def consider(text, label, source_index):
        """Screen and critique one candidate. Returns True if it is now the best passing post."""
        nonlocal best_post, best_critique, best_score, critic_calls

        # Never queue text that already exists, repeats an earlier idea, breaks the post rules,
        # names a mechanic the media does not show, or contradicts what the media shows.
        reason = screen_candidate(text, recent, project, media, post_type, facts)

        if reason:
            print(f"\n--- SKIPPED {label}: {reason} ---\n")
            attempts.append({"candidate": label, "text": text, "outcome": "rejected before the critic", "reason": reason})
            return False

        if critic_calls >= MAX_CRITIC_CALLS:
            print(f"\n--- SKIPPED {label}: the per-run critic limit ({MAX_CRITIC_CALLS}) was reached ---\n")
            attempts.append({"candidate": label, "text": text, "outcome": "not reviewed", "reason": "per-run critic call limit reached"})
            return False

        critic_calls += 1
        print(f"\n--- CRITIC {label} ---\n")

        critique = critique_post(text, media, post_type, facts)
        print(critique)

        keep, reasons = critique_verdict(critique, sources=sources)
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

        draft = build_draft(best_post, media, post_type, best_score, best_critique, attempts, media.get("media_check"))
        print("\n" + format_preview(best_post, media, post_type, best_score, best_critique))
        print(f"\nDRAFT ID (approval code): {draft['draft_id']}   expires {draft['expires_at']}")
        write_step_summary(format_draft_summary(draft))

        if args.draft_file:
            write_draft_file(draft, args.draft_file)
            print(f"Draft written to {args.draft_file}")

        print("\n--- RESULT ---\n")
        print("DRAFT COMPLETE: nothing was saved or sent.")
    else:
        if args.draft_file:
            write_draft_file(build_no_winner_report(
                media, post_type, attempts,
                "Every candidate was rejected. Nothing would be posted. Re-run preview, or see 'attempts' for the reasons.",
                skipped_types=skipped_types), args.draft_file)
            print(f"Report written to {args.draft_file}")

        print("\n--- RESULT ---\n")
        print("NO CANDIDATE PASSED")


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
