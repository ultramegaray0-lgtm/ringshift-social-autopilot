# Media factory: audit before changes (2026-10-26)

Base of this work: the head of PR #3 (`779ed69`, branch `feature/preview-draft-and-publish-guards`), because `main`
(`87967b2`) does not contain the approval system. This branch is `feature/gameplay-media-factory-v2`. Nothing was merged,
published, queued, scheduled or deleted, and no secret, setting or schedule was touched.

Everything below was read or run in this session. "Reproduced" means I ran it myself.

## Publishing safeguards currently implemented (PR #3 head)
| Check | Result |
|---|---|
| Modes in `autopilot.yml` | `test`, `preview`, `rehearse-approval`, `publish-approved`; the daily schedule writes a draft only |
| Where the Buffer key is used | analytics step (read-only, schedule) and the `publish-approved` step; the draft step gets the Gemini key only |
| Code that can create a post | `createPost` appears only in `send_to_buffer`; the only caller is `publish_approved` (`scripts/autopilot.py`) |
| `autopilot.py` without a mode flag | exits with an error (no autonomous path) |
| Draft approval | ID + content hash + expiry + single-use claim (`scripts/approvals.py`); `publish-approved` runs from `main` only |
| Baseline tests | **Reproduced: 357 run, OK, 1 skipped** on `779ed69` |
| Real-game capture test | **Reproduced: passes** (15 tests in `test_capture`, none skipped) against the game build below |

Not verified by me: GitHub-side behaviour (Actions run history, Environment settings, secrets), and any real Buffer publish.

## Game source and tooling
* The playable game is **not in this repository** (`game/` is git-ignored by design). The attached Project holds
  `ringshift v0.26.html`; its SHA-256 prefix is `13a0195f794a`, **identical to the build recorded in
  `data/capture_history.json`** for every existing capture. So existing captures and new ones come from the same build.
* This environment: Python 3.13, Node 22, FFmpeg/ffprobe 6.1, Playwright 1.63 and a working headless Chromium
  (`PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`; `playwright install` is not needed or possible here).
* GitHub-hosted runners cannot see your PC's game file. Capture stays a local (Windows) step; Actions only validates metadata.
* `scripts/capture_gameplay.py` already does the hard part (real keyboard play, win check, FFmpeg encode, validation).
  It is extended here, not replaced.

## Media inventory (55 catalog entries, 200 MB under `media/`)
| Group | Files | Origin | Metadata quality |
|---|---|---|---|
| `media/gameplay/*.png` | 11 stills | manual recording | description reviewed by hand; mechanisms named in the description |
| `media/gameplay/ringshift_*.mp4` | 9 clips | manual recording | same; 3 are special-mode clips (flagged `special_mode`) |
| `media/gameplay/2026-10-09 *.mp4` | 17 clips | raw 16:9 recordings, earlier build | generic descriptions, no tags, 1 excluded |
| `media/captured/*` | 6 levels x (clip + puzzle + solved) = 18 files, 9.6 MB | `capture_gameplay.py`, build `13a0195f794a` | facts read from game data; solved/unsolved stated in description text only |

Gaps found:
1. Level, difficulty, solved/unsolved state, aspect ratio, build, and "shows an action" exist only as free text or in a scene
   string. There is no structured, validated manifest.
2. Selector tags mix real mechanics with internal aliases (`black hole`, `satellite`, `mechanism`). Descriptions use the
   canonical names, so the fact layer is fine, but tags must never reach the writer (already enforced by `game_terms`).
3. **Every captured clip ends on the BREACHED (solved) screen**, and only *images* are screened for "no solved still on a
   challenge post". A challenge or hook post could therefore be paired with a video that shows the whole solution.
4. Media is picked by tag overlap and rotation, with no notion of what each post type needs.
5. Draft-only runs write no state (correct for safety), so the post type comes from the publish counter and does not
   change day to day; media is random among the least used. Repeats are possible until something is published.
6. Repetition detection is lexical (`post_rules.similarity`: sequence ratio, word overlap, containment). It is not
   semantic, and history is `content/generated_posts.md` + Buffer's last 10 posts in `data/analytics.md`.
7. Analytics does retrieve real metrics (e.g. impressions 2, 5, 52 on recent posts), but only the 10 posts Buffer returned.
8. No dependency check command, no seed option, no per-scene wall-clock limit, no near-duplicate capture detection,
   no landscape/vertical naming (the same scene and date would overwrite across aspects), no contact sheet or run report.

## Plan (small, additive)
* New `media/manifest.json` + validator, migrated from `media_tags.json` without discarding anything (tags file untouched).
* Capture: `doctor`, `--seed`, deadlines, near-duplicate check, aspect-aware names, `teaser` (real play that stops before the
  solution), manifest records, report + contact sheet.
* Matching: post type -> media requirements, applied before the critic; solved-state rules cover videos too.
* Writer/critic: grounded prompt (audience, objective, safe claims, visible action), claim classification with evidence,
  at most one revision, failure report. Question posts cannot ask for a solution the media already shows.
* Draft rotation that needs no committed state; honest limits documented.
* Approval system, workflows, schedules, secrets: untouched.
