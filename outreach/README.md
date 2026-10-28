# Ringshift creator outreach (approval-first, 0 SAR)

Finds YouTube creators, puzzle blogs and curators who might like Ringshift, writes one honest message per prospect,
and lets **you** approve each email individually. Nothing is ever sent by a schedule.

```
 every day 07:23 Riyadh (job "pipeline": NO Gmail secret)          only when YOU start it (job "mail", owner only)
 ┌─────────────────────────────────────────────────────────┐        ┌───────────────────────────────────────────────┐
 │ YouTube API + your feeds/curators → evidence, verified  │        │ deliver: draft id + approval code + recipient │
 │ contacts → score → drafts → summary with full text      │  ───►  │ typed in full → checks → claim → Gmail Drafts │
 │ state: encrypted, branch outreach-state                 │        │ (you press Send) / SMTP / manual              │
 └─────────────────────────────────────────────────────────┘        │ sync-replies: read-only Gmail check           │
                                                                    └───────────────────────────────────────────────┘
```

It is separate from the X autopilot: own workflow (`outreach.yml`), own concurrency group, own state branch
(`outreach-state`, never `main`), own tags (`outreach-claim-*`), own tests (`outreach/tests`, not in `tests/`).
`autopilot.yml`, `scripts/` and `tests/` were not changed.

## One-time setup

| What | Where | Needed for |
|---|---|---|
| `OUTREACH_STATE_KEY` secret: any random 32+ characters (e.g. a password-manager password). **Keep a copy**: without it the saved state cannot be read. | Settings → Secrets and variables → Actions → *Repository secrets* | everything (state is encrypted because this repository is public) |
| `OUTREACH_SENDER_NAME` variable: the name you sign emails with | same page → *Variables* | drafts (none are made without it) |
| `YOUTUBE_API_KEY` secret | Google Cloud console → new project → enable **YouTube Data API v3** → Credentials → API key (no billing needed). Restrict it to that API. | YouTube discovery (without it only `sources.json` is used) |
| Gemini | nothing: reuses `GEMINI_API_KEY`. Optional separate `OUTREACH_GEMINI_API_KEY` keeps quotas apart. | the optional personal sentence |
| Environment `outreach-send` with secrets `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` | Settings → Environments → New environment `outreach-send` → *Environment secrets*. Recommended: tick *Required reviewers* = you, and *Deployment branches* = `main` only. | `deliver` with gmail-draft/smtp, and `sync-replies`. Not needed for `manual`. |

**Gmail app password:** Google Account → Security → turn on 2-Step Verification → search "App passwords" → create one
named "Ringshift outreach" → paste the 16 characters into `GMAIL_APP_PASSWORD`. Revoke it there any time. If your
account can't create app passwords (some work/school accounts), skip it and use `manual` delivery.

Then merge the branch, and run *Actions → Ringshift creator outreach → Run workflow → mode `daily`* once.

## What you see every day

The run's **Summary** page shows: new / qualified prospects, drafts waiting for you, sends, replies, positive
interest, errors and rejected candidates (with reasons). For each draft: draft id, **approval code**, the masked
recipient and the public page where the address is published, the evidence (video/article link, date), why it scored
well, warnings, and the **full subject and body**. Addresses are masked (`pa***@gm***.com`) because Actions logs of a
public repository are public; open the "published at" link to see the full address on the creator's own page.

## How to approve your first email (from your phone)

1. GitHub app → this repo → **Actions** → *Ringshift creator outreach* → the newest run → **Summary**.
2. Pick a draft. Read the body. Open the evidence link and the "published at" page; check the address there.
3. **Run workflow** (branch `main`) with:
   * mode `deliver`, `draft_id` (like `od-1a2b3c4d`), `approval_code` (12 characters),
   * `recipient` = the address **typed in full** (it must match the draft exactly),
   * `delivery` = `gmail-draft` (recommended).
4. If you set *Required reviewers*, approve the `outreach-send` deployment when GitHub asks.
5. Open Gmail → **Drafts**. The exact message is there. Read it once more and press **Send** (or delete it and run
   mode `mark`, action `not-sent`, with the draft id).
6. Later, run mode `sync-replies`: it finds the email in Sent Mail and records **SENT** (and any replies).

Other delivery modes: `smtp` sends immediately after the same checks (SENT is recorded only when Gmail's server
accepts it); `manual` needs no Gmail access: copy the text from the summary into Gmail yourself, then run `mark` with
action `sent`.

Not happy with a draft? Run `mark` with action `reject` and its draft id: that prospect is not drafted again. Or do
nothing: drafts expire after 14 days.

## Modes

| Mode | Job | Gmail | Effect |
|---|---|---|---|
| *(schedule)* / `daily` | pipeline | none | discover, score, draft (max 5 new/day), queue follow-ups, summary |
| `review` | pipeline | none | show the queue again |
| `deliver` | mail | gmail-draft/smtp only | deliver ONE approved draft (max 5/day) |
| `sync-replies` | mail | read-only | SENT confirmations, replies, bounces, opt-outs |
| `mark` | pipeline | none | `sent`, `not-sent`, `reject`, `replied`, `interested`, `declined`, `bounced`, `opt-out` |
| `suppress` | pipeline | none | never contact this address (`recipient`) or site/channel (`source_url`) |
| `add-contact` | pipeline | none | add a business address you found yourself (e.g. YouTube's About page) with the page it's on |

## Statuses

Prospects: DISCOVERED → QUALIFIED → DRAFTED (or NEEDS_REVIEW when a draft has a warning or is a form) → APPROVED →
SENT → REPLIED / DECLINED. SUPPRESSED and BOUNCED are final. Drafts: AWAITING_APPROVAL, APPROVED, SENT, SEND_FAILED
(definitely not sent; a fresh draft is made later), SEND_UNKNOWN (connection broke mid-send: check Gmail and `mark`
it), SUPERSEDED, REJECTED.

## Guarantees (each covered by tests in `outreach/tests`)

* Discovery and drafting cannot send: they don't import `smtplib`/`imaplib`, and the scheduled job has no Gmail secret.
* Only a manual run by the repository owner on `main`, with the matching approval code and the recipient typed in
  full, can deliver; any edit to a draft changes its code. A draft is claimed (git tag `outreach-claim-<id>`) before
  delivery, so it can leave at most once, even if the state push fails.
* An address that was sent to (or whose send is unclear), suppressed, bounced or declined is refused. Max 5 per day.
  At most one follow-up, only after 14 days with no reply, and only after you approve it like any other draft.
* No guessed addresses: an address is used only if it is written out on the page credited as its source, checked in
  the same run (YouTube channel description via the API, or the page in `sources.json`), or you added it yourself.
  robots.txt is respected. Obfuscated addresses ("name [at] site") are not decoded.
* Every message is checked: names Ringshift in the subject, no "Re:", references the real video/article by its exact
  title, never says you watched/read/loved anything, no keys/payment/exclusives/early access, no invented numbers,
  awards or release plans, only the verified demo link, an opt-out line. A draft that fails can never be approved.
* The demo link is checked (HTTP 200, page title contains "RINGSHIFT") on every run and again before delivery.
* Gemini (free tier, whose terms allow human review and ask not to send personal data) only ever receives a public
  video/article title and description excerpt, never a name, address or link; its one sentence is checked and
  replaced by a fixed sentence when it fails or the quota is used up.
* Replies are read with read-only IMAP; nothing is ever sent automatically in response. Reply text is not stored.
  A reply containing "no thanks", "unsubscribe", "not interested" and similar suppresses the address.

## Free-tier limits

* YouTube Data API: 10,000 units a day by default; a search costs 100 units. A daily run uses the 9 queries in
  `config.json` (about 900 units) + 1 unit per 50 channels, and stops at `youtube.daily_quota_budget` (2,500). The API
  does **not** expose creators' business emails. Discovery therefore also reads the free public RSS feed of up to
  `youtube.feed_checks_per_run` (20) of the most relevant, smallest channels (no API quota): it records how often they
  upload, and uses an address only when the creator labels it as their business contact ("Business inquiries: …") in
  at least `youtube.business_email_min_videos` (2) of their own latest video descriptions. Addresses introduced as a
  developer's, publisher's or support desk's are ignored. Other channels stay DISCOVERED until you add an address you
  found yourself (`add-contact`).
* Gemini free tier: rate-limited; on 429 the run uses the fixed sentence.
* Gmail has its own daily sending limits for personal accounts; this tool allows 5 deliveries a day.
* Blog/curator/directory discovery has no free search API (Google's Custom Search API is closed to new users and ends
  2027-01-01), so those come from `sources.json`, which you curate. See [SOURCES.md](SOURCES.md) for what was
  checked and why each source was added, held or turned down.
* GitHub Actions: free for public repositories.

## sources.json

Every entry carries the record of what a person checked, and discovery skips (and reports) any entry whose record is
incomplete:

```json
{"curators": [{"name": "A Curator", "category": "curator", "source_type": "puzzle-curator",
               "site_url": "https://...", "submission_url": "https://...",
               "must_contain": "a phrase that proves submissions are open", "route_kind": "form",
               "aliases": ["https://www.youtube.com/@theirchannel"],
               "enabled": true, "last_checked": "2026-10-28",
               "fit": "why Ringshift fits this source",
               "eligibility": {"status": "likely",
                               "verified": ["requirements read on their own page"],
                               "uncertain": ["what could not be confirmed"]}}],
 "youtube_channels": [{"name": "A Channel", "channel_id": "UC...", "contact_email": "...",
                       "contact_source_url": "https://www.youtube.com/watch?v=...", "...": "same record fields"}],
 "feeds": [{"name": "A Blog", "site_url": "https://...", "feed_url": "https://.../feed", "...": "same record fields"}],
 "excluded": [{"name": "A Site", "url": "https://...", "last_checked": "2026-10-28", "reason": "why not"}]}
```

* `category`: `curator`, `newsletter`, `community`, `blog` or `directory` (a browser-game site that publishes
  playable web games). `source_type`: `puzzle-curator`, `indie-curator`, `browser-game-directory`,
  `youtube-creator`, `blog`, `newsletter`.
* `route_kind`: `form` (paste the draft into their form), `upload` (create the listing on their developer portal) or
  `email` (an address written on `contact_source_url`; never falls back to a form if the address disappears).
* `eligibility.status`: `meets` (every stated requirement is met), `likely` (route verified, nothing known to fail,
  something unconfirmed), `uncertain` (a requirement may not be met). Only `meets`/`likely` may be `enabled`;
  `uncertain` entries are listed under *Sources on hold* in the run summary and never drafted. Anything in
  `uncertain` is repeated as a warning on the draft, so the draft waits in NEEDS_REVIEW until you confirm it.
* `aliases`: the same organisation's other sites or channels. A YouTube search result that matches an alias merges
  into your entry instead of becoming a second prospect, so one organisation is never contacted twice.
* `contact_email` is only used if it is written on `contact_source_url` when the run checks it. For a YouTube channel
  it must be labelled as a business contact in one of the channel's latest video descriptions (read from its RSS
  feed; YouTube pages are not scraped).
* `excluded`: sources looked at and turned down, with the reason, so they are not re-added by mistake. An entry
  cannot be both excluded and a target.
* Records whose `last_checked` is over 120 days old are listed in the summary so you can re-check their pages.
* `python -m unittest discover -s outreach/tests -t .` checks the committed file (complete records, nothing
  ineligible enabled, no duplicate organisations, Games Y'all and Playlin never targeted).

## Local use

```
python -m unittest discover -s outreach/tests -t .      # 100+ offline tests
OUTREACH_SENDER_NAME="Your Name" YOUTUBE_API_KEY=... python -m outreach --state-dir .outreach-state daily
```
Locally, state is plain `state.json` unless `OUTREACH_STATE_KEY` is set (`.outreach-state/` is git-ignored).

The older `scripts/outreach.py` (manual CSV tracker) still works and is untouched; this module replaces it for new work.
