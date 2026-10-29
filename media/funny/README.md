# Funny clips for TikTok videos

Put the funny clips for the TikTok videos here. Each clip becomes the **top half** of one vertical video, with real,
muted Ringshift gameplay underneath. The clip's own sound is kept exactly; nothing else is added to the audio.

**Upload on GitHub (phone or PC):** open this folder on github.com → **Add file → Upload files** → pick the clip →
**Commit changes** (straight to `main` is fine). The next scheduled run (or a manual run of the **Ringshift TikTok
video** workflow) turns the next unused clip into a video.

## What is accepted

| | |
|---|---|
| Formats | `.mp4`, `.mov`, `.webm` (iPhone and Android recordings are fine) |
| Length | 2 to 180 seconds; trim longer clips first |
| Size | up to 25 MB through the GitHub website (GitHub's own limit); up to 100 MB with git |
| Picture | at least 240 px on the short side; any shape (16:9, square and vertical all fit the top panel) |

Every clip is checked before use (real video file, not truncated, decodes completely). A clip that fails is skipped
and the run summary says why. Clips are tracked by their content (SHA-256): renaming a clip does not make it new, and
replacing it with an edited version does. New clips always go first. When none is new, an old clip may come back
once at least 7 days have passed since its last video, at most 3 times in total, always with different gameplay;
otherwise the day's video is gameplay-only. The run never stops just because this folder has run dry.

Only upload clips you have the right to use and repost on TikTok.

These files are never used by the X autopilot: it skips this folder entirely.
