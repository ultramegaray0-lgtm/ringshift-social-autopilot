# Ringshift Social Autopilot

Promotes the indie puzzle game **Ringshift** on X with genuine gameplay media, at roughly zero cost:
GitHub Actions + Gemini (free tier) + Buffer (free plan) + FFmpeg/Playwright on your own PC.

```
 your PC (occasionally)                       GitHub Actions (daily DRAFT only)
 ┌──────────────────────────┐                ┌─────────────────────────────────────────────┐
 │ capture_gameplay.py      │  commit/push   │ 1. offline tests (gate)                      │
 │  plays the real game,    ├───────────────►│ 2. analytics.py   Buffer -> data/metrics.json│
 │  records clip + stills   │  media/ or     │ 3. autopilot.py   pick media FIRST, write the│
 │ media_store.py (optional)│  media-store   │    post about it, screen it, critic, queue   │
 └──────────────────────────┘                │ 4. commit counters/logs                      │
                                             └──────────────────────────┬──────────────────┘
                                                                        ▼
                                                              Buffer queue -> X
```

## What it does

| Area | Script | Notes |
|---|---|---|
| Capture real gameplay | `scripts/capture_gameplay.py` | Drives your game file in headless Chromium using the game's own solver and real key presses; only keeps a clip if the level is actually won. |
| Validate / edit media | `scripts/media_tools.py` | FFmpeg: H.264, 4:5, ≤15 MB, ≤45 s, rejects black/static/broken files. |
| Free media hosting | `scripts/media_store.py` | Optional `media-store` branch (one squashed commit, newest 60 clips) so `main` doesn't grow. |
| Choose media | `scripts/media_selector.py` | Verified tags + descriptions, least-used rotation. |
| Write + publish posts | `scripts/autopilot.py` | Media first, then text about what it really shows; 6 rotating post types; rule, game-vocabulary and near-duplicate screening; claim-checking critic; Buffer publish with media confirmation. |
| Approved drafts | `scripts/approvals.py` | Draft ID + content hash + expiry; verifies, then claims a draft once before publishing exactly it. |
| Game vocabulary | `scripts/game_terms.py` | Only the knowledge file's mechanic names; rejects unknown names and mechanics the media doesn't show. |
| Post rules | `scripts/post_rules.py` | 280 weighted chars, ≤2 hashtags, no @mentions/links (except your project URL), no invented sales/players/reviews/awards. |
| Analytics | `scripts/analytics.py`, `scripts/report.py` | Paginated Buffer fetch, `data/metrics.json`, evidence-only report, X CSV import, follower log. |
| Outreach | `scripts/outreach.py` | Lead tracker + drafts. **Never sends anything.** |

## Safety rules built in

* **A manual post needs your explicit approval of the exact draft.** Manual modes are `test`, `preview` (writes a draft, sends nothing), `rehearse-approval` (re-checks a draft and the claim mechanism, sends nothing) and `publish-approved` (publishes exactly the draft whose ID you type, once). The daily schedule **only writes a draft** (it never calls Buffer to post); `publish-approved` is the only path that can post, and only for a draft ID you type. See [docs/APPROVING_A_POST.md](docs/APPROVING_A_POST.md).
* Tests never touch real APIs: every network call raises, keys are fake. CI has a read-only token and no secrets.
* A failed Buffer call never advances the counter or media history; unconfirmed media = run fails visibly.
* Only HTTP 429 is retried (Buffer rejects those before creating anything). Timeouts/5xx are **not** retried, because the post might exist.
* The media URL is checked (HEAD, no redirects, right size) *before* a post is created.
* The same text can't be queued twice (`data/post_log.json` + history + Buffer export).
* Metrics are never invented. Missing is "unknown", not zero; groups with fewer than 3 measured posts are never ranked.
* Secrets are never printed. Don't paste them into chats or commit them.

## Quick start

1. Add repository secrets `GEMINI_API_KEY` and `BUFFER_API_KEY` (Settings → Secrets and variables → Actions). See [docs/SETUP.md](docs/SETUP.md).
2. Actions → *Ringshift AI Autopilot* → Run workflow → `preview`. Read the summary.
3. To publish that draft: run `publish-approved` with its draft ID. Step by step, from a phone: [docs/APPROVING_A_POST.md](docs/APPROVING_A_POST.md). The daily schedule (06:00 UTC) makes a draft each day and never publishes; how to switch it off and on is in the same document.
4. Capture more media on your PC: [docs/WINDOWS_CAPTURE.md](docs/WINDOWS_CAPTURE.md). How media is matched to posts: [docs/MEDIA_FACTORY.md](docs/MEDIA_FACTORY.md).

Optional: put your public game page in `data/project.json` (`"project_url": "https://..."`) to enable the "invitation" post type. It is never invented; without it the post type is skipped.

## Tests

```
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

FFmpeg is needed for the media tests. The one real-browser test is skipped unless you set
`RINGSHIFT_GAME_HTML=/path/to/game.html` (and installed `requirements-capture.txt`).

## What is and is not verified

See [docs/TESTING.md](docs/TESTING.md). In short: logic is covered by offline mocked tests; media capture was run for real
against the game; **publishing to Buffer has not been verified live by this code change** and must be confirmed by one
`publish-approved` run you start.

More: [docs/AUDIT.md](docs/AUDIT.md) · [docs/MEDIA_HOSTING.md](docs/MEDIA_HOSTING.md) · [docs/GROWTH.md](docs/GROWTH.md) · [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) · [docs/KNOWLEDGE_PROPOSALS.md](docs/KNOWLEDGE_PROPOSALS.md)
