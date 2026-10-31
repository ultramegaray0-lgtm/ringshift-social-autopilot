# TikTok videos: a funny clip on top, real Ringshift gameplay below (or gameplay only)

Workflow **Ringshift TikTok video** (`.github/workflows/tiktok-video.yml`), scripts `scripts/tiktok_video.py`,
`scripts/tiktok_board.py`, `scripts/tiktok_state.py`. Separate from the X autopilot: different workflow, different
concurrency group, no Buffer or Gemini key, never commits to `main`.

```
 media/funny/<your clip>.mp4 ─┐
                              ├─► 1080x1920 MP4 ─► artifact on the run page ─► optional official TikTok inbox upload ─► you tap Publish
 real gameplay (muted) ───────┘
   preferred: played now from ringshift-game@main in headless Chromium (capture_gameplay.py, real key presses, real win)
   fallback:  media/gameplay/ recordings
```

Two formats, both 1080x1920 and cropped to the real puzzle board:

| Format | When | What |
|---|---|---|
| **funny** | a funny clip is due (new, or an old one allowed back) | funny clip on top with its own sound, muted board below, demo card between |
| **gameplay** | no funny clip is due | larger board (820 px) between a "ROTATE THE RINGS / LIGHT EVERY LOCK" card and the demo card; **silent** (add a sound in the TikTok app) |

Which one a run makes (`format: auto`, also the schedule):

1. a funny clip that was **never used** → funny;
2. else an **old clip that may come back**: at least **7 days** since its last video, used fewer than **3** times, not
   failing → funny again, with gameplay it was **not** shown with before (other levels or recordings);
3. else → **gameplay**. Running out of funny clips never stops the daily video.

Any video that would show exactly what an earlier one showed (same format, clip and gameplay) is refused.

## 1. Add a funny clip

On github.com open **media/funny/** → **Add file → Upload files** → choose the clip → **Commit changes**.

- MP4, MOV or WebM; 2 to 180 seconds; up to 25 MB through the website (GitHub's limit), 100 MB with git.
- New clips are used first. Tracking is by content (SHA-256): renaming a used clip does not make it new. An old clip
  comes back only under the rules above (7-day rest, 3 uses at most, different gameplay).
- Only upload clips you have the right to repost.

## 2. Make the video

**Automatically:** every day at 07:41 UTC (10:41 Riyadh) the workflow makes one video with `format: auto`: the next
unused clip (alphabetical order), else a clip allowed back, else a gameplay-only video. The run summary says which
format it made and why, and for each used clip when it can come back. With no usable gameplay at all (game unavailable
and no curated recording with a whole board) the run **fails** with the reasons, instead of a quiet green skip.

**By hand:** **Actions → Ringshift TikTok video → Run workflow** (branch `main`):

| Input | Meaning |
|---|---|
| `format` | `auto` (default): as above. `funny`: only a funny-clip video; with none due the run is green and says **SKIPPED** and why. `gameplay`: a gameplay-only video even if a new clip is waiting. |
| `gameplay` | `auto` (default): live gameplay captured now from the stable game, falling back to `media/gameplay/` recordings if Chromium or the game is unavailable. `live`: live only (fails instead of falling back). `curated`: recordings only. |
| `funny_file` | Optional: a specific clip, e.g. `media/funny/cat.mp4`, instead of the next unused one. |
| `allow_reuse` | With `funny_file`: re-make a video from a clip that was already used (ignores the 7-day rest). |

A run on another branch (for testing) makes the video but does not mark the clip as used.

## 3. Download or send to TikTok inbox

Open the finished run → **Artifacts** → `ringshift-tiktok-<date>-<clip>-<hash>` (kept 30 days). The zip holds:

- the MP4 (H.264 + AAC, 1080x1920, 30 fps),
- `…json`: source files and their SHA-256 hashes, duration, resolution, the board measurements and crop, every
  validation result, and a **caption suggestion**,
- `…_preview.jpg`: one frame, to check at a glance.

You can still download the MP4 and upload it manually. Alternatively, use the new **Ringshift TikTok inbox upload**
workflow to send the selected artifact to your TikTok inbox as an editable draft. It does not publish automatically:
open the TikTok notification, review/edit the video and caption, then tap **Publish**. Setup and one-time credentials
are documented in [TIKTOK_UPLOAD.md](TIKTOK_UPLOAD.md). Make sure the demo link is in your TikTok bio: the video only
says "LINK IN BIO", so it stays correct whenever the link changes.

## Layout

| Area (1080x1920) | Content |
|---|---|
| Top, 608-660 px | The funny clip, fitted whole (never cropped) over a blurred copy of itself. A 16:9 clip fills the width. |
| Card, 112 px | "PLAY THE DEMO / LINK IN BIO", centred, inside TikTok's safe area, above the board. |
| Board, ~724-776 px square | Only the puzzle board: rings, rim locks and beams, scaled as large as the safe area allows. |
| Bottom ~400 px, right ~130 px | Left free for TikTok's caption and like/comment rail (blurred gameplay background only). |

Gameplay format: hook card (112 px) above an 820 px board above the demo card, stacked in the middle of the safe area,
over a blurred, darkened enlargement of the gameplay. Same safe area, same board measurement and crop.

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
  and almost nothing left after subtracting it, so no gameplay sound or music can be in it). Gameplay format: no
  audio track at all,
- the board circle is found inside the board area of the finished video, whole, in most frames, filling ≥ 82% of it,
- the card's letters are bright on a dark panel at the planned position; card and board inside the safe area and
  not overlapping (gameplay format: the same for the hook card, and the layout must be the gameplay layout),
- gameplay format: at least 8 s of real gameplay (at most 60 s).

## State and duplicates

Which clips were used lives in `tiktok_state.json` on the **`tiktok-state`** branch (never `main`): one entry per
video, with its format, clip (none for gameplay videos), the levels/themes or recordings it showed and a content
fingerprint. A video is recorded only after its artifact was uploaded. Re-running a workflow run that already made its
video makes nothing new. A clip whose video failed 3 times is no longer picked automatically (pick it with `funny_file`
to retry); a refused duplicate or missing gameplay never counts against a clip. The levels shown are remembered, so
live gameplay keeps moving on to levels not shown yet.

## Running it on your own computer

```
pip install -r requirements-tiktok.txt          # + FFmpeg; for live gameplay also requirements-capture.txt and
python -m playwright install chromium           #   a checkout of ringshift-game
python scripts/tiktok_video.py status
python scripts/tiktok_video.py make --gameplay curated
python scripts/tiktok_video.py make --game-dir ../ringshift-game
python scripts/tiktok_video.py make --format gameplay --gameplay curated   # a gameplay-only video
```

Output goes to `media/tiktok-output/` (git-ignored); state to `data/tiktok_state.json` (git-ignored).

## TikTok inbox upload

The separate **Ringshift TikTok inbox upload** workflow uses the official Content Posting API `video.upload` scope.
It is manual and opt-in: select a successful video artifact and dispatch the upload workflow. TikTok sends an inbox
notification so you can edit the caption, sound, cover and settings and tap Publish. It never publishes automatically.
See [TIKTOK_UPLOAD.md](TIKTOK_UPLOAD.md) for app approval, OAuth and secret setup.
