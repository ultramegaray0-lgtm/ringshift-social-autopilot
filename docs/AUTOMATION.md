# Unattended operation: fresh gameplay media, daily posts, new levels and themes

Written 2026-10-27. This is how the system runs without you opening anything, what it checks, what it never does,
and what you still have to set up once.

## The daily post (workflow *Ringshift AI Autopilot*, `.github/workflows/autopilot.yml`)

**When.** `cron: "0 6 * * *"` = 06:00 UTC = **09:00 Riyadh** (GitHub cron is UTC; Riyadh is UTC+3 all year). A second
schedule, `37 13 * * *` (16:37 Riyadh), is a **catch-up**: it does nothing unless no post was queued that day. GitHub
starts scheduled runs late under load; over Oct 9-27 the 06:00 run actually started between 10:05 and 14:19 UTC. That
does not move the post: **Buffer** publishes it at its own queue slot (your queue currently posts at 04:00 UTC = 07:00
Riyadh), so each daily run fills the next morning's slot.

**What one run does** (the run page's top table shows every stage):

| # | Step | Fails safe how |
|---|---|---|
| 1 | Offline test suite (no secrets, network blocked) | Any failure: nothing below runs |
| 2 | **Post window** (read-only): today (Riyadh) has no `ringshift-day-YYYY-MM-DD` tag, no post queued by this tool in the last 12 h, nothing waiting in Buffer | Closed: the run ends green with the reason; no capture, no Gemini, no post |
| 3 | Check out the game: `ultramegaray0-lgtm/ringshift-game`, branch **main** (the stable build the demo publishes), read-only | Missing repo/file: the run fails with the reason, no media |
| 4 | **Capture** one fresh file (`scripts/daily_media.py`, below) and host it on the `media-store` branch | No file passes: the run fails, nothing is posted, the reasons are recorded |
| 5 | **Write** the post with the existing Gemini writer about exactly that file; existing screening (post rules, game vocabulary, mechanics shown, near-duplicates, caption quality) and the evidence-checked **critic** | No caption passes: the run fails, nothing is posted |
| 6 | **Publish** through `publish_approved`, the same function `publish-approved` uses: draft hash, expiry, run id, channel, media re-download + decode + SHA-256 + provenance, post rules, Buffer queue check, 12 h spacing, then the **day claim**, the draft claim, and Buffer `createPost` | Any check fails before the claims: nothing used up, nothing posted. Buffer must confirm the media attachment or the run fails |
| 7 | Commit state (manifest, media tags, capture and daily logs, post log, counter, analytics) to `main` | Push fails: the same files are in the run's `ringshift-state` artifact |

**Never twice a day.** The day claim is a git tag created right before the draft is claimed. A re-run, the catch-up
schedule or a manual `daily` run on the same Riyadh day is refused at step 2 or 6, even if the repository state was lost.

**Kill switch.** Repository variable `RINGSHIFT_AUTOPUBLISH` = `off` (Settings > Secrets and variables > Actions >
Variables). The schedule then still captures fresh media and writes a draft, and you approve it with `publish-approved`
as before. Delete the variable (or set `on`) to go back to unattended posting. To stop everything: Actions >
Ringshift AI Autopilot > ... > Disable workflow.

**Run it now by hand.** Actions > Ringshift AI Autopilot > Run workflow > branch `main` > mode `daily`. It does exactly
what the schedule does, including the once-per-day rule. Mode **`daily-dry-run`** does the same fresh capture, caption
and critic but only writes a draft (nothing is sent): use it to see what the system would post.

## Fresh gameplay media (`scripts/daily_media.py`, `scripts/capture_gameplay.py`)

* **Kind.** Over six posts: puzzle still, puzzle still, **full solution clip**, puzzle still, solved still, **teaser
  clip** (real play that stops before the solution). So a video every third post, alternating full clip and teaser.
  If a video cannot be made, the run falls back to a real still; it never turns a still into a "video".
* **Level.** One of the 75 Easy/Normal/Hard levels the game's own solver can solve, with at least one mechanic under its
  knowledge-file name, solution length 3-16 moves for stills, 9-16 for full clips, 11-16 for teasers (so clips run
  about 10-15 s). Levels not shown before come first, chosen for mechanic variety; after that the least recently shown.
* **Theme.** One of the game's own themes (39 in v0.33), equipped the way the Shop does it. The least used theme that
  was not in the last six runs; a theme that appears in the game for the first time (for example a merged theme
  experiment) is shown next. Themes the game marks "unlock not assigned yet" are skipped: posts only show looks players
  can actually get.
* **Real gameplay.** Headless Chromium plays the level with real arrow-key presses. A clip is kept only if the game itself
  reports the win; a teaser only if the level is still unsolved; a still only if the puzzle screen is showing, the whole
  board is on screen and no menu, pop-up, toast or banner covers it. The game logging any JavaScript error discards the file.
* **Framing.** 4:5 (1080x1350), cropped from the phone layout so the whole board and the HUD are in the picture.
  Videos are H.264, yuv420p, faststart, silent, about 0.5-2.5 MB.
* **Checks before it can be posted.** FFmpeg/ffprobe: format, dimensions, size, duration, decodes, contrast and (for
  clips) motion; a still-usability check (contrast, colour count, no flat wash; real screenshots measure 27-53 and
  1,600+ colours, blank screens fail); not identical to, and not a near-duplicate of, anything captured before; the
  public URL serves exactly these bytes.
* **Provenance.** `media/manifest.json` gets a record per file: path `store/<file>`, SHA-256, level, mode, mechanisms
  (from game data), puzzle state, theme id and name, capture time, game build and **game commit**. The existing media
  gate refuses any post whose bytes are not vouched for by such a record.
* **Hosting.** The `media-store` branch of this repository (rewritten as one commit, newest 60 files), served by
  raw.githubusercontent.com. Files dropped by that retention are marked excluded so they are never selected again.

## New levels, weekly (workflow *Ringshift Game Lab*, `scripts/level_lab.py`)

Every **Monday 02:23 UTC**. Inside the real game page, with the game's own `traceAllBeams`:
a board is laid out in the game's level schema (12 slots, 3 rings, gimmick encodings copied from existing levels), all
1,728 ring rotations are traced, and locks are chosen so **exactly one rotation lights every lock including the coin**,
the start lights no required lock, and the shortest solution is 6-14 moves. Every gimmick is removed in turn: a candidate
needs 3+ gimmicks the solution really depends on, of 2+ different mechanics, and a structure no existing level has.
Two candidates with different mechanic mixes are kept.

They are appended to Hard in a marked block after `LEVELS` (existing levels, ids and order untouched), and the patched
build is validated in a fresh browser: loads with no errors; every level of all 7 modes byte-identical to the stable
build; the game's solver agrees there is exactly one coin solution; a **real keyboard playthrough wins and the game
records the coin**; the level grid and save have the new levels. The result goes to a new `lab/levels-...` branch of the
game repository with a pull request (screenshots, the checks, what each level needs). **It is never merged by the tool.**

## New themes, every two weeks (`scripts/theme_lab.py`)

On the Monday run when the last theme experiment is 13+ days old. Concepts come first from the directions in
`RINGSHIFT_KNOWLEDGE.md` the game does not have yet (**Rain, Creepy, Treasure, Diamond/gemstone, Old black-and-white
cartoon, Wood**), then a list of material/place ideas. Gemini writes one `THEME_V2.<id> = {...}` entry, shown three
complete themes from the game and the helpers it may use. Before anything runs: one statement, size limit, no network,
DOM, timers, `Math.random` or links, a new id and name, no mechanic words in the name. Then on a patched copy of the
stable build:

* loads with no errors; every level and every existing theme unchanged; other themes still switch and render;
* renders on three levels with the whole board visible at 540x960 and 390x844, no sideways scrolling;
* **measured against the 39 shipped themes** (`scripts/theme_metrics.py`, calibration cached per build in
  `data/theme_calibration.json`): playing-field brightness and busyness inside the shipped range, beam visibility at
  least that of the least visible shipped theme, at most 60% near-black surroundings (no generic space look), a colour
  palette at least 0.12 (Hellinger distance) from every shipped theme, theme switch time and background size within range.

A failure goes back to Gemini once with the measured reasons. A pass goes to a `lab/theme-<id>-...` branch with a pull
request showing screenshots and the measurements. The theme is added to the shop with its unlock marked *not assigned
yet*; you decide whether to keep it and how players unlock it. **It is never merged by the tool.**

Once you merge a level or theme PR, the next daily runs pick it up automatically: new levels join the rotation, and a
new theme (once its unlock is assigned) is the next theme shown.

## One-time setup

| What | Where | Needed for |
|---|---|---|
| `GEMINI_API_KEY` secret | already set | captions, theme experiments |
| `BUFFER_API_KEY` secret | already set | the post window check, posting, analytics |
| `RINGSHIFT_GAME_TOKEN` secret (**new, optional**) | github.com > Settings > Developer settings > Fine-grained tokens > Generate: *Only select repositories* = `ringshift-game`; Repository permissions: **Contents: Read and write**, **Pull requests: Read and write**. Then this repository > Settings > Secrets and variables > Actions > New repository secret | the Game Lab opening branches and pull requests on the game repository. Without it the lab still runs and attaches its results to the run as an artifact, and its step fails saying exactly this |
| `RINGSHIFT_AUTOPUBLISH` variable (optional) | Settings > Secrets and variables > Actions > Variables | `off` = drafts only |
| Failure e-mails | github.com > Settings > Notifications > Actions: "Send notifications for failed workflows only" | being told when a day had no post |

Nothing here costs money: the repositories are public, so GitHub Actions minutes are free; Gemini's free tier, Buffer's
free plan, Chromium, FFmpeg and Playwright cost nothing. A daily run takes about 6-10 minutes; the Monday lab about
10-25 minutes (the first theme run after a game update spends about 7 minutes measuring the shipped themes).

## What to watch

* **Actions > Ringshift AI Autopilot**: one run a day (plus the catch-up, which usually ends after the window check).
  The run's summary table says what was captured, which checks ran, the post text, and the Buffer post id and time,
  or exactly why nothing was posted.
* **Actions > Ringshift Game Lab**: Mondays. Its summary links the pull request it opened on the game repository.
* **ringshift-game > Pull requests**: candidate levels and themes waiting for you to play and merge (or close).

## Limits (honest)

* **Publishing to Buffer by the unattended path has not been run live by this change.** The code path is the one
  `publish-approved` already uses (it queued two real posts with Buffer-confirmed media on 2026-10-26 and 2026-10-27, `data/post_log.json`), but the first unattended post will be the first
  scheduled run after this is on `main`. Check that run's summary and your Buffer queue once.
* Gemini output quality is not testable offline. The captions still pass the same critic and rules as before; theme
  experiments are measured, but "is it beautiful" needs your eyes on the pull request.
* The automatic checks cannot tell whether a candidate level is *fun*; they prove it is solvable, unique, coin-correct,
  non-trivial and structurally new.
* GitHub can delay or (rarely) skip scheduled runs, and disables schedules in a repository with no activity for 60 days
  (the daily state commits count as activity). The catch-up schedule covers a delayed or failed morning run.
* If Buffer has a post waiting that you queued yourself, the unattended run waits for it rather than stacking a second one.
* Media hosting relies on raw.githubusercontent.com serving the `media-store` branch; the run proves the URL serves the
  exact bytes before posting, and fails instead of posting if it does not.
