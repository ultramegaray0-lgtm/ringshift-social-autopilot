# Drafts, approval and publishing (from your phone)

**Since 2026-10-27 the daily schedule publishes by itself** (see [AUTOMATION.md](AUTOMATION.md)): it captures fresh
gameplay from the real game, writes the post, runs every check and, if all pass, queues it in Buffer through the same
verified path described below, at most once per Riyadh day. This page is about the **manual** path, which still works:

* With the repository variable `RINGSHIFT_AUTOPUBLISH=off`, the schedule goes back to **writing a draft only**, and a
  post goes out only when **you** start a `publish-approved` run on `main` and type its **draft ID**.
* `preview` and `publish-approved` can always be used by hand, for example to post something in addition to the daily
  post (with `allow_extra_post`) or while auto-publish is off.
* The post is exactly the approved draft: same text, same media, same Buffer channel. It cannot be regenerated,
  edited, reused or used late.

Modes of the *Ringshift AI Autopilot* workflow (Actions -> Run workflow):

| Mode | Uses | Can it post? |
|---|---|---|
| *(daily schedule)* | Gemini + Buffer keys | **Yes, at most one post a day**, only fresh media that passed every check. With `RINGSHIFT_AUTOPUBLISH=off`: no, a draft only. |
| `daily` | same as the schedule | Same as the schedule (counts toward the one post a day). |
| `daily-dry-run` | Gemini key | **No.** Fresh capture, caption and critic exactly like the schedule, then a draft only. |
| `test` | nothing | No. Picks media only. |
| `preview` | Gemini key | No. Writes a draft artifact. |
| `rehearse-approval` | GitHub token only | **No.** Re-checks a draft and exercises the claim mechanism on a throwaway tag. |
| `publish-approved` | Buffer key | **Yes, one post**, only with a valid draft ID, only on `main`. |

## 1. Inspect today's draft on your phone
With auto-publish off, each day at 06:00 UTC (09:00 Riyadh) the schedule creates a draft. Drafts expire after
**48 hours**. You can also make one any time with mode `preview`.

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

### Choosing the media for a preview (optional)
By default the rotation picks the media. To write a draft about one specific committed file, run `preview` with
**media_file** set to its repository path, for example `media/gameplay/06_hard15_five_mechanisms.png`.

* It only narrows the choice to that file. Every check still applies: the file must fit a post type (a still that
  shows no solution never gets a "solution" post), pass the real-file and public-URL byte checks, and the text must pass
  the writer, the critic and the post rules. If any of that fails you get **no draft** and a report, never a different file.
* The text is still written by Gemini and screened as usual; you cannot supply your own text this way.
* It is refused for every mode except `preview`, and it is not part of an approval: a publish run posts exactly the
  approved draft whatever this input was.
* The daily schedule never sets it.

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

## Turning the daily posts off and on

**A. Drafts only (recommended way to pause posting).** Settings -> Secrets and variables -> Actions -> **Variables** ->
New repository variable `RINGSHIFT_AUTOPUBLISH` = `off`. The schedule keeps capturing fresh media and writing a draft
each day, and posts nothing until you approve a draft. Delete the variable (or set it to `on`) to resume.

**B. Disable the whole workflow** (stops the schedule *and* manual runs)
* Off: **Actions** -> **Ringshift AI Autopilot** -> **...** (top right of the run list) -> **Disable workflow**.
* On: same place -> **Enable workflow**. You must enable it again before you can run `preview` or `publish-approved`.
* From a computer: `gh workflow disable "Ringshift AI Autopilot"` / `gh workflow enable "Ringshift AI Autopilot"`.

Also know: GitHub automatically disables scheduled workflows in a repository with no activity for 60 days. If daily
posts stop appearing, open the Actions tab; GitHub shows a banner with an **Enable workflow** button.

## What stops a mistake
| Situation | What happens |
|---|---|
| Daily schedule runs | Captures fresh media, writes a post, and publishes it only if every check below passes; at most one post per Riyadh day. With `RINGSHIFT_AUTOPUBLISH=off`: a draft only. |
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
