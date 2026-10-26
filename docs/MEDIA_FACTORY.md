# Gameplay media factory and content intelligence

What this branch adds on top of PR #3 (the approval system is untouched). Written 2026-10-26.
The audit that led to it is in [MEDIA_FACTORY_AUDIT.md](MEDIA_FACTORY_AUDIT.md).

## What it does, in one paragraph
Real gameplay is captured from your actual game file (headless Chromium, the game's own solver, real key presses, FFmpeg).
Every file gets a structured record in `media/manifest.json` (level, mechanics read from game data, puzzle state, aspect,
validation, fingerprints). When a draft is made, the post type is chosen first, then only media that honestly supports that
post type is eligible (a "can you solve it?" post can never get a clip that ends on the solution). The writer is told exactly
what the media shows; the critic must quote evidence for every factual claim and the code checks the quote exists.
Nothing here can publish: `createPost` still exists only in `send_to_buffer`, reached only from `publish-approved`.

## Files
| File | Role |
|---|---|
| `scripts/capture_gameplay.py` | Capture CLI: `doctor`, `list`, `plan`, `capture` |
| `scripts/media_manifest.py` | Manifest build (`sync`), checks (`validate`), summary (`report`) |
| `scripts/content_matching.py` | Which media fits which post type; text-vs-media checks; project facts |
| `scripts/media_selector.py` | Picks media for a post type (manifest mode) or the older tag rules (no manifest) |
| `scripts/autopilot.py` | 12 post types, grounded writer, evidence-checked critic, no-winner report |
| `scripts/game_terms.py` | Canonical mechanic names; ordinary words ("an echo of last week") are no longer flagged |
| `data/project_facts.json` | The only source for "progress" and "behind the scenes" posts (empty = those types never run) |
| `media/manifest.json` | One record per media file; `media_tags.json` is kept unchanged inside `legacy` |
| `data/capture_preview/` | Latest capture report and contact sheet |

## Capture (on your PC; needs the game file)
```
python scripts/capture_gameplay.py doctor  --game game\ringshift.html     # checks Python, Playwright, Chromium, FFmpeg, libx264, disk, game
python scripts/capture_gameplay.py plan    --game game\ringshift.html --count 2 --seed 7
python scripts/capture_gameplay.py capture --game game\ringshift.html --count 2 --seed 7 --kind all --aspect 9:16 --aspect 16:9
python scripts/media_manifest.py validate
```
* `--kind`: `video`, `screenshot`, `both`, `teaser`, `all`. A **teaser** is real play that stops before the solution and is
  verified unsolved (the capture fails rather than produce a solved teaser).
* `--aspect` (repeatable): `4:5` (default), `1:1`, `9:16`, `16:9`. File names get `_9x16` / `_16x9` so shapes never overwrite each other.
* `--seed` makes the level choice repeatable; `--scene hard:24` picks one level; `--force` recaptures; `--dry-run` shows the plan.
* Each level runs in its own browser with a wall-clock limit (`--scene-timeout`, default 150 s); an unexpected browser crash is retried once.
* Every file is validated with FFmpeg/ffprobe (size, length, decodes, picture changes). A near-duplicate of something already in the manifest is skipped.
* No API key, no network, nothing paid. The output is a report (`data/capture_preview/capture_report.md`) and a contact sheet to review on a phone.
Output goes to `media/captured/` and is registered in `media_tags.json` (unchanged format), `manifest.json` and `data/capture_history.json`.

## Post types and what media each accepts
| Post type | Needs |
|---|---|
| challenge | unsolved puzzle (still, or video with visible play that stops unsolved) |
| hook | a video with visible play (unsolved or solved) |
| mechanic | media with a verified mechanic |
| observation / visual / feedback / discovery / invite | any described, non-failed media |
| solution | solved media |
| moment | a video with visible play |
| progress / behind_scenes | any media **and** a verified entry of that kind in `data/project_facts.json` |

A post type with no fitting media is skipped (the draft tries the next one and records why). If none fits, or the manifest
exists but is unreadable, the run writes a "no candidate passed" report with the reasons and drafts nothing.

## Quality gates, in order
1. Writer prompt contains audience, objective, the verified media facts, and what must not be said (solved/unsolved, still vs video).
2. Deterministic screening before the critic: duplicate/near-duplicate text, post rules (no invented numbers/availability), mechanic
   names the media does not show, names the game does not use, text that contradicts the media, repeated opening words, a streak of questions.
3. Critic labels every claim `KNOWLEDGE`, `MEDIA`, `OPINION`, `QUESTION` or `UNSUPPORTED`. `KNOWLEDGE`/`MEDIA` must quote evidence;
   the code verifies the quote appears in RINGSHIFT_KNOWLEDGE.md, the media description/facts or project facts. Opinions, questions,
   hashtags and ordinary words are not treated as claims needing proof.
4. At most one revision round: the critic's rewrite is only a new candidate and passes the same gates.
5. If nothing passes, a failure report lists every attempt and reason. Nothing is ever forced through.

## Media gate for approval codes (fail closed)
An approval code is only issued when the media has passed every check below, in the same run. There is no switch to turn this off:
`RINGSHIFT_VERIFY_MEDIA=0` is ignored (the run prints a warning and checks anyway).
1. **The file is real.** Header inspection in pure Python: signature must match the extension (PNG/JPEG/MP4), size floors
   (images >= 1 KiB, videos >= 4 KiB), plausible dimensions, no truncation, MP4 box structure. A 3-byte or zero-byte file is never valid.
2. **It decodes.** ffprobe when present; otherwise the manifest must record a validation of the same bytes (matching SHA-256). Neither -> refused.
3. **The public URL answers.** Exactly HTTP 200 (no redirects), and the downloaded bytes' SHA-256 equals the repo file's.
   404, 3xx, 5xx, HTML-with-200, unreachable, or stale bytes -> refused.
4. **The check is part of the signed draft.** `media.check` is inside the content hash; `publish-approved` re-runs the full check and refuses
   drafts with a missing, failed, fixture or mismatching check.
`MEDIA_CHECK_MODE` is a code-only constant (`strict` by default). Tests may set `fixture`; fixture drafts are stamped and can never be approved.

## Caption quality (deterministic, no AI)
`scripts/caption_quality.py` rejects: stock filler ("Brand new idea about rings.", "A genuinely new thought about beams."),
captions below a specificity floor, captions with no anchor to the media (mechanic, level, theme, mode or a verified fact word),
captions naming a different level than the media, and repeats of recent posts by content-word overlap (so reworded repeats fail even though their hashes differ).

## Per-run limits
`CANDIDATE_COUNT=3` candidates, `MAX_CRITIC_CALLS=4`, `MEDIA_TRIES_PER_TYPE=3`, and `MAX_DRAFTS_PER_RUN=1`: a run issues at most one approval code,
enforced in `build_draft` (a second call raises). Many codes in a workflow log came from many test/dry runs, not from one run.

## Genuine-gameplay provenance (required for an approvable draft)
A valid file signature and a clean decode only prove a file is an image or video. For any approvable draft, `autopilot.manifest_provenance`
also requires the checkout's committed `media/manifest.json` to vouch for the exact bytes being posted: a record for that path whose SHA-256
equals the checked bytes, not excluded, validation `ok`, a recognised gameplay source (`capture_gameplay.py` with a recorded game build and
`verified`, or a person's `manual-recording` that is `reviewed`/`verified`), and the recorded level, mechanisms and description the caption is
written from. It is looked up from the checkout at preview time and again at publish time, never read from a draft. A person still has to look at
the picture: the manifest proves where a file came from, not that it looks good.

## What a preview run shows
A real preview writes exactly one draft summary, one `::notice` and one artifact, and issues one approval code (`MAX_DRAFTS_PER_RUN`). Nothing made in
test-fixture mode is ever written to the job summary or announced, and a process running inside GitHub Actions refuses fixture mode outright.
The unit tests never receive `GITHUB_STEP_SUMMARY`/`GITHUB_OUTPUT` (see `tests/support.py`): before this, the "tests" job of a run filled the run's
summary with dozens of fixture drafts (`media/gameplay/black_hole.png`, "FIXTURE ... can never be approved") next to the one real draft.

## Honest limits
* Repetition detection is lexical (wording overlap, same opening words), not semantic. Two posts can make the same point in different words.
* Draft-only runs save no state (by design), so the post type rotates on the publish counter plus the day of the year, and media is chosen
  at random among the least used. The same file can repeat until a post is actually published and recorded.
* Captured clips are 2x upscaled from the browser's screencast, so clips are softer than stills. Native 1080x1920 is not possible: the game switches layout.
* In the 16:9 shots the game's own landscape layout draws the ring small and overlaps some HUD; at phone size the top-bar buttons overlap the "LEVELS" text. Both are the game's layout, not capture artefacts.
* A human has not yet looked at the new captures; the tool verifies file validity, win state and mechanics from game data, not taste.
* The game file is not in the repo, so GitHub Actions cannot capture; it only validates metadata.
* `url_verified` in the manifest is `null`: the raw URLs point at `main` and are not live until this branch is merged and the media pushed there.
* Real Buffer publishing and the full approve-then-publish path remain unverified end to end (PR #3's rehearsal covers the approval logic offline).

## Adding verified project facts
Edit `data/project_facts.json`:
```json
{"facts": [{"id": "levels-2026-10", "kind": "progress", "statement": "Ringshift now has 75 levels across Easy, Normal and Hard.", "source": "capture_gameplay.py list, 2026-10-26"}]}
```
Only state things you can point to a source for. Entries missing a field are ignored.

## Costs
Capture, manifest and all tests: 0 (local CPU, free tools). Drafting uses the Gemini key already used by PR #3 (one writer call, one critic call per candidate that passes screening, at most one rewrite check; I did not measure the exact call count against the real API because no key is available here). Buffer and X: unchanged and not touched here. Media storage in git: about 17 MB for 34 captured files so far; use `scripts/media_store.py` (see MEDIA_HOSTING.md) if the repo grows.
