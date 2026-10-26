# Drafts, approval and publishing (from your phone)

**Nothing in this repository publishes by itself.**

* The **daily schedule (06:00 UTC) only writes a draft** and saves it. It has no Buffer key in the step that
  generates, so it cannot post, and no code path in `autopilot.py` can post except `--publish-approved`.
* A post goes out only when **you** start a `publish-approved` run on `main` and type the **draft ID** of one
  specific draft that is **unexpired** and **unused**. The post is exactly that draft: same text, same media, same
  Buffer channel. It cannot be regenerated, edited, reused or used late.

Modes of the *Ringshift AI Autopilot* workflow (Actions -> Run workflow):

| Mode | Uses | Can it post? |
|---|---|---|
| *(daily schedule)* | Gemini key (+ read-only Buffer analytics) | **No.** Writes a draft artifact. |
| `test` | nothing | No. Picks media only. |
| `preview` | Gemini key | No. Writes a draft artifact (same as the schedule, on demand). |
| `rehearse-approval` | GitHub token only | **No.** Re-checks a draft and exercises the claim mechanism on a throwaway tag. |
| `publish-approved` | Buffer key | **Yes, one post**, only with a valid draft ID, only on `main`. |

## 1. Inspect today's draft on your phone
Each day at 06:00 UTC (09:00 Riyadh) the schedule creates a draft. Drafts expire after **48 hours**. You can also
make one any time with mode `preview`.

1. GitHub app or github.com in your phone's browser -> your repo -> **Actions** -> **Ringshift AI Autopilot**.
2. Open the newest run (event *schedule*, or *workflow_dispatch* for a manual preview).
3. On the run page, under **Annotations**, find the notice **"Ringshift preview (not sent)"**. It shows the
   **draft ID**, the exact **post text**, the **media file, type and URL**, the **channel ID** and the **expiry**.
   (This is the part to rely on from a phone.)
4. The run **Summary** shows the same draft in more detail (critic score, media check, target). Open the media
   URL to watch the clip or see the image before you approve.
5. The exact file is also a downloadable **artifact** at the bottom of the run page, named
   `ringshift-draft-<draft ID>` (small JSON). The approval checks run against this file.
6. If no draft was produced, the artifact is `ringshift-preview-report` and explains why every candidate was
   rejected. Nothing can be published from it.

Not happy with it? Do nothing. It expires and nothing is ever sent. Run `preview` again for another.

## 2. Optional: rehearse the approval (publishes nothing)
Run the workflow on any branch with **mode = rehearse-approval** and **approval_code** = the draft ID. It downloads
the draft from the run that made it, re-verifies it exactly as a publish run would (hash, code, expiry, run,
channel, media file and URL, post rules, unused), and exercises the claim mechanism on a *throwaway* tag
`ringshift-rehearsal-...` (create, repeat -> refused, delete). It has no Buffer or Gemini key and claims nothing for
your draft. Green means "a publish would have passed these checks right now"; it is not a post.

## 3. Approve and publish exactly one post
1. **Actions** -> **Ringshift AI Autopilot** -> **Run workflow**.
2. Branch: **main** (other branches are refused before any key is used).
3. **mode = publish-approved**.
4. **approval_code** = the draft ID (copy it from the annotation; long-press to paste).
5. Leave **allow_extra_post** OFF.
6. Tap **Run workflow**.

The run, in order: checks the code's format; finds the one unexpired draft artifact with that ID and downloads it;
re-verifies it; checks Buffer has no waiting post and nothing was queued in the last 12 hours; **claims the draft ID**
(a git tag `ringshift-used-<id>`); and only then sends it to Buffer. It succeeds only if Buffer confirms the media is
attached. The log ends with `APPROVED DRAFT d-... QUEUED WITH MEDIA CONFIRMED`.

## 4. Check the result
* Buffer app -> the channel's queue: the post is there with its video/image attached.
* After Buffer's next slot, confirm it appears on X.
* `data/post_log.json` (committed by the run) records the draft ID, the approver and the Buffer post ID.

## Turning the daily drafts off and on
Daily drafts cost nothing and cannot post, but you may not want them. Two ways:

**A. Disable the whole workflow** (stops the schedule *and* manual runs)
* Off: **Actions** -> **Ringshift AI Autopilot** -> **...** (top right of the run list) -> **Disable workflow**.
* On: same place -> **Enable workflow**. You must enable it again before you can run `preview` or `publish-approved`.
* From a computer: `gh workflow disable "Ringshift AI Autopilot"` / `gh workflow enable "Ringshift AI Autopilot"`.
* Some mobile app versions lack the **...** menu; use the GitHub website in your phone browser ("Desktop site" if
  needed).

**B. Remove only the schedule** (manual modes keep working)
* Off: on GitHub, open `.github/workflows/autopilot.yml` on `main`, edit it (pencil icon), delete the two lines
  `schedule:` and `- cron: "0 6 * * *"`, commit. No daily run will fire.
* On: put those two lines back under `on:` and commit.

Also know: GitHub automatically disables scheduled workflows in a repository with no activity for 60 days. If daily
drafts stop appearing, open the Actions tab; GitHub shows a banner with an **Enable workflow** button.

## What stops a mistake
| Situation | What happens |
|---|---|
| Daily schedule runs | Writes a draft only. No Buffer call, no state change except read-only analytics files. |
| Wrong or mistyped code | Rejected before anything is claimed. |
| Draft file edited in any way (text, media, channel, expiry) | Rejected (`tampered`). |
| Older than 48 hours | Rejected (`expired`). Make a new preview. |
| Draft from a different run than the one found by the code | Rejected (`wrong_run`). |
| Media file changed or missing since the draft | Rejected (`media_changed` / `media_missing`). |
| Text no longer passes the post rules | Rejected. Approval does not override the rules. |
| Buffer already has a waiting post / one queued <12 h ago | Rejected, nothing is used up; try again later. |
| You run it a second time, or GitHub re-runs a failed job, or two runs race | Rejected (`already_used`). Exactly one post can come from a draft. |
| The run fails **after** the claim (e.g. Buffer error) | The draft stays used up, because the result in Buffer is unknown. **Check Buffer first.** If no post exists, make a new draft and approve that. |

If you are certain Buffer has no post and want to retry the *same* draft, delete the git tag
`ringshift-used-<draft ID>` (repo -> Tags). Do this only after checking Buffer.

## Housekeeping
* Each used draft leaves one small tag `ringshift-used-<id>`; keep them (they are the record of use). Rehearsals
  create and delete their own `ringshift-rehearsal-*` tags and leave nothing behind.
* Draft artifacts are kept 14 days.
* Unapproved drafts are not recorded anywhere, so a later day's draft may resemble an earlier unapproved one.
  Rotation of post type and media advances only when a post is actually published.

## Strongly recommended: protect the Buffer key
The code cannot publish without an approved draft, but the **repository secret** can still be read by any workflow
file in the repo. Old branches may contain old workflow files that still post (the branch `fix/loud-failures-env-checks-tests`
does, and so does `feature/media-factory-and-hardening`, which was merged). **Delete old branches**, and move
`BUFFER_API_KEY` into a GitHub **Environment** (Settings -> Environments) limited to the `main` branch, with
yourself as a required reviewer, then add `environment:` to the publish job. Those are repository settings, so
they are yours to apply; this change does not alter them.
