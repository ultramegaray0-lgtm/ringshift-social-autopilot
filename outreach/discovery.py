"""Find prospects. Read-only: this module has no way to send, post, follow or message anyone.

Sources (all free):
  * YouTube Data API v3: recent videos matching config.youtube.queries, then the channels' public stats and
    description. A business email is used ONLY if the creator wrote it in that public description. The API does not
    expose the About page's hidden "View email address" (CAPTCHA), and this code never tries to get it.
  * YouTube channel RSS feeds for channels you list in sources.json.
  * RSS/Atom feeds of blogs you list in sources.json.
  * Submission pages of curators/newsletters you list in sources.json.
Every contact address must be written out literally on the page (or description) it is credited to, in this run.
"""

import re
import xml.etree.ElementTree as ET
from datetime import timedelta

from . import core, scoring, store, web

YOUTUBE_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_CHANNELS = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
MAX_FEED_BYTES = 2_000_000


class DiscoveryReport:
    def __init__(self):
        self.new, self.updated, self.qualified, self.rejected = [], [], [], 0
        self.errors, self.fatal = [], []
        self.sources = {}

    def error(self, source, message, fatal=False):
        (self.fatal if fatal else self.errors).append(f"{source}: {message}")

    def as_dict(self):
        return {"new": self.new, "updated": self.updated, "qualified": self.qualified, "rejected": self.rejected,
                "errors": self.errors, "fatal": self.fatal, "sources": self.sources}


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
        if contact.get("source_kind") not in ("youtube-description", "web-page", "owner"):
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

def youtube_candidates(config, now, fetcher, api_key, report):
    settings = config["youtube"]
    after = core.iso(now - timedelta(days=int(settings["published_within_days"])))
    videos_by_channel = {}
    for query in settings["queries"]:
        try:
            payload = fetcher.google_api(YOUTUBE_SEARCH, {
                "part": "snippet", "type": "video", "q": query, "order": "relevance", "publishedAfter": after,
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
            videos_by_channel.setdefault(channel_id, {"channel_url": channel_url, "videos": []})["videos"].append({
                "url": video_url, "title": core.clean_text(snippet.get("title"), 200),
                "date": snippet.get("publishedAt"), "excerpt": core.clean_text(snippet.get("description"), 300),
                "source": f"youtube search: {query}"})
    report.sources["youtube_channels_seen"] = len(videos_by_channel)

    candidates = []
    ids = list(videos_by_channel)
    for start in range(0, len(ids), 50):
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
    return candidates


def _best_evidence(videos, keywords):
    def rank(video):
        hits = len(scoring.hits(f"{video['title']} {video['excerpt']}", keywords["puzzle"] + keywords["indie"]))
        when = core.parse_time(video.get("date"))
        return (hits > 0, hits, when.timestamp() if when else 0)
    return max(videos, key=rank)


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
    return {"name": name, "category": "youtube", "url": info["channel_url"],
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


# ---- feeds (blogs and seeded YouTube channels) -----------------------------------------------------------------

def _strip_ns(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_text):
    """Entries of an RSS 2.0 or Atom feed: [{title, url, date, excerpt}]. Refuses DTDs/entities."""
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
        entries.append({"title": core.clean_text(fields.get("title"), 200), "url": url,
                        "date": core.iso(when) if when else None, "excerpt": core.clean_text(fields.get("excerpt"), 300)})
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


def feed_candidates(sources, config, now, fetcher, report):
    candidates = []
    for seed in sources["feeds"]:
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
                           "url": site, "canonical": core.canonical_url(site), "aliases": [],
                           "evidence": {**best, "source": f"feed {seed['feed_url']}"},
                           "contact": _seed_contact(seed, fetcher, now, report, label),
                           "channel_text": core.clean_text(seed.get("about"), 500), "seed_bonus": 10})
    for seed in sources["youtube_channels"]:
        label = f"youtube feed {seed.get('name', '?')}"
        try:
            channel_url = core.youtube_channel_url(seed["channel_id"])
            _, xml_text = fetcher.page(YOUTUBE_FEED.format(seed["channel_id"]))
            entries = parse_feed(xml_text)
        except (core.OutreachError, KeyError) as error:
            report.error(label, str(error) if not isinstance(error, KeyError) else f"missing field {error}")
            continue
        if not entries:
            continue
        best = _best_evidence([{**e, "source": "youtube channel feed"} for e in entries], config["keywords"])
        candidates.append({"name": core.clean_text(seed.get("name") or "YouTube channel", 100), "category": "youtube",
                           "url": channel_url, "canonical": core.canonical_url(channel_url), "aliases": [],
                           "evidence": best, "contact": _seed_contact(seed, fetcher, now, report, label),
                           "channel_text": core.clean_text(seed.get("about"), 500), "seed_bonus": 10})
    return candidates


def curator_candidates(sources, config, now, fetcher, report):
    candidates = []
    for seed in sources["curators"]:
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
        if not contact and seed.get("submission_url"):
            contact = {"submission_url": page_url, "source_kind": "web-page", "source_url": page_url,
                       "excerpt": evidence["excerpt"], "verified_at": core.iso(now), "verified_by": "page fetch"}
        candidates.append({"name": core.clean_text(seed["name"], 100), "category": seed.get("category", "curator"),
                           "url": site, "canonical": core.canonical_url(site), "aliases": [], "evidence": evidence,
                           "contact": contact, "channel_text": core.clean_text(seed.get("notes"), 300),
                           "seed_bonus": 30})
    return candidates


# ---- entry point ------------------------------------------------------------------------------------------------

def run(state, config, sources, now, *, fetcher=None, youtube_key=None):
    """Discover, dedupe, score and qualify. Returns a DiscoveryReport. Never sends anything."""
    fetcher = fetcher or web.Fetcher()
    report = DiscoveryReport()
    candidates = []
    if youtube_key:
        found = youtube_candidates(config, now, fetcher, youtube_key, report)
        for candidate in found:
            reason = size_filter(candidate, config)
            if reason:
                store.reject(state, now, name=candidate["name"], url=candidate["url"], reason=reason, source="youtube")
                report.rejected += 1
            else:
                candidates.append(candidate)
        report.sources["youtube"] = len(found)
    else:
        report.sources["youtube"] = "skipped: YOUTUBE_API_KEY not set"
    for name, collect in (("feeds", feed_candidates), ("curators", curator_candidates)):
        found = collect(sources, config, now, fetcher, report)
        report.sources[name] = len(found)
        candidates.extend(found)
    for candidate in candidates:
        ingest(state, candidate, config, now, report)
    # Prospects found earlier may qualify now (owner added a contact) or stop qualifying (evidence aged out).
    for prospect in state["prospects"].values():
        if prospect["status"] in (core.DISCOVERED, core.QUALIFIED) and prospect["id"] not in report.qualified:
            prospect["score"], prospect["score_reasons"], prospect["disqualifiers"] = scoring.score(
                {**prospect, "subscribers": (prospect.get("stats") or {}).get("subscribers"),
                 "videos": (prospect.get("stats") or {}).get("videos")}, config["keywords"], now,
                max_age_days=config["limits"]["evidence_max_age_days"])
            if qualify(prospect, config, now):
                report.qualified.append(prospect["id"])
    store.event(state, "discovery", now, **{k: (len(v) if isinstance(v, list) else v)
                                            for k, v in report.as_dict().items() if k != "sources"})
    return report
