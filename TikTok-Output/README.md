# Ringshift TikTok output

Finished, validated 1080x1920 TikTok videos made by the **Ringshift TikTok video** workflow.

Each video is three files with the same name:

- `<name>.mp4` - the video, re-checked before it was saved (SHA-256, 1080x1920 H.264, full decode)
- `<name>.json` - what it shows, every quality check, and the caption suggestion
- `<name>_preview.jpg` - a still frame

`index.json` lists them newest first, with the workflow run that made each one. Only the newest 30 videos are
kept; older ones are removed (each is also an Actions artifact for 30 days).

Nothing here is public. When the TikTok inbox upload is set up, each new video is also sent to the TikTok inbox
as an editable draft (see docs/TIKTOK_UPLOAD.md on main); it goes live only when you tap Post in TikTok.
