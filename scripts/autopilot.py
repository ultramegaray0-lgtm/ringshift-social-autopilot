import os
import time
from pathlib import Path

import requests
from google import genai

MODEL = "gemini-3.5-flash-lite"
GEMINI_RETRIES = 4
CANDIDATE_COUNT = 3

BUFFER_API_URL = "https://api.buffer.com"
BUFFER_CHANNEL_ID = "6ac7b4286a5c39ccb6523f7c"

BASE_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_FILE = BASE_DIR / "RINGSHIFT_KNOWLEDGE.md"
POSTS_FILE = BASE_DIR / "content" / "generated_posts.md"

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

knowledge = KNOWLEDGE_FILE.read_text(encoding="utf-8")
posts_history = POSTS_FILE.read_text(encoding="utf-8") if POSTS_FILE.exists() else ""


def generate_with_retry(prompt):
    for attempt in range(1, GEMINI_RETRIES + 1):
        try:
            return client.models.generate_content(
                model=MODEL,
                contents=prompt
            )
        except Exception as e:
            print(f"\nGemini connection failed (attempt {attempt}/{GEMINI_RETRIES}).")
            print(f"Error: {e}")

            if attempt == GEMINI_RETRIES:
                raise

            print("Retrying in 5 seconds...")
            time.sleep(5)


def generate_candidates():
    prompt = f"""
You are the social-media writer for the indie puzzle game Ringshift.

--- KNOWLEDGE ---
{knowledge}
--- END KNOWLEDGE ---

--- PREVIOUS POSTS ---
{posts_history}
--- END PREVIOUS POSTS ---

Generate exactly {CANDIDATE_COUNT} DIFFERENT X posts about Ringshift.

Each candidate must use a different angle.

Possible formats:

1. Short challenge
2. Curiosity hook
3. Unexpected observation
4. Confession
5. Minimalist
6. Contrast
7. Developer thought
8. Direct showcase

Rules:
- Natural human tone.
- Curious and slightly playful.
- Do NOT sound corporate.
- Do NOT use generic marketing language.
- Do NOT invent ANY facts.
- Do NOT invent numbers, time durations, player reactions,
  achievements, release dates, sales, wishlists, or features.
- Only use information explicitly present in the knowledge.
- Themes are visual only unless the knowledge explicitly says otherwise.
- Do not exaggerate mechanics.
- Do not explain mechanics like documentation.
- Avoid starting with "Equip", "Try", "Check out", or "Ringshift is".
- Do NOT reuse hooks, opening sentences, jokes, questions,
  or core ideas from previous posts.
- The three candidates must be meaningfully different.
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

    candidates = []
    current = None

    for line in response.text.strip().splitlines():
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

    return [c for c in candidates if c]


def critique_post(post):
    prompt = f"""
You are the senior social-media editor for the indie puzzle game Ringshift.

--- KNOWLEDGE ---
{knowledge}
--- END KNOWLEDGE ---

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
  supported by the knowledge or clearly presented as subjective opinion.
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


def send_to_buffer(post):
    query = """
    mutation CreatePost {
      createPost(input: {
        text: POST_TEXT
        channelId: "6ac7b4286a5c39ccb6523f7c"
        schedulingType: automatic
        mode: addToQueue
      }) {
        ... on PostActionSuccess {
          post {
            id
            text
            dueAt
          }
        }
        ... on MutationError {
          message
        }
      }
    }
    """

    query = query.replace("POST_TEXT", '"' + post.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"')

    response = requests.post(
        BUFFER_API_URL,
        headers={
            "Authorization": f"Bearer {os.environ['BUFFER_API_KEY']}",
            "Content-Type": "application/json"
        },
        json={"query": query},
        timeout=30
    )

    print("\n--- BUFFER ---\n")
    print(f"HTTP status: {response.status_code}")
    print(response.text)

    if response.status_code != 200:
        raise RuntimeError("Buffer API request failed.")

    data = response.json()

    if data.get("errors"):
        raise RuntimeError(f"Buffer GraphQL error: {data['errors']}")

    result = data.get("data", {}).get("createPost", {})

    if "post" in result:
        print("\nPOST ADDED TO BUFFER QUEUE")
        print(f"Buffer post ID: {result['post']['id']}")
        print(f"Scheduled time: {result['post']['dueAt']}")
        return True

    if "message" in result:
        raise RuntimeError(f"Buffer rejected the post: {result['message']}")

    raise RuntimeError(f"Unexpected Buffer response: {data}")


def main():
    candidates = generate_candidates()

    print("\n--- GENERATED CANDIDATES ---\n")

    for i, candidate in enumerate(candidates, 1):
        print(f"--- CANDIDATE {i} ---")
        print(candidate)
        print()

    if not candidates:
        print("No candidates were generated.")
        return

    best_post = None
    best_critique = None
    best_score = -1

    for i, candidate in enumerate(candidates, 1):
        print(f"\n--- CRITIC CANDIDATE {i} ---\n")

        critique = critique_post(candidate)
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

        save_post(best_post, best_critique)

        print("\n--- SENDING TO BUFFER ---")

        try:
            send_to_buffer(best_post)
        except Exception as e:
            print("\nBUFFER ERROR:")
            print(e)
            print("\nThe post was saved locally but was NOT confirmed as queued.")

        print("\n--- RESULT ---\n")
        print("BEST CANDIDATE ACCEPTED")
    else:
        print("\n--- RESULT ---\n")
        print("NO CANDIDATE PASSED")


if __name__ == "__main__":
    main()