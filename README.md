# Ringshift Social Autopilot

Promotes the indie puzzle game **Ringshift** on X with genuine gameplay media, fully unattended, at zero cost:
GitHub Actions + the real game (public repo `ringshift-game`) + Playwright/Chromium + FFmpeg + Gemini (free tier) + Buffer (free plan).

```
 GitHub Actions, every day 09:00 Riyadh (catch-up 16:37 if the morning produced no post)
 ┌───────────────────────────────────────────────────────────────────────────────────────────────┐
 │ offline tests ─► post window? (once per Riyadh day, nothing waiting in Buffer)                │
 │   ─► check out ringshift-game@main ─► daily_media.py: play a real level in headless Chromium, │
 │      choose theme/level/kind, capture still or clip, validate (FFmpeg, usability, duplicates) │
 │      ─► media-store branch (public URL, exact bytes) + manifest provenance (game commit, …)   │
 │   ─► autopilot.py --auto-publish: Gemini caption about THAT file, rules + critic, then the     │
 │      same verified publish path as publish-approved ─► Buffer queue ─► X                       │
 │   ─► commit state + run summary                                                                │
 └───────────────────────────────────────────────────────────────────────────────────────────────┘
 Mondays: Game Lab ─► candidate levels (weekly) and an experimental theme (every 2 weeks), proven in the
          real game, proposed as pull requests on ringshift-game lab/ branches. Never merged automatically.
```

Full description, setup and limits: **[docs/AUTOMATION.md](docs/AUTOMATION.md)**.

**TikTok videos** (separate workflow, manual upload): drop a funny clip into `media/funny/`; the *Ringshift TikTok
video* workflow puts it on top of real, muted Ringshift gameplay cropped to the puzzle board, adds a "PLAY THE DEMO /
LINK IN BIO" card, validates the result and hands you a 1080x1920 MP4 as a run artifact. See
**[docs/TIKTOK.md](docs/TIKTOK.md)**.

## What it does

| Area | Script | Notes |
|---|---|---|
| Daily fresh media | `scripts/daily_media.py` | Picks kind/level/theme, captures from the real game in Actions, validates, hosts on `media-store`, records provenance (game commit, level, theme, SHA-256). |
| Capture real gameplay | `scripts/capture_gameplay.py` | Drives the game in headless Chromium using the game's own solver and real key presses; equips themes like the Shop; frames the board; only keeps a clip if the level is actually won. |
| Candidate levels | `scripts/level_lab.py` | Generates levels with the game's tracer, proves one coin solution and a real winning playthrough, proposes a PR on the game repo. |
| Theme experiments | `scripts/theme_lab.py`, `scripts/theme_metrics.py` | Gemini-written v2 theme, measured against the 39 shipped themes (readability, distinctness, performance, mobile), proposed as a PR. |
| Validate / edit media | `scripts/media_tools.py` | FFmpeg: H.264, 4:5, ≤15 MB, ≤45 s, rejects black/static/broken files. |
| Free media hosting | `scripts/media_store.py` | Optional `media-store` branch (one squashed commit, newest 60 clips) so `main` doesn't grow. |
| Choose media | `scripts/media_selector.py` | Verified tags + descriptions, least-used rotation. |
| Write + publish posts | `scripts/autopilot.py` | Media first, then text about what it really shows; 12 post types; rule, game-vocabulary and near-duplicate screening; claim-checking critic; `--auto-publish` (unattended, once per day) or `--publish-approved`, both through the same verified publish path with media confirmation. |
| Approved drafts | `scripts/approvals.py` | Draft ID + content hash + expiry; verifies, then claims a draft once before publishing exactly it. |
| Game vocabulary | `scripts/game_terms.py` | Only the knowledge file's mechanic names; rejects unknown names and mechanics the media doesn't show. |
| Post rules | `scripts/post_rules.py` | 280 weighted chars, ≤2 hashtags, no @mentions/links (except your project URL), no invented sales/players/reviews/awards. |
| Analytics | `scripts/analytics.py`, `scripts/report.py` | Paginated Buffer fetch, `data/metrics.json`, evidence-only report, X CSV import, follower log. |
| Outreach | `scripts/outreach.py` | Lead tracker + drafts. **Never sends anything.** |
| TikTok videos | `scripts/tiktok_video.py`, `scripts/tiktok_board.py`, `scripts/tiktok_state.py` | Funny clip + muted live/curated gameplay cropped to the measured board, demo card in TikTok's safe area, full output validation, artifact for manual upload. State on the `tiktok-state` branch. **Never posts.** |

## Safety rules built in

* **Unattended, but never unchecked.** The daily run publishes only a post whose fresh media passed every media check
  and whose text passed the rules and the critic, through the same `publish_approved` function as a manual approval
  (hash, expiry, run, channel, media re-download + decode + provenance, post rules, Buffer queue check, 12 h spacing,
  single-use claim). At most **one post per Riyadh day** (a `ringshift-day-*` tag), never while Buffer has one waiting.
* **Kill switch:** repository variable `RINGSHIFT_AUTOPUBLISH=off` turns the schedule back into "draft only"; you then
  approve drafts with `publish-approved` exactly as before ([docs/APPROVING_A_POST.md](docs/APPROVING_A_POST.md)).
* **Genuine media only.** Every post's media was captured from the real stable game in the same run, with a manifest
  record (game commit, level, theme, SHA-256). No fresh file = no post; an old file is never re-posted as new.
* **The stable game build is never changed by this automation.** Level and theme experiments go to new `lab/` branches
  of the game repository as pull requests; the stable build and the live demo change only when you merge.
* Tests never touch real APIs: every network call raises, keys are fake. CI has a read-only token and no secrets.
* A failed Buffer call never advances the counter or media history; unconfirmed media = run fails visibly.
* Only HTTP 429 is retried (Buffer rejects those before creating anything). Timeouts/5xx are **not** retried, because the post might exist.
* The same text can't be queued twice (`data/post_log.json` + history + Buffer export).
* Metrics are never invented. Missing is "unknown", not zero; groups with fewer than 3 measured posts are never ranked.
* Secrets are never printed. Don't paste them into chats or commit them.

## Quick start

1. Secrets `GEMINI_API_KEY` and `BUFFER_API_KEY` are set (Settings → Secrets and variables → Actions). Optional:
   `RINGSHIFT_GAME_TOKEN` so the Game Lab can open pull requests on the game repository ([docs/AUTOMATION.md](docs/AUTOMATION.md)).
2. Nothing else to do: the schedule runs daily. To see it now: Actions → *Ringshift AI Autopilot* → Run workflow → `daily`.
3. Watch: the run summary (media, checks, post text, Buffer id and time) and your Buffer queue.
4. Manual modes still exist: `preview` (draft only), `publish-approved` (one post you approve), `rehearse-approval`.

## Tests

```
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

FFmpeg is needed for the media tests. The real-browser tests (capture, daily media, level lab, theme lab) are skipped
unless you set `RINGSHIFT_GAME_HTML=/path/to/ringshift vX.Y.html` (and installed `requirements-capture.txt`).

## What is and is not verified

See [docs/TESTING.md](docs/TESTING.md). In short: logic is covered by offline mocked tests (632); real capture, the level
lab and the theme validation were run against the real game; `publish-approved` has queued real posts with confirmed
media; **the unattended publish has not posted live yet**: the first scheduled run on `main` is its first live use.

More: [docs/AUTOMATION.md](docs/AUTOMATION.md) · [docs/AUDIT.md](docs/AUDIT.md) · [docs/MEDIA_HOSTING.md](docs/MEDIA_HOSTING.md) · [docs/GROWTH.md](docs/GROWTH.md) · [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) · [docs/KNOWLEDGE_PROPOSALS.md](docs/KNOWLEDGE_PROPOSALS.md)
