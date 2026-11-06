# Reddit autopublishing: approval-gated setup

## Current status

The publisher is implemented but **disabled by default**. It will not contact Reddit or submit posts until all of these independent gates are satisfied:

1. Reddit has explicitly approved the Data API use case.
2. Reddit has given written permission for commercial use. Promoting Ringshift is treated as commercial because it is a game/product that may be monetized.
3. The target subreddit has been checked against its current official rules and the registry records that self-promotion, links, and automated submissions are allowed.
4. GitHub Actions has the required OAuth credentials and approval variables.
5. The global and per-community cooldowns allow the submission.

Reddit's current policy requires explicit approval before API access and written approval for commercial use. It prohibits spam through automated posts, including identical or substantially similar content across subreddits. This implementation therefore limits automation to one distinct post every seven days globally and a 30-day cooldown per subreddit. It does not automate comments, votes, DMs, chats, follows, or user targeting.

Before requesting access, use the prepared honest application draft in [REDDIT_ACCESS_REQUEST.md](REDDIT_ACCESS_REQUEST.md). For a candidate community, use [REDDIT_MODERATOR_REQUEST.md](REDDIT_MODERATOR_REQUEST.md) to ask moderators whether scheduled submissions are permitted. Submit it through Reddit's official [API request form](https://support.reddithelp.com/hc/en-us/requests/new); this repository cannot submit or obtain approval on your behalf.

Official policy references:
- https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy
- https://support.reddithelp.com/hc/en-us/articles/14945211791892-Developer-Platform-Accessing-Reddit-Data
- https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki
- https://www.reddit.com/dev/api/

## GitHub Actions configuration (only after approval)

Register the app with Reddit, create the required developer profile/app label, and use a dedicated Reddit account that is used only for this app's functions (not a mixed personal account). The OAuth username configured below must be that dedicated account. Do not disguise the account as a human-operated account.

Add these repository Actions secrets:
- REDDIT_CLIENT_ID
- REDDIT_CLIENT_SECRET
- REDDIT_REFRESH_TOKEN

Before enabling the workflow, edit reddit/automation.json and set enabled, api_access_approved, and commercial_use_approved to true only after the corresponding Reddit approvals are received. Set approval_reference to the exact approval record reference and target_subreddit to the one reviewed community. Keep every value false/null until then.

Add these repository Actions variables:
- REDDIT_USERNAME — the actual Reddit account username.
- REDDIT_SUBREDDIT — one subreddit name without r/.
- REDDIT_API_APPROVED=true — only after Reddit explicitly approves this API use case.
- REDDIT_COMMERCIAL_APPROVED=true — only after Reddit's written commercial-use permission.
- REDDIT_APPROVAL_REFERENCE — a non-secret reference/record identifier for that written approval.
- REDDIT_AUTOPUBLISH=on — final kill-switch; leave off until everything above is complete.

Never put credentials in repository files, commit messages, or chat. Use an OAuth app and refresh token with the minimum submit scope; do not use a Reddit password or browser/session cookies. Use the truthful User-Agent format required by Reddit.

## Community registry

Edit reddit/communities.json only after manually checking the live rules for the exact subreddit. Each approved entry must have:
- rules_reviewed: true
- rules_url: official current rules page
- rules_reviewed_at: ISO date when the rules were checked
- autopost_permission_reference: link or record of the moderator's explicit permission for automated submissions
- self_promo_allowed: true
- allow_links: true
- autopost_allowed: true
- optional flair_id if the subreddit requires a flair

If a subreddit does not explicitly permit automated promotional submissions, leave autopost_allowed false. A general permission to post projects is not automatically permission for automated submissions. Never bypass moderator restrictions.

## Test and preview

Offline preview (no Reddit credentials and no network call):

~~~bash
python scripts/reddit_autopublish.py --mode draft --subreddit IndieGaming
~~~

Publish (only after every gate above is satisfied):

~~~bash
python scripts/reddit_autopublish.py --mode publish --subreddit IndieGaming
~~~

The GitHub Actions workflow runs offline tests first. Manual preview creates a draft. The scheduled publisher remains disabled unless the repository variables are deliberately enabled.

## Duplicate and failure safety

- A source X post can be submitted only once.
- The global cooldown is at least seven days; each subreddit has a 30-day cooldown.
- A pending/unknown submission blocks all future writes until manually reconciled.
- The pending state claim is committed to main before the API request. Only then does the workflow submit. Timeouts and ambiguous responses are persisted as unknown and are never retried automatically because the post may already exist.
- The post is built from a verified media-manifest record and includes developer disclosure.
- Reddit-native title/body are generated; X hashtags are not copied.
- No Reddit user identities or voter lists are collected.
