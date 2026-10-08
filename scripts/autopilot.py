import os
from pathlib import Path
from google import genai

MODEL = "gemini-3.5-flash-lite"
MAX_ATTEMPTS = 3

BASE_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_FILE = BASE_DIR / "RINGSHIFT_KNOWLEDGE.md"
POSTS_FILE = BASE_DIR / "content" / "generated_posts.md"

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

knowledge = KNOWLEDGE_FILE.read_text(encoding="utf-8")


def generate_post():
    prompt = f"""
You are the social-media writer for the indie puzzle game Ringshift.

--- KNOWLEDGE ---
{knowledge}
--- END KNOWLEDGE ---

Write ONE short X post about Ringshift.

Choose ONE post format:

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
- Maximum 280 characters.
- Maximum 2 hashtags.
- Output ONLY the finished post.
"""

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt
    )

    return response.text.strip()


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
- Do not invent ANY specific details, numbers, time durations,
  player reactions, achievements, release information, or gameplay facts.
- Only use facts explicitly present in the knowledge or original post.
- Never add details simply to make the post more interesting.
- Do not imply that visual themes change gameplay, puzzle difficulty,
  player perception, strategy, or mechanics unless explicitly stated.
- Do not insult the writer.
- Do not use generic marketing language.
- Maximum 280 characters.
- Maximum 2 hashtags.
"""

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt
    )

    return response.text.strip()


def extract_keep(critique):
    for line in critique.splitlines():
        if line.strip().upper().startswith("KEEP:"):
            return "YES" in line.upper()

    return False


def extract_improved(critique):
    lines = critique.splitlines()

    for i, line in enumerate(lines):
        if line.strip().upper().startswith("IMPROVED VERSION:"):
            improved = []

            for next_line in lines[i + 1:]:
                if next_line.strip().upper().startswith("SCORES:"):
                    break
                improved.append(next_line)

            text = "\n".join(improved).strip()

            if text and text.lower() != "not needed.":
                return text

    return None


def save_post(post, critique):
    POSTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing = POSTS_FILE.read_text(encoding="utf-8") \
        if POSTS_FILE.exists() else ""

    post_number = existing.count("## Post ") + 1

    with POSTS_FILE.open("a", encoding="utf-8") as file:
        file.write(f"\n## Post {post_number:03d}\n\n")
        file.write(post + "\n\n")
        file.write("### Critic\n\n")
        file.write(critique + "\n")


def main():
    post = generate_post()

    print("\n--- GENERATED POST ---\n")
    print(post)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"\n--- CRITIC ATTEMPT {attempt} ---\n")

        critique = critique_post(post)
        print(critique)

        if extract_keep(critique):
            print("\n--- RESULT ---\n")
            print("POST ACCEPTED")

            save_post(post, critique)
            return

        improved = extract_improved(critique)

        if not improved:
            print("\nNo usable improved version was returned.")
            return

        post = improved

        print("\n--- IMPROVED POST ---\n")
        print(post)

    print("\n--- RESULT ---\n")
    print("POST REJECTED AFTER MAXIMUM ATTEMPTS")


if __name__ == "__main__":
    main()