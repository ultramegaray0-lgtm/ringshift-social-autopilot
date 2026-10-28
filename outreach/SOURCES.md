# Outreach sources: research log

Checked by hand on **2026-10-28** from each source's own submission or contact page. Nobody was contacted and no form
was submitted. `sources.json` holds the machine-readable record; this page explains the decisions. Every run re-checks
each enabled page (it must load, allow us in robots.txt and still contain the `must_contain` phrase), so a route that
closes stops being used automatically.

Facts about Ringshift that decide eligibility, read from `ringshift-game@main` (`ringshift v0.34.html`, README):

* A free single-file HTML5 game (about 1 MB) on GitHub Pages; in development; works in the browser with nothing to
  install.
* No ads, no analytics, no network requests, no logins, no name entry; progress is kept in `localStorage`.
* No daily-puzzle mode.
* One external link: a "Wishlist on Steam" button that points at the Steam **front page** (there is no Ringshift
  Steam page yet). Outreach texts must not mention Steam; the checker already blocks "on Steam" and "wishlist".
* Whether any assets were AI-generated is not recorded anywhere in either repository. Sources with an AI-assets rule
  can only be used once the owner documents this.

## Added and enabled

| Source | Type | Route | Eligibility | Why Ringshift fits |
|---|---|---|---|---|
| Coolmath Games | browser-game directory | [developer submission page](https://www.coolmathgames.com/submit-a-game/nav) → Google Form | likely | Licenses HTML5 "thinking" games (logic, strategy, problem solving), non-exclusive. Requires no ads, no external links, no stats counter, no name entry; the shown version need not comply yet. **Unconfirmed:** the Steam button would have to go. |
| CrazyGames | browser-game directory | [Developer Portal](https://docs.crazygames.com/requirements/intro/) upload | likely | "Basic Launch" needs no SDK or customisation; limits (50 MB initial download, PEGI 12, no external ads or logins) are met. **Unconfirmed:** acceptance after QA; covers and a video must be prepared. |
| Alpha Beta Gamer | indie curator | [Game Submissions page](https://www.alphabetagamer.com/contact-us/) e-mail | meets | Features games that are in development and free to access; Ringshift is both. Every submission is read, not every one is featured. |
| I Dream of Indie Games | YouTube creator | business address in its own video descriptions | likely | Published "10 Upcoming Indie Puzzle Games Releasing in 2026" (2026-03-04). **Unconfirmed:** channel size (no API key during research) and whether free browser games are covered. |
| Thinky Games *(already present)* | puzzle curator | About page → suggestion forms | meets | Puzzle-only site. Its YouTube channel is now an alias, so a search hit for it can't create a second prospect. |

## Added but held (route verified, eligibility uncertain; never drafted)

| Source | Why held | Re-check when |
|---|---|---|
| [Overlooked.gg](https://overlooked.gg/request-review.html) | Submissions are open, but the team buys every game it reviews at full price and rates value for money; a free demo with no store page may not qualify. | Ringshift has a store page (itch.io or Steam). |

## Looked at and turned down (`excluded` in sources.json)

| Source | Reason |
|---|---|
| Playlin | Directory of **daily** web games ("Built a daily web game? Submit it"); Ringshift has no daily mode. |
| Games Y'all | A monthly in-person meetup; the submission rules page with the AI-generated-assets policy (moha.wiki) could not be read, and the repositories don't document how Ringshift's assets were made, so compliance cannot be shown. |
| Poki | Closed beta, hand-picked, expects web exclusivity and a mandatory SDK; Ringshift is already public on GitHub Pages. |
| Thinky Third Thursday (Draknek & Friends) | Very relevant (October 2026 edition includes free browser games) but publishes no submission address or form. Revisit if one appears. |
| Warp Door | Editors' address on the About page, but no statement that it takes submissions and no recent activity could be confirmed. |
| Turn Based Lovers | Turn-based RPG/strategy only; address obfuscated. |
| Draknek New Voices Puzzle Grant | A grant, closed 2026-09-27. |
| INDIE Live Expo | Showcase submissions closed 2026-09-28. |
| player.gg | Steam-App-ID listing service, not a contact route. |

Also checked: Higher Plain Games, Wanderbots and Matrix Plays Games all posted puzzle-game round-ups in 2026, but none
of the checked descriptions publishes a business address. They are not seeded; if the YouTube API finds them and a
business address appears in their own descriptions, discovery picks it up, otherwise they stay DISCOVERED until you
`add-contact` an address you found yourself.

## YouTube discovery (automatic, needs the optional `YOUTUBE_API_KEY`)

Nine queries (indie puzzle game and puzzle game demo newest-first; logic, spatial reasoning, brain teaser, thinky,
browser puzzle, puzzle review, unusual indie by relevance) over the last 90 days, about 900 of the free 10,000 daily
units. Channels between 200 and 500,000 subscribers are kept; scoring prefers 1k-100k channels, recent evidence and
channels that uploaded at least 3 videos in 90 days (read from their free RSS feed).
