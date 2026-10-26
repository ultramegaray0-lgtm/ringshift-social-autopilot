"""Performance report built ONLY from numbers that were actually recorded.

Sources (all optional; whatever is missing is reported as missing, never guessed):
- data/metrics.json      written by analytics.py from Buffer
- data/post_log.json     written by autopilot.py (which post type / media each post used)
- data/x_metrics.json    imported from an X Analytics CSV export (manual fallback)
- data/followers.csv     follower counts you log by hand (the API does not provide them)

    python scripts/report.py                       print the report and write data/report.md
    python scripts/report.py import-csv FILE.csv   merge an X Analytics CSV export
    python scripts/report.py followers 1234        log today's follower count
    python scripts/report.py followers 1250 --date 2026-11-02
"""

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
METRICS_FILE = BASE_DIR / "data" / "metrics.json"
POST_LOG_FILE = BASE_DIR / "data" / "post_log.json"
X_METRICS_FILE = BASE_DIR / "data" / "x_metrics.json"
FOLLOWERS_FILE = BASE_DIR / "data" / "followers.csv"
REPORT_FILE = BASE_DIR / "data" / "report.md"

MIN_POSTS_FOR_COMPARISON = 3        # a group with fewer posts is listed but never ranked
KEY_METRICS = ["Impressions", "Reactions", "Reposts", "Comments", "Clicks"]

# X Analytics export column -> the metric name used in this project.
CSV_COLUMNS = {
    "impressions": "Impressions",
    "likes": "Reactions",
    "reposts": "Reposts",
    "replies": "Comments",
    "url link clicks": "Clicks",
    "url clicks": "Clicks",
    "link clicks": "Clicks",
    "bookmarks": "Bookmarks",
    "profile visits": "Profile visits",
    "new follows": "New follows",
    "engagements": "Engagements",
}


def _squash(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def text_key(text):
    return hashlib.sha256(_squash(text).encode("utf-8")).hexdigest()[:16]


def read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return fallback


def number(value):
    """A real number, or None. Blank, '-', and non-numeric cells are 'unknown', not zero."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    cleaned = str(value).strip().replace(",", "").rstrip("%")
    if cleaned in ("", "-", "--", "n/a", "N/A"):
        return None
    try:
        parsed = float(cleaned)
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


# ---- importing an X Analytics CSV ---------------------------------------------

def parse_x_csv(path):
    """Rows of an X Analytics 'posts' export as [{'text', 'metrics': {...}}]. Unknown columns are ignored."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = {h: h.strip().lower() for h in (reader.fieldnames or [])}
        text_column = next((h for h, low in headers.items() if low in ("post text", "tweet text", "text")), None)

        if text_column is None:
            raise ValueError("no 'Post text' column found; export posts (not account) analytics from X")

        rows = []
        for row in reader:
            text = (row.get(text_column) or "").strip()
            if not text:
                continue
            metrics = {}
            for header, low in headers.items():
                name = CSV_COLUMNS.get(low)
                value = number(row.get(header))
                if name and value is not None:
                    metrics[name] = value
            rows.append({"text": text, "metrics": metrics})

    return rows


def import_csv(path, store=None, known_texts=None):
    """Merge a CSV export into x_metrics.json, keyed by post text. Returns (matched, unmatched).

    A row counts as 'matched' when its text equals a post we queued; other rows (your own
    manual posts, replies) are still stored but flagged so reports can ignore them."""
    store = store or X_METRICS_FILE
    existing = read_json(store, {})
    if not isinstance(existing, dict):
        existing = {}

    known = {text_key(t) for t in (known_texts or [])}
    matched = unmatched = 0

    for row in parse_x_csv(path):
        key = text_key(row["text"])
        is_ours = key in known
        existing[key] = {"text": row["text"], "metrics": row["metrics"], "ours": is_ours,
                         "imported_at": datetime.now().isoformat(timespec="seconds")}
        matched += is_ours
        unmatched += not is_ours

    Path(store).parent.mkdir(parents=True, exist_ok=True)
    Path(store).write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return matched, unmatched


# ---- followers (manual) --------------------------------------------------------

def read_followers(path=None):
    path = Path(path or FOLLOWERS_FILE)
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            rows = [(r["date"], int(r["followers"])) for r in csv.DictReader(handle)]
    except (OSError, KeyError, ValueError):
        return []
    return sorted(rows)


def log_followers(count, when=None, path=None):
    path = Path(path or FOLLOWERS_FILE)
    when = when or date.today().isoformat()
    datetime.strptime(when, "%Y-%m-%d")                 # reject malformed dates
    rows = dict(read_followers(path))
    rows[when] = int(count)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "followers"])
        writer.writerows(sorted(rows.items()))
    return sorted(rows.items())


# ---- the report ----------------------------------------------------------------

def collect(metrics=None, post_log=None, x_metrics=None):
    """One record per post: text, Buffer metrics, imported X metrics, and what we know of it."""
    metrics = read_json(METRICS_FILE, []) if metrics is None else metrics
    post_log = read_json(POST_LOG_FILE, []) if post_log is None else post_log
    x_metrics = read_json(X_METRICS_FILE, {}) if x_metrics is None else x_metrics

    by_id = {e.get("buffer_id"): e for e in post_log if isinstance(e, dict)}
    by_text = {text_key(e.get("text")): e for e in post_log if isinstance(e, dict)}
    records = []

    for post in metrics if isinstance(metrics, list) else []:
        logged = by_id.get(post.get("id")) or by_text.get(text_key(post.get("text"))) or {}
        imported = (x_metrics.get(text_key(post.get("text"))) or {}).get("metrics", {})
        records.append({
            "id": post.get("id"), "text": post.get("text", ""), "status": post.get("status"),
            "sentAt": post.get("sentAt"), "buffer": post.get("metrics") or {}, "x": imported,
            "post_type": logged.get("post_type"), "media_type": logged.get("media_type"),
            "media_file": logged.get("media_file"), "media_tags": logged.get("media_tags") or [],
        })

    return records


def best_metrics(record):
    """Prefer the larger of the two sources for each metric: a CSV export is usually newer than Buffer's sync."""
    merged = dict(record["buffer"])
    for name, value in record["x"].items():
        if name not in merged or (number(value) or 0) > (number(merged[name]) or 0):
            merged[name] = value
    return merged


def has_evidence(record):
    """True only when the post was sent and reported a non-zero impression count."""
    return record["status"] in (None, "sent") and (number(best_metrics(record).get("Impressions")) or 0) > 0


def summarize(records, key):
    groups = {}
    for record in records:
        values = key(record)
        for value in (values if isinstance(values, list) else [values]):
            groups.setdefault(value or "unknown", []).append(record)

    rows = []
    for name, items in groups.items():
        evidence = [r for r in items if has_evidence(r)]
        totals = {m: sum(number(best_metrics(r).get(m)) or 0 for r in evidence) for m in KEY_METRICS}
        rows.append({"group": name, "posts": len(items), "with_data": len(evidence), "totals": totals})

    return sorted(rows, key=lambda r: (-r["totals"]["Impressions"], r["group"]))


def table(rows, label):
    lines = [f"| {label} | Posts | With data | " + " | ".join(KEY_METRICS) + " | Avg impressions |",
             "|---|---|---|" + "---|" * (len(KEY_METRICS) + 1)]
    for row in rows:
        n = row["with_data"]
        avg = f"{row['totals']['Impressions'] / n:.1f}" if n else "n/a"
        lines.append(f"| {row['group']} | {row['posts']} | {n} | "
                     + " | ".join(str(row["totals"][m]) for m in KEY_METRICS) + f" | {avg} |")
    return "\n".join(lines)


def build_report(records, followers):
    sent = [r for r in records if r["status"] == "sent"]
    scheduled = [r for r in records if r["status"] not in (None, "sent")]
    evidence = [r for r in records if has_evidence(r)]

    lines = ["# Ringshift performance report", "",
             f"Posts known: {len(records)} ({len(sent)} sent, {len(scheduled)} not sent yet). "
             f"Posts with real impression data: {len(evidence)}.", ""]

    if not records:
        lines += ["No posts found. Run `python scripts/analytics.py` (needs BUFFER_API_KEY) or the daily workflow first.", ""]
        return "\n".join(lines)

    if len(evidence) < MIN_POSTS_FOR_COMPARISON:
        lines += [f"**Not enough data to compare anything.** Fewer than {MIN_POSTS_FOR_COMPARISON} posts have "
                  "recorded impressions, so no post type or media is ranked. Zero-metric posts are "
                  "unmeasured, not failures.", ""]

    totals = {m: sum(number(best_metrics(r).get(m)) or 0 for r in evidence) for m in KEY_METRICS}
    lines += ["## Totals (posts with data only)", "",
              ", ".join(f"{m}: {totals[m]}" for m in KEY_METRICS), ""]

    for title, label, key in (
        ("By post type", "Post type", lambda r: r["post_type"]),
        ("By media type", "Media", lambda r: r["media_type"]),
        ("By mechanic on screen", "Mechanic", lambda r: r["media_tags"] or ["untagged"]),
    ):
        rows = summarize(records, key)
        lines += [f"## {title}", "", table(rows, label), ""]
        small = [r["group"] for r in rows if r["with_data"] < MIN_POSTS_FOR_COMPARISON]
        if small:
            lines += [f"_Fewer than {MIN_POSTS_FOR_COMPARISON} posts with data, so no conclusion: {', '.join(small)}._", ""]

    if evidence:
        lines += ["## Posts with data", "", "| Date | Impressions | Reactions | Reposts | Text |", "|---|---|---|---|---|"]
        for r in sorted(evidence, key=lambda r: -(number(best_metrics(r).get("Impressions")) or 0)):
            m = best_metrics(r)
            text = re.sub(r"\s+", " ", r["text"])[:70].replace("|", "/")
            lines.append(f"| {(r['sentAt'] or '')[:10]} | {m.get('Impressions', '-')} | {m.get('Reactions', '-')} "
                         f"| {m.get('Reposts', '-')} | {text} |")
        lines.append("")

    lines += ["## Followers (logged by hand)", ""]
    if len(followers) >= 2:
        (d0, f0), (d1, f1) = followers[0], followers[-1]
        lines += [f"{d0}: {f0} -> {d1}: {f1} ({f1 - f0:+d}). This is the account total, not caused by any one post.", ""]
    elif followers:
        lines += [f"{followers[0][0]}: {followers[0][1]}. Log it again later to see a change.", ""]
    else:
        lines += ["Nothing logged. Run `python scripts/report.py followers <count>` now and then; Buffer does not provide it.", ""]

    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Report real post performance.")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("import-csv", help="merge an X Analytics posts CSV export")
    p.add_argument("file")
    p = sub.add_parser("followers", help="log a follower count")
    p.add_argument("count", type=int)
    p.add_argument("--date", help="YYYY-MM-DD (default today)")
    args = parser.parse_args(argv)

    try:
        if args.command == "import-csv":
            known = [e.get("text") for e in read_json(POST_LOG_FILE, []) if isinstance(e, dict)]
            known += [r.get("text") for r in read_json(METRICS_FILE, []) if isinstance(r, dict)]
            matched, other = import_csv(args.file, known_texts=known)
            print(f"Imported: {matched} matched posts we queued, {other} other posts.")
            return 0

        if args.command == "followers":
            rows = log_followers(args.count, args.date)
            print(f"Logged. {len(rows)} entries in {FOLLOWERS_FILE}.")
            return 0
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    report = build_report(collect(), read_followers())
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(report + "\n", encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
