# Ringshift player-acquisition campaign (from 2026-11-06)

KPI: real people reaching https://ringshift.itch.io/ringshift and playing it. Impressions are not the goal.

| Folder / file | What |
|---|---|
| `reddit/POSTMORTEM.md` | Why the three early-November posts were filtered/removed, and the recovery rules |
| `reddit/DRAFTS.md`, `reddit/MODMAIL.md` | One post per community, different angle each, dated ≥ 3 days apart; mod messages |
| `channels/ITCH.md` | itch page checklist, Release Announcements post, first devlog |
| `channels/PORTALS_AND_SOCIAL.md` | Newgrounds / CrazyGames / Game Jolt, Bluesky + Shorts posts, curators, creators, jams |
| `assets/` | Trailer (9:16, 16:9), challenge, satisfying-solve, mechanism clips, comparison still — all from real captured gameplay (`build_assets.py`) |
| `build_portal_zip.sh` | Packs the stable game build as `index.html` in a zip for portals |
| `copy/angles.json` → `copy/READY_TO_POST.md` | 16 tested promotional angles with X, Bluesky and Shorts copy and a tagged link per platform (`scripts/angle_bank.py`) |
| `itch/ITCH_PAGE.md`, `itch/description.html`, `itch/screenshots/` | itch page package: settings, tags, description, screenshot order, trailer |
| `outreach/TRIAGE.md` | The 38-draft creator queue: what was wrong, tiers to approve or reject, prospects added |
| `reddit/PARTICIPATION.md` | A week of link-free participation before the first Reddit post |
| `build_conversion_assets.py` | Before/after composites, the itch GIF and the screenshot set |
| `../data/campaign/links.json` | Every tagged demo link and the angle/asset it belongs to |
| `../data/campaign/ledger.json` | Every activity and its status; `REPORT.md` is regenerated from it daily |

Tracking: every social, video and email link carries `utm_*` tags (`scripts/tracking.py`); community posts
(Reddit, HN, itch forums) use the plain URL. Measurement: `scripts/itch_stats.py` (daily itch views + downloads/plays via itch's server-side API, secret
`ITCH_API_KEY`), Buffer metrics for X clicks, ledger entries for everything else. Unmeasured = `unavailable`.
