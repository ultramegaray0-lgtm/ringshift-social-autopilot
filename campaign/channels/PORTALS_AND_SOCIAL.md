# Browser-game portals, social, newsletters and creators

Ranked by expected real players per hour of effort.

## A. Browser-game portals (people there are already looking for a game to play now)

The game is one self-contained HTML file, so portal uploads are cheap. Package built by `campaign/build_portal_zip.sh`
→ `campaign/assets/ringshift-web.zip` (`index.html` at the zip root, as Newgrounds requires).

| Portal | Route | Verified (2026-11-06) | Note |
|---|---|---|---|
| **Newgrounds** | Upload HTML5 zip on newgrounds.com/projects/games | Zip needs `index.html` at the root; new uploads appear immediately and go through a public *judgment* vote; low-scoring ones are deleted | Large audience that plays browser games daily. Use the 9:16 cover + the trailer GIF. |
| **CrazyGames** | developer.crazygames.com → *Basic Launch* | SDK **not** required for Basic Launch; ≤ 50 MB without SDK | Already in `outreach/sources.json` as a curator; this is the self-serve path. |
| **Game Jolt** | gamejolt.com → Add game → HTML build | Not verified from here | Free; has a puzzle/browser audience. |
| **Coolmath Games** | coolmathgames.com/submit-a-game | In `outreach/sources.json` | Logic/puzzle audience; selective licensing review. |
| ~~Poki~~ | — | Hand-picked, wants exclusivity + SDK | Excluded (see sources.json). |

Plays on a portal don't show in itch's counters. Record each portal's own play count in the ledger.

## B. Social (via the existing Buffer pipeline)

- **X:** the daily autopilot resumes automatically (it was blocked 2026-11-04 → 06 by a failing TikTok unit test in
  its gate; fixed on main by PR #21, all 889 offline tests pass). From this campaign's PR, **every 2nd X post carries
  the demo link** — 47 earlier posts had 0 clicks because none was allowed a link.
- **Bluesky:** the puzzle/"thinky" community is active there. Buffer's free plan supports Bluesky; connecting the
  channel is a 1-minute owner step in Buffer (not doable from here). Once connected, the same autopilot can post to it.
  First manual posts, one per day:
  1. `satisfying_solve_hard24.mp4` — "Three rings, three locks, all lit at once. Hard 24 in Ringshift, a free browser
     puzzle game I'm making: https://ringshift.itch.io/ringshift #thinkygames #puzzlegame #indiedev"
  2. `looks_impossible_hard18.mp4` — "15 moves in and 0 of 4 locks lit. Hard 18 looks impossible until you notice which
     ring is doing the blocking. #thinkygames #puzzlegame"
  3. `simple_vs_advanced.jpg` — "Easy 5 has one mechanism. Hard 15 has five. Same rule throughout: rotate the rings until
     every lock is lit at once. Free demo, 110 levels: https://ringshift.itch.io/ringshift #indiedev #gamedev"
- **YouTube Shorts:** `ringshift_trailer_9x16.mp4` and `looks_impossible_hard18.mp4` as Shorts, demo link in the
  description and pinned comment. Shorts are where puzzle clips get organic views without followers.

## C. Puzzle press, newsletters, curators

- **Thinky Games** (thinkygames.com): suggest-a-game forms on its About page; its free biweekly newsletter recommends
  one free web game per issue — Ringshift fits that slot exactly. Use the form; mention the free browser demo and 110
  levels. (Already enabled in `outreach/sources.json`; the daily outreach run drafts it.)
- **Alpha Beta Gamer**: covers demos/alphas; contact form route already in sources.json.
- **Overlooked.gg**: stays held — they buy every game they review at full price and rate value-for-money, which a free
  demo can't be judged on.

## D. Creators (YouTube)

The outreach pipeline already tracks 21 researched channels (Near Tragedy, I Dream of Indie Games, Olexa, Jugando
Indies have verified public business addresses) and drafts one honest email each; **its drafts now link the itch
page** (PR #23 merged in this campaign's PR). Sending stays approval-only: Actions → *Ringshift creator outreach* →
`deliver` with the draft id + approval code. Highest-fit first: Near Tragedy (hard logic puzzles), I Dream of Indie
Games (monthly indie puzzle round-ups).

## E. Jams that accept existing games

- **itch.io "2026 Jam"** (itch.io/jam/2026-jam, open to 2026-12-31, no theme): accepts an existing game only if it was
  developed **entirely in 2026**. Enter only if that is true for Ringshift; a jam entry page is another discovery
  surface.

## F. Hacker News — Show HN (added 2026-11-07)

Show HN is for something you made that people can try, ideally without signups (HN's Show HN guidelines, read
2026-11-07). A free browser puzzle with no account fits. Rules that matter: title starts with "Show HN:", the maker
posts and stays to answer, and asking anyone to upvote or comment is not allowed. Plain URL, no tracking tags
(news.ycombinator.com shows up as its own referrer).

- **Title:** `Show HN: Ringshift – a browser puzzle where you rotate rings until every beam hits a lock`
- **URL:** https://ringshift.itch.io/ringshift
- **First comment:**

  > I built this as a single self-contained HTML file. Each ring carries beam sources that fire through the centre;
  > a level is solved when every lock on the rim is lit at the same time. The rule the whole design hangs on: a ring's
  > own mirrors, prisms and portals rotate with it, so they never affect that ring's beam, only the others'.
  >
  > Every hand-made level is run through the game's own beam tracer before it ships. That caught two real bugs: two
  > sources on one ring sitting exactly opposite each other (they destroy each other's beams), and a portal pair with
  > mouths opposite on the same ring (an endless loop). The tracer is recursive because prisms split beams and echo
  > portals duplicate them, with one shared bounce budget for the whole branch tree.
  >
  > 110 levels in the demo. Happy to talk about the tracer or the level design.
- **When:** a weekday morning US Eastern, on a day nothing else is posted. Post once; never re-submit.
