# Curated Ringshift gameplay recordings

Real Ringshift gameplay recorded by hand (screen recordings of the game). This folder has two users:

- **X autopilot**: the recordings and screenshots here that are listed in `media/manifest.json` can be attached to X
  posts (see docs/MEDIA_FACTORY.md). Files not in the manifest are never posted to X.
- **TikTok videos**: when live gameplay cannot be captured from the game (or a run asks for `curated`), the TikTok
  workflow uses the videos here (`.mp4`, `.mov`, `.webm`) for the bottom half of the video. Their sound is always
  removed.

For a recording to be usable in a TikTok video, the **whole circular board must be on screen**, at least 3 seconds
long. Both desktop (16:9) and phone (9:16) recordings work: the board's position and size are measured from the
pixels of each recording (no fixed coordinates), and only the square around the board is used, scaled up to fill the
board area. A recording where the board is cut off by the screen edge, or not visible most of the time (menus, level
select), is skipped and the run summary says why. Least-used recordings are picked first.
