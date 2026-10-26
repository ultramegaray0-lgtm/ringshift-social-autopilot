# Analytics and growth workflow

## Numbers
`python scripts/analytics.py` (daily in Actions) writes `data/analytics.md` and `data/metrics.json`.
`python scripts/report.py` writes `data/report.md`: totals, then breakdowns by post type, media type, and mechanic on screen.

* Posts with zero or no metrics are **unmeasured**, not failures.
* A group needs 3+ measured posts before the report says anything about it.
* Buffer's metrics can lag. Export posts from X Analytics (CSV) and merge them: `python scripts/report.py import-csv posts.csv`
  (the larger value per metric wins; unknown cells are ignored, never zero).
* Followers are not available from Buffer: `python scripts/report.py followers 1234` now and then. The report shows the
  account-level change and says it isn't attributable to a single post.

## Outreach (always human)
`scripts/outreach.py` keeps `data/outreach.csv` and writes drafts into `outreach/drafts/`.

```
python scripts/outreach.py discover "indie puzzle game" --max 10    # optional, YouTube search; adds leads as "found"
python scripts/outreach.py add "Some Channel" --platform youtube --url https://... --why "plays logic puzzles"
python scripts/outreach.py draft L001           # unsent draft with [BRACKETS] you must fill in
python scripts/outreach.py mark L001 approved   # you read and edited it
# send it yourself on the platform, then:
python scripts/outreach.py mark L001 contacted --note "sent via channel email"
python scripts/outreach.py due                  # contacted 14+ days ago, no reply logged
```
Guard rails: no sending code exists in the tool; `contacted` requires `approved`, which requires a draft; declined/skipped
people are never drafted again; nobody contacted in the last 90 days is redrafted; duplicates are refused.
Follow each platform's rules (subreddit self-promotion rules, Discord server rules, YouTube/X terms). No mass DMs, no
auto-follow, no unsolicited public replies. Start with 5-10 well-chosen people, and personalise every message.
