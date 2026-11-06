# Reddit drafts — one community at a time, each with its own reason to exist

Status: **prepared, not published.** No Reddit integration or logged-in browser is available to this automation, and
Reddit's API terms (docs/REDDIT_GROWTH.md) bar automated promotional posting without written approval.

Facts are limited to what the game does (RINGSHIFT_KNOWLEDGE.md; 110 levels counted in `ringshift v0.44.html`: Easy
25, Normal 25, Hard 25, Challenge 5, Space Orchestra 10, Meteor Attack 10, Rocket Attack 10).

**Links:** Reddit posts use the plain demo URL. Tracking parameters on a community link read as marketing to
moderators and readers; reddit.com already shows up as its own referrer in itch's analytics, and each post has its
own date in the ledger, so attribution is not lost.

**Order and spacing** (POSTMORTEM.md): participation first (PARTICIPATION.md), then one community every 3+ days,
nothing before 2026-11-13. Each draft below answers a different question for a different audience.

| # | Community | Date | The reader's question it answers | Media |
|---|---|---|---|---|
| 1 | r/WebGames | 2026-11-13 | "Is there something good I can play in this tab right now?" | link post |
| 2 | r/playmygame | 2026-11-16 | "Who wants testers, and for what?" | text post |
| 3 | r/puzzlevideogames | after a mod yes | "What's interesting about this puzzle's design?" | image |
| 4 | r/IndieDev | 2026-11-22 | "What are other developers building, and how?" | video |

---

## 1 · r/WebGames — 2026-11-13 (link post)

Rules (r/WebGames wiki, read 2026-11-06 via a public mirror): plays in a browser with no download; title **starts
with the game's name**; a developer's own games ≤ 1 post per week; same game not again within 3 months; itch.io is
whitelisted.

- **Link:** https://ringshift.itch.io/ringshift (first confirm it runs logged out on a phone and on desktop)
- **Title:** `Ringshift - rotate the rings until every beam lights every lock at once (free, 110 levels)`
- **First comment, posted right away:**

  > I made this. Each ring carries beam sources that fire through the centre; you rotate rings until every lock on
  > the rim is lit at the same moment. Mirrors bend beams, prisms split them, portals and echo portals move them,
  > half-blocks only stop one direction. A ring's own pieces never touch its own beam, so every move is really about
  > the *other* rings. Easy starts with one mechanism; Hard 15 has five. Works on phones too.

## 2 · r/playmygame — 2026-11-16 (text post)

Rules (mod post "How to avoid having your posts removed v2"): free to play; written description; you must be on the
team; platform flair after posting; **once per calendar month**; direct link.

- **Title:** `[Browser] Ringshift - ring-rotation beam puzzle; I want to know where the difficulty jumps too fast`
- **Body:**

  > **What it is:** you rotate concentric rings. Beam sources on the rings fire through the centre, and a level is
  > solved when every lock on the rim is lit at the same time.
  >
  > **Play (free, browser, desktop or phone):** https://ringshift.itch.io/ringshift
  >
  > **In the demo:** 110 levels. Easy, Normal and Hard ladders of 25 each, plus Challenge, Space Orchestra, Meteor
  > Attack and Rocket Attack sets. Mechanisms: mirrors, blocks, amplifiers, portals, half-blocks, prisms, echo portals.
  >
  > **What I'd like to learn:** the first level where you got stuck because you couldn't *read* the board (as opposed
  > to the puzzle being hard). A level number is all I need.
- **After posting:** add the *Browser* flair. Reply to every comment the same day.

## 3 · r/puzzlevideogames — only after a moderator says yes (MODMAIL.md #1)

~24k members; the community describes itself as being about talking about, analyzing, recommending and creating
puzzle games. Its self-promotion rules could not be read from here.

- **Title:** `Design question: a ring's own mirror can never redirect that ring's beam. Rule worth teaching, or let players find it?`
- **Image:** `campaign/assets/challenge_hard24_puzzle.jpg`
- **Body:**

  > I'm making Ringshift, a puzzle about rotating concentric rings so beams light every lock on the rim. Because a
  > ring's sources and its own pieces turn together, a ring's mirrors, prisms and portals only ever act on the other
  > rings' beams. The whole design leans on that.
  >
  > For people who play a lot of puzzle games: would you rather a rule like this be stated in a tutorial level, or
  > discovered by failing once? The board above is Hard 24 (three rings, three locks) if anyone wants to reason about it.
- **Link:** none in the post. If someone asks to play, reply with the plain URL.

## 4 · r/IndieDev — 2026-11-22 (video post)

Rules not readable from here; re-read the sidebar on the day and use the flair it asks for.

- **Title:** `Every level in my ring puzzle is checked against the game's own beam logic before it ships. Here's 30 s of it`
- **Video:** `campaign/assets/ringshift_trailer_16x9.mp4`
- **Body:**

  > Ringshift: rotate concentric rings until every lock on the rim is lit at once. I hand-author the Easy, Normal and
  > Hard ladders and run each level through the game's own beam tracer before it goes in. That check caught two real
  > design bugs: a ring whose two sources sat exactly opposite each other (its beams destroyed each other), and a
  > portal pair whose mouths sat opposite on one ring (an endless loop). Free browser demo in the comments if the
  > rules allow.

## Never again
r/RatchetAndClank (off-topic), r/NewItchio (site-wide removal), r/itchio until its mods answer MODMAIL.md #2.
