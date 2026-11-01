"""Find prospects. Read-only: this module has no way to send, post, follow or message anyone.

Sources (all free):
  * YouTube Data API v3: recent videos matching config.youtube.queries, then the channels' public stats and
    description, their latest uploads (playlistItems.list, 1 unit) and those uploads' view/comment counts
    (videos.list, 1 unit per 50). A business email is used ONLY if the creator wrote it in the channel description,
    or labelled it as their business contact in the descriptions of several of their own latest videos. The API does
    not expose the About page's hidden "View email address" (CAPTCHA), and this code never tries to get it.
    YouTube's robots.txt disallows its RSS feeds (/feeds/videos.xml), so channel activity is read through the API only.
  * YouTube channels you researched and listed in sources.json (by channel id or @handle), checked the same way.
  * RSS/Atom feeds of blogs you list in sources.json.
  * Submission pages of curators, newsletters and browser-game directories you list in sources.json. Only entries
    marked enabled, with a verified route and eligibility 'meets' or 'likely', are used; the rest are reported as held.
Every contact address must be written out literally on the page (or description) it is credited to, in this run.
"""

import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from urllib.parse import urlsplit

from . import core, scoring, settings, store, web

YOUTUBE_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_CHANNELS = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_PLAYLIST_ITEMS = "https://www.googleapis.com/youtube/v3/playlistItems"
YOUTUBE_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
MAX_FEED_BYTES = 2_000_000
SEARCH_UNITS, CHANNELS_UNITS = 100, 1          # YouTube Data API quota cost per call (search.list, channels.list)
LIST_UNITS = 1                                 # playlistItems.list and videos.list
NO_KEY_ACTIVITY = ("channel activity and contacts can only be re-checked through the YouTube Data API "
                   "(YouTube's robots.txt blocks its RSS feeds); set YOUTUBE_API_KEY")
# A creator's own business contact is labelled as such just before the address ("Business inquiries: ..."). An
# address introduced as someone else's (a game's developer or publisher, a support desk) is never theirs to use.
BUSINESS_LABEL = re.compile(r"(business|inquir|enquir|sponsor|collab|partnership|press|work with me|contact me|"
                            r"reach me|email me|mail me|\bcontact\s*[:\-]|contacto|negocios)", re.I)
THIRD_PARTY_LABEL = re.compile(r"(developer|\bdevs?\b|publisher|studio|support|made by|created by|game by|"
                               r"\bby @|credits?|music by|art by)", re.I)


class DiscoveryReport:
    def __init__(self):
        self.new, self.updated, self.qualified, self.rejected = [], [], [], 0
        self.errors, self.fatal = [], []
        self.sources = {}
        self.held = []          # seeds not used this run, each with the reason (eligibility uncertain, disabled...)

    def error(self, source, message, fatal=False):
        (self.fatal if fatal else self.errors).append(f"{source}: {message}")

    def as_dict(self):
        return {"new": self.new, "updated": self.updated, "qualified": self.qualified, "rejected": self.rejected,
                "errors": self.errors, "fatal": self.fatal, "sources": self.sources, "held": self.held}


# ---- ingest: dedup, suppression, qualification ---------------------------------------------------------------

def _contact_email(candidate):
    return (candidate.get("contact") or {}).get("email")


def ingest(state, candidate, config, now, report):
    """Add or refresh one candidate. Suppressed people are never (re-)added."""
    keys = {candidate["canonical"], *candidate.get("aliases", [])}
    email = _contact_email(candidate)
    if any(store.is_suppressed(state, canonical=k) for k in keys) or (email and store.is_suppressed(state, email=email)):
        store.reject(state, now, name=candidate["name"], url=candidate["url"], reason="on the suppression list",
                     source=candidate["evidence"]["source"])
        report.rejected += 1
        return None

    points, reasons, disqualifiers = scoring.score(candidate, config["keywords"], now,
                                                   max_age_days=config["limits"]["evidence_max_age_days"])
    existing = store.find_duplicate(state, keys, email)
    if existing is None and disqualifiers:
        store.reject(state, now, name=candidate["name"], url=candidate["url"], reason="; ".join(disqualifiers),
                     source=candidate["evidence"]["source"])
        report.rejected += 1
        return None

    if existing is None:
        prospect = {
            "id": core.prospect_id(candidate["canonical"]), "name": candidate["name"],
            "category": candidate["category"], "url": candidate["url"], "canonical": candidate["canonical"],
            "aliases": sorted(set(candidate.get("aliases", [])) - {candidate["canonical"]}),
            "evidence": candidate["evidence"], "contact": candidate.get("contact") or {},
            "score": points, "score_reasons": reasons, "disqualifiers": disqualifiers,
            "first_discovered": core.iso(now), "updated": core.iso(now), "status": core.DISCOVERED,
            "last_contact": None, "interest": None, "history": [], "stats": candidate.get("stats", {}),
            "channel_text": candidate.get("channel_text", ""), "seed_bonus": candidate.get("seed_bonus", 0),
            "source_meta": candidate.get("source_meta") or {}, "research": candidate.get("research") or {},
        }
        if prospect["id"] in state["prospects"]:          # same hash, different alias set: treat as duplicate
            existing = state["prospects"][prospect["id"]]
        else:
            state["prospects"][prospect["id"]] = prospect
            report.new.append(prospect["id"])
    if existing is not None:
        prospect = existing
        if existing["status"] in core.TERMINAL:
            return None
        if existing.get("source_meta") and not candidate.get("source_meta"):
            # A search result for an organisation you listed (an alias of your seed): your record and its official
            # route stay as they are; the search result only adds its keys, so it is never contacted separately.
            existing["aliases"] = sorted(set(existing.get("aliases", [])) | keys - {existing["canonical"]})
        else:
            newer = core.parse_time(candidate["evidence"].get("date"))
            older = core.parse_time(existing["evidence"].get("date"))
            if newer and (older is None or newer > older) and not disqualifiers:
                existing["evidence"] = candidate["evidence"]
            if candidate.get("contact", {}).get("verified_at") and (
                    not existing.get("contact") or existing["contact"].get("source_kind") != "owner"):
                existing["contact"] = candidate["contact"]
            existing["aliases"] = sorted(set(existing.get("aliases", [])) | keys - {existing["canonical"]})
            existing["score"], existing["score_reasons"], existing["disqualifiers"] = points, reasons, disqualifiers
            existing["stats"] = candidate.get("stats", existing.get("stats", {}))
            existing["channel_text"] = candidate.get("channel_text") or existing.get("channel_text", "")
            existing["seed_bonus"] = max(int(candidate.get("seed_bonus") or 0), int(existing.get("seed_bonus") or 0))
            if candidate.get("source_meta"):
                existing["source_meta"] = candidate["source_meta"]
            if candidate.get("research"):
                existing["research"] = candidate["research"]
        existing["updated"] = core.iso(now)
        if prospect["id"] not in report.new:
            report.updated.append(existing["id"])
    if qualify(prospect, config, now):
        report.qualified.append(prospect["id"])
    return prospect


def contact_problem(prospect):
    contact = prospect.get("contact") or {}
    if contact.get("email"):
        try:
            core.validate_email(contact["email"])
        except core.OutreachError as error:
            return f"contact address rejected: {error}"
        if contact.get("source_kind") not in ("youtube-description", "youtube-video-description", "web-page", "owner",
                                           "research"):
            return "contact address has no recorded public source"
        if not contact.get("source_url") or not contact.get("verified_at"):
            return "contact address was not verified on its source"
        return None
    if contact.get("submission_url"):
        return None if contact.get("verified_at") else "submission page was not checked"
    return "no public business email or submission route"


def qualify(prospect, config, now):
    """DISCOVERED <-> QUALIFIED only. Later statuses are never changed here."""
    if prospect["status"] not in (core.DISCOVERED, core.QUALIFIED):
        return False
    problems = list(prospect.get("disqualifiers") or [])
    if prospect.get("owner_skipped"):
        problems.append("you rejected a draft for this prospect")
    if prospect["score"] < config["limits"]["min_score"]:
        problems.append(f"score {prospect['score']} below {config['limits']['min_score']}")
    route = contact_problem(prospect)
    if route:
        problems.append(route)
    if problems:
        prospect["not_qualified"] = problems
        store.set_status(prospect, core.DISCOVERED, now, "; ".join(problems))
        return False
    prospect.pop("not_qualified", None)
    store.set_status(prospect, core.QUALIFIED, now, "relevant, recent and reachable")
    return True


# ---- YouTube Data API ------------------------------------------------------------------------------------------

def _queries(settings):
    """config.youtube.queries: plain strings (ranked by relevance) or {"q": ..., "order": "date"|"relevance"}."""
    out = []
    for entry in settings.get("queries", []):
        if isinstance(entry, str) and entry.strip():
            out.append((entry.strip(), "relevance"))
        elif isinstance(entry, dict) and str(entry.get("q") or "").strip():
            order = entry.get("order") if entry.get("order") in ("date", "relevance") else "relevance"
            out.append((entry["q"].strip(), order))
    return out


def youtube_candidates(config, now, fetcher, api_key, report, quota=None):
    settings = config["youtube"]
    after = core.iso(now - timedelta(days=int(settings["published_within_days"])))
    quota = quota or Quota(settings.get("daily_quota_budget"))
    videos_by_channel = {}
    for query, order in _queries(settings):
        if not quota.take(SEARCH_UNITS):
            report.sources["youtube_quota_note"] = (f"stopped searching at the run's quota budget of {quota.budget} "
                                                    "units (config youtube.daily_quota_budget)")
            break
        try:
            payload = fetcher.google_api(YOUTUBE_SEARCH, {
                "part": "snippet", "type": "video", "q": query, "order": order, "publishedAfter": after,
                "maxResults": max(1, min(int(settings["max_results_per_query"]), 50)), "videoCategoryId": "20",
                "relevanceLanguage": settings.get("relevance_language") or "en", "safeSearch": "strict",
                "key": api_key})
        except core.QuotaError as error:
            report.error("youtube", str(error))
            break
        except core.AuthError as error:
            report.error("youtube", str(error), fatal=True)
            return []
        except core.SourceError as error:
            report.error("youtube", f"search '{query}': {error}")
            continue
        for item in payload.get("items", []):
            snippet = item.get("snippet") or {}
            video_id = (item.get("id") or {}).get("videoId")
            channel_id = snippet.get("channelId")
            try:
                video_url, channel_url = core.youtube_video_url(video_id), core.youtube_channel_url(channel_id)
            except core.OutreachError:
                continue
            info = videos_by_channel.setdefault(channel_id, {"channel_url": channel_url, "videos": []})
            if any(v["url"] == video_url for v in info["videos"]):
                continue                                    # the same video found by two queries
            info["videos"].append({
                "url": video_url, "title": core.clean_text(snippet.get("title"), 200),
                "date": snippet.get("publishedAt"), "excerpt": core.clean_text(snippet.get("description"), 300),
                "source": f"youtube search: {query}"})
    report.sources["youtube_channels_seen"] = len(videos_by_channel)

    candidates = []
    ids = list(videos_by_channel)
    for start in range(0, len(ids), 50):
        quota.used += CHANNELS_UNITS              # always allowed: without it the searches above were wasted
        try:
            payload = fetcher.google_api(YOUTUBE_CHANNELS, {"part": "snippet,statistics",
                                                            "id": ",".join(ids[start:start + 50]), "key": api_key})
        except core.QuotaError as error:
            report.error("youtube", str(error))
            break
        except core.AuthError as error:
            report.error("youtube", str(error), fatal=True)
            return []
        except core.SourceError as error:
            report.error("youtube", f"channel details: {error}")
            continue
        for channel in payload.get("items", []):
            info = videos_by_channel.get(channel.get("id"))
            if not info:
                continue
            candidate = _youtube_candidate(channel, info, config, now)
            if candidate:
                candidates.append(candidate)
    report.sources["youtube_quota_units"] = quota.used
    return candidates


def _best_evidence(videos, keywords):
    def rank(video):
        hits = len(scoring.hits(f"{video['title']} {video['excerpt']}", keywords["puzzle"] + keywords["indie"]))
        when = core.parse_time(video.get("date"))
        return (hits > 0, hits, when.timestamp() if when else 0)
    best = max(videos, key=rank)
    return {k: v for k, v in best.items() if k != "text"}     # full descriptions are never stored as evidence


def _youtube_candidate(channel, info, config, now):
    snippet, stats = channel.get("snippet") or {}, channel.get("statistics") or {}
    name = core.clean_text(snippet.get("title"), 100)
    if not name:
        return None
    hidden = bool(stats.get("hiddenSubscriberCount"))
    subscribers = None if hidden or stats.get("subscriberCount") is None else int(stats["subscriberCount"])
    videos = int(stats["videoCount"]) if str(stats.get("videoCount", "")).isdigit() else None
    description = str(snippet.get("description") or "")
    custom = str(snippet.get("customUrl") or "")
    aliases = [f"youtube:handle:{custom.lower()}"] if re.fullmatch(r"@[\w.\-]+", custom) else []
    contact = {}
    emails = core.find_emails(description)
    if emails:
        email, excerpt = emails[0]
        contact = {"email": email, "source_kind": "youtube-description", "source_url": info["channel_url"],
                   "excerpt": excerpt, "verified_at": core.iso(now), "verified_by": "youtube-api channels.list"}
    return {"name": name, "category": "youtube", "url": info["channel_url"], "channel_id": channel.get("id"),
            "canonical": core.canonical_url(info["channel_url"]), "aliases": aliases,
            "evidence": _best_evidence(info["videos"], config["keywords"]), "contact": contact,
            "channel_text": core.clean_text(description, 1000), "subscribers": subscribers, "videos": videos,
            "stats": {"subscribers": subscribers, "videos": videos, "checked_at": core.iso(now)}}


def size_filter(candidate, config):
    """Reason to skip a channel by size, or None. Size never qualifies anyone by itself."""
    subs = candidate.get("subscribers")
    low, high = config["youtube"].get("min_subscribers"), config["youtube"].get("max_subscribers")
    if subs is not None and low and subs < low:
        return f"fewer than {low} subscribers"
    if subs is not None and high and subs > high:
        return f"more than {high:,} subscribers"
    return None


def business_emails(text):
    """[(email, excerpt)] the author labels as their own business contact in `text`.

    The address must be written out literally (core.find_emails: no de-obfuscation) and a business label must appear in
    the 60 characters before it. Nothing from just before that label up to the address may say it belongs to someone
    else ("Publisher's press contact:", "Business inquiries for the developer:")."""
    text = str(text or "")
    lower = text.lower()
    found = []
    for email, excerpt in core.find_emails(text):
        index = lower.find(email)
        if index < 0:
            continue
        window_start = max(0, index - 60)
        labels = list(BUSINESS_LABEL.finditer(text, window_start, index))
        if not labels:
            continue
        claim = text[max(0, labels[-1].start() - 25): index]
        if THIRD_PARTY_LABEL.search(claim):
            continue
        found.append((email, excerpt))
    return found


def feed_business_contact(entries, now, *, min_videos):
    """A contact dict for the address the creator labels as their business contact in at least `min_videos` of the
    feed's video descriptions (newest first), or {}. Several videos = their standing contact, not a one-off mention."""
    seen = {}
    for entry in sorted(entries, key=lambda e: e.get("date") or "", reverse=True):
        for email, excerpt in business_emails(entry.get("text") or entry.get("excerpt")):
            record = seen.setdefault(email, {"count": 0, "url": entry["url"], "excerpt": excerpt})
            record["count"] += 1
    best = max(seen.items(), key=lambda item: item[1]["count"], default=None)
    if not best or best[1]["count"] < max(1, int(min_videos)):
        return {}
    email, record = best
    return {"email": email, "source_kind": "youtube-video-description", "source_url": record["url"],
            "excerpt": record["excerpt"], "verified_at": core.iso(now),
            "verified_by": f"youtube channel feed: labelled as a business contact in {record['count']} of the latest "
                           f"{len(entries)} video descriptions"}


def recent_uploads(entries, now, days=90):
    return sum(1 for e in entries if (when := core.parse_time(e.get("date"))) and 0 <= core.days_between(when, now) <= days)


class Quota:
    """YouTube Data API units spent this run, shared by search, channel checks and upload checks."""

    def __init__(self, budget):
        self.budget, self.used = int(budget or 2000), 0

    def take(self, units):
        if self.used + units > self.budget:
            return False
        self.used += units
        return True


def channel_uploads(fetcher, channel_id, api_key, max_results=15):
    """The channel's latest uploads, newest first: [{title, url, date, excerpt, text, video_id}], via the uploads
    playlist (playlistItems.list, 1 unit). Each entry keeps up to 5,000 characters of the creator's own description so
    a labelled business contact can be found; descriptions are never stored as evidence."""
    core.youtube_channel_url(channel_id)                                     # validates the id
    payload = fetcher.google_api(YOUTUBE_PLAYLIST_ITEMS, {
        "part": "snippet,contentDetails", "playlistId": "UU" + channel_id[2:],
        "maxResults": max(1, min(int(max_results), 50)), "key": api_key})
    entries = []
    for item in payload.get("items", []):
        snippet, details = item.get("snippet") or {}, item.get("contentDetails") or {}
        video_id = details.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
        try:
            url = core.youtube_video_url(video_id)
        except core.OutreachError:
            continue
        when = core.parse_time(details.get("videoPublishedAt") or snippet.get("publishedAt"))
        description = str(snippet.get("description") or "")
        entries.append({"title": core.clean_text(snippet.get("title"), 200), "url": url, "video_id": video_id,
                        "date": core.iso(when) if when else None, "excerpt": core.clean_text(description, 300),
                        "text": core.clean_text(description, 5000)})
    entries.sort(key=lambda e: e.get("date") or "", reverse=True)
    return entries


def _median(values):
    values = sorted(v for v in values if isinstance(v, int))
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) // 2


def upload_engagement(fetcher, entries, api_key, now, sample=10):
    """Median views and comments of the latest uploads that are at least 3 days old (videos.list, 1 unit), or {}."""
    settled = [e for e in entries if (when := core.parse_time(e.get("date"))) and core.days_between(when, now) >= 3]
    ids = [e["video_id"] for e in settled[:sample] if e.get("video_id")]
    if not ids:
        return {}
    payload = fetcher.google_api(YOUTUBE_VIDEOS, {"part": "statistics", "id": ",".join(ids), "key": api_key})
    views, comments = [], []
    for item in payload.get("items", []):
        stats = item.get("statistics") or {}
        if str(stats.get("viewCount", "")).isdigit():
            views.append(int(stats["viewCount"]))
        if str(stats.get("commentCount", "")).isdigit():
            comments.append(int(stats["commentCount"]))
    if not views:
        return {}
    return {"median_views": _median(views), "median_comments": _median(comments), "sampled_videos": len(views)}


def engagement_rate(stats):
    """Median recent views per subscriber (0.12 = a typical new video reaches 12% of the subscriber count), or None."""
    subs, views = (stats or {}).get("subscribers"), (stats or {}).get("median_views")
    if not isinstance(subs, int) or not isinstance(views, int) or subs <= 0:
        return None
    return round(views / subs, 3)


def apply_uploads(candidate, entries, engagement, config, now, *, find_contact=True):
    """Fold a channel's latest uploads into a candidate: activity, engagement, better evidence, labelled contact."""
    stats = candidate.setdefault("stats", {})
    candidate["recent_uploads"] = stats["recent_uploads"] = recent_uploads(entries, now)
    latest = entries[0]["date"] if entries else None
    stats["latest_upload"] = latest
    stats.update(engagement or {})
    rate = engagement_rate({**stats, "subscribers": candidate.get("subscribers", stats.get("subscribers"))})
    if rate is not None:
        stats["engagement_rate"] = rate
    relevant = [e for e in entries if scoring.hits(f"{e['title']} {e['excerpt']}",
                                                   config["keywords"]["puzzle"] + config["keywords"]["indie"])]
    if relevant:
        newest = max(relevant, key=lambda e: e.get("date") or "")
        current = core.parse_time((candidate.get("evidence") or {}).get("date"))
        if current is None or (core.parse_time(newest["date"]) or current) > current:
            candidate["evidence"] = {k: v for k, v in newest.items() if k not in ("text", "video_id")}
            candidate["evidence"]["source"] = "youtube uploads (API)"
    if find_contact and not _contact_email(candidate):
        contact = feed_business_contact(entries, now,
                                        min_videos=int(config["youtube"].get("business_email_min_videos", 2)))
        if contact:
            candidate["contact"] = contact
            return True
    return False


def enrich_channels(candidates, config, now, fetcher, api_key, quota, report):
    """For the most relevant, smallest channels found by search: latest uploads (activity, newer evidence, a labelled
    business contact when the channel description had none) and engagement. Bounded by youtube.feed_checks_per_run
    and the run's quota budget; a failure only skips the enrichment of that channel."""
    settings = config["youtube"]
    limit = int(settings.get("feed_checks_per_run", 15))
    words = config["keywords"]["puzzle"] + config["keywords"]["indie"]

    def priority(candidate):
        evidence = candidate["evidence"]
        relevant = bool(scoring.hits(f"{evidence.get('title', '')} {evidence.get('excerpt', '')}", words))
        subs = candidate.get("subscribers")
        return (not relevant, subs if subs is not None else 10 ** 9)

    checked = contacts = 0
    for candidate in sorted(candidates, key=priority)[:max(0, limit)]:
        if not candidate.get("channel_id") or not quota.take(2 * LIST_UNITS):
            continue
        try:
            entries = channel_uploads(fetcher, candidate["channel_id"], api_key)
            engagement = upload_engagement(fetcher, entries, api_key, now)
        except core.QuotaError as error:
            report.error("youtube", str(error))
            break
        except core.OutreachError:
            continue
        checked += 1
        if apply_uploads(candidate, entries, engagement, config, now):
            contacts += 1
    report.sources["youtube_channels_checked"] = checked
    report.sources["youtube_contacts_from_video_descriptions"] = contacts


# ---- feeds (blogs and seeded YouTube channels) -----------------------------------------------------------------

def _strip_ns(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_text, keep_text=False):
    """Entries of an RSS 2.0 or Atom feed: [{title, url, date, excerpt}] (+ 'text', the description up to 5,000
    characters, with keep_text). Refuses DTDs/entities."""
    if len(xml_text) > MAX_FEED_BYTES or re.search(r"<!(DOCTYPE|ENTITY)", xml_text[:5000], re.I):
        raise core.SourceError("feed is too large or declares a DTD/entities; skipped")
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError:
        raise core.SourceError("feed is not valid XML") from None
    entries = []
    for node in root.iter():
        if _strip_ns(node.tag) not in ("item", "entry"):
            continue
        fields = {}
        for child in node.iter():
            tag = _strip_ns(child.tag)
            if tag == "title" and "title" not in fields:
                fields["title"] = child.text
            elif tag == "link":
                href = child.get("href") or (child.text or "").strip()
                if href and (child.get("rel") in (None, "alternate")) and "url" not in fields:
                    fields["url"] = href
            elif tag in ("pubDate", "published", "updated", "date") and "date" not in fields:
                fields["date"] = child.text
            elif tag in ("description", "summary", "content") and "excerpt" not in fields:
                fields["excerpt"] = web.html_to_text(child.text or "")
        try:
            url = core.validate_url(fields.get("url"))
        except core.OutreachError:
            continue
        when = core.parse_time(fields.get("date"))
        entry = {"title": core.clean_text(fields.get("title"), 200), "url": url,
                 "date": core.iso(when) if when else None, "excerpt": core.clean_text(fields.get("excerpt"), 300)}
        if keep_text:
            entry["text"] = core.clean_text(fields.get("excerpt"), 5000)
        entries.append(entry)
    return entries


def verify_email_on_page(fetcher, email, source_url, now):
    """Proof that `email` is published on `source_url` right now, or SourceError."""
    email = core.validate_email(email)
    final, html = fetcher.page(source_url)
    text = web.html_to_text(html)
    if email not in web.mailto_addresses(html) and email not in text.lower():
        raise core.SourceError(f"the address is not written on {source_url} (it may be hidden or obfuscated)")
    index = text.lower().find(email)
    excerpt = core.clean_text(text[max(0, index - 40): index + len(email) + 20], 120) if index >= 0 else "mailto link"
    return {"email": email, "source_kind": "web-page", "source_url": core.validate_url(source_url),
            "excerpt": excerpt, "verified_at": core.iso(now), "verified_by": "page fetch"}


def _seed_contact(seed, fetcher, now, report, label):
    if seed.get("contact_email"):
        try:
            return verify_email_on_page(fetcher, seed["contact_email"], seed.get("contact_source_url") or "", now)
        except core.OutreachError as error:
            report.error(label, f"contact not used: {error}")
    return {}


def _source_meta(seed):
    eligibility = seed.get("eligibility") or {}
    return {"source_type": seed.get("source_type"), "status": eligibility.get("status"),
            "last_checked": seed.get("last_checked"), "fit": core.clean_text(seed.get("fit"), 300),
            "uncertain": [core.clean_text(u, 200) for u in eligibility.get("uncertain", [])][:5],
            "route_kind": seed.get("route_kind")}


def _seed_aliases(seed):
    aliases = []
    for alias in seed.get("aliases", []):
        try:
            aliases.append(core.canonical_url(alias))
        except core.OutreachError:
            continue
    return aliases


def usable_seeds(sources, kind, report):
    """Seeds of `kind` that may be used this run. Others are reported (held or invalid) and skipped."""
    problems = settings.source_problems(sources)
    usable = []
    for seed in sources.get(kind, []):
        label = settings._seed_label(kind, seed)
        if label in problems:
            report.error(label, "skipped: " + "; ".join(problems[label]))
            continue
        if not seed.get("enabled"):
            status = (seed.get("eligibility") or {}).get("status")
            report.held.append(f"{seed.get('name')}: held (eligibility {status}; "
                               f"{'; '.join(u.rstrip('. ') for u in (seed.get('eligibility') or {}).get('uncertain', [])[:2]) or 'disabled'})")
            continue
        usable.append(seed)
    return usable


def feed_candidates(sources, config, now, fetcher, report):
    candidates = []
    for seed in usable_seeds(sources, "feeds", report):
        label = f"feed {seed.get('name', '?')}"
        try:
            site = core.validate_url(seed["site_url"])
            _, xml_text = fetcher.page(seed["feed_url"], accept="application/rss+xml, application/atom+xml, */*")
            entries = parse_feed(xml_text)
        except (core.OutreachError, KeyError) as error:
            report.error(label, str(error) if not isinstance(error, KeyError) else f"missing field {error}")
            continue
        relevant = [e for e in entries if scoring.hits(f"{e['title']} {e['excerpt']}",
                                                        config["keywords"]["puzzle"] + config["keywords"]["indie"])]
        if not relevant:
            continue
        best = max(relevant, key=lambda e: (core.parse_time(e["date"]) or core.parse_time("1970-01-01")).timestamp())
        candidates.append({"name": core.clean_text(seed["name"], 100), "category": seed.get("category", "blog"),
                           "url": site, "canonical": core.canonical_url(site), "aliases": _seed_aliases(seed),
                           "evidence": {**best, "source": f"feed {seed['feed_url']}"},
                           "contact": _seed_contact(seed, fetcher, now, report, label),
                           "channel_text": core.clean_text(seed.get("about"), 500), "seed_bonus": 10,
                           "source_meta": _source_meta(seed)})
    return candidates


# ---- YouTube channels you researched (sources.json youtube_channels) -------------------------------------------

RESEARCH_CONTACT_MAX_DAYS = 45      # an address seen by research is used without an API re-check for this long only


def research_meta(seed):
    """The researched extras of a seed: similar games it covered, a one-sentence angle, language, submissions."""
    coverage = []
    for item in seed.get("similar_coverage") or []:
        if not isinstance(item, dict):
            continue
        try:
            url = core.validate_url(item.get("url"))
        except core.OutreachError:
            continue
        title = core.clean_text(item.get("title"), 160)
        if title:
            coverage.append({"title": title, "url": url, "date": str(item.get("date") or "")[:10] or None,
                             "game": core.clean_text(item.get("game"), 80) or None})
    return {"similar_coverage": coverage[:5], "angle": core.clean_text(seed.get("angle"), 260) or None,
            "language": str(seed.get("language") or "en").lower()[:5],
            "accepts_submissions": bool(seed.get("accepts_submissions")),
            "verification": "youtube-api"}


def research_evidence(seed):
    """The dated video recorded by research, as evidence (used when the API can't be asked), or None."""
    evidence = seed.get("evidence") if isinstance(seed.get("evidence"), dict) else {}
    try:
        url = core.validate_url(evidence.get("url"))
    except core.OutreachError:
        return None
    when, title = core.parse_time(evidence.get("date")), core.clean_text(evidence.get("title"), 200)
    if not when or not title:
        return None
    return {"url": url, "title": title, "date": core.iso(when), "excerpt": core.clean_text(evidence.get("excerpt"), 300),
            "source": f"research record (sources.json, checked {seed.get('last_checked')})"}


def seed_channel_url(seed):
    if seed.get("channel_id"):
        return core.youtube_channel_url(seed["channel_id"])
    handle = str(seed.get("handle") or "")
    if not re.fullmatch(r"@[\w.\-]{3,30}", handle):
        raise core.OutreachError(f"youtube seed needs channel_id or @handle ({seed.get('name', '?')})")
    return f"https://www.youtube.com/{handle}"


def _is_youtube(url):
    host = (urlsplit(str(url or "")).hostname or "").lower()
    return host == "youtu.be" or host.removeprefix("www.") in core.YOUTUBE_HOSTS


def _channel_numbers(channel):
    stats = channel.get("statistics") or {}
    hidden = bool(stats.get("hiddenSubscriberCount"))
    subs = None if hidden or not str(stats.get("subscriberCount", "")).isdigit() else int(stats["subscriberCount"])
    videos = int(stats["videoCount"]) if str(stats.get("videoCount", "")).isdigit() else None
    return subs, videos


def _resolve_channels(seeds, fetcher, api_key, quota, report):
    """{channel_id or ('handle', '@x'): channels.list item}. Ids are batched (1 unit per 50); a handle costs 1 unit."""
    resolved = {}
    ids = [s["channel_id"] for s in seeds if s.get("channel_id")]
    for start in range(0, len(ids), 50):
        quota.used += CHANNELS_UNITS
        try:
            payload = fetcher.google_api(YOUTUBE_CHANNELS, {"part": "snippet,statistics",
                                                            "id": ",".join(ids[start:start + 50]), "key": api_key})
        except core.QuotaError as error:
            report.error("youtube", str(error))
            return resolved
        except core.AuthError as error:
            report.error("youtube", str(error), fatal=True)
            return None
        except core.SourceError as error:
            report.error("youtube", f"researched channels: {error}")
            continue
        for item in payload.get("items", []):
            resolved[item.get("id")] = item
    for seed in seeds:
        if seed.get("channel_id") or not seed.get("handle") or not quota.take(CHANNELS_UNITS):
            continue
        try:
            payload = fetcher.google_api(YOUTUBE_CHANNELS, {"part": "snippet,statistics", "forHandle": seed["handle"],
                                                            "key": api_key})
        except core.QuotaError as error:
            report.error("youtube", str(error))
            break
        except core.AuthError as error:
            report.error("youtube", str(error), fatal=True)
            return None
        except core.SourceError as error:
            report.error("youtube", f"researched channel {seed['handle']}: {error}")
            continue
        items = payload.get("items") or []
        if items:
            resolved[("handle", seed["handle"].lower())] = items[0]
    return resolved


def _verified_seed_email(seed, description, entries, fetcher, now):
    """Contact proof for the address you listed: written in the channel description, labelled as a business contact
    in one of the latest video descriptions, or written on a non-YouTube page you named. SourceError otherwise."""
    email = core.validate_email(seed["contact_email"])
    match = next(((m, x) for m, x in core.find_emails(description) if m == email), None)
    if match:
        return {"email": email, "source_kind": "youtube-description", "source_url": seed_channel_url(seed),
                "excerpt": match[1], "verified_at": core.iso(now), "verified_by": "youtube-api channels.list"}
    hits = [(e, x) for e in entries for m, x in business_emails(e.get("text")) if m == email]
    if hits:
        entry, excerpt = hits[0]
        return {"email": email, "source_kind": "youtube-video-description", "source_url": entry["url"],
                "excerpt": excerpt, "verified_at": core.iso(now),
                "verified_by": f"youtube-api playlistItems: labelled as a business contact in {len(hits)} of the "
                               f"latest {len(entries)} video descriptions"}
    source = seed.get("contact_source_url")
    if source and not _is_youtube(source):
        return verify_email_on_page(fetcher, email, source, now)
    raise core.SourceError("the address is not in the channel description or labelled as a business contact in the "
                           "latest video descriptions")


def _research_contact(seed, fetcher, now, report, label, max_days=RESEARCH_CONTACT_MAX_DAYS):
    """Without an API key: a non-YouTube page is fetched as usual; an address research saw on YouTube is used for
    RESEARCH_CONTACT_MAX_DAYS after it was checked, flagged so the draft waits for you to look at its source."""
    email, source = seed.get("contact_email"), seed.get("contact_source_url")
    if not email:
        return {}
    try:
        if source and not _is_youtube(source):
            return verify_email_on_page(fetcher, email, source, now)
        checked = core.parse_time(seed.get("last_checked"))
        if not checked or core.days_between(checked, now) > max_days:
            raise core.SourceError(f"research record is older than {max_days} days; "
                                   + NO_KEY_ACTIVITY)
        return {"email": core.validate_email(email), "source_kind": "research", "source_url": core.validate_url(source),
                "excerpt": core.clean_text(seed.get("contact_excerpt"), 160) or "recorded by research",
                "verified_at": core.iso(checked), "verified_by": f"research on {seed['last_checked']}; not re-checked "
                                                                 "this run (no YOUTUBE_API_KEY)"}
    except core.OutreachError as error:
        report.error(label, f"contact not used: {error}")
        return {}


def youtube_seed_candidates(sources, config, now, fetcher, report, *, api_key=None, quota=None):
    """Channels you researched. With an API key each is re-checked: it exists, its stats, latest uploads (activity,
    engagement, newer evidence) and the contact. Without one, only the research record is used, and marked so."""
    seeds = usable_seeds(sources, "youtube_channels", report)
    if not seeds:
        return []
    quota = quota or Quota(config["youtube"].get("daily_quota_budget"))
    resolved = _resolve_channels(seeds, fetcher, api_key, quota, report) if api_key else {}
    if resolved is None:                                                    # the key was refused: fatal, reported
        return []
    candidates, unverified = [], 0
    for seed in seeds:
        name = core.clean_text(seed.get("name") or "YouTube channel", 100)
        label = f"youtube channel {name}"
        try:
            url = seed_channel_url(seed)
        except core.OutreachError as error:
            report.error(label, str(error))
            continue
        research = research_meta(seed)
        aliases = _seed_aliases(seed) + ([f"youtube:handle:{seed['handle'].lower()}"] if seed.get("handle") else [])
        candidate = {"name": name, "category": "youtube", "aliases": aliases, "seed_bonus": 10,
                     "channel_text": core.clean_text(seed.get("about"), 500), "source_meta": _source_meta(seed),
                     "research": research, "evidence": research_evidence(seed), "contact": {}, "stats": {}}
        if api_key:
            channel = resolved.get(seed.get("channel_id")) or resolved.get(("handle", str(seed.get("handle", "")).lower()))
            if not channel:
                report.error(label, "not found through the YouTube API (deleted, renamed, wrong id, or quota used up)")
                continue
            channel_id = channel.get("id")
            url = core.youtube_channel_url(channel_id)
            description = str((channel.get("snippet") or {}).get("description") or "")
            subs, videos = _channel_numbers(channel)
            candidate.update({"url": url, "canonical": core.canonical_url(url), "channel_id": channel_id,
                              "subscribers": subs, "videos": videos,
                              "channel_text": core.clean_text(description, 1000) or candidate["channel_text"],
                              "stats": {"subscribers": subs, "videos": videos, "checked_at": core.iso(now)}})
            entries, engagement = [], {}
            if quota.take(2 * LIST_UNITS):
                try:
                    entries = channel_uploads(fetcher, channel_id, api_key)
                    engagement = upload_engagement(fetcher, entries, api_key, now)
                except core.OutreachError as error:
                    report.error(label, f"latest uploads not read: {error}")
            apply_uploads(candidate, entries, engagement, config, now, find_contact=False)
            evidence_date = core.parse_time((candidate["evidence"] or {}).get("date"))
            too_old = evidence_date is None or core.days_between(evidence_date, now) > \
                int(config["limits"]["evidence_max_age_days"])
            if too_old and entries:
                # Active channel, but nothing recent matched the puzzle/indie words: its newest upload is the evidence
                # of activity, and the researched similar coverage is the evidence of relevance.
                candidate["evidence"] = {**{k: v for k, v in entries[0].items() if k not in ("text", "video_id")},
                                         "source": "youtube uploads (API)"}
            if seed.get("contact_email"):
                try:
                    candidate["contact"] = _verified_seed_email(seed, description, entries, fetcher, now)
                except core.OutreachError as error:
                    report.error(label, f"contact not used: {error}")
            else:
                emails = core.find_emails(description)
                if emails:
                    candidate["contact"] = {"email": emails[0][0], "source_kind": "youtube-description",
                                            "source_url": url, "excerpt": emails[0][1], "verified_at": core.iso(now),
                                            "verified_by": "youtube-api channels.list"}
                else:
                    candidate["contact"] = feed_business_contact(
                        entries, now, min_videos=int(config["youtube"].get("business_email_min_videos", 2)))
        else:
            research["verification"] = "research-record"
            unverified += 1
            candidate.update({"url": url, "canonical": core.canonical_url(url), "channel_id": seed.get("channel_id"),
                              "contact": _research_contact(
                                  seed, fetcher, now, report, label,
                                  int(config["limits"].get("research_contact_max_days", RESEARCH_CONTACT_MAX_DAYS)))})
            for key in ("subscribers", "recent_uploads"):
                if isinstance(seed.get(key), int):
                    candidate[key] = candidate["stats"][key] = seed[key]
        if candidate["evidence"] is None:
            report.error(label, "no dated evidence: " + (NO_KEY_ACTIVITY if not api_key else "no uploads were read"))
            continue
        candidates.append(candidate)
    if unverified:
        report.sources["youtube_seeds_not_rechecked"] = unverified
    return candidates


def curator_candidates(sources, config, now, fetcher, report):
    candidates = []
    for seed in usable_seeds(sources, "curators", report):
        label = f"curator {seed.get('name', '?')}"
        try:
            site = core.validate_url(seed["site_url"])
            page_url = core.validate_url(seed.get("submission_url") or seed.get("contact_source_url") or site)
            _, html = fetcher.page(page_url)
        except (core.OutreachError, KeyError) as error:
            report.error(label, str(error) if not isinstance(error, KeyError) else f"missing field {error}")
            continue
        text = web.html_to_text(html)
        phrase = str(seed.get("must_contain") or "").lower()
        if phrase and phrase not in text.lower():
            report.error(label, f"'{seed['must_contain']}' is no longer on {page_url}; not used")
            continue
        index = text.lower().find(phrase) if phrase else 0
        evidence = {"url": page_url, "title": web.page_title(html) or seed["name"], "date": None,
                    "excerpt": core.clean_text(text[max(0, index - 60): index + 160], 220),
                    "source": "submission page (checked this run)"}
        contact = _seed_contact(seed, fetcher, now, report, label)
        if not contact and seed.get("submission_url") and seed.get("route_kind") != "email":
            contact = {"submission_url": page_url, "source_kind": "web-page", "source_url": page_url,
                       "excerpt": evidence["excerpt"], "verified_at": core.iso(now), "verified_by": "page fetch",
                       "route_kind": seed.get("route_kind") or "form"}
        candidates.append({"name": core.clean_text(seed["name"], 100), "category": seed.get("category", "curator"),
                           "url": site, "canonical": core.canonical_url(site), "aliases": _seed_aliases(seed),
                           "evidence": evidence, "contact": contact,
                           "channel_text": core.clean_text(seed.get("notes"), 300), "seed_bonus": 30,
                           "source_meta": _source_meta(seed)})
    return candidates


# ---- entry point ------------------------------------------------------------------------------------------------

def run(state, config, sources, now, *, fetcher=None, youtube_key=None):
    """Discover, dedupe, score and qualify. Returns a DiscoveryReport. Never sends anything."""
    fetcher = fetcher or web.Fetcher()
    report = DiscoveryReport()
    # Your own seeds first: when a seed and a search result are the same organisation (an alias), the seed's record
    # and official route become the prospect, and the search result only merges into it.
    candidates = []
    for name, collect in (("feeds", feed_candidates), ("curators", curator_candidates)):
        found = collect(sources, config, now, fetcher, report)
        report.sources[name] = len(found)
        candidates.extend(found)
    quota = Quota(config["youtube"].get("daily_quota_budget"))
    found = youtube_seed_candidates(sources, config, now, fetcher, report, api_key=youtube_key, quota=quota)
    report.sources["researched_youtube_channels"] = len(found)
    candidates.extend(found)
    if youtube_key:
        found = youtube_candidates(config, now, fetcher, youtube_key, report, quota=quota)
        kept = []
        for candidate in found:
            reason = size_filter(candidate, config)
            if reason:
                store.reject(state, now, name=candidate["name"], url=candidate["url"], reason=reason, source="youtube")
                report.rejected += 1
            else:
                kept.append(candidate)
        enrich_channels(kept, config, now, fetcher, youtube_key, quota, report)
        candidates.extend(kept)
        report.sources["youtube"] = len(found)
        report.sources["youtube_quota_units"] = quota.used
    else:
        report.sources["youtube"] = "skipped: YOUTUBE_API_KEY not set"
    stale = settings.stale_sources(sources, now.date())
    if stale:
        report.sources["stale_source_records"] = stale
    for candidate in candidates:
        ingest(state, candidate, config, now, report)
    # Prospects found earlier may qualify now (owner added a contact) or stop qualifying (evidence aged out).
    for prospect in state["prospects"].values():
        if prospect["status"] in (core.DISCOVERED, core.QUALIFIED) and prospect["id"] not in report.qualified:
            stats = prospect.get("stats") or {}
            prospect["score"], prospect["score_reasons"], prospect["disqualifiers"] = scoring.score(
                {**prospect, "subscribers": stats.get("subscribers"), "videos": stats.get("videos"),
                 "recent_uploads": stats.get("recent_uploads")}, config["keywords"], now,
                max_age_days=config["limits"]["evidence_max_age_days"])
            if qualify(prospect, config, now):
                report.qualified.append(prospect["id"])
    store.event(state, "discovery", now, **{k: (len(v) if isinstance(v, list) else v)
                                            for k, v in report.as_dict().items() if k != "sources"})
    return report
