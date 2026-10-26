# Audit of the repository as received

What was inspected: all of `scripts/`, `tests/`, `.github/workflows/autopilot.yml`, `data/`, `content/`, `media/`
(including `media_tags.json`), `RINGSHIFT_KNOWLEDGE.md`, and the Buffer/GitHub behaviour the code depends on.
The game source (`ringshift v0.26.html`) came from the attached Project, not the repo.

## Already good (kept)
* Buffer `createPost` integration with image/video assets, media-confirmation check, fail-closed state handling.
* Fail-fast env validation (`env_check.py`) that never prints secrets.
* 41 offline tests with a network-blocking base class.
* A Gemini candidate + critic loop with fact rules.

## Problems found
| # | Finding | Fixed by |
|---|---|---|
| 1 | Text was written **before** media was chosen, so posts could describe things the clip doesn't show. | Media-first generation; verified descriptions in the prompt and critic. |
| 2 | Media library: 4 clips unfit (38 MB/81 s, 32 MB/88 s, 17 MB, one static 1.6 s). Few had tags, none had descriptions. | Validator, excludes, 37 hand-verified catalog entries. |
| 3 | The repo already holds ~190 MB of media; every new video on `main` is permanent. | `media_store.py` (squashed branch), size caps, captured clips are small (~1 MB). |
| 4 | Analytics fetched only Buffer's first page (10 posts). | Cursor pagination with loop guards and a safe fallback. |
| 5 | No rule screening: nothing stopped invented claims, @mentions, links, or near-duplicates (only exact duplicates). | `post_rules.py`, near-duplicate threshold, tests calibrated on the 27 past posts. |
| 6 | One past post contained stacked "glitch" combining characters and was echoed into prompts. | Prompt context is cleaned. |
| 7 | Analytics file had no structured form, so no real reporting. | `data/metrics.json`, `report.py`. |
| 8 | No retry for Buffer rate limits (free plan: 100 requests/15 min, 250/24 h). | 429-only retry honouring `Retry-After`. |
| 9 | A media URL that 404s (file not pushed) would still create a post attempt. | Pre-flight HEAD check. |
| 10 | Workflow: tests and publish in one job, no timeouts, no preview mode, no PR CI. | Split jobs, timeouts, `preview`, read-only `ci.yml`. |
| 11 | Special game modes appear in existing clips but `RINGSHIFT_KNOWLEDGE.md` doesn't describe them. | Clips flagged `special_mode`, excluded from fact-based prompts; see KNOWLEDGE_PROPOSALS. |

## Not changed
* `RINGSHIFT_KNOWLEDGE.md` (your voice and facts): proposals only.
* The Buffer request shape, channel/organisation IDs, secret names, `main` branch behaviour of the daily commit.
