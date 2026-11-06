# Reddit API access and commercial-use approval request

Use this as the basis for Reddit's official API access request. Submit it honestly; approval is not guaranteed.

Official request form: https://support.reddithelp.com/hc/en-us/requests/new

## Subject

API access and written commercial-use approval request — Ringshift developer post scheduler

## Details of inquiry

I am an independent game developer building Ringshift, a small indie puzzle game. I am requesting explicit permission to use Reddit's Data API to submit occasional, developer-disclosed posts from a dedicated Reddit account used only for this app to communities where moderators explicitly permit this kind of promotion.

The proposed tool repurposes my own original X posts and verified gameplay captures into Reddit-native titles and text. It does not scrape Reddit or collect Reddit user data. It will not read user profiles, identify voters, collect comment histories, or target individual users. It will not automate comments, replies, direct messages, chats, votes, follows, or other engagement.

The proposed posting limit is one original post per seven days across Reddit, with at least 30 days between posts to the same subreddit. It will not cross-post identical or substantially similar text to multiple communities. Each post clearly discloses that I am the developer of Ringshift. The tool checks each community's current rules and will remain disabled unless the rules and moderators explicitly permit automated submissions and self-promotion.

Because Ringshift may be monetized, I am also requesting explicit written permission for this commercial promotional use case before enabling any API activity. I will not use the API until Reddit grants the required approvals and I agree to the applicable terms.

## Purpose and benefit to Redditors

The intended posts are occasional puzzle challenges, transparent development updates, and specific feedback requests. The aim is to share original gameplay and invite genuine puzzle/design discussion in communities that allow it, not to manufacture engagement or send unsolicited outreach. If this use case is not eligible, I will keep Reddit posting manual and will not use the API.

## What the app does

- Reads only my own X post log and my own verified gameplay media manifest from my GitHub repository.
- Generates a distinct Reddit-native title and body with clear developer disclosure.
- Uses OAuth with the minimum submit permission needed to publish a self-post, under a registered app profile and a dedicated account used only for the app.
- Submits no more than one post every seven days, and no more than once per 30 days to a given subreddit.
- Stores only the IDs and permalinks of my own submissions for duplicate prevention.
- Makes no requests to Reddit when disabled; no Reddit data discovery, scraping, or user-level collection is implemented.

## Why this is not a Devvit app

This is an external creator-side scheduling tool that publishes my own original content through the dedicated app account. It is not an in-Reddit user-facing app or a moderator tool. If Reddit requires a different supported integration path, I will follow that direction or leave the workflow disabled.

## Planned communities

No community is enabled yet. Candidate communities are IndieGaming, indiegames, puzzlevideogames, WebGames, logicpuzzles, and itchio. I will only add a community after checking its current rules and obtaining any moderator permission required for automated promotional submissions. I will not assume that general self-promotion permission includes automation.

## Source code

https://github.com/ultramegaray0-lgtm/ringshift-social-autopilot

## Requested approval

Please confirm whether Reddit will grant:
1. Explicit Data API access for the OAuth submission use case described above.
2. Written permission for this commercial/promotional use case.
3. Any required app registration, review, scope, rate-limit, or retention conditions.

I am happy to provide further details or adjust the implementation to meet Reddit's requirements.
