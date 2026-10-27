"""One table at the top of a daily run's page: media, checks, caption, Buffer, and the overall outcome.

Reads only what the run itself wrote (data/daily_media.json for this run, auto_result.json) and the step outcomes the
workflow passes in. It states what happened; it never says a post was published unless Buffer confirmed it in this run.

    python scripts/run_summary.py        # in GitHub Actions (writes to the job summary); prints the same text
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _read(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def this_runs_media(state, run_id):
    """The daily_media.json entry written by THIS run (matched by run id), or None."""
    runs = (state or {}).get("runs") or []
    if run_id:
        mine = [run for run in runs if str(run.get("run_id")) == str(run_id)]
        return mine[-1] if mine else None
    return None


def build(env, media_run, result):
    window_ok = env.get("WINDOW_OK")
    autopublish = env.get("AUTOPUBLISH") or "on"
    rows = []

    if autopublish == "off":
        rows.append(("Auto-publish", "switched OFF (variable RINGSHIFT_AUTOPUBLISH): drafts only"))
    elif window_ok == "false":
        rows.append(("Today's post window", "closed - " + (env.get("WINDOW_REASON") or "see the log")))
    elif window_ok == "true":
        rows.append(("Today's post window", "open - " + (env.get("WINDOW_REASON") or "")))
    else:
        rows.append(("Today's post window", "not checked (an earlier step stopped the run)"))

    if media_run and media_run.get("status") == "captured":
        seconds = f", {media_run['duration']:.1f}s" if media_run.get("duration") else ""
        rows.append(("Fresh media", f"`{media_run['file']}` - {media_run['type']} ({media_run['kind']}{seconds}), "
                                    f"level `{media_run['scene']}`, theme {media_run.get('theme_name') or media_run.get('theme')}"))
        rows.append(("Media checks", "passed: FFmpeg decode, size, duration, contrast/motion, still usability, "
                                     "not a near-duplicate, public URL serves the same bytes"))
        rows.append(("Game source", f"`{media_run.get('game_file')}` at `{str(media_run.get('game_commit') or '?')[:10]}`"))
    elif media_run and media_run.get("status") == "failed":
        rows.append(("Fresh media", "NONE - " + str(media_run.get("why") or "every attempt failed")))
    elif env.get("MEDIA_STATUS") in ("failure", "cancelled"):
        rows.append(("Fresh media", "NONE - the capture step failed (see its log)"))
    else:
        rows.append(("Fresh media", "not made (the run stopped before capturing)"))

    status = (result or {}).get("status")
    if status:
        if result.get("text"):
            rows.append(("Caption", f"critic score {result.get('critic_score')}/60, post type {result.get('post_type')}"))
        if status == "queued":
            rows.append(("Buffer", f"QUEUED with media confirmed - post `{result.get('buffer_id')}`, due {result.get('due_at')} UTC"))
        elif status == "no_draft":
            rows.append(("Caption", "no caption passed the rules and the critic"))
            rows.append(("Buffer", "nothing sent"))
        elif status == "disabled":
            rows.append(("Buffer", f"nothing sent (auto-publish off); draft `{result.get('draft_id')}` can be approved with publish-approved"))
        else:
            rows.append(("Buffer", f"NOT queued - {result.get('reason')}"))
    else:
        rows.append(("Caption / Buffer", "not attempted"))

    if status == "queued":
        outcome = "POSTED TO THE BUFFER QUEUE (Buffer publishes it to X at the due time)"
    elif window_ok == "false" and autopublish != "off":
        outcome = "NO POST NEEDED - " + (env.get("WINDOW_REASON") or "today's post window is closed")
    elif status == "disabled":
        outcome = "DRAFT ONLY (auto-publish is off)"
    else:
        outcome = "NOT POSTED"

    lines = ["## Ringshift daily run", "", f"**Outcome: {outcome}**", "", "| Stage | Result |", "|---|---|"]
    lines += [f"| {stage} | {str(text).replace('|', '/')} |" for stage, text in rows]
    if result and result.get("text"):
        lines += ["", "**Post text:**", "", "> " + result["text"].replace("\n", "\n> ")]
    return "\n".join(lines) + "\n"


def main():
    env = dict(os.environ)
    media_run = this_runs_media(_read(BASE_DIR / "data" / "daily_media.json", {}), env.get("GITHUB_RUN_ID"))
    result = _read(BASE_DIR / "auto_result.json", None)
    text = build(env, media_run, result)
    print(text)
    path = env.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
