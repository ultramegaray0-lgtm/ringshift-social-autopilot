# Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Configuration error ... missing or empty` | Secret not set, or named differently. Names are exact: `GEMINI_API_KEY`, `BUFFER_API_KEY`. |
| `media URL answered HTTP 404 ... no post was created` | The file exists locally but isn't on `main` yet (merge/push), or the name differs. Nothing was queued. |
| `media URL serves N bytes but the file is M bytes` | Stale file on `main`; push the latest version. Nothing was queued. |
| `Buffer returned a queued post but did not confirm attached media` | **A post may exist without media.** Check Buffer and delete it before re-running. |
| `This exact post text is already in data/post_log.json` | Safety stop against double-queueing. |
| Rate limit message (429) | Retried automatically up to 3 times, honouring `Retry-After`. Persistent: wait 15 minutes. |
| `NO CANDIDATE PASSED` | All candidates failed rules/critic. Harmless; the run posts nothing. Re-run later. |
| Capture: `no win` | The level may need the NEXT button or the solver found no solution; try `--scene mode:index`. |
| Capture: `game file not found` / missing globals | Wrong file, or a game version that renamed internals (`REQUIRED_GLOBALS` in `capture_gameplay.py`). |
| Preview shows a clip with an odd description | Fix its entry in `media/media_tags.json` (`description`, `tags`, or `"exclude": true`). |
