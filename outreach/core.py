"""Shared vocabulary: statuses, errors, URL and email normalisation, masking, ids. No network, no I/O."""

import hashlib
import re
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---- statuses -------------------------------------------------------------------------------------------------

DISCOVERED = "DISCOVERED"        # found, but not (yet) a fit or no usable contact route
QUALIFIED = "QUALIFIED"          # relevant, recent evidence and a verified contact route; a draft can be made
DRAFTED = "DRAFTED"              # a draft passed every automatic check and waits for your approval
NEEDS_REVIEW = "NEEDS_REVIEW"    # a draft exists but has a warning you must look at (or the route is manual-only)
APPROVED = "APPROVED"            # you approved a draft; it is in your Gmail Drafts or waiting for you to send it
SENT = "SENT"                    # the email provider confirmed it (SMTP accepted / found in Sent Mail), or you said so
REPLIED = "REPLIED"
DECLINED = "DECLINED"
SUPPRESSED = "SUPPRESSED"        # opted out or blocked by you: never contacted or re-added again
BOUNCED = "BOUNCED"

PROSPECT_STATUSES = [DISCOVERED, QUALIFIED, DRAFTED, NEEDS_REVIEW, APPROVED, SENT, REPLIED, DECLINED,
                     SUPPRESSED, BOUNCED]
TERMINAL = {SUPPRESSED, BOUNCED, DECLINED}

# Draft lifecycle (separate from the prospect's status).
D_AWAITING = "AWAITING_APPROVAL"
D_APPROVED = "APPROVED"          # claimed; in Gmail Drafts (gmail-draft) or handed to you (manual)
D_SENT = "SENT"
D_SEND_FAILED = "SEND_FAILED"    # definitely not sent (refused before the message was accepted)
D_SEND_UNKNOWN = "SEND_UNKNOWN"  # the connection broke mid-send: maybe sent. Never retried automatically.
D_SUPERSEDED = "SUPERSEDED"
D_REJECTED = "REJECTED"          # you rejected it
DRAFT_STATUSES = [D_AWAITING, D_APPROVED, D_SENT, D_SEND_FAILED, D_SEND_UNKNOWN, D_SUPERSEDED, D_REJECTED]

CATEGORIES = ["youtube", "blog", "curator", "newsletter", "community", "directory"]
# Sources that are reached through an open submission route rather than dated content of their own: an undated
# submission page is acceptable evidence for them (it is re-checked on every run).
ROUTE_CATEGORIES = ("curator", "newsletter", "community", "directory")


# ---- errors ---------------------------------------------------------------------------------------------------

class OutreachError(RuntimeError):
    """A refusal with a reason a person can act on."""


class QuotaError(OutreachError):
    """An API said 'too many requests / quota used up'. Not a bug; try tomorrow."""


class AuthError(OutreachError):
    """A key or password was rejected. Needs your attention."""


class SourceError(OutreachError):
    """A source could not be read (network, HTTP status, robots.txt, bad data)."""


# ---- time -----------------------------------------------------------------------------------------------------

def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment):
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value):
    """ISO 8601 / RFC 3339 / a plain date -> aware datetime, or None. Never guesses."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return datetime.combine(date.fromisoformat(text), datetime.min.time(), tzinfo=timezone.utc)
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def days_between(earlier, later):
    return (later - earlier).total_seconds() / 86400.0


# ---- URLs -----------------------------------------------------------------------------------------------------

TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|si|feature|ref|ref_src|mc_cid|mc_eid)$", re.I)
YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com"}


def validate_url(url):
    """A public http(s) URL with a real-looking host, or OutreachError. Returns it normalised to https."""
    text = str(url or "").strip()
    if not text or any(c.isspace() for c in text):
        raise OutreachError(f"not a usable URL: {text[:80]!r}")
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise OutreachError(f"URL must start with https:// ({text[:80]!r})")
    host = (parts.hostname or "").lower()
    if not host or "." not in host or host.endswith((".invalid", ".local", ".localhost", ".test", ".example")) \
            or host in ("localhost", "example.com", "example.org", "example.net") \
            or re.fullmatch(r"[\d.]+", host) or parts.username or parts.password:
        raise OutreachError(f"URL host is not a public site: {text[:80]!r}")
    netloc = host + (f":{parts.port}" if parts.port and parts.port not in (80, 443) else "")
    return urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))


def canonical_url(url):
    """The dedup key of a site/channel. YouTube channels become youtube:channel:<id> or youtube:handle:@name."""
    parts = urlsplit(validate_url(url))
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", parts.path)
    if host in YOUTUBE_HOSTS:
        match = re.match(r"^/channel/(UC[\w-]{22})", path)
        if match:
            return f"youtube:channel:{match.group(1)}"
        match = re.match(r"^/(@[\w.\-]+)", path)
        if match:
            return f"youtube:handle:{match.group(1).lower()}"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not TRACKING_PARAMS.match(k)))
    path = path.rstrip("/") or ""
    return f"{host}{path}" + (f"?{query}" if query else "")


def youtube_channel_url(channel_id):
    if not re.fullmatch(r"UC[\w-]{22}", str(channel_id or "")):
        raise OutreachError(f"not a YouTube channel id: {str(channel_id)[:40]!r}")
    return f"https://www.youtube.com/channel/{channel_id}"


def youtube_video_url(video_id):
    if not re.fullmatch(r"[\w-]{11}", str(video_id or "")):
        raise OutreachError(f"not a YouTube video id: {str(video_id)[:40]!r}")
    return f"https://www.youtube.com/watch?v={video_id}"


# ---- email ----------------------------------------------------------------------------------------------------

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,24})(?![\w-])")
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "email.com", "yourdomain.com",
                       "test.com", "mail.com.invalid", "sentry.io", "wixpress.com"}
NO_REPLY_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|bounce[s]?|abuse|"
                            r"privacy|dmca|legal|copyright|unsubscribe)$", re.I)


def normalize_email(value):
    text = str(value or "").strip().strip("<>").strip()
    if text.lower().startswith("mailto:"):
        text = text[7:].split("?", 1)[0]
    return text.lower()


def validate_email(value):
    """Return the normalised address or raise OutreachError. Syntax only: a real mailbox is proven by delivery."""
    email = normalize_email(value)
    if not email or len(email) > 254 or email.count("@") != 1:
        raise OutreachError("not an email address")
    if not EMAIL_RE.fullmatch(email) or ".." in email or email.split("@")[0].endswith("."):
        raise OutreachError("not a valid email address")
    local, domain = email.split("@")
    if domain in PLACEHOLDER_DOMAINS or domain.endswith((".invalid", ".test", ".example", ".local", ".localhost")):
        raise OutreachError("placeholder or non-public email domain")
    if NO_REPLY_LOCAL.match(local):
        raise OutreachError("automated / no-reply / legal mailbox, not a business contact")
    return email


def find_emails(text):
    """Addresses written out literally in `text` (no de-obfuscation, no guessing), each with a short excerpt."""
    found = []
    seen = set()
    for match in EMAIL_RE.finditer(str(text or "")):
        try:
            email = validate_email(match.group(1))
        except OutreachError:
            continue
        if email in seen:
            continue
        seen.add(email)
        start, end = max(0, match.start() - 40), min(len(text), match.end() + 20)
        found.append((email, " ".join(text[start:end].split())))
    return found


def mask_email(value):
    """For public logs: ab***@do***.com. Never reversible."""
    email = normalize_email(value)
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    name, _, tld = domain.rpartition(".")
    return f"{local[:2]}***@{name[:2]}***.{tld}" if name else f"{local[:2]}***@***"


def mask_emails_in(text):
    return EMAIL_RE.sub(lambda m: mask_email(m.group(1)), str(text or ""))


# ---- ids ------------------------------------------------------------------------------------------------------

def prospect_id(canonical):
    """Stable: the same canonical URL always gives the same id."""
    return "p-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]


def clean_text(value, limit=None):
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip() if limit else text
