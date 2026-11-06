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
import performance

BASE_DIR = Path(__file__).resolve().parent.parent
LEDGER_FILE = BASE_DIR / "data" / "campaign" / "ledger.json"
METRICS_FILE = BASE_DIR / "data" / "metrics.json"
POST_LOG_FILE = BASE_DIR / "data" / "post_log.json"
REPORT_FILE = BASE_DIR / "data" / "campaign" / "REPORT.md"
NA = "unavailable"


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _fmt(value):
    return NA if value is None else str(value)


def x_summary(posts, project_url, post_log=None):
    """Totals over X posts that have Buffer metrics, split by whether the post carried the demo link. The queued text
    in data/post_log.json is used too, because X shows sent links only as t.co."""
    rows = performance.join(posts, post_log or [], project_url)
    names = {"demo": "with demo link", "none": "without link", "other": "other link (t.co, unidentified)"}
    out = []
    for key in ("demo", "none", "other"):
        items = [r for r in rows if r["link"] == key]
        if key == "other" and not items:
            continue
        measured = [r for r in items if r["impressions"] is not None]
        imp = sum(r["impressions"] for r in measured) if measured else None
        clicks = sum(r["clicks"] or 0 for r in measured) if measured else None
        rate = f"{100 * clicks / imp:.2f}%" if imp and clicks is not None else NA
        out.append((names[key], len(items), len(measured), imp, clicks, rate))
    return out

def _pct(value):
    return NA if value is None else f"{value:.2%}"


def _group_table(title, groups):
    rows = [f"### {title}", "", "| Group | Posts | Measured | Avg impressions | Clicks | CTR | Compared? |",
            "|---|---|---|---|---|---|---|"]
    for name, g in groups.items():
        avg = NA if g["avg_impressions"] is None else f"{g['avg_impressions']:.1f}"
        clicks = g["clicks"] if g["measured"] else NA
        rows.append(f"| {name} | {g['posts']} | {g['measured']} | {avg} | {clicks} | {_pct(g['ctr'])} | "
                    f"{'yes' if g['comparable'] else f'no (<{performance.MIN_GROUP})'} |")
    return rows + [""]


def channel_rows(ledger, x_rows):
    """Channel comparison: X from Buffer, every other channel from ledger entries the owner/automation recorded."""
    measured_x = [r for r in x_rows if r["impressions"] is not None]
    rows = [("X (Buffer)", len(x_rows), sum(r["impressions"] for r in measured_x) if measured_x else None,
             sum(r["clicks"] or 0 for r in measured_x) if measured_x else None)]
    channels = {}
    for a in ledger.get("activities", []):
        if a.get("status") != "published":
            continue
        c = channels.setdefault(a.get("channel") or "?", [0, None, None])
        c[0] += 1
        m = a.get("metrics") or {}
        for i, key in ((1, "views"), (2, "clicks")):
            if isinstance(m.get(key), (int, float)):
                c[i] = (c[i] or 0) + m[key]
    rows += [(name, n, views, clicks) for name, (n, views, clicks) in sorted(channels.items())]
    return rows


def performance_sections(ledger, posts, post_log, project_url):
    rows = performance.join(posts, post_log, project_url)
    out = ["", "## Channel comparison", "",
           "itch visits per channel need GA4 on the itch page (UTM tags are already on every campaign link) or the "
           "referrer table copied into the ledger; until then that column is unavailable.", "",
           "| Channel | Published | Impressions / views | Link clicks | itch visits |", "|---|---|---|---|---|"]
    for name, n, views, clicks in channel_rows(ledger, rows):
        out.append(f"| {name} | {n} | {_fmt(views)} | {_fmt(clicks)} | {NA} |")

    out += ["", "## Daily clicks (X, by send date)", "", "| Date | Posts | Measured | Impressions | Clicks |",
            "|---|---|---|---|---|"]
    for date, n, m, imp, clicks in performance.daily(rows)[-21:]:
        out.append(f"| {date} | {n} | {m} | {imp if m else NA} | {clicks if m else NA} |")

    out += ["", "## Which creative performs (X, measured posts only)", ""]
    out += _group_table("Demo link", {{"demo": "with demo link", "none": "without link",
                                       "other": "other link (t.co)"}[k]: v for k, v in performance.group(rows, "link").items()})
    out += _group_table("Media kind (from the captured file name; 'unknown' = posted before the post log existed)",
                        performance.group(rows, "media"))
    out += _group_table("Post type", performance.group(rows, "post_type"))

    flagged = performance.underperformers(rows)
    out += ["### Underperforming posts", ""]
    if flagged:
        out += ["| Date | # | Type | Media | Why flagged | Text |", "|---|---|---|---|---|---|"]
        for row, why in flagged:
            text = row["text"].replace("|", "/").replace("\n", " ")[:70]
            out.append(f"| {row['date']} | {row['number'] or '-'} | {row['post_type']} | {row['media']} | {why} | {text} |")
    else:
        out.append("None flagged (needs at least 5 measured posts).")

    out += ["", "## Recommendations (from measured data only)", ""]
    out += [f"- {r}" for r in performance.recommendations(rows)]
    return out


def render(ledger, posts, itch_history, project_url, now=None, post_log=None):
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
    for name, count, measured, imp, clicks, ctr in x_summary(posts, project_url, post_log):
        out.append(f"| {name} | {count} | {measured} | {_fmt(imp)} | {_fmt(clicks)} | {ctr} |")

    out += performance_sections(ledger, posts, post_log or [], project_url)

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
    post_log = _load(POST_LOG_FILE, [])
    history = itch_stats.load()
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(render(ledger, posts, history, project_url, post_log=post_log), encoding="utf-8")
    print(f"Wrote {REPORT_FILE.relative_to(BASE_DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
