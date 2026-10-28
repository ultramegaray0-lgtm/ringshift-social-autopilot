# TikTok output (local runs only)

When you run `python scripts/tiktok_video.py make` on your own computer, the finished video, its metadata JSON and a
preview frame are written here. Everything in this folder except this README is git-ignored, so finished videos are
never committed to `main`.

In GitHub Actions, finished videos are **not** stored in the repository at all: each one is a downloadable artifact of
the workflow run (named `ringshift-tiktok-<date>-<clip>-<hash>`, kept 30 days). See docs/TIKTOK.md.

The X autopilot never reads this folder.
