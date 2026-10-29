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
import caption_plan
import caption_quality
import content_matching
import dev_story
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
CANDIDATE_COUNT = 3              # captions the writer is asked for in each writing round
MAX_GENERATION_ROUNDS = 2        # a first round and, only if nothing passed, one better-aimed second round
FIRST_ROUND_CRITIC_CALLS = CANDIDATE_COUNT + 1      # each surviving first-round caption plus at most one critic rewrite
SECOND_ROUND_CRITIC_CALLS = 4    # second round: regenerated captions and verified-fact captions, interleaved
MAX_CRITIC_CALLS = FIRST_ROUND_CRITIC_CALLS + SECOND_ROUND_CRITIC_CALLS     # hard per-run limit (8)
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
# plus, for draft-only runs, the day (see draft_rotation_offset); two of every three rotation positions go to the
# developer-led types (dev_story.DEVELOPER_LED_TYPES), the third to the others, least recently published first.
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
    {"id": "dev_story", "label": "Developer story", "developer_led": True,
     "audience": "fellow indie developers and puzzle fans who like following a game being made",
     "objective": "Share what I built as Ringshift's developer, and invite the right people to connect.",
     "brief": "Speak as the indie developer building Ringshift, in the first person: the mechanic I built, the level I "
              "made, what makes this puzzle interesting, or a small development update, always about what THIS media "
              "verifiably shows. Use only verified facts and the knowledge's DEVELOPER VOICE and VERIFIED MECHANIC "
              "BEHAVIOUR sections; invent no history, anecdote or reaction. End the way the DEVELOPER VOICE block asks."},
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


def rotation_log():
    """The post log for the rotation (which types and endings were published last). A missing or unreadable log only
    means no history here; publishing itself still refuses a corrupt log (read_post_log)."""
    try:
        return read_post_log()
    except RuntimeError:
        return []


def rotation_types(project, facts=None, position=0, post_log=None):
    """The eligible post types in the order this rotation position tries them (see dev_story.rotation_order)."""
    return dev_story.rotation_order(eligible_post_types(project, facts), position,
                                    rotation_log() if post_log is None else post_log)


def pick_post_type(successful_posts, project, facts=None, post_log=None):
    return rotation_types(project, facts, successful_posts, post_log)[0]


def is_developer_led(post_type):
    return bool(post_type) and post_type.get("id") in dev_story.DEVELOPER_LED_TYPES


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


def recent_closings(limit=dev_story.CLOSING_WINDOW):
    """The newest posts whose closing lines a new post may not repeat: our own newest (the history file is oldest
    first) and Buffer's newest (its export is newest first)."""
    own = post_rules.extract_generated_posts(posts_history)[-limit:]
    queued = post_rules.extract_analytics_posts(analytics)[:limit]
    return own + [text for text in queued if text not in own]


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


def question_share(posts):
    """Fraction of `posts` that end in a question mark (0.0 when there are none)."""
    posts = [p for p in posts if str(p).strip()]
    return sum(1 for p in posts if str(p).strip().endswith("?")) / len(posts) if posts else 0.0


def own_recent_posts(limit=6):
    """Our own most recent generated posts, oldest first (content/generated_posts.md is append-only)."""
    return post_rules.extract_generated_posts(posts_history)[-limit:]


def generate_candidates(media=None, post_type=None, project=None, facts=None, *, seed=0, feedback=None):
    """Ask the writer for CANDIDATE_COUNT captions, each about a DIFFERENT verified subject (caption_plan.angle_slots).

    feedback: for the second writing round, what was tried in the first round and why it failed (see
    caption_plan.retry_feedback). It adds no facts; it only steers away from repeats, invented details and plain text."""
    project = project or {"project_url": None}
    post_type = post_type or POST_TYPES[0]
    recent = "\n".join(f"- {clean_for_prompt(t)}" for t in recent_post_texts()) or "(none yet)"
    media_block = describe_media(media) if media else "Attached media: not chosen yet."
    brief = content_matching.grounding_block(media or {}, post_type, facts)
    style = post_type.get("connection_style")
    developer = post_type.get("id") == dev_story.DEV_STORY and style in dev_story.CONNECTION_STYLES
    if developer:
        slots = dev_story.angle_slots(media or {}, style, seed, CANDIDATE_COUNT)
        brief += "\n\n" + dev_story.brief(style)
    else:
        slots = caption_plan.angle_slots(media or {}, post_type.get("id"), seed, CANDIDATE_COUNT)
    angles = "\n".join(f"  {i}. SUBJECT: {slot['focus']}. {slot['instruction']} FORM: {slot['shape']}."
                       for i, slot in enumerate(slots, 1))
    claims = caption_plan.verified_claims(media or {}, knowledge)
    if developer:
        claims += [c for c in dev_story.verified_claims(media or {}, knowledge) if c not in claims]
    safe_claims = "\n".join(f"- {claim}" for claim in claims) or "- (none)"
    writer = ("You are the indie developer of the puzzle game Ringshift, writing your own posts in the first person."
              if developer else "You are the social-media writer for the indie puzzle game Ringshift.")
    questions = question_share(own_recent_posts())
    variety = (f"{questions:.0%} of our last posts were questions: use a statement unless the post type needs a question."
               if questions >= 0.5 else "Vary sentence shape, length and opening words from the recent posts.")
    retry = f"\n{feedback}\n" if feedback else ""

    prompt = f"""
{writer}

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

--- SAFE CLAIMS (the only factual statements a post may make, in your own words) ---
{safe_claims}
--- END SAFE CLAIMS ---
{retry}
POST TYPE FOR THIS POST: {post_type['label']}
{post_type['brief']}

Write exactly {CANDIDATE_COUNT} DIFFERENT X posts for the attached media. Each has its OWN subject below; do not
mention the other posts' subjects, so the three are about different things, not one sentence reworded:
{angles}

What makes a good post here: the media does the explaining, so say something short, specific and a little
curious about its subject. A concrete verified detail, a contrast, or an honest opinion beats a vague feeling.
{variety}

Hard rules (a post that breaks one is rejected by code and wasted):
- Facts come ONLY from the knowledge and the verified media facts above. Nothing else is true.
- Name a mechanic only if it is listed under "Mechanics on screen", using exactly that name. Never use other
  names for mechanics (not "black hole", "satellite", "wormhole" or similar).
- Never say how a mechanic behaves, how the puzzle is solved, or how hard it is, unless the knowledge or the
  verified media facts say so. Never imply a solution exists or is unique unless stated.
- Never say where anything sits or what it is next to, what moves or shifts, or what a beam does or hits:
  positions and movements are not in the verified facts. The one exception is a mechanic's general behaviour
  in the words of the knowledge's VERIFIED MECHANIC BEHAVIOUR section, for a mechanic on screen, stated about
  the mechanic in general and never as something happening at a spot in this media.
- Never say a theme changes gameplay. Themes are visual.
- No numbers, durations, achievements, player reactions, reviews, sales, wishlists, awards, release dates,
  or personal experiences unless they appear in the verified media facts. Speaking as the developer ("I built
  this level") is fine where the knowledge's DEVELOPER VOICE section says so; development history, anecdotes and
  what anyone said are not.
- No engagement bait ("like and retweet", "follow for more", "tag a friend") and no stock ending that a recent
  post already used (for example "Let's connect" or "What are you working on?").
- A question must be answerable by looking at the media. Never ask the viewer to solve a puzzle the media shows solved.
- No @mentions. {link_rule(post_type, project)}
- Natural, curious, human tone. Not corporate, no marketing phrases, no "changes everything" style hype.
- Every post must refer to something concrete and verified: a mechanic listed under "Mechanics on screen", the
  level number, or the theme, or (for the puzzle-solving-experience subject) the game's verified identity in the
  knowledge's own words, such as rings and beams around a circular puzzle. No filler such as "a brand new idea",
  "a genuinely new thought", "check this out" or "you won't believe".
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

The level number and difficulty, whether it is solved, whether it is a still or a video, the visual theme name and the
"Mechanics on screen" list above are verified MEDIA facts: a post that states them is supported (label it MEDIA and
quote that line as evidence). Positions, movements and what a mechanic does are NOT among them.

What a mechanic does in general is KNOWLEDGE only when the knowledge's VERIFIED MECHANIC BEHAVIOUR section says it
(quote that line), the mechanic is on screen, and the post states it as a general fact, not as something happening at
a particular spot in this media. The developer speaking in the first person ("I built", "I'm building", "I hand-author",
"I check every level") is KNOWLEDGE only when the knowledge's DEVELOPER VOICE section says it (quote it). How long
something took, attempts, versions, what was changed or cut, what testers, friends or players said or felt, plans and
dates are UNSUPPORTED unless the verified facts say them. An invitation to follow, connect or share is not a claim.

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
- UNSUPPORTED: anything else. This includes how a mechanic behaves (beyond the VERIFIED MECHANIC BEHAVIOUR section), how the puzzle is solved or how many ways,
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
(NO if any claim is UNSUPPORTED, or the post is dull, hype-y, corporate, engagement bait, or insults the writer.)

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

    # Every post: no engagement bait, and no closing line a recent post already ended with ("Let's connect" every day).
    problems.extend(dev_story.bait_problems(candidate))
    problems.extend(dev_story.closing_problems(candidate, recent_closings()))

    # The developer story: first person, about a verified detail of THIS media, the run's ending style, and no
    # invented development history or anecdote.
    if (post_type or {}).get("id") == dev_story.DEV_STORY:
        problems.extend(dev_story.voice_problems(candidate, media or {}, (post_type or {}).get("connection_style")))

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


# Where a file may have come from to count as genuine gameplay. Both are written only by the manifest tooling:
# the capture tool plays the real game (and records its build), and "manual-recording" is a screen recording a person made
# and described from the frames. Anything else (no record, "unknown", a test fixture) is not gameplay evidence.
GAMEPLAY_SOURCES = {
    "capture_gameplay.py": ("verified",),
    "manual-recording": ("reviewed", "verified"),
}


def manifest_provenance(relative, sha256):
    """The manifest evidence that `relative` is genuine Ringshift gameplay, bound to these exact bytes. Raises
    MediaUnavailableError if there is none. A valid file signature or a decodable picture is NOT enough: the committed
    media/manifest.json must have a record for this path whose SHA-256 equals the bytes being posted, which was not
    excluded or failed, comes from a recognised gameplay source, carries the game facts the caption is written from,
    and (for the capture tool) names the game build it was captured from. Looked up here, never read from a draft."""
    import media_selector
    try:
        records = media_selector._manifest_records()
    except media_selector.NoSuitableMediaError as error:
        raise MediaUnavailableError(f"cannot show where {relative} came from: {error}") from error
    if records is None:
        raise MediaUnavailableError(f"cannot show where {relative} came from: there is no media/manifest.json in this checkout")

    record = records.get(relative)
    if not isinstance(record, dict):
        raise MediaUnavailableError(f"{relative} is not in media/manifest.json, so it is not known gameplay media")
    if record.get("sha256") != sha256:
        raise MediaUnavailableError(
            f"the manifest describes different bytes for {relative} than the ones being posted "
            f"(manifest {str(record.get('sha256'))[:12]}..., posting {str(sha256)[:12]}...); run media_manifest.py sync")
    if record.get("exclude"):
        raise MediaUnavailableError(f"{relative} is excluded in the manifest")
    validation = (record.get("validation") or {}).get("status")
    if validation != "ok":
        raise MediaUnavailableError(f"the manifest does not mark {relative} as valid (validation: {validation})")

    capture = record.get("capture") or {}
    source = capture.get("source")
    allowed = GAMEPLAY_SOURCES.get(source)
    if allowed is None:
        raise MediaUnavailableError(f"{relative} has no recognised gameplay source in the manifest (source: {source!r})")
    verification = (record.get("verification") or {}).get("status")
    if verification not in allowed:
        raise MediaUnavailableError(f"{relative} is {verification!r} in the manifest; {source} media must be {' or '.join(allowed)}")
    if source == "capture_gameplay.py" and not capture.get("game_build"):
        raise MediaUnavailableError(f"{relative} was captured without a recorded game build")

    game = record.get("game") or {}
    mechanisms = [m for m in record.get("mechanisms") or [] if m]
    if game.get("level") is None or not mechanisms or not str(record.get("description") or "").strip():
        raise MediaUnavailableError(f"{relative} has no recorded level, mechanisms and description, so a caption cannot be matched to it")

    return {"manifest_id": record.get("id"), "source": source, "game_build": capture.get("game_build"),
            "verification": verification, "level": game.get("level"), "mode": game.get("mode"),
            "mechanisms": mechanisms, "manifest_sha256": record.get("sha256")}


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

        provenance = manifest_provenance(relative, remote_sha)

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
        "provenance": provenance,
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
        proof = check.get("provenance") or {}
        origin = (f"; manifest {proof.get('manifest_id')} ({proof.get('source')}, {proof.get('verification')}, "
                  f"level {proof.get('level')})") if proof else ""
        return f"ok (URL serves the repository bytes; decode proof: {check.get('decode_proof')}{origin})"
    return f"{str(check.get('status')).upper()}: {check.get('note') or 'not verified'}"


def buffer_asset(media):
    """The exact asset object sent to Buffer's createPost `assets` array (typed shape, not the deprecated one)."""
    return {"video": {"url": media["url"]}} if media["type"] == "video" else {"image": {"url": media["url"]}}


DRAFTS_THIS_RUN = []      # draft IDs issued by this process; reset at the start of every run (see main)


def build_draft(post, media, post_type, score, critique, attempts=None, media_check=None, pinned_media=None):
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
            "pinned_media": pinned_media,
            "developer_led": is_developer_led(post_type),
            "connection_style": post_type.get("connection_style"),
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
        *([f"**Post type:** {draft.get('post_type')}"
           + (f" (ending: {draft['connection_style']})" if draft.get("connection_style") else "") + " - **Media facts:** "
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


def is_approvable_draft(draft):
    """A winning draft whose media passed the mandatory check (so a person could approve it). Fixture drafts, drafts
    without a passing check and "no candidate" reports are not."""
    return (isinstance(draft, dict) and draft.get("result") == "winner" and bool(draft.get("draft_id"))
            and ((draft.get("media") or {}).get("check") or {}).get("status") == "ok")


def write_draft_file(draft, path, title=None):
    atomic_write(path, json.dumps(draft, indent=2, ensure_ascii=False) + "\n")

    # A GitHub Actions annotation, so the draft can be read from the run without downloading anything.
    # Annotations are cut at about 4 KB, so only the essentials go here; the artifact has everything.
    # A winning draft that is not approvable (a test fixture, an unchecked draft) is never announced as a draft.
    announce = draft.get("result") != "winner" or is_approvable_draft(draft)
    if os.environ.get("GITHUB_ACTIONS") == "true" and announce:
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
            "diagnosis": (draft.get("diagnosis") or {}).get("summary") if draft.get("result") != "winner" else None,
            "rejected": [{"text": (a.get("text") or "")[:110], "round": a.get("round"),
                          "why": (a.get("reason") or "; ".join(a.get("reasons") or []))[:100]}
                         for a in (draft.get("attempts") or []) if a.get("outcome") != "critic KEEP"][:6]
            if draft.get("result") != "winner" else None,
        }
        encoded = json.dumps({k: v for k, v in compact.items() if v is not None}, ensure_ascii=False)
        escaped = encoded.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::notice title={title or 'Ringshift preview (not sent)'}::{escaped}")


LAST_DIAGNOSIS = {}       # why the last generate_draft in this process found no caption; reset at the start of every run


def build_no_winner_report(media, post_type, attempts, note, skipped_types=None, diagnosis=None):
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
    if diagnosis:
        report["diagnosis"] = diagnosis
    return report


def write_step_summary(text):
    """Append to the Actions job summary. Only a real, strict-mode run writes drafts there: in fixture mode (unit tests)
    nothing is written, so a job summary can never show a test fixture as if it were a draft."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")

    if not path or MEDIA_CHECK_MODE != "strict":
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
    if not reason and draft.get("post_type") == dev_story.DEV_STORY:
        voice = dev_story.voice_problems(draft["text"], {**media, **facts}, draft.get("connection_style"))
        reason = ("failed post rules: " + "; ".join(voice)) if voice else None
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


def publish_approved(draft_path, approval_code, expected_run_id=None, *, now=None, claimer=None, approver=None,
                     before_claim=None):
    """Publish EXACTLY the text and media of an approved draft, once. The only code path that reaches Buffer.

    Nothing is regenerated and nothing is edited: the text and media come from the verified draft file.
    Every rejection below happens BEFORE the draft is claimed, so a rejected attempt does not use it up.

    approver: who approved it, for the log ("autopilot (unattended daily run)" for the schedule; default: the GitHub
    actor who typed the approval code). before_claim: called after every check and right before the draft is claimed
    (the unattended run claims the day there); if it raises, nothing is claimed or sent."""
    require_env("BUFFER_API_KEY")        # no Gemini key is needed or used

    draft, media, claimer = prepare_approved(draft_path, approval_code, expected_run_id, now=now, claimer=claimer)
    draft_id = draft["draft_id"]
    text = draft["text"]
    approver = approver or os.environ.get("GITHUB_ACTOR")

    ensure_safe_to_publish()

    # The last full look at the target and the media, after every slower check (Buffer queue, state, spacing) and
    # immediately before the draft is claimed. Only the claim, the local log write and the size/type check inside
    # send_to_buffer sit between this and the Buffer call. A failure here does NOT use up the draft.
    final_publish_checks(draft, media)

    if before_claim is not None:
        before_claim(draft)

    # Point of no return. After this the draft ID is spent, whatever happens next.
    claimer.claim(draft_id, {"actor": approver, "content_hash": draft["content_hash"]})
    print(f"Draft {draft_id} claimed. It cannot be used again.")

    save_post(text, f"Approved draft {draft_id}, approved by {approver or 'a local user'}")

    post_type = next((t for t in POST_TYPES if t["id"] == draft.get("post_type")), None)

    try:
        send_to_buffer(text, media, post_type, extra_log={
            "draft_id": draft_id, "content_hash": draft["content_hash"],
            "approved_by": approver, "approved_run_id": draft.get("run_id"),
            # what the rotation reads back: developer-led share and the last ending used
            "developer_led": is_developer_led(post_type), "connection_style": draft.get("connection_style")})
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


PINNED_MEDIA_RE = re.compile(r"^(?:media|store)/(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9_ ./-]*$")      # store/ = media-store branch


def validate_pinned_media(value):
    """The repository path a person pinned a preview to, or an error. Only a plain relative path under media/ with a
    supported extension is accepted: no absolute paths, no '..', no URLs, no shell characters."""
    import media_selector

    text = (value or "").strip()
    if not text or not PINNED_MEDIA_RE.match(text) or "//" in text or text.endswith("/") \
            or Path(text).suffix.lower() not in media_selector.SUPPORTED_EXTENSIONS:
        raise SystemExit(
            f"--media-file must be a repository path under media/ (or a store/ path of the media-store branch) to a "
            f"supported file (got {value!r}), for example media/gameplay/06_hard15_five_mechanisms.png")
    return text


def choose_post_and_media(project, facts, *, counter, offset=0, rng=None, check=None, only_file=None):
    """Walk the post types in rotation order and return (post_type, media, skipped) for the first one that has
    media supporting it AND passing `check` (the mandatory media verification; see check_media_for_approval).
    `skipped` lists the types and files passed over, and why. Raises NoSuitableMediaError if nothing is usable:
    a post is never built on media that is missing, invalid, unreachable or different from the repository file.

    only_file: a person-pinned repository path. The walk is unchanged (post-type fit, mandatory check, tries per type);
    the only difference is that no other file can be chosen. If the pinned file fits no post type, or fails the
    check, the result is NoSuitableMediaError with the reasons, never a different file."""
    import media_selector

    types = rotation_types(project, facts, counter + offset)
    skipped = []
    failed_files = set()

    for post_type in types:

        for _ in range(MEDIA_TRIES_PER_TYPE):
            try:
                media = media_selector.select_media(
                    "", require_description=True, require_tags=post_type["id"] == "mechanic",
                    allow_special_mode=False, avoid_solved_stills=bool(post_type.get("avoid_solved_still")),
                    post_type=post_type, rng=rng, exclude_files=failed_files,
                    only_files=None if only_file is None else {only_file})
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

def generate_draft(args):
    """Choose media, write captions, screen and critique them, and build at most one approvable draft.

    Returns the winning draft (also written to args.draft_file) or None. Sends nothing: a draft only reaches Buffer
    through publish_approved, either with a code a person typed or, in the unattended daily run, through auto_publish.
    """
    # A pinned file is validated before anything else, so a bad path costs no Gemini call.
    pinned = validate_pinned_media(args.media_file) if args.media_file else None

    # Fail fast, before spending any Gemini calls, if a key is missing or malformed.
    require_env("GEMINI_API_KEY")
    if not getattr(args, "auto_publish", False):
        print("DRAFT ONLY: nothing will be saved or sent to Buffer.")
    if pinned:
        print(f"PINNED MEDIA: only {pinned} may be used. If it does not fit a post type or fails a check there is no draft.")
    DRAFTS_THIS_RUN.clear()
    LAST_DIAGNOSIS.clear()

    project = load_project()
    facts = load_facts()

    # Choose the media FIRST so the text is written about what it really shows. Each post type only accepts media
    # that supports it (a challenge never gets media that shows the solution); a type with no fitting media is skipped.
    try:
        import media_selector
        post_type, media, skipped_types = choose_post_and_media(
            project, facts, counter=read_counter(), offset=draft_rotation_offset(),
            check=lambda candidate: check_media_for_approval(candidate, media_selector.BASE_DIR),
            only_file=pinned)
    except NoSuitableMediaError as error:
        print(f"NO DRAFT: {error}")
        report = build_no_winner_report(None, None, [], f"No post type had media that fits it: {error}",
                                        skipped_types=getattr(error, "skipped", None))
        if args.draft_file:
            write_draft_file(report, args.draft_file)
            print(f"Report written to {args.draft_file}")
        print("\n--- RESULT ---\n")
        print("NO CANDIDATE PASSED")
        return None
    if pinned and media["file"] != pinned:
        raise SystemExit(f"internal error: the draft was about {media['file']} but {pinned} was pinned; no draft was made")

    for entry in skipped_types:
        print(f"Skipped post type '{entry['post_type']}': {entry['reason']}")

    seed = read_counter() + draft_rotation_offset()
    if post_type["id"] == dev_story.DEV_STORY:
        own = own_recent_posts()
        style = dev_story.choose_connection_style(seed, rotation_log(), question_share(own), len(own))
        post_type = dict(post_type, connection_style=style)
        print(f"Post type: {post_type['label']} (developer-led; ending: {dev_story.CONNECTION_STYLES[style]['label']})")
    else:
        print(f"Post type: {post_type['label']}" + (" (developer-led)" if is_developer_led(post_type) else ""))
    print(f"Media: {media['file']} ({media['type']}, {media.get('match')})")
    print(f"Media shows: {media.get('description') or '(no verified description)'}")
    if media.get("puzzle_state"):
        print(f"Media facts: state={media.get('puzzle_state')}, action={media.get('shows_action')}, "
              f"mechanisms={', '.join(media.get('mechanisms') or []) or '-'}, verification={media.get('verification')}")

    candidates = generate_candidates(media, post_type, project, facts, seed=seed)

    print("\n--- GENERATED CANDIDATES ---\n")

    for i, candidate in enumerate(candidates, 1):
        print(f"--- CANDIDATE {i} ---")
        print(candidate)
        print()

    recent = recent_post_texts()
    sources = content_matching.evidence_sources(media, knowledge, [f for f in facts if f.get("kind") == post_type.get("fact_kind")])

    best_post = None
    best_critique = None
    best_score = -1
    attempts = []
    rejected = []          # (score, index, critique) of critic-rejected candidates, for one revision round
    critic_calls = 0
    tried = []             # every caption text screened this run, so the second round never re-reviews one
    rounds = 1

    def already_tried(text):
        for other in tried:
            same, why = caption_quality.same_idea(text, other)
            if same:
                return why
        return None

    def consider(text, label, source_index, round_no, critic_limit):
        """Screen and critique one candidate. Returns True if it is now the best passing post."""
        nonlocal best_post, best_critique, best_score, critic_calls
        tried.append(text)

        # Never queue text that already exists, repeats an earlier idea, breaks the post rules,
        # names a mechanic the media does not show, or contradicts what the media shows.
        reason = screen_candidate(text, recent, project, media, post_type, facts)

        if reason:
            print(f"\n--- SKIPPED {label}: {reason} ---\n")
            attempts.append({"candidate": label, "round": round_no, "text": text, "outcome": "rejected before the critic",
                             "reason": reason})
            return False

        if critic_calls >= min(critic_limit, MAX_CRITIC_CALLS):
            print(f"\n--- SKIPPED {label}: the critic call limit for this round was reached ---\n")
            attempts.append({"candidate": label, "round": round_no, "text": text, "outcome": "not reviewed",
                             "reason": "per-run critic call limit reached"})
            return False

        critic_calls += 1
        print(f"\n--- CRITIC {label} ---\n")

        critique = critique_post(text, media, post_type, facts)
        print(critique)

        keep, reasons = critique_verdict(critique, sources=sources)
        score = get_score(critique)
        attempts.append({"candidate": label, "round": round_no, "text": text,
                         "outcome": "critic KEEP" if keep else "critic rejected",
                         "score": score, "reasons": reasons, "critique": critique[:1500]})

        if not keep:
            print(f"--- REJECTED {label}: " + "; ".join(reasons) + " ---")
            rejected.append((score, source_index, critique))
            return False

        if score > best_score:
            best_score, best_post, best_critique = score, text, critique
            return True

        return False

    # ---- round 1 ----------------------------------------------------------------------------------------------
    # Repeats are removed first, by what the captions SAY: the same idea reworded, a repeat of an earlier post, or
    # two candidates that are one sentence with a word swapped. They cost no critic call and can never be drafted.
    candidates, repeats = caption_quality.distinct_candidates(candidates, recent, limit=CANDIDATE_COUNT)
    for text, why in repeats:
        print(f"--- DROPPED (repeat): {why}: {text!r} ---")
        tried.append(text)
        attempts.append({"candidate": "repeat", "round": 1, "text": text, "outcome": "dropped before screening", "reason": why})
    if not candidates:
        print("No usable first-round candidates (none generated, or every one was a repeat).")

    for i, candidate in enumerate(candidates, 1):
        consider(candidate, f"candidate {i}", i, 1, FIRST_ROUND_CRITIC_CALLS)

    # One revision: the critic's own rewrite of the best rejected candidate is only a NEW candidate.
    # It goes through exactly the same screening and critic; a rejection is never overridden.
    if not best_post:
        for score, source_index, critique in sorted(rejected, key=lambda r: -r[0]):
            revised = improved_version(critique)
            if revised and revised not in candidates:
                print(f"\nTrying the critic's rewrite of candidate {source_index} as a new candidate.")
                consider(revised, f"revision of candidate {source_index}", source_index, 1, FIRST_ROUND_CRITIC_CALLS)
                break

    # ---- round 2: only when nothing passed --------------------------------------------------------------------
    # A second, better-aimed writing round: the writer sees every first-round caption and why it failed (repeat,
    # invented detail, too plain: the near-threshold case), and the pool also holds captions assembled only from
    # verified fields. Every one of them is screened and must pass the same evidence-checked critic and minimum score.
    if not best_post and MAX_GENERATION_ROUNDS > 1:
        rounds = 2
        feedback = caption_plan.retry_feedback(attempts, MIN_TOTAL_SCORE)
        close = caption_plan.near_miss(attempts, MIN_TOTAL_SCORE)
        print("\n--- SECOND ROUND ---\n")
        if close:
            print(f"Near miss: {close['text']!r} scored {close['score']}/60 (minimum {MIN_TOTAL_SCORE}); asking for "
                  "captions as grounded as that one, with a hook.")
        try:
            regenerated = generate_candidates(media, post_type, project, facts, seed=seed + 1, feedback=feedback)
        except Exception as error:          # a writer outage still leaves the verified-fact captions to review
            print(f"The second writing round failed ({error}); continuing with the verified-fact captions only.")
            attempts.append({"candidate": "second round", "round": 2, "outcome": "not generated",
                             "reason": f"writer error: {str(error)[:200]}"})
            regenerated = []
        regenerated, repeats = caption_quality.distinct_candidates(regenerated, recent, limit=CANDIDATE_COUNT)
        for text, why in repeats:
            print(f"--- DROPPED (repeat): {why}: {text!r} ---")
            attempts.append({"candidate": "repeat", "round": 2, "text": text, "outcome": "dropped before screening",
                             "reason": why})

        written = [(text, f"second-round candidate {i}") for i, text in enumerate(regenerated, 1)]
        if post_type.get("connection_style"):
            floor = dev_story.fact_captions(media, post_type["connection_style"], seed)
        else:
            floor = caption_plan.fact_captions(media, seed)
        grounded = [(text, f"verified-fact caption {i}") for i, text in enumerate(floor, 1)]
        pool = [item for pair in zip(written, grounded) for item in pair]
        pool += written[len(grounded):] + grounded[len(written):]

        for index, (text, label) in enumerate(pool, 1):
            if best_post or critic_calls >= MAX_CRITIC_CALLS:
                break          # the second round stops at the first caption that passes every check
            why = already_tried(text)
            if why:
                print(f"--- DROPPED {label}: repeats a caption already tried in this run ({why}) ---")
                attempts.append({"candidate": label, "round": 2, "text": text, "outcome": "dropped before screening",
                                 "reason": f"repeats a caption already tried in this run ({why})"})
                continue
            consider(text, label, index, 2, MAX_CRITIC_CALLS)

    if best_post:
        print("\n--- WINNER ---\n")
        print(best_post)
        print(f"\nScore: {best_score}/60")

        draft = build_draft(best_post, media, post_type, best_score, best_critique, attempts, media.get("media_check"), pinned)
        print("\n" + format_preview(best_post, media, post_type, best_score, best_critique))
        print(f"\nDRAFT ID (approval code): {draft['draft_id']}   expires {draft['expires_at']}")
        unattended = bool(getattr(args, "auto_publish", False)) and autopublish_enabled()
        if not is_approvable_draft(draft):
            print("NOTE: this draft is not approvable (its media was not verified), so it is not shown as a draft in the run summary.")
        elif not unattended:
            write_step_summary(format_draft_summary(draft))      # the unattended run writes its own outcome summary

        if args.draft_file:
            write_draft_file(draft, args.draft_file,
                             title="Ringshift daily draft (publishing next)" if unattended else None)
            print(f"Draft written to {args.draft_file}")

        print("\n--- RESULT ---\n")
        print("DRAFT COMPLETE: nothing was saved or sent yet.")
        return draft

    diagnosis = caption_plan.diagnose(attempts, MIN_TOTAL_SCORE, rounds=rounds, critic_calls=critic_calls)
    LAST_DIAGNOSIS.update(diagnosis)
    print("\n--- DIAGNOSIS ---\n")
    print(diagnosis["summary"])
    if args.draft_file:
        write_draft_file(build_no_winner_report(
            media, post_type, attempts,
            "Every candidate was rejected. Nothing would be posted. See 'diagnosis' and 'attempts' for the reasons.",
            skipped_types=skipped_types, diagnosis=diagnosis), args.draft_file)
        print(f"Report written to {args.draft_file}")

    print("\n--- RESULT ---\n")
    print("NO CANDIDATE PASSED")
    return None


AUTOPUBLISH_OFF = ("off", "0", "false", "no", "disabled", "draft-only")


def autopublish_enabled():
    """The unattended daily run publishes unless the repository variable RINGSHIFT_AUTOPUBLISH says off/draft-only.
    Then the schedule falls back to the old behaviour: a draft a person approves with publish-approved."""
    return str(os.environ.get("RINGSHIFT_AUTOPUBLISH", "on")).strip().lower() not in AUTOPUBLISH_OFF


LOCAL_TZ = "Asia/Riyadh"


def local_day(now=None):
    """The calendar day (YYYY-MM-DD) in Riyadh: the unattended run posts at most once per local day."""
    from zoneinfo import ZoneInfo
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(LOCAL_TZ)).date().isoformat()


def write_result(path, result):
    """A small JSON outcome for the workflow summary ({status, reason, ...}). Never holds secrets."""
    if path:
        atomic_write(path, json.dumps(result, indent=2, ensure_ascii=False) + "\n")


def check_publish_window(*, now=None, day_claimer=None, fetch=None):
    """Read-only: may the unattended run post today? Returns (ok, reason). Checks the day claim (already posted today),
    the 12 h spacing in the post log and Buffer's queue (a post already waiting). Fails closed when Buffer cannot be
    read. Claims, creates and sends nothing."""
    now = now or datetime.now(timezone.utc)
    day = local_day(now)
    try:
        claimer = day_claimer or approvals.day_claimer(COUNTER_FILE.parent)
        if claimer.is_used(day):
            return False, f"a post was already queued for {day} (Riyadh); nothing more to do today"
    except approvals.ApprovalError as error:
        return False, f"could not check whether {day} already has a post: {error}"
    try:
        ensure_safe_to_publish(now=now, fetch=fetch, allow_extra=False)
    except RuntimeError as error:
        return False, str(error)
    return True, f"no post queued for {day} yet and Buffer has none waiting"


def auto_publish(draft, draft_path, *, now=None, claimer=None, day_claimer=None):
    """Publish today's draft without a person typing its code, through EXACTLY the same verified path as
    publish-approved: integrity hash, expiry, run, channel, media re-download and decode, provenance, post rules,
    Buffer queue check, 12 h spacing, single-use draft claim. On top of that the local day is claimed right before the
    draft, so a second run on the same day can never post again."""
    if not is_approvable_draft(draft):
        raise approvals.ApprovalError("unverified_media", "the draft is not approvable (its media did not pass the checks)")
    if not os.environ.get("GITHUB_RUN_ID") and os.environ.get("GITHUB_ACTIONS") == "true":
        raise RuntimeError("GITHUB_RUN_ID is missing; refusing to publish without a run to bind the draft to")
    day = local_day(now)
    days = day_claimer or approvals.day_claimer(COUNTER_FILE.parent)

    def claim_day(_draft):
        days.claim(day, {"draft_id": _draft["draft_id"], "run_id": os.environ.get("GITHUB_RUN_ID")})
        print(f"Day {day} claimed: no other run can post today.")

    publish_approved(draft_path, draft["draft_id"], os.environ.get("GITHUB_RUN_ID"), now=now, claimer=claimer,
                     approver="autopilot (unattended daily run)", before_claim=claim_day)


def last_logged_post():
    try:
        log = read_post_log()
    except RuntimeError:
        return {}
    return log[-1] if log else {}


def format_auto_summary(result):
    status = result.get("status")
    title = {"queued": "QUEUED in Buffer (with media confirmed)", "disabled": "NOT published: auto-publish is switched off",
             "no_draft": "NOT published: no caption passed every check", "refused": "NOT published: refused by a safety check",
             "failed": "NOT published: error"}.get(status, status)
    lines = [f"## Ringshift daily post: {title}", ""]
    if result.get("reason"):
        lines += [f"**Why:** {result['reason']}", ""]
    if result.get("text"):
        lines += ["**Post text:**", "", "> " + result["text"].replace("\n", "\n> "), ""]
    for label, key in (("Media", "media_file"), ("Media URL", "media_url"), ("Draft ID", "draft_id"),
                       ("Buffer post ID", "buffer_id"), ("Buffer will publish at (UTC)", "due_at"), ("Post type", "post_type"),
                       ("Ending", "connection_style"), ("Critic score", "critic_score")):
        if result.get(key) not in (None, ""):
            lines.append(f"- **{label}:** `{result[key]}`")
    if status == "queued":
        lines += ["", "Buffer posts it to X at the time above. This run did not see X itself."]
    return "\n".join(lines) + "\n"


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Generate and queue one Ringshift post.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="generate and review a draft post and show it; saves nothing and sends nothing "
             "(needs GEMINI_API_KEY only).")
    parser.add_argument(
        "--auto-publish", action="store_true",
        help="the unattended daily run: write a draft exactly like --dry-run, then publish it through the same checks "
             "as --publish-approved (needs GEMINI_API_KEY and BUFFER_API_KEY, and --draft-file). Posts at most once per "
             "day. With the repository variable RINGSHIFT_AUTOPUBLISH=off it only writes the draft.")
    parser.add_argument(
        "--draft-file", metavar="PATH",
        help="with --dry-run / --auto-publish: also write the full draft (text, media, target channel, Buffer payload) to this JSON file")
    parser.add_argument(
        "--media-file", metavar="PATH",
        help="with --dry-run / --auto-publish: write the post about THIS media file (media/... in the repository or "
             "store/... on the media-store branch) instead of the rotation's choice. Every check still applies; if the "
             "file does not fit or fails a check there is no draft, never a different file.")
    parser.add_argument("--result-file", metavar="PATH", help="with --auto-publish: write the outcome as JSON here")
    parser.add_argument("--check-publish-window", action="store_true",
                        help="read-only: say whether the unattended run may post today (writes should_post to GITHUB_OUTPUT)")
    parser.add_argument("--publish-approved", metavar="DRAFT_JSON",
                        help="publish exactly the text and media of this approved preview draft (needs --approval-code)")
    parser.add_argument("--verify-approved", metavar="DRAFT_JSON",
                        help="check an approved draft exactly as --publish-approved would, but claim and send NOTHING")
    parser.add_argument("--rehearse-claims", action="store_true",
                        help="in GitHub Actions: exercise the claim mechanism on a throwaway tag (create, repeat, delete); no Buffer")
    parser.add_argument("--approval-code", help="the draft ID you read in the preview; typing it is your approval")
    parser.add_argument("--expected-run-id", help="the preview run number the draft must come from")
    return parser.parse_args(argv)


def _github_output(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path and MEDIA_CHECK_MODE == "strict":
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def main(argv=None):
    args = parse_args(argv if argv is not None else [])

    # Test-fixture media checking exists only for unit tests. A real GitHub Actions process must never be in it.
    if os.environ.get("GITHUB_ACTIONS") == "true" and MEDIA_CHECK_MODE != "strict":
        raise SystemExit("refusing to run in test-fixture media mode inside GitHub Actions: media checks must be strict")

    if args.media_file and (args.publish_approved or args.verify_approved or args.rehearse_claims
                            or not (args.dry_run or args.auto_publish)):
        raise SystemExit("--media-file only applies to --dry-run or --auto-publish. A published post is exactly its approved draft.")

    if args.auto_publish and (args.dry_run or args.publish_approved or args.verify_approved or args.rehearse_claims):
        raise SystemExit("--auto-publish cannot be combined with another mode")

    if args.check_publish_window:
        ok, reason = check_publish_window()
        print(("MAY POST: " if ok else "WILL NOT POST: ") + reason)
        _github_output({"should_post": "true" if ok else "false", "window_reason": " ".join(reason.split())[:300]})
        return

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

    if not (args.dry_run or args.auto_publish):
        raise SystemExit(
            "autopilot.py does not publish by default. Use --dry-run to write a draft (nothing is sent), "
            "--auto-publish (the unattended daily run), or --publish-approved DRAFT.json --approval-code CODE.")

    if not args.auto_publish:
        generate_draft(args)
        return

    # ---- the unattended daily run ----
    if not args.draft_file:
        raise SystemExit("--auto-publish needs --draft-file (the draft is kept as the record of what was posted)")
    enabled = autopublish_enabled()
    if enabled:
        require_env("GEMINI_API_KEY", "BUFFER_API_KEY")      # both, before any Gemini call is spent
        print("UNATTENDED DAILY RUN: the draft below is published if, and only if, it passes every check.")
    else:
        print("AUTO-PUBLISH IS SWITCHED OFF (repository variable RINGSHIFT_AUTOPUBLISH): writing a draft only.")

    result = {"status": "failed", "media_file": args.media_file}
    try:
        draft = generate_draft(args)
        if not draft:
            why = LAST_DIAGNOSIS.get("summary") or "no caption passed the screening and the critic"
            result.update(status="no_draft", reason=f"{why} (details: the preview report artifact)",
                          diagnosis=dict(LAST_DIAGNOSIS) or None)
        elif not enabled:
            result.update(status="disabled", reason="RINGSHIFT_AUTOPUBLISH is off; approve the draft with publish-approved",
                          draft_id=draft["draft_id"], text=draft["text"])
        else:
            result.update(draft_id=draft["draft_id"], text=draft["text"], media_url=draft["media"]["url"],
                          post_type=draft.get("post_type"), critic_score=draft.get("critic_score"),
                          connection_style=draft.get("connection_style"))
            auto_publish(draft, args.draft_file)
            logged = last_logged_post()
            result.update(status="queued", reason="published through every approval check", buffer_id=logged.get("buffer_id"),
                          due_at=logged.get("due_at"))
            print("\n--- RESULT ---\n")
            print(f"DAILY POST QUEUED WITH MEDIA CONFIRMED (draft {draft['draft_id']}, Buffer {logged.get('buffer_id')})")
    except approvals.ApprovalError as error:
        result.update(status="refused", reason=f"{error.reason}: {error}")
    except RuntimeError as error:
        result.update(status="refused", reason=str(error))
    except SystemExit as error:          # publish_approved exits 1 after a Buffer error (the draft stays claimed)
        result.update(status="failed", reason="Buffer did not confirm the post; check Buffer before retrying (see the log)")
        write_result(args.result_file, result)
        write_step_summary(format_auto_summary(result))
        raise

    write_result(args.result_file, result)
    write_step_summary(format_auto_summary(result))
    if result["status"] != "queued":
        print("\n--- RESULT ---\n")
        print(f"NOT PUBLISHED ({result['status']}): {result.get('reason')}")
        if result["status"] != "disabled":
            raise SystemExit(1)


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
