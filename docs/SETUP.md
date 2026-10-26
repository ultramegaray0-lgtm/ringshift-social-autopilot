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
| `publish-approved` | Buffer key | Queues **one real post**: exactly the draft whose ID you enter as `approval_code`. See [APPROVING_A_POST.md](APPROVING_A_POST.md). |
| `rehearse-approval` | GitHub token only | Re-verifies a draft and exercises the claim mechanism on a throwaway tag. Publishes and claims nothing. |
| *(schedule)* | Gemini key (+ read-only analytics) | Writes one draft per day (06:00 UTC) and saves it as an artifact. **Never publishes.** Switch it off/on: see [APPROVING_A_POST.md](APPROVING_A_POST.md). |

## Safety around publishing
* **At most one post per run**, and runs never overlap (single concurrency group, never cancelled mid-run).
* Before saving or sending anything, a publishing run asks Buffer for its posts and **refuses if one is already waiting**
  (no sent time, due in the future), and refuses if this tool queued a post less than 12 h ago. If Buffer can't be checked it
  refuses too. The manual input `allow_extra_post` (default off) overrides this on purpose.
* If anything fails after Buffer accepted the post (e.g. the push back to `main`), the state files are saved as the
  `ringshift-state` run artifact, and the Buffer check above stops the next run from posting again.
* `publish-approved` only works when dispatched on `main`; on another branch it stops before any key is used.
* A draft can be used **once**: its ID is claimed (a `ringshift-used-<id>` git tag) before Buffer is called, and is never released automatically. Drafts expire after 48 hours, and any edit to text, media or channel invalidates them.
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
2. Run `preview`, read the draft and its media (all on your phone: see APPROVING_A_POST.md).
3. Run `publish-approved` once with that draft's ID. Check Buffer shows the post **with media**, and X shows it after Buffer publishes it.
4. The schedule never posts, so nothing changes there: each day you choose whether to approve that day's draft.

## Optional files
* `data/project.json`: `{"project_url": "https://your-game-page"}` enables the invitation post type.
* `data/followers.csv`: maintained by `python scripts/report.py followers N`.
