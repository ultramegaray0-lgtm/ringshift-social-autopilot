"""Find prospects. Read-only: this module has no way to send, post, follow or message anyone.

Sources (all free):
  * YouTube Data API v3: recent videos matching config.youtube.queries, then the channels' public stats and
    description. A business email is used ONLY if the creator wrote it in that public description, or labelled it as
    their business contact in the descriptions of several of their own latest videos (read from the channel's free
    public RSS feed, which costs no API quota). The API does not expose the About page's hidden "View email address"
    (CAPTCHA), and this code never tries to get it.
  * YouTube channel RSS feeds for channels you list in sources.json.
  * RSS/Atom feeds of blogs you list in sources.json.
  * Submission pages of curators, newsletters and browser-game directories you list in sources.json. Only entries
    marked enabled, with a verified route and eligibility 'meets' or 'likely', are used; the rest are reported as held.
Every contact address must be written out literally on the page (or description) it is credited to, in this run.
"""

import re
import xml.etree.ElementTree as ET
from datetime import timedelta

from . import core, scoring, settings, store, web

YOUTUBE_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_CHANNELS = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
MAX_FEED_BYTES = 2_000_000
SEARCH_UNITS, CHANNELS_UNITS = 100, 1          # YouTube Data API quota cost per call (search.list, channels.list)
# A creator's own business contact is labelled as such just before the address ("Business inquiries: ..."). An
# address introduced as someone else's (a game's developer or publisher, a support desk) is never theirs to use.
BUSINESS_LABEL = re.compile(r"(business|inquir|enquir|sponsor|collab|partnership|press|work with me|contact me|"
                            r"reach me|email me|mail me)", re.I)
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
            "source_meta": candidate.get("source_meta") or {},
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
        if contact.get("source_kind") not in ("youtube-description", "youtube-video-description", "web-page", "owner"):
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


def youtube_candidates(config, now, fetcher, api_key, report):
    settings = config["youtube"]
    after = core.iso(now - timedelta(days=int(settings["published_within_days"])))
    budget = int(settings.get("daily_quota_budget") or 2000)
    units = 0
    videos_by_channel = {}
    for query, order in _queries(settings):
        if units + SEARCH_UNITS > budget:
            report.sources["youtube_quota_note"] = (f"stopped searching at the run's quota budget of {budget} units "
                                                    "(config youtube.daily_quota_budget)")
            break
        try:
            units += SEARCH_UNITS
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
        try:
            units += CHANNELS_UNITS
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
    report.sources["youtube_quota_units"] = units
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


def _channel_feed(fetcher, channel_id):
    _, xml_text = fetcher.page(YOUTUBE_FEED.format(channel_id))
    return parse_feed(xml_text, keep_text=True)


def enrich_from_feeds(candidates, config, now, fetcher, report):
    """For the most relevant, smallest channels: activity from their free RSS feed, and a business contact when the
    channel description had none. Bounded by youtube.feed_checks_per_run; failures only skip the enrichment."""
    settings = config["youtube"]
    limit = int(settings.get("feed_checks_per_run", 15))
    min_videos = int(settings.get("business_email_min_videos", 2))
    words = config["keywords"]["puzzle"] + config["keywords"]["indie"]

    def priority(candidate):
        evidence = candidate["evidence"]
        relevant = bool(scoring.hits(f"{evidence.get('title', '')} {evidence.get('excerpt', '')}", words))
        subs = candidate.get("subscribers")
        return (not relevant, subs if subs is not None else 10 ** 9)

    checked = contacts = 0
    for candidate in sorted(candidates, key=priority)[:max(0, limit)]:
        if not candidate.get("channel_id"):
            continue
        try:
            entries = _channel_feed(fetcher, candidate["channel_id"])
        except core.OutreachError:
            continue
        checked += 1
        candidate["recent_uploads"] = recent_uploads(entries, now)
        candidate.setdefault("stats", {})["recent_uploads"] = candidate["recent_uploads"]
        if not _contact_email(candidate):
            contact = feed_business_contact(entries, now, min_videos=min_videos)
            if contact:
                candidate["contact"] = contact
                contacts += 1
    report.sources["youtube_feeds_checked"] = checked
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


def _seed_contact(seed, fetcher, now, report, label, feed_entries=None):
    if seed.get("contact_email"):
        try:
            if feed_entries is not None:
                # A YouTube seed: the address must be labelled as the creator's business contact in one of the
                # channel's latest video descriptions, read this run. YouTube pages themselves are not scraped.
                email = core.validate_email(seed["contact_email"])
                hits = [(e, x) for e in feed_entries for m, x in business_emails(e.get("text")) if m == email]
                if not hits:
                    raise core.SourceError("the address is not labelled as a business contact in the channel's "
                                           "latest video descriptions")
                entry, excerpt = hits[0]
                return {"email": email, "source_kind": "youtube-video-description", "source_url": entry["url"],
                        "excerpt": excerpt, "verified_at": core.iso(now),
                        "verified_by": f"youtube channel feed: labelled as a business contact in {len(hits)} of the "
                                       f"latest {len(feed_entries)} video descriptions"}
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
    for seed in usable_seeds(sources, "youtube_channels", report):
        label = f"youtube feed {seed.get('name', '?')}"
        try:
            channel_url = core.youtube_channel_url(seed["channel_id"])
            entries = _channel_feed(fetcher, seed["channel_id"])
        except (core.OutreachError, KeyError) as error:
            report.error(label, str(error) if not isinstance(error, KeyError) else f"missing field {error}")
            continue
        if not entries:
            continue
        best = _best_evidence([{**e, "source": "youtube channel feed"} for e in entries], config["keywords"])
        uploads = recent_uploads(entries, now)
        candidates.append({"name": core.clean_text(seed.get("name") or "YouTube channel", 100), "category": "youtube",
                           "url": channel_url, "canonical": core.canonical_url(channel_url),
                           "aliases": _seed_aliases(seed), "evidence": best,
                           "contact": _seed_contact(seed, fetcher, now, report, label, feed_entries=entries),
                           "channel_text": core.clean_text(seed.get("about"), 500), "seed_bonus": 10,
                           "recent_uploads": uploads, "stats": {"recent_uploads": uploads, "checked_at": core.iso(now)},
                           "source_meta": _source_meta(seed)})
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
    if youtube_key:
        found = youtube_candidates(config, now, fetcher, youtube_key, report)
        kept = []
        for candidate in found:
            reason = size_filter(candidate, config)
            if reason:
                store.reject(state, now, name=candidate["name"], url=candidate["url"], reason=reason, source="youtube")
                report.rejected += 1
            else:
                kept.append(candidate)
        enrich_from_feeds(kept, config, now, fetcher, report)
        candidates.extend(kept)
        report.sources["youtube"] = len(found)
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
