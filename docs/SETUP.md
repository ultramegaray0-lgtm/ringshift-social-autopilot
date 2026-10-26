# Setup

## Secrets (GitHub → Settings → Secrets and variables → Actions → New repository secret)
| Name | What | Where from |
|---|---|---|
| `GEMINI_API_KEY` | Gemini API key | Google AI Studio (free tier) |
| `BUFFER_API_KEY` | Buffer API key | Buffer → Settings → API |
| `YOUTUBE_API_KEY` | *optional*, outreach discovery only; run locally, not in Actions | Google Cloud, YouTube Data API v3 |

Store only the key itself (no `Bearer `, no line breaks). The scripts reject malformed values without printing them.
Never paste keys into chats, issues, or commits.

## Workflow modes (Actions → Ringshift AI Autopilot → Run workflow)
| Mode | Needs | Effect |
|---|---|---|
| `test` | nothing | Picks media for two sample topics. No post. |
| `preview` | `GEMINI_API_KEY` | Writes a post for real media and shows it in the run summary. Saves and sends nothing. |
| `live` | both keys | Queues **one real post** in Buffer, then commits counters/logs. |
| *(schedule)* | both keys | Same as `live`, daily 06:00 UTC. Disable the schedule in the Actions tab if you want manual only. |

## Safety around live posts
* **At most one post per run**, and runs never overlap (single concurrency group, never cancelled mid-run).
* Before saving or sending anything, a live run asks Buffer for its posts and **refuses if one is already waiting**
  (no sent time, due in the future), and refuses if this tool queued a post less than 12 h ago. If Buffer can't be checked it
  refuses too. The manual input `allow_extra_post` (default off) overrides this on purpose.
* If anything fails after Buffer accepted the post (e.g. the push back to `main`), the state files are saved as the
  `ringshift-state` run artifact, and the Buffer check above stops the next run from posting again.
* `live` only works when dispatched on `main`; on another branch it stops before any key is used.
* State files are written atomically (log first, then counter and media history); a corrupt `post_log.json` stops the run before posting.
* `preview` writes the full draft (text, media file + URL, target Buffer channel, exact `assets` payload) as the downloadable
  artifact `ringshift-preview-draft`, and as a "notice" annotation on the job.

## Local run
```
pip install -r requirements.txt
export GEMINI_API_KEY=...        # PowerShell: $env:GEMINI_API_KEY="..."
python scripts/autopilot.py --dry-run
```

## First live verification (recommended, needs your approval)
1. Merge the PR. Media on `main` becomes reachable at `raw.githubusercontent.com` only after the merge.
2. Run `preview` and read the draft and media description.
3. Run `live` once. Check Buffer shows the post **with media**, and X shows it after Buffer publishes it.
4. Only then rely on the schedule.

## Optional files
* `data/project.json`: `{"project_url": "https://your-game-page"}` enables the invitation post type.
* `data/followers.csv`: maintained by `python scripts/report.py followers N`.
