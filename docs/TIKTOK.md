# TikTok videos: a funny clip on top, real Ringshift gameplay below

Workflow **Ringshift TikTok video** (`.github/workflows/tiktok-video.yml`), scripts `scripts/tiktok_video.py`,
`scripts/tiktok_board.py`, `scripts/tiktok_state.py`. Separate from the X autopilot: different workflow, different
concurrency group, no Buffer or Gemini key, never commits to `main`.

```
 media/funny/<your clip>.mp4 ─┐
                              ├─► 1080x1920 MP4 ─► artifact on the run page ─► you upload it in the TikTok app
 real gameplay (muted) ───────┘
   preferred: played now from ringshift-game@main in headless Chromium (capture_gameplay.py, real key presses, real win)
   fallback:  media/gameplay/ recordings
```

## 1. Add a funny clip

On github.com open **media/funny/** → **Add file → Upload files** → choose the clip → **Commit changes**.

- MP4, MOV or WebM; 2 to 180 seconds; up to 25 MB through the website (GitHub's limit), 100 MB with git.
- Each clip is used once. Tracking is by content (SHA-256): renaming a used clip does not make it new.
- Only upload clips you have the right to repost.

## 2. Make the video

**Automatically:** every day at 07:41 UTC (10:41 Riyadh) the workflow takes the next unused clip (alphabetical order).
With no new clip the run is green and its summary says **SKIPPED** and why (no clips / all used / which ones are
invalid and why).

**By hand:** **Actions → Ringshift TikTok video → Run workflow** (branch `main`):

| Input | Meaning |
|---|---|
| `gameplay` | `auto` (default): live gameplay captured now from the stable game, falling back to `media/gameplay/` recordings if Chromium or the game is unavailable. `live`: live only (fails instead of falling back). `curated`: recordings only. |
| `funny_file` | Optional: a specific clip, e.g. `media/funny/cat.mp4`, instead of the next unused one. |
| `allow_reuse` | With `funny_file`: re-make a video from a clip that was already used. |

A run on another branch (for testing) makes the video but does not mark the clip as used.

## 3. Download and upload

Open the finished run → **Artifacts** → `ringshift-tiktok-<date>-<clip>-<hash>` (kept 30 days). The zip holds:

- the MP4 (H.264 + AAC, 1080x1920, 30 fps),
- `…json`: source files and their SHA-256 hashes, duration, resolution, the board measurements and crop, every
  validation result, and a **caption suggestion**,
- `…_preview.jpg`: one frame, to check at a glance.

Upload the MP4 in the TikTok app and paste (or edit) the caption. Make sure the demo link is in your TikTok bio: the
video only says "LINK IN BIO", so it stays correct whenever the link changes.

## Layout

| Area (1080x1920) | Content |
|---|---|
| Top, 608-660 px | The funny clip, fitted whole (never cropped) over a blurred copy of itself. A 16:9 clip fills the width. |
| Card, 112 px | "PLAY THE DEMO / LINK IN BIO", centred, inside TikTok's safe area, above the board. |
| Board, ~724-776 px square | Only the puzzle board: rings, rim locks and beams, scaled as large as the safe area allows. |
| Bottom ~400 px, right ~130 px | Left free for TikTok's caption and like/comment rail (blurred gameplay background only). |

**How the board is found (no fixed coordinates):** for live capture the browser reports the bounding box of what the
game draws in its `#puzzle` and `#puzzleBeams` layers (measured before and after the level; the run stops if they
differ). For recordings the board's outer rim is found in the pixels (circle voting over several frames, then a
least-squares fit). Each piece is cropped to a square around the rim with a 3% margin and scaled to the board area; a
board cut off by the recording's edge, or missing in most frames, is rejected. Live captures are also cross-checked
against the pixel detector (in testing: centres within 1% of the radius, radii within about 3%).

## Checks before a video counts as made

Every run validates the finished file and **deletes it if any check fails** (the run then fails and uploads only the
metadata as `ringshift-tiktok-FAILED-…`):

- decodes completely; H.264 yuv420p; 1080x1920; duration equals the funny clip's,
- audio: exactly as many tracks as the funny clip had (0 or 1); its sound matches the funny clip (correlation ≥ 0.95
  and almost nothing left after subtracting it, so no gameplay sound or music can be in it),
- the board circle is found inside the board area of the finished video, whole, in most frames, filling ≥ 82% of it,
- the card's letters are bright on a dark panel at the planned position; card and board inside the safe area and
  not overlapping.

## State and duplicates

Which clips were used lives in `tiktok_state.json` on the **`tiktok-state`** branch (never `main`). A clip is marked
used only after its artifact was uploaded. Re-running a workflow run that already made its video makes nothing new.
A clip whose video failed 3 times is no longer picked automatically (pick it with `funny_file` to retry).

## Running it on your own computer

```
pip install -r requirements-tiktok.txt          # + FFmpeg; for live gameplay also requirements-capture.txt and
python -m playwright install chromium           #   a checkout of ringshift-game
python scripts/tiktok_video.py status
python scripts/tiktok_video.py make --gameplay curated
python scripts/tiktok_video.py make --game-dir ../ringshift-game
```

Output goes to `media/tiktok-output/` (git-ignored); state to `data/tiktok_state.json` (git-ignored).

## Direct publishing to TikTok (not built; possible future mode)

Manual upload is the supported path and needs no TikTok credentials. Posting automatically would need TikTok's
Content Posting API, which requires, per TikTok's developer documentation:

- a registered app on TikTok for Developers with the Content Posting API product and its Direct Post configuration,
- approval for the `video.publish` scope, and your account authorising the app (OAuth access token and open ID),
- an audit of the app: until it passes, everything posted through the API is restricted to private viewing.

None of this is configured, so nothing in this repository talks to TikTok. If you set it up later, it would be a
separate, opt-in step after the artifact upload, with the token in a repository secret.
