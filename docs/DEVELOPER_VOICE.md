# Developer voice: posts from the person building Ringshift

Written 2026-10-29. Until then most automated posts described the attached screenshot ("The Terracotta Courtyard visual
theme changes how the puzzle looks without altering the gameplay."). Now about two thirds of them are the developer
talking about what they built, and each one ends in a way that invites the right people to reply or connect.

## The mix

| Rotation position | Post types tried first | Share |
|---|---|---|
| 2 of every 3 | developer-led: `dev_story`, plus `progress` / `behind_scenes` once `data/project_facts.json` has verified facts | about 2/3 |
| every 3rd | the other types (challenge, hook, mechanic, observation, visual, solution, moment, feedback, discovery, invite with a project URL), least recently published first | about 1/3 |

The position is the post counter plus the day (as before). Any three consecutive positions, and positions that move by
two (a day with a post moves both), hold exactly two developer slots, so the share stays at two thirds whether a post
goes out every day, some days fail, or the kill switch makes the schedule draft-only. If the day's fresh media cannot
carry the first choice, the run falls back through every other eligible type, as before.

## What a developer story is

`dev_story` (scripts/dev_story.py) speaks in the first person about **this** media: the mechanic I built, the level I
made, what makes this puzzle interesting, or a small development update. It needs media with a verified level number or
mechanic list (every daily capture has both). The writer gets three different subjects per round and the knowledge file's
DEVELOPER VOICE and VERIFIED MECHANIC BEHAVIOUR sentences as its only facts.

Each run picks one ending, least recently used first and never the one the last developer post used:

| Ending | Example (from the verified-fact floor) |
|---|---|
| Indie-dev invitation | "Fellow devs, reply with a screenshot of your game; I'd like to see it." |
| Design question | "Which catches your eye first here, the Mirror or the Half-Block?" |
| Maker's note | "I'm sharing the build here as Ringshift comes together." |
| Showcase | no ask at all: the level stands on its own |

Question endings are skipped while most recent posts were questions. A closing line that one of the newest posts (ours
or Buffer's) already used is rejected, so "Let's connect" / "What are you working on?" cannot become a daily ending.

## What is still rejected (before the critic, in code)

* Not first person, or not naming this media's level, an on-screen mechanic or its theme.
* Invented development history or anecdotes: how long it took, attempts, versions, what was changed or cut, testers,
  friends or players' reactions, dates, "last night", plans, "soon".
* Invented mechanic behaviour or positions ("the Portal catches the beam", "the Block shifts", "a Prism next to a
  Mirror"). The only behaviour allowed is the knowledge file's own wording for a mechanic the media shows ("a Prism splits
  a beam into two beams at 45 degrees"), and the critic still has to find it in the knowledge.
* Engagement bait on every post type ("like and retweet", "follow for more", "tag a friend").
* Everything that applied before: post rules, game vocabulary, repeats, generic filler, the evidence-checked critic and
  its 30/60 minimum. A draft is still published only through `publish_approved`, which re-checks the developer voice.

## Making the developer posts richer

Add true statements to `data/project_facts.json` (`progress` or `behind_the_scenes`, with a source). They join the
developer-led share automatically as their own post types. Statements about the developer belong in the DEVELOPER VOICE
section of `RINGSHIFT_KNOWLEDGE.md`; anything not written there or in a verified fact cannot be posted.

## Not verified

Gemini's real output and how X users respond can only be seen after merging: check the first scheduled runs' summaries
(post type `dev_story`, ending style in the post log) and the X analytics a few weeks later.
