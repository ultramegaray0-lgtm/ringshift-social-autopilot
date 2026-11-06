"""Measured-only performance analysis for the campaign report.

Inputs are the repository's own records: data/metrics.json (Buffer: one row per X post with Impressions/Clicks),
data/post_log.json (what each queued post was: type, media file, number) and data/campaign/ledger.json.

Principles:
* Only posts with a Buffer metrics block count as measured. Everything else is reported as unavailable.
* A group (post type, media kind, link/no link, channel) is compared only when it has MIN_GROUP measured posts.
* A recommendation is produced only from a comparison that passed that bar, and it states its own sample sizes.
* Underperformers are flagged against the account's own recent median, never against an invented benchmark.
"""
import re
import statistics
from collections import OrderedDict

import tracking

MIN_GROUP = 3                 # measured posts a group needs before it is compared
LOW_REACH_FRACTION = 0.5      # below half the recent median impressions = low reach
RECENT = 30                   # how many recent measured posts define "recent median"
CLICKLESS_REACH = 100         # a linked post this visible with 0 clicks is flagged

_KIND_RE = re.compile(r"_(puzzle|solved|clip|teaser)_", re.IGNORECASE)


def measured(post):
    m = post.get("metrics")
    return isinstance(m, dict) and isinstance(m.get("Impressions"), (int, float))


def media_kind(media_file):
    """puzzle / solved / clip / teaser from the capture file name; 'still' or 'video' for other files; unknown."""
    if not media_file:
        return "unknown"
    match = _KIND_RE.search(media_file)
    if match:
        return match.group(1).lower()
    return "video" if str(media_file).lower().endswith((".mp4", ".mov", ".webm")) else "still"


def has_demo_link(text, project_url):
    return bool(project_url) and any(tracking.is_project_link(u, project_url)
                                     for u in re.findall(r"https?://\S+", str(text or "")))


def link_status(text, project_url, logged_text=None):
    """'demo' when the post (or the text we queued for it) links the demo page; 'other' when it carries a link we
    cannot identify (X rewrites links to t.co, so posts written by hand show only t.co); 'none' otherwise."""
    if has_demo_link(text, project_url) or has_demo_link(logged_text, project_url):
        return "demo"
    return "other" if re.search(r"https?://\S+", str(text or "")) else "none"


def join(posts, post_log, project_url):
    """One row per sent X post: Buffer metrics + what the post log knows about it."""
    by_id = {p.get("buffer_id"): p for p in post_log or [] if p.get("buffer_id")}
    rows = []
    for post in posts or []:
        if post.get("status") != "sent":
            continue
        log = by_id.get(post.get("id"), {})
        m = post.get("metrics") if measured(post) else None
        rows.append({
            "date": str(post.get("sentAt") or post.get("dueAt") or "")[:10],
            "number": log.get("number"),
            "text": post.get("text") or "",
            "post_type": log.get("post_type") or "unknown",
            "media": media_kind(log.get("media_file")),
            "media_file": log.get("media_file"),
            "link": link_status(post.get("text"), project_url, log.get("text")),
            "impressions": int(m["Impressions"]) if m else None,
            "clicks": int(m.get("Clicks") or 0) if m else None,
        })
    return sorted(rows, key=lambda r: r["date"])


def ctr(impressions, clicks):
    return None if not impressions or clicks is None else clicks / impressions


def group(rows, key):
    """{group value: stats} over measured rows."""
    out = OrderedDict()
    for row in rows:
        value = row[key] if not callable(key) else key(row)
        g = out.setdefault(value, {"posts": 0, "measured": 0, "impressions": 0, "clicks": 0})
        g["posts"] += 1
        if row["impressions"] is not None:
            g["measured"] += 1
            g["impressions"] += row["impressions"]
            g["clicks"] += row["clicks"] or 0
    for g in out.values():
        g["avg_impressions"] = g["impressions"] / g["measured"] if g["measured"] else None
        g["ctr"] = ctr(g["impressions"], g["clicks"]) if g["measured"] else None
        g["comparable"] = g["measured"] >= MIN_GROUP
    return out


def daily(rows):
    """[(date, posts, measured, impressions, clicks)] by send date."""
    days = OrderedDict()
    for row in rows:
        d = days.setdefault(row["date"], [0, 0, 0, 0])
        d[0] += 1
        if row["impressions"] is not None:
            d[1] += 1
            d[2] += row["impressions"]
            d[3] += row["clicks"] or 0
    return [(k, *v) for k, v in days.items()]


def underperformers(rows):
    """[(row, reason)]: measured posts with low reach against the recent median, or reach without any click."""
    recent = [r for r in rows if r["impressions"] is not None][-RECENT:]
    if len(recent) < 5:
        return []
    median = statistics.median(r["impressions"] for r in recent)
    flagged = []
    for row in recent:
        if row["impressions"] < LOW_REACH_FRACTION * median:
            flagged.append((row, f"{row['impressions']} impressions, under half the recent median ({median:g})"))
        elif row["link"] == "demo" and row["impressions"] >= CLICKLESS_REACH and not row["clicks"]:
            flagged.append((row, f"{row['impressions']} impressions with the demo link and 0 clicks"))
    return flagged


def recommendations(rows):
    """Plain sentences, each backed by measured groups that passed MIN_GROUP. Never invents a number."""
    recs = []
    links = group(rows, "link")
    with_link, without = links.get("demo"), links.get("none")
    if with_link and with_link["comparable"] and without and without["comparable"]:
        recs.append(f"Linked posts: {with_link['clicks']} clicks from {with_link['impressions']} impressions over "
                    f"{with_link['measured']} posts (CTR {with_link['ctr']:.2%}); unlinked: {without['clicks']} clicks over "
                    f"{without['measured']} posts. Keep the link cadence if linked CTR stays above 0.")
    else:
        have = with_link["measured"] if with_link else 0
        recs.append(f"Not enough linked posts to judge click-through yet: {have} measured, {MIN_GROUP} needed. "
                    "Keep posting the demo link every second post.")
    for key, label in (("media", "media kind"), ("post_type", "post type")):
        groups = {k: g for k, g in group(rows, key).items() if g["comparable"] and k != "unknown"}
        if len(groups) >= 2:
            best = max(groups.items(), key=lambda kv: kv[1]["avg_impressions"])
            worst = min(groups.items(), key=lambda kv: kv[1]["avg_impressions"])
            if best[0] != worst[0]:
                recs.append(f"By {label}: '{best[0]}' averages {best[1]['avg_impressions']:.0f} impressions over "
                            f"{best[1]['measured']} posts, '{worst[0]}' {worst[1]['avg_impressions']:.0f} over "
                            f"{worst[1]['measured']}. Favour '{best[0]}'.")
        else:
            recs.append(f"By {label}: fewer than two {label}s have {MIN_GROUP}+ measured posts with known metadata; "
                        "no comparison yet.")
    return recs
