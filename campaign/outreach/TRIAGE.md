# Creator outreach: queue triage (2026-11-07)

The outreach pipeline has been running daily since 2026-10-28 with a working YouTube API key. Its run of
2026-11-06 12:15 UTC reported **38 drafts awaiting approval, 0 sent, 0 replies**. Reading those drafts showed three
problems, now fixed on this branch:

1. **All 38 link the old GitHub Pages demo.** Delivery re-checks the link against the current config (the itch page
   since PR #24), so none of them could have been sent. `drafting.expire_drafts` now retires any waiting draft whose
   demo link is out of date and returns its prospect to the queue; the next daily runs redraft them (5 a day) with
   `https://ringshift.itch.io/ringshift?utm_source=email&utm_medium=outreach&utm_campaign=creators`.
2. **Drafts named the latest upload, even when it had nothing to do with puzzles.** Olexa's draft opened with
   "I'm writing because of your video 'I'm Organizing Orgies.'". `drafting.reference` now names the newest researched
   puzzle video instead (for Olexa: "I Love Minimalistic Logic Puzzles Games", 2025-08-26) whenever the latest upload
   isn't about puzzles.
3. **Search found mobile ad-style "Only 1% can solve this" shorts.** Those phrases (and bus-jam / screw-puzzle /
   parking-jam) are now exclusion keywords; three new search queries target browser and beam puzzles.

## How to read the redrafted queue

Approve from the top tier down. Every send still needs its draft id, approval code and the address typed in full
(Actions → *Ringshift creator outreach* → `deliver`, delivery `gmail-draft`). The pipeline sends nothing by itself.

### Tier A: clear fit, send first

| Prospect | Why (evidence in the draft) |
|---|---|
| Thinky Games | Puzzle-game site; suggest-a-game form; its newsletter picks one free web game per issue |
| Near Tragedy | Hard logic puzzle videos (Sokogram, Your Painting, Sir) |
| I Dream of Indie Games | Monthly indie lists and an indie-puzzle round-up |
| Vane | Puzzle game demo video, 2026-11-05 |
| ChippyFindsGames | "Are your friends SMART ENOUGH for this puzzle game?", 2026-11-04 |
| SableGhost | Puzzle game coverage (Escape Academy 2), 2026-11-04 |
| lunaloo | New arcade puzzle game video, 2026-11-05 |
| Hayden Got Games | "NODE: Is this the Best Indie Puzzle Game on Steam?", 2026-08-23 |
| Alpha Beta Gamer | Covers games in demo; email route on its submissions page |
| Jugando Indies | "Los Mejores Juegos de Rompecabezas del 2026"; channel is Spanish, draft is English |
| Games Puff | "10 Best Puzzle Games (2026 Edition)", "10 Best Logic Games 2026" |
| Olexa | Minimalist logic puzzle video (redraft names it now) |

### Tier B: plausible, open the channel before approving

kat 'n chat (cozy wooden puzzle), Tasselfoot (browser puzzle walkthroughs, newest dated video Sept), JustNovaGames,
Shared XP, BergerBunz Gaming (escape-room puzzles), Natplayscozy (detective), Feiito Gaming (indie lists),
Alex Blogoodf (cozy indie), pugbeans (indie twists), BrainBendersBanter (brain-teaser shorts), Coolmath Games (form;
its old warning about a Steam wishlist link no longer applies: in v0.44 that button stays hidden unless a store URL is
set), CrazyGames (portal upload).

### Tier C: recommend rejecting (`mark` → `reject` with the draft id)

| Prospect | Why |
|---|---|
| eagleRUDRA Shorts | Train-track shorts, not puzzle games |
| Joker Yt, Rajput Gamer | "Only 1% can solve" mobile ad puzzles |
| tytrgames | Mobile block-puzzle clips |
| ChrisMZD | Indonesian story shorts |
| VoodooDE VR, Mets ta Quest - FrOgZ | VR-only channels; Ringshift isn't a VR game |
| The Stingy Gamer, The Cosmik Hub | Climbing game (Cairn) coverage, one marked #ad |
| Vic's Gaming Reviews, Lord Rollie, UniqueGeese | No puzzle coverage in the evidence |
| ErdnaVerse | One-off puzzle short, otherwise unrelated |
| A Daily Slice Of Oranje | Daily word/logic puzzles (Wordle, Connections); Ringshift has no daily mode |

## Prospects added this sprint (`outreach/sources.json`)

Read on their own pages 2026-11-06/07. "Enabled" ones are drafted by the next daily run; "held" ones wait until the
run can confirm what research couldn't.

| Prospect | Type | Evidence | Status |
|---|---|---|---|
| BJ & Co | YouTube | "Sokogram is WAY harder than it looks!" (2026-09-10) | enabled |
| heyKipp | YouTube | "A BRAND NEW PUZZLE GAME THAT GET'S SO DIFFICULT! Piece By Piece" (2026-08-28) | enabled |
| SB | YouTube | "Next Fest Spring '26: Piece By Piece" (2026-02-26) | enabled (older evidence flagged) |
| Yay! Indie Games | YouTube | "9 MIND BLOWING Indie PUZZLE Games That Rewrite the Rules" (2025-11-29) | enabled (older evidence flagged) |
| Armor Games | Browser portal | Developer docs: accepts only HTML5 games, submissions by email | enabled |
| SoftGuide | YouTube | "5 Free Browser Games You Need to Try in 2026" (2026-09-01) | held: games unnamed |
| Next Indie | YouTube | Channel lists Puzzle Games as a topic | held: no dated video |
| jetpackpaul | YouTube | "October 2026 Demos! (no GenAI games)" (2026-10-21) | held: puzzle coverage unconfirmed |
| Cozy Game Reviews | Blog | "Best Cozy Browser Games" (2026-07-17), itch.io games listed | held: contact page unread |
| Joe Plays Puzzle Games | alias | Same person as Thinky Games (its About page) | alias, not a second contact |

Also recorded as **excluded** so nobody re-adds them: Jay Is Games (a review URL on the domain now serves casino
spam), Good Game Lobby (no route), Puzzle Review (physical puzzles), Indirect, FireStarW, Keenangraz and Seldell
(no recent puzzle evidence).

**Why not 30:** YouTube refused most page reads from this workspace (rate-limited after a few requests), so only
channels whose pages could be read were added; nothing was added from search snippets alone. The daily pipeline,
which reads YouTube through the API, now runs 12 targeted searches and adds verified channels on its own.
