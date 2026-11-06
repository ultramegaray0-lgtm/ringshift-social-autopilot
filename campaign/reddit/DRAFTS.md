# Reddit drafts — one community at a time

Every post below is different in angle, title, body and media. Each has its own date; **do not** post two on the same
day. Dates assume the cool-down in POSTMORTEM.md. Facts are limited to what the game really does (110 levels counted
in `ringshift v0.44.html`: Easy 25, Normal 25, Hard 25, Challenge 5, Space Orchestra 10, a special mode 10, Rocket
Attack 10).

Status: **prepared, not published.** No Reddit integration or logged-in browser is available to this automation, and
Reddit's API terms (see docs/REDDIT_GROWTH.md) bar automated promotional posting without written approval.

---

## 1 · r/WebGames — 2026-11-13 (link post)

Rules checked (r/WebGames wiki, read 2026-11-06 via a public mirror): must run in a browser with no download; title
must **start with the game's name**; a developer's personal games ≤ 1 post per week; the same game not again within
3 months; itch.io is on the link whitelist.

- **Type:** link → https://ringshift.itch.io/ringshift (first confirm the page plays in the browser without login)
- **Title:** `Ringshift - rotate concentric rings so every beam lands on a lock at the same time (free, browser, 110 levels)`
- **Flair:** Puzzle (if offered)
- **First comment (post immediately):**

  > I'm the developer. Each ring carries beam sources that fire straight through the centre; you rotate rings until
  > every lock on the outer rim is lit at once. Mirrors bend beams, prisms split them, portals and echo portals move
  > them, half-blocks only stop one direction. A ring's own pieces never affect its own beam, which is most of the
  > puzzle. Easy starts gently; Hard gets mean. Happy to hear where it gets confusing.

## 2 · r/playmygame — 2026-11-16 (text post)

Rules checked (mod post "How to avoid having your posts removed v2"): free to play; written description; you must be on
the team; add platform flair after posting; **once per calendar month**; direct link to where it's played.

- **Title:** `[Browser] Ringshift - a ring-rotation beam puzzle, looking for feedback on the difficulty curve`
- **Body:**

  > **What it is:** a puzzle game where you rotate concentric rings. Beam sources on the rings fire through the
  > centre, and a level is solved when every lock on the rim is lit at the same time.
  >
  > **Play (free, in the browser, nothing to install):** https://ringshift.itch.io/ringshift
  >
  > **What's in the demo:** 110 levels — Easy, Normal and Hard ladders of 25 each plus Challenge, Space Orchestra and
  > Rocket Attack sets. Mechanisms: mirrors, blocks, amplifiers, portals, half-blocks, prisms, echo portals.
  >
  > **What I'd love feedback on:** where you got stuck for the wrong reason (couldn't read the board) versus the right
  > reason (the puzzle was hard). Screenshots of the exact level help a lot.
  >
  > I'm the solo developer.
- **Media:** none needed (text post); optionally link `simple_vs_advanced.jpg` in a comment.
- **After posting:** add the *Browser* platform flair.

## 3 · r/puzzlevideogames — after a mod reply (discussion post)

Community description (via GummySearch, 2026-11-05): "talking about, analyzing, recommending and **creating** puzzle
games", ~24k members. Self-promotion rules could not be read → **send MODMAIL.md #1 first; post only if they say yes.**

- **Title:** `Design question: in my ring puzzle, a ring's own mirror can never redirect its own beam. Does that read as a rule or as a bug?`
- **Media:** `campaign/assets/challenge_hard24_puzzle.jpg`
- **Body:**

  > I'm making Ringshift, a puzzle about rotating concentric rings so beams light every lock on the rim. Because a
  > ring's beam and its own pieces rotate together, a ring's mirrors/prisms/portals only ever act on *other* rings'
  > beams. Mechanically that's what makes the puzzles work, but new players sometimes expect their own ring's mirror
  > to bend their own beam.
  >
  > For people who play a lot of puzzle games: do you prefer that taught by a tutorial level, or discovered?
  > (Free browser demo in a comment if anyone wants to try the board above — only if that's OK here.)

## 4 · r/IndieDev — 2026-11-22 (video post, developer introduction)

Rules not verified from here; r/IndieDev is the developer-showcase community (r/gamedev is not). Check the sidebar
the day of posting; use the showcase/video flair it asks for.

- **Title:** `Solo dev here: 30 seconds of my ring-rotation puzzle, from the first Easy level to Hard 24`
- **Media:** `campaign/assets/ringshift_trailer_16x9.mp4`
- **Body:** two lines on what it is, one on what you learned building it (e.g. every level is checked against the
  game's own beam tracer before it ships), link in a comment only if the rules allow.

## Never again
r/RatchetAndClank (off-topic), r/NewItchio (site-wide removal), r/itchio until its mods answer MODMAIL.md #2.
