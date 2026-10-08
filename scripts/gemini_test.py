import os
from pathlib import Path
from google import genai

knowledge = Path("RINGSHIFT_KNOWLEDGE.md").read_text(encoding="utf-8")

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

prompt = f"""
You are the social-media writer for the indie puzzle game Ringshift.

Read the Ringshift knowledge below.

--- KNOWLEDGE ---
{knowledge}
--- END KNOWLEDGE ---

Write ONE short X post about Ringshift.

Choose ONE post format at random:

1. Short challenge:
   Make the reader want to figure something out.

2. Curiosity hook:
   Reveal only enough to make someone want to watch the gameplay.

3. Unexpected observation:
   Point out an interesting detail about a known mechanic.

4. Confession:
   Use a short first-person statement based only on known information.

5. Minimalist:
   1–2 short sentences. Let the gameplay provide the context.

6. Contrast:
   Put two known game elements or ideas against each other.

7. Developer thought:
   A short natural observation about making/designing Ringshift.

8. Direct showcase:
   Introduce something visually interesting in a way that makes someone want to see it.

Do NOT explain the mechanic like documentation.
Do NOT give instructions to the player unless the post is specifically a challenge.
Do NOT begin with "Equip", "Try", "Check out", or "Ringshift is".

1. Puzzle challenge
2. Strange mechanic
3. Satisfying moment
4. Development thought
5. Visual/theme showcase
6. "I didn't notice this" moment
7. Short mysterious statement
8. Question that genuinely invites discussion

Rules:
- Natural human tone.
- Curious and slightly playful.
- Do NOT sound like a corporation.
- Do NOT use generic marketing language.
- Do NOT invent ANY facts.
- Do not imply that visual themes change gameplay, puzzle difficulty, player perception, strategy, or mechanics unless explicitly stated in the knowledge.
- Themes are visual only unless the knowledge explicitly says otherwise.
- Do NOT make subjective claims about how good, difficult, satisfying, powerful, fun, or revolutionary something is unless that claim is explicitly supported by the knowledge.
- Do NOT invent player reactions, numbers, time durations, achievements, release dates, sales, wishlists, or development progress.
Prefer concrete, visually interesting details from the knowledge.

Good:
"One power. One UFO. Ten seconds to make it count."

Bad:
"Equipping the UFO power changes how things interact."

When writing about a mechanic, make the post feel like it reveals something interesting rather than simply describing the feature.

You may use short fragments, contrasts, suspense, or a challenge.

Do not invent what happens beyond what the knowledge explicitly says.

Good:
"One power. One UFO. Ten seconds to make it count."

Bad:
"Equipping the UFO power changes how things interact."

When writing about a mechanic, make the post feel like it reveals something interesting rather than simply describing the feature.

You may use short fragments, contrasts, suspense, or a challenge.

Do not invent what happens beyond what the knowledge explicitly says.
- Do not exaggerate mechanics.
- If mentioning a mechanic, describe only what the knowledge explicitly says about it.
- Avoid repeating the same wording or structure as previous posts.
- The gameplay or image should do most of the explaining.
- Maximum 280 characters.
- Maximum 2 hashtags.
STYLE EXAMPLES:

Example 1:
"There is only one solution.
Would you spot it?"

Example 2:
"The mirror isn't the problem.
The angle is."

Example 3:
"One power.
One attempt.
No coin."

Example 4:
"Some puzzles look impossible until you notice the one thing you're ignoring."

These are style examples only.
Do not copy them directly.
Do not invent facts to imitate them.
- Output ONLY the finished post.
"""

response = client.models.generate_content(
    model="gemini-3.5-flash-lite",
    contents=prompt
)

print("\n--- GENERATED POST ---\n")
print(response.text)