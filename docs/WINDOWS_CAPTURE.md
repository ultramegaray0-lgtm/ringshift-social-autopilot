# Capturing gameplay on your Windows PC

One-time setup (PowerShell):
```
git clone https://github.com/ultramegaray0-lgtm/ringshift-social-autopilot
cd ringshift-social-autopilot
git checkout main
py -m venv .venv ; .venv\Scripts\Activate.ps1
pip install -r requirements-capture.txt
python -m playwright install chromium
winget install Gyan.FFmpeg          # then reopen PowerShell; check: ffmpeg -version
```
Put the game file somewhere outside the repo (the `game/` folder is git-ignored), e.g. `game\ringshift.html`.

Check the setup first (read-only, installs nothing):
```
python scripts/capture_gameplay.py doctor --game game\ringshift.html
```
Each time:
```
python scripts/capture_gameplay.py list --game game\ringshift.html        # all levels + solver result
python scripts/capture_gameplay.py plan --game game\ringshift.html --count 6   # what would be captured
python scripts/capture_gameplay.py capture --game game\ringshift.html --count 6
```
More shapes and unsolved teasers (see [MEDIA_FACTORY.md](MEDIA_FACTORY.md)):
```
python scripts/capture_gameplay.py capture --game game\ringshift.html --count 2 --seed 7 --kind all --aspect 9:16 --aspect 16:9
python scripts/media_manifest.py validate
```
A review report and contact sheet are written to `data/capture_preview/`.
Output goes to `media/captured/` (video + puzzle still + solved still per level) and is registered in
`media/media_tags.json` with a description that states only facts read from the game
(level, gimmicks, move count, "ends on the BREACHED screen"). `--headed` shows the browser. `--force` recaptures a level.

Then either
* **commit** them (about 1 MB per clip, 0.15 MB per still), or
* **host on the `media-store` branch** to keep `main` small: `python scripts/media_store.py publish media/captured/*.mp4 --delete-local`, then commit `media/media_tags.json`.

Check what you got: open the clips. The tool validates files and win state, but a human glance is the real review.
Known cosmetic point: at the 540x960 capture viewport the game's top bar buttons overlap its text.
It is the game's own layout, not a capture artifact; consider whether you want to fix it before promoting.
