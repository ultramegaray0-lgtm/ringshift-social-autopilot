# itch.io page package — conversion audit and ready-to-paste materials

**Limitation (2026-11-06):** the live page https://ringshift.itch.io/ringshift could not be opened from this
workspace (itch.io refuses the connection; the page is not in web-search results yet). The audit below is therefore
against what a good itch page needs, not a reading of the current page. Everything is prepared locally; applying it
takes the owner's itch login.

## The visitor's 10 seconds, and what each part of the page must do

| Moment | What they see on itch | What it must answer | Prepared |
|---|---|---|---|
| 0–2 s | Title, cover, first screenshot | *What is this?* | Cover (`ringshift-game/marketing/itch-cover/…630x500.png`); first screenshot is a **solved** board (green locks read as "success" at thumbnail size) |
| 2–5 s | First line of the description | *What would I be doing?* | Heading: "Rotate the rings until every beam lands on a lock, all at the same time." |
| 5–8 s | Line 2 + the GIF | *Can I play it right now, for free?* | Bold line: free, browser, desktop or phone, no download, no account; 6 s GIF of Hard 24 being solved |
| 8 s+ | "Run game" | Start | Embed options below; "Run game" must be visible without scrolling on a phone |

## Settings (Edit game)

- **Kind of project:** HTML · **Embed options:** "Click to launch in fullscreen" **on phones** (mobile-first board),
  embed in page on desktop · **Mobile friendly** ✔ · **Orientation:** portrait · **Fullscreen button** ✔ ·
  viewport size for a portrait board, e.g. 540 × 960.
- **Classification:** Games · **Release status:** In development (the demo is a demo; don't claim "Released").
- **Genre:** Puzzle. **Tags** (itch allows 10; ordered by how players browse): `puzzle`, `logic`, `brain-teaser`,
  `minimalist`, `singleplayer`, `lasers`, `rotation`, `relaxing`, `html5`, and `mobile` if Mobile friendly is ticked.
- **Short description / tagline** (shown in every feed and search card):
  *Rotate the rings until every beam lights every lock at once. 110 levels, free in your browser.*
- **Pricing:** No payments (or "$0 or donate"): a free demo converts best when nothing looks like a paywall.
- **Community:** Comments ✔ (itch counts engagement; the description ends by asking for level-numbered feedback).
- **Analytics (attribution):** itch supports a Google Analytics 4 measurement ID per project (dashboard → the
  project's *Interact* section) or account-wide (user settings → Analytics). With it, every `utm_*` link this campaign
  uses (`data/campaign/links.json`) becomes a per-channel, per-angle visit count. Without it, itch shows referrer
  sites only.

## Description

Paste `description.html` (this folder). It opens with the rule, then the browser/free line, then the GIF
(`campaign/assets/itch_solve_hard24.gif`, 480 px, 1 MB, loops), then how it plays, the mechanisms in the knowledge
base's own words, the 110-level breakdown counted in `ringshift v0.44.html`, controls (from the game's own hint text),
and one specific call to comment.

What it deliberately leaves out: review quotes, player counts, release dates, platform promises, "addictive",
"mind-bending" and similar words nobody can check.

## Screenshots — upload in this order (`screenshots/`)

| # | File | Why it is in this position |
|---|---|---|
| 1 | `01_hard24_solved.jpg` | The payoff, readable as a thumbnail: green locks, "BREACHED". Becomes the feed image if no cover. |
| 2 | `02_hard24_unsolved.jpg` | The same board before: the reader now understands the goal by comparing 1 and 2. |
| 3 | `03_easy5_vs_hard15.jpg` | Reassurance that it starts gently and goes deep. |
| 4 | `04_before_after_hard25.jpg` | A second worked example with the HUD counts (0/3 → 3/3). |
| 5 | `05_hard1_halfblock_portal.jpg` | Shows a Hard level is approachable on a landscape screen. |
| 6 | `06_lava_theme_normal20.jpg` | Visual variety for people who skip abstract boards. |

## Trailer

Upload `campaign/assets/ringshift_trailer_16x9.mp4` to YouTube (any visibility except private), paste the link into
*Gameplay video or trailer*. itch shows it above the screenshots. Use
`https://ringshift.itch.io/ringshift?utm_source=youtube&utm_medium=video&utm_campaign=angles&utm_content=a01-rule`
as the link in its description.

## After the page goes live

1. Open it logged out on a phone. "Run game" visible without scrolling? Board fits portrait? Arrows usable?
2. Post the Release Announcements topic and the first devlog (`campaign/channels/ITCH.md` §2–3).
3. Add `ITCH_API_KEY` so the daily stats job starts recording views and plays.
