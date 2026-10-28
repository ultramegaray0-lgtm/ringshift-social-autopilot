# Creator outreach: audit and implementation plan

Written 2026-10-28 on branch `feature/creator-outreach`, before any outreach code was changed.

## 1. Audit findings

**Repository.** Default branch `main` (head `d1b867c`). Other long-lived branches: `media-store` (hosted media),
`tiktok-state` (TikTok state), `game-lab-previews`. Python 3.12 in Actions; dependencies `google-genai` + `requests`.

**Workflows.**

| Workflow | Trigger | Secrets | Writes |
|---|---|---|---|
| `autopilot.yml` (X posting) | daily 06:00 + 13:37 UTC, manual | `GEMINI_API_KEY`, `BUFFER_API_KEY` | commits to `main`, `media-store`, tags `ringshift-day-*` / `ringshift-used-*` |
| `ci.yml` | PRs and non-main pushes | none | nothing |
| `game-lab.yml` | weekly | `GEMINI_API_KEY`, `RINGSHIFT_GAME_TOKEN` | PRs on the game repo |
| `tiktok-video.yml` | manual / on clip | none | `tiktok-state` branch |

Important coupling: `autopilot.yml`'s gate job runs `python -m unittest discover -s tests` with only `requirements.txt`.
Anything added under `tests/` runs inside the X posting gate, so a broken outreach test would stop X posting.

**Tests.** 703 offline tests, all passing on `main` (6 skipped: real-browser tests). `tests/support.py` blocks every
network call.

**Knowledge.** `RINGSHIFT_KNOWLEDGE.md` lists the mechanics (Mirrors, Portals, Amplifiers, Blocks, Half-Blocks, Prisms,
Echo, Locks, Source beams) and forbids inventing features, numbers, reviews, awards, release dates.

**Demo URL.** `data/project.json` has `project_url: null`. The game repository's README documents the live demo as
`https://ultramegaray0-lgtm.github.io/ringshift-game/` (published from `main` by `pages.yml`; current build
`ringshift v0.34.html`, page title "RINGSHIFT — Demo"). This session could not reach github.io, so the URL is
**documented, not yet verified live**: the pipeline checks it (HTTP 200 and the page title contains "RINGSHIFT") on every
run and refuses to make a draft approvable while that check fails.

**Existing outreach.** `scripts/outreach.py` is a manual CSV tracker with fixed templates and `[BRACKET]` placeholders; it
never sends. It stays untouched (its 15 tests keep passing). The new module does not import it.

**Gemini.** The X autopilot uses `google-genai` with `gemini-3.5-flash-lite` then `gemini-2.5-flash-lite`, free tier.
Google's Gemini API terms for unpaid services allow human review of inputs/outputs and say not to submit personal
information. So: outreach may use Gemini only for one optional "why this fits your audience" sentence, fed the public
video/article title and a short public description excerpt, never a name or email address; its output is checked
against strict rules and a fixed template is used whenever Gemini fails, is rate-limited or writes something disallowed.

**Discovery sources that work for free.**

| Source | Cost / access | Gives | Does NOT give |
|---|---|---|---|
| YouTube Data API v3 (`search.list`, `channels.list`) | free key, no billing; default 100 searches/day + 10,000 units/day | recent videos matching a query, channel stats, the channel's public description | business email (the About-page "View email" is behind a CAPTCHA and not in the API) |
| YouTube channel RSS (`feeds/videos.xml?channel_id=`) | free, no key | latest uploads of a known channel | contact info |
| RSS/Atom feeds of blogs you list | free | dated articles | contact info |
| Public submission/contact pages you list | free | an official route, re-checked each run | anything private |
| Google Custom Search JSON API | closed to new customers, shuts down 2027-01-01 | — | not usable |
| Brave Search API | needs a credit card even for free credits | — | not used (0 SAR budget) |

Consequence: a YouTube prospect gets an email draft only when the creator published an address in the channel's
public description, or you add one yourself (`add-contact`, recorded as owner-entered with its source URL). Blogs and
curators come from a seed file you control; the pipeline fetches their pages and only keeps a contact route it can see.

**Gmail.** Free options: Gmail API OAuth (an unverified "Testing" OAuth app gets refresh tokens that expire after
7 days for any Gmail scope, so it needs re-authorising weekly) or an **app password** (needs 2-Step Verification) with IMAP/SMTP from Python's standard library.
Plan: app password, kept as secrets of a GitHub *environment* `outreach-send` that only the manual delivery job uses.
If you never add it, the manual fallback works with no Gmail access at all.

## 2. Design

```
 schedule (daily)            manual: approve                       manual: sync-replies
 ┌─────────────────────┐     ┌────────────────────────────────┐    ┌──────────────────────────┐
 │ discover → score →  │     │ verify draft id + approval code│    │ IMAP read-only: Sent,    │
 │ draft → report      │     │ + typed recipient → checks →   │    │ replies, bounces, opt-out│
 │ NO Gmail secret     │     │ claim tag → deliver            │    │ never replies            │
 │ NO send code        │     │ (gmail-draft | smtp | manual)  │    └──────────────────────────┘
 └─────────────────────┘     └────────────────────────────────┘
        state: encrypted JSON on branch `outreach-state` (never main)
```

* **Separate everything.** New package `outreach/` (own tests in `outreach/tests/`, run in their own process, so they
  can't clash with `scripts/outreach.py` or block the X gate), new workflow `outreach.yml`, own concurrency group, own
  state branch, own secrets. `autopilot.yml`, `scripts/` and `tests/` are not modified. `ci.yml` gets one extra job.
* **State** lives on the `outreach-state` branch as `state.json.enc`, AES-256 (openssl, PBKDF2) with secret
  `OUTREACH_STATE_KEY`, because this repository is public and a plain list of creators' addresses would be harvestable.
  Logs and run summaries mask addresses (`ab***@domain.com`).
* **Prospect record:** id (hash of canonical URL), name, category, canonical URL, evidence (URL, title, date, excerpt,
  source), contact (email or submission URL, source URL, source kind, excerpt proving it, verified-at), score + reasons,
  first-discovered, status, last-contact, interest, history.
* **Statuses:** prospects DISCOVERED → QUALIFIED → DRAFTED / NEEDS_REVIEW → APPROVED → SENT → REPLIED / DECLINED;
  SUPPRESSED and BOUNCED are terminal. Drafts: NEEDS_REVIEW, APPROVED, SENT, SEND_FAILED, SEND_UNKNOWN, SUPERSEDED.
* **Dedup:** canonical URL (YouTube → channel id) and normalized address; suppressed URLs/addresses are never re-added.
* **Drafts:** fixed honest structure, specific evidence line ("I came across your video "X" (date) while looking for
  channels that cover puzzle games" — never "I watched/loved"), accurate game description from the knowledge file,
  verified demo URL, "no reply needed" line, opt-out line. A checker rejects promises (keys, payment, exclusive, early
  access…), invented numbers, unknown mechanics, other URLs, "Re:"/"Fwd:" subjects. Max new drafts per day (default 5).
* **Approval:** each draft has `draft_id` and an approval code = first 12 hex of SHA-256 over recipient + subject + body.
  Delivery needs a manual workflow run with draft id, approval code and the recipient typed in full; any mismatch or
  edit refuses. Only `workflow_dispatch` by the repository owner; the job runs in environment `outreach-send`.
* **Idempotency:** before delivering, a git tag `outreach-claim-<draft_id>` is created (fails if it exists), plus a
  ledger in state; an address already SENT/SEND_UNKNOWN, suppressed, bounced or declined is refused. Max sends per day.
* **Delivery modes:** `gmail-draft` (default: the exact message is placed in your Gmail Drafts; you press Send; the
  next sync sees it in Sent Mail → SENT), `smtp` (sends now; SENT only after Gmail's SMTP server accepts it; ambiguous
  failures → SEND_UNKNOWN, never retried automatically), `manual` (no Gmail access; you send it and then run
  `mark … sent`, recorded as owner-confirmed).
* **Follow-ups:** after 14 days without a reply, one follow-up draft is queued as NEEDS_REVIEW; it goes through the same
  approval. Never more than one.
* **Replies:** `sync-replies` (manual) reads Gmail over IMAP, read-only: SENT confirmation, REPLIED, BOUNCED, and
  opt-out phrases → SUPPRESSED. Reply contents are not stored. Positive interest is only set by you (`mark … interested`).
* **Report:** each run writes a summary (new, qualified, awaiting approval with full text, sent, replies, interest,
  errors, rejected candidates with reasons) to the job summary and into state.

## 3. Build order

1. Core: schema, validation, canonicalization, store (encrypted), config + facts.
2. Discovery: YouTube API, channel RSS, feeds, seed pages; scoring; robots.txt respected; errors classified.
3. Drafting + checker + optional Gemini sentence; follow-up queue.
4. Approval codes, claims, Gmail delivery (IMAP draft / SMTP / manual), sync, report.
5. CLI + workflow + CI job + docs.
6. Tests (all mocked), full existing suite, workflow YAML validation, push the branch. No merge to `main`.
