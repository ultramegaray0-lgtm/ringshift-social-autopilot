import os
from pathlib import Path
from google import genai

post = Path("content/generated_posts.md").read_text(encoding="utf-8")

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

prompt = f"""
You are the senior social-media editor for the indie puzzle game Ringshift.

Your job is to decide whether this X post is strong enough to publish.

--- RINGSHIFT KNOWLEDGE ---
{Path("RINGSHIFT_KNOWLEDGE.md").read_text(encoding="utf-8")}
--- END KNOWLEDGE ---

--- POST TO REVIEW ---
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
Give the single biggest reason the post is weak, if it is weak.

IMPROVED VERSION:
If KEEP is NO, rewrite the post.
If KEEP is YES, write "Not needed."

STRICT FACT RULES:
- Do not invent ANY specific details, numbers, time durations, player reactions, achievements, release information, or gameplay facts.
- Never add details simply to make the post more interesting.
- Only use facts explicitly present in the Ringshift knowledge or the original post.
- If the original post contains an unsupported claim, flag it.
- You may improve wording, structure, rhythm, and personality.
- You may make the post more intriguing without adding facts.
- Do not criticize the post for failing to contain information that is not necessary for a short X post.
- Do not insult the original writer.
- Do not use phrases like "lazy engagement-bait" or "homework assignment."
- Do not make the post sound corporate.
- Avoid generic marketing language.
- Maximum 280 characters.
- Maximum 2 hashtags.
"""

response = client.models.generate_content(
    model="gemini-3.5-flash-lite",
    contents=prompt
)

print("\n--- AI CRITIC ---\n")
print(response.text)