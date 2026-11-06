# itch.io — the demo's home, and its own discovery engine

itch.io lists new games under tag/genre/platform feeds ("Newest", "New & Popular" for Puzzle + Web). Early views,
browser plays and ratings in the first days decide whether a game climbs those lists. So: concentrate the first
outside traffic into the same few days, and make sure the page converts a visit into a play.

Status: **prepared.** This automation cannot log in to itch.io; each item below takes a few minutes in the itch
dashboard.

## 1 · Page check (before any traffic is sent)

- [ ] Visibility **Public** (not Draft/Restricted). Open the page logged out on a phone: does it load, and does
      "Run game" start without login? (This could not be verified from here: itch.io refuses this environment, and the
      page is not yet in web search results.)
- [ ] **Kind of project: HTML** · "This file will be played in the browser" ticked · **Mobile friendly** ticked
      (the game is built mobile-first) · viewport sized for a phone-portrait board, or "Fullscreen button" on.
- [ ] **Genre: Puzzle.** Tags (max 10): `puzzle`, `logic`, `brain-teaser`, `minimalist`, `singleplayer`, `html5`,
      `relaxing`, `mouse-only` / `touch-friendly` as applicable, `lasers`, `rotation`.
- [ ] Short description (shown in feeds): *Rotate concentric rings so every beam lights every lock at once. 110
      levels, free in your browser.*
- [ ] Cover: `ringshift-game/marketing/itch-cover/ringshift-itch-cover-630x500.png` (already made).
- [ ] Screenshots: `campaign/assets/simple_vs_advanced.jpg`, `challenge_hard24_puzzle.jpg` + 2–3 from
      `media/gameplay/*.png`.
- [ ] Trailer: upload `campaign/assets/ringshift_trailer_16x9.mp4` to YouTube (unlisted is fine) and paste the link in
      itch's *Gameplay video or trailer* field.
- [ ] Community: enable **Comments** so players can respond (itch counts this engagement).
- [ ] Analytics: Dashboard → Analytics shows views, browser plays and **Referrers**. Copy the referrer table into
      `data/campaign/ledger.json` → `referrers` once a week.
- [ ] Settings → API keys → *Generate new API key* → add it as the repository secret **ITCH_API_KEY**. The new
      *Ringshift itch stats* workflow then records views and downloads/plays every day.

## 2 · Release Announcements post (itch community) — once, now

That board is for announcing a newly published project (an itch moderator: it is meant for posting when you've
published a new project; updates belong in devlogs).

**Title:** Ringshift — a ring-rotation beam puzzle (free, browser, 110 levels)

> Hi! I've just put Ringshift on itch. You rotate concentric rings; beam sources on each ring fire straight through
> the centre, and a level is solved when every lock on the outer rim is lit at the same time.
>
> Mirrors bend beams 90°, prisms split them in two, portals and echo portals move them (an echo keeps the original
> going too), amplifiers punch through other rings' sources, and half-blocks only stop one direction. The twist: a
> ring's own pieces never affect its own beam, so every move changes what the *other* rings can do.
>
> 110 levels: Easy, Normal and Hard ladders of 25 each, plus Challenge, Space Orchestra and Rocket Attack sets. It
> plays in the browser on desktop and phones.
>
> https://ringshift.itch.io/ringshift — feedback on where you got stuck is very welcome.

Attach `campaign/assets/simple_vs_advanced.jpg`.

## 3 · First devlog (on the game page → Devlog → New post)

Devlogs appear in itch's devlog feed and in followers' feeds. Type: *Game design*.

**Title:** Why a ring's own mirror can't bend its own beam

> Every ring in Ringshift carries its beam sources *and* its mechanisms. Rotate a ring and both move together, so
> their angle to each other never changes. That means a ring's own mirror, prism or portal can never redirect that
> ring's own beam — they only ever act on beams from the other rings.
>
> It sounds like a limitation, but it's the core of the puzzle: rotating one ring changes what every other ring's
> beams run into. It also gave me two rules I check on every level: a ring's two sources must never sit exactly
> opposite each other (its beams would destroy each other on its own second source), and a portal's two mouths must
> never sit exactly opposite on the same ring (the beam would loop forever). Every level is run through the game's own
> beam tracer before it ships, which is how both problems were caught.
>
> The board above is Hard 24: three rings, three locks. Which ring would you turn first?

Attach `campaign/assets/challenge_hard24_puzzle.jpg` and the clip `satisfying_solve_hard24.mp4` (or a GIF of it).
