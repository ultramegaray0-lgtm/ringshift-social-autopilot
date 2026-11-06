"""Campaign traffic report: what was published where, and what it measurably did.

Inputs (all in the repository, nothing fetched here):
  data/campaign/ledger.json  every promotional activity (prepared / published / blocked / removed) with its URL and
                             any metrics read from the platform, plus optional hand-copied itch referrers
  data/metrics.json          X posts and their Buffer metrics (impressions, clicks)
  data/itch_stats.json       daily itch.io snapshots from scripts/itch_stats.py (views, downloads/plays)

Output: data/campaign/REPORT.md.

Rules: a metric nobody measured is "unavailable", never 0. Impressions, clicks and itch views are kept in separate
columns and never added together. itch views on a day are not credited to a channel unless itch's own referrer list
says so; the report only lines them up with what was published that day.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import itch_stats

BASE_DIR = Path(__file__).resolve().parent.parent
LEDGER_FILE = BASE_DIR / "data" / "campaign" / "ledger.json"
METRICS_FILE = BASE_DIR / "data" / "metrics.json"
REPORT_FILE = BASE_DIR / "data" / "campaign" / "REPORT.md"
NA = "unavailable"


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _fmt(value):
    return NA if value is None else str(value)


def x_summary(posts, project_url):
    """Totals over X posts that have Buffer metrics, split by whether the post carried the demo link."""
    groups = {"with demo link": [], "without link": []}
    for post in posts or []:
        if post.get("status") != "sent":
            continue
        has_link = bool(project_url) and project_url.rstrip("/") in str(post.get("text") or "")
        groups["with demo link" if has_link else "without link"].append(post)
    rows = []
    for name, items in groups.items():
        measured = [p for p in items if isinstance(p.get("metrics"), dict) and p["metrics"].get("Impressions") is not None]
        imp = sum(int(p["metrics"].get("Impressions") or 0) for p in measured) if measured else None
        clicks = sum(int(p["metrics"].get("Clicks") or 0) for p in measured) if measured else None
        ctr = f"{100 * clicks / imp:.2f}%" if imp and clicks is not None else NA
        rows.append((name, len(items), len(measured), imp, clicks, ctr))
    return rows


def render(ledger, posts, itch_history, project_url, now=None):
    now = now or datetime.now(timezone.utc)
    activities = ledger.get("activities", [])
    out = [f"# Ringshift campaign traffic report",
           f"",
           f"Generated {now.strftime('%Y-%m-%d %H:%M UTC')}. Demo: {project_url or NA}. "
           f"Missing numbers are `{NA}`, never 0.", ""]

    out += ["## Demo page (itch.io's own counters)", ""]
    if itch_history:
        last = itch_history[-1]
        out.append(f"Latest snapshot {last['date']}: views {_fmt(last.get('views_count'))}, "
                   f"downloads/plays {_fmt(last.get('downloads_count'))}, purchases {_fmt(last.get('purchases_count'))}.")
        out += ["", "| Date | New views | New downloads/plays | Published that day |", "|---|---|---|---|"]
        by_day = {}
        for a in activities:
            if a.get("status") == "published" and a.get("date"):
                by_day.setdefault(a["date"][:10], []).append(f"{a.get('channel')}: {a.get('community') or a.get('title')}")
        for date, delta in itch_stats.daily_deltas(itch_history)[-30:]:
            out.append(f"| {date} | {_fmt(delta['views_count'])} | {_fmt(delta['downloads_count'])} | "
                       f"{'; '.join(by_day.get(date, [])) or '-'} |")
    else:
        out.append(f"No snapshots yet: itch.io views and plays are `{NA}` until the ITCH_API_KEY secret is set and "
                   f"the *Ringshift itch stats* workflow has run on two days.")
    refs = ledger.get("referrers") or []
    out += ["", "Referrers (copied by hand from itch.io → Analytics, the only place itch shows them):", ""]
    if refs:
        out += ["| Period | Source | Views |", "|---|---|---|"]
        out += [f"| {r.get('period')} | {r.get('source')} | {_fmt(r.get('views'))} |" for r in refs]
    else:
        out.append(f"`{NA}` (none recorded yet).")

    out += ["", "## X via Buffer (measured by Buffer)", "",
            "| Posts | Count | With metrics | Impressions | Link clicks | CTR |", "|---|---|---|---|---|---|"]
    for name, count, measured, imp, clicks, ctr in x_summary(posts, project_url):
        out.append(f"| {name} | {count} | {measured} | {_fmt(imp)} | {_fmt(clicks)} | {ctr} |")

    out += ["", "## Activities by status", ""]
    for status in ("published", "prepared", "blocked", "removed"):
        items = [a for a in activities if a.get("status") == status]
        out += [f"### {status.capitalize()} ({len(items)})", ""]
        if not items:
            out += ["-", ""]
            continue
        out += ["| Date | Channel | Community / target | Kind | URL | Impressions/views | Clicks | Replies | Note |",
                "|---|---|---|---|---|---|---|---|---|"]
        for a in sorted(items, key=lambda a: (a.get("date") or a.get("prepared_at") or "")):
            m = a.get("metrics") or {}
            out.append(f"| {a.get('date') or a.get('prepared_at') or '-'} | {a.get('channel')} | "
                       f"{a.get('community') or '-'} | {a.get('kind')} | {a.get('url') or '-'} | "
                       f"{_fmt(m.get('views'))} | {_fmt(m.get('clicks'))} | {_fmt(m.get('replies'))} | "
                       f"{(a.get('blocker') or a.get('notes') or '').replace('|', '/')[:140]} |")
        out.append("")
    return "\n".join(out) + "\n"


def main():
    project_url = itch_stats.project_url()
    ledger = _load(LEDGER_FILE, {"activities": []})
    posts = _load(METRICS_FILE, [])
    history = itch_stats.load()
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(render(ledger, posts, history, project_url), encoding="utf-8")
    print(f"Wrote {REPORT_FILE.relative_to(BASE_DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
