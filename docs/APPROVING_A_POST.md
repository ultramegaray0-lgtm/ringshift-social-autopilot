# Approving a draft and publishing exactly one post (from your phone)

Nothing is published until you type a draft's ID into a publish run. The post that goes out is **exactly** the
draft you read: same text, same media, same Buffer channel. It cannot be regenerated, edited, reused or used late.

## 1. Make a draft (nothing is sent)
1. GitHub app or github.com (mobile) -> your repo -> **Actions** -> **Ringshift AI Autopilot** -> **Run workflow**.
2. Pick the branch `main`, set **mode = preview**, tap **Run workflow**. Wait about 2-3 minutes.
3. Open the finished run -> scroll to **Summary**. You will see:
   * **Draft ID (the approval code)**, like `d-1a2b3c4d-5e6f`
   * the exact **post text**, the **media** (tap *open it* to watch the clip; images are shown inline),
     the **media check**, the **target** (X via Buffer + channel ID), length, critic score, **expiry** (48 h)
4. The same draft is a downloadable **artifact** at the bottom of the run page, named `ringshift-draft-<draft ID>`
   (a small JSON file; on a phone the summary above is easier to read). If no draft was produced, the artifact is
   `ringshift-preview-report` and explains why each candidate was rejected: nothing can be published from it.

If you do not like the draft: do nothing, or run **preview** again. Drafts you don't use simply expire.

## 2. Approve it and publish once
1. **Actions** -> **Ringshift AI Autopilot** -> **Run workflow**.
2. Branch: **main** (other branches are refused).
3. **mode = publish-approved**.
4. **approval_code** = the draft ID from the summary (copy it; long-press to paste).
5. Leave **allow_extra_post** OFF.
6. Tap **Run workflow**.

The run then, in order: checks the code's format, finds the one draft with that ID and downloads it, re-verifies it
(content hash, expiry, media file unchanged, URL reachable, text still passes the post rules), checks Buffer has no
waiting post and nothing was queued in the last 12 hours, **claims the draft ID**, and only then sends it to Buffer.
It succeeds only if Buffer confirms the media is attached. The summary of the run says
`APPROVED DRAFT d-... QUEUED WITH MEDIA CONFIRMED`.

## 3. Check the result
* Buffer app -> the channel's queue: the post is there with its video/image attached.
* After Buffer's next slot, confirm it appears on X.
* `data/post_log.json` (committed by the run) records the draft ID, the approver and the Buffer post ID.

## What stops a mistake
| Situation | What happens |
|---|---|
| Wrong or mistyped code | Rejected before anything is claimed. |
| Draft file edited in any way (text, media, channel, expiry) | Rejected (`tampered`). |
| Older than 48 hours | Rejected (`expired`). Run a new preview. |
| Media file changed or missing since the preview | Rejected (`media_changed` / `media_missing`). |
| Text no longer passes the post rules | Rejected. Approval does not override the rules. |
| Buffer already has a waiting post / one queued <12 h ago | Rejected, nothing is used up; try again later. |
| You run it a second time, or GitHub re-runs a failed job | Rejected (`already_used`). Exactly one post can come from a draft. |
| The run fails **after** the claim (e.g. Buffer error) | The draft stays used up, because the result in Buffer is unknown. **Check Buffer first.** If no post exists, make a new preview and approve that. |

If you are certain Buffer has no post and want to retry the *same* draft, delete the git tag
`ringshift-used-<draft ID>` (repo -> Tags). Do this only after checking Buffer.

## Housekeeping
* Each used draft leaves one small tag `ringshift-used-<id>`; they are safe to keep (they are the record of use).
* The daily schedule is separate and unchanged: it still generates, reviews and queues one post itself. To make every
  post approval-gated, disable the schedule in the Actions tab (a separate decision).
