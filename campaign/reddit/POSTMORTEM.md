# Reddit post-mortem — removals of early November 2026

## What happened (as reported by the owner)

| Community | Outcome | Posted |
|---|---|---|
| r/RatchetAndClank | filtered | ~same 32-minute window |
| r/itchio | filtered | ~same 32-minute window |
| r/NewItchio | removed "for content-policy reasons" | ~same 32-minute window |

Similar promotional text and the same new link, three communities, about 32 minutes.

## Most likely causes, most to least certain

1. **Cross-posting the same promotion in a burst.** Reddit's Content Policy (Rule 2) asks people to post authentic
   content into communities they personally take part in, and forbids spamming. The same link + similar text to
   several communities within minutes is the textbook pattern that both Reddit's site-wide spam filter and AutoModerator
   rules catch. Two "filtered" outcomes (held before anyone saw them) fit this exactly.
2. **A brand-new link domain.** `ringshift.itch.io` had no history on Reddit and is not yet indexed by search engines
   (checked 2026-11-06: two web searches for "ringshift.itch.io" returned three other games named Ringshift, not this page). New domains posted
   repeatedly in a short window look like link-spam to the filters.
3. **Account history.** If the account has little non-promotional activity, subreddit filters (karma / account-age /
   "mostly self-links") remove or hold posts automatically. The 10 %-of-activity guideline is a community norm, not a
   published Reddit number, but moderators apply it.
4. **Off-topic venue (r/RatchetAndClank).** A franchise fan subreddit is about Ratchet & Clank, not other games that
   resemble it; an unrelated game's promo is off-topic there almost everywhere. Do not retry it.
5. **"Content policy" removal (r/NewItchio).** That wording usually means the removal came from Reddit's site-wide
   systems rather than a moderator. It is the strongest signal of the three: the account/link combination was
   judged spammy. Do not re-post or appeal by re-submitting.

**Could not verify:** the current rule pages of r/itchio, r/NewItchio and r/RatchetAndClank. Reddit refuses requests
from this research environment and no reliable mirror of their sidebars was found. Before any message to those mods,
read their rules on Reddit itself.

## Recovery rules (built into the drafts in this folder)

* **Cool-down:** no link posts from this account before **2026-11-13** (7 days after the removals). In the meantime,
  ordinary participation only: comment helpfully on other people's puzzle games, no links.
* **One community at a time, ≥ 3 days apart**, each with its own angle and its own media. Never the same text twice.
* **Only communities whose rules were read and allow it** (see `communities.json`). Unclear → ask the mods first.
* **Never** repost a filtered/removed post, re-word it to get past a filter, use a second account, or link-shorten.
* Disclose "I made this" in every post. Reply to every comment for the first few hours.
* If the account is site-wide restricted (posts silently vanish everywhere), stop all Reddit activity and use
  https://www.reddit.com/appeals — do not keep testing.
