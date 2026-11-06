# Reddit growth: X-to-Reddit repurposing and community discovery

Status: design foundation; no Reddit API access, scraping, posting, comments, votes, follows, or messages are enabled.

## Goals

1. Turn Ringshift's existing X posts and verified media into Reddit-native draft submissions.
2. Find relevant communities and high-performing discussion opportunities (including high-score / low-comment posts) without pretending that upvoters are identifiable.
3. Keep every Reddit submission and interaction under the developer's manual approval.

## Hard platform constraints

- Reddit does not expose the usernames of people who upvoted a post. Never claim to identify voters or infer a voter list.
- Reddit's Responsible Builder Policy (updated 2026-10-05) requires explicit approval before accessing Reddit data through the API. Commercial use requires explicit written approval.
- Do not scrape Reddit pages, use unofficial APIs, evade OAuth/access controls, or use browser automation to collect Reddit data.
- Do not automate unsolicited DMs, chats, comments, votes, follows, or submissions. No auto-posting.
- Do not use Reddit data for ads targeting, spam, sensitive-trait inference, cross-platform identity matching, or model training.
- Respect each community's rules, self-promotion limits, flair, title conventions, and required disclosure. If unclear, hold the draft for manual review.
- If API access is later approved, store only fields required by the approved use case, obey rate limits and deletion requirements, and remove deleted content/user data promptly (Reddit recommends routine deletion within 48 hours).

Primary policy references (review again before enabling any integration):
- https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy
- https://support.reddithelp.com/hc/en-us/articles/14945211791892-Developer-Platform-Accessing-Reddit-Data
- https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki
- https://redditinc.com/policies/data-api-terms

## Phase 1 — X-to-Reddit draft converter (safe foundation)

Input:
- Existing X post records from the autopilot's own post log / approved drafts.
- The corresponding verified media manifest record (game commit, level, theme, mechanisms, SHA-256, public media URL).
- Existing verified Ringshift facts and vocabulary; no invented development history, gameplay claims, metrics, awards, or player reactions.

Output: a separate Reddit draft artifact, never an X post copied verbatim.

Draft styles:
- Developer story: first-person context, what was built, a genuine discussion question.
- Puzzle challenge: invite people to reason about a board without demanding votes or engagement.
- Mechanism discussion: explain one verified mechanism shown in the media and ask a design question.
- Feedback request: ask for specific, answerable feedback and disclose that the developer made the game.
- Showcase: only for communities whose rules permit self-promotion; use the required flair and disclosure.

Reddit-native adaptations:
- No X hashtag block; use a natural title and body.
- No engagement bait, cross-post spam, repetitive daily promotion, or generic 'check out my game' text.
- Never assume that a community allows promotional posts. Maintain a reviewed community rules record and per-community cooldown.
- Use the right media aspect/format, add alt text where supported, and include the demo link only where rules allow.
- Generate multiple candidate community variants from one source post, but require a human to choose one and submit it manually.

## Phase 2 — Community and discussion opportunity discovery

Until Reddit grants the necessary API access and written permission for this promotional use case:
- Use a manually curated list of relevant subreddits and rules pages.
- Let the owner paste a Reddit post URL and its visible score/comment count into a local review command or form.
- Calculate an opportunity score from the supplied aggregate values only (e.g. high score relative to comments, topic fit, recency supplied by the owner); clearly label it as user-supplied and unverified.
- Produce community-level insights and draft ideas, not lists of ordinary users to target.

If Reddit approves an API use case in writing, first document the approved scope, endpoints, retention, and permitted purpose. Reassess whether author-level data is permitted; default remains no user-level prospect harvesting, no voter identification, and no unsolicited outreach. API access is not permission to market to individual users.

## Phase 3 — Approval-first workflow

Proposed workflow: reddit-growth.yml, separate from autopilot.yml and outreach.yml.
- Scheduled/manual run may create drafts and community opportunity reports only.
- No Reddit write scopes or posting secrets in the discovery job.
- Manual run can validate a selected draft and print a submission checklist; the owner submits in Reddit.
- Keep state separate from X and creator outreach state.
- Add offline tests for policy gates, community rules, duplicate detection, disclosure, link restrictions, cooldowns, and exact media provenance.
- Kill switch disables the workflow; no hidden or fallback posting path.

## Proposed draft record

id, source_x_post_id, source_post_hash, media_sha256, media_url, game_commit, level, theme, mechanisms, target_subreddit, rules_checked_at, title, body, disclosure, status, created_at, expires_at.

Do not store Reddit usernames, user profile histories, or voter identities in this phase.

## Acceptance criteria

- Existing X autopilot behavior, schedules, secrets, state, and tests remain unchanged.
- Each Reddit draft traces back to an exact X post and verified media manifest.
- Every factual game statement is checked against the existing knowledge/facts source.
- Drafts are community-specific and include developer disclosure.
- Unknown subreddit rules, unverified media, missing provenance, duplicates, or policy uncertainty cause a hold, not a publish.
- No code path can post, comment, vote, follow, or send a Reddit message.
- Reddit API discovery remains disabled until explicit written approval for this use case is recorded.

## First implementation slice

Implement the local converter and curated community rules file first. Then add opportunity scoring for owner-supplied public post URLs and aggregate metrics. Keep API-based discovery behind an explicit approval gate and do not collect individual user profiles.
