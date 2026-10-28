"""Read-only HTTP for discovery: public pages and feeds only, robots.txt respected, small bounded reads.

No cookies, no logins, no CAPTCHA handling, no retries that hammer a site. A page that is blocked, missing or too big is
reported as a SourceError and skipped. API keys are sent as parameters to Google's APIs only and never logged.
"""

import re
import urllib.robotparser
from html import unescape
from urllib.parse import urlsplit

from . import core

USER_AGENT = "RingshiftOutreach/1.0 (+https://github.com/ultramegaray0-lgtm/ringshift-social-autopilot)"
TIMEOUT = 20
MAX_BYTES = 2_000_000


class Fetcher:
    """`get` is injectable (tests pass a fake). Caches robots.txt per host for one run."""

    def __init__(self, get=None):
        self._get = get
        self._robots = {}

    def _request(self, url, params=None, accept=None):
        import requests
        get = self._get or requests.get
        headers = {"User-Agent": USER_AGENT}
        if accept:
            headers["Accept"] = accept
        try:
            response = get(url, params=params, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        except Exception as error:      # network errors become a reason, never a crash
            raise core.SourceError(f"could not reach {urlsplit(url).hostname} ({type(error).__name__})") from None
        return response

    def allowed(self, url):
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                response = self._request(host + "/robots.txt")
                status = response.status_code
                text = response.text if status == 200 else ""
            except core.SourceError:
                status, text = 0, ""
            if status in (401, 403):
                parser.disallow_all = True           # same rule as the standard library: access denied = keep out
            elif status >= 500 or status == 0:
                parser.disallow_all = True           # can't tell: stay out this run
            else:
                parser.parse(text.splitlines())
            self._robots[host] = parser
        return self._robots[host].can_fetch(USER_AGENT, url)

    def page(self, url, accept=None):
        """(final_url, text) of a public page, or SourceError."""
        url = core.validate_url(url)
        if not self.allowed(url):
            raise core.SourceError(f"robots.txt of {urlsplit(url).hostname} does not allow reading {urlsplit(url).path}")
        response = self._request(url, accept=accept)
        if response.status_code != 200:
            raise core.SourceError(f"{url} answered HTTP {response.status_code}")
        body = response.content if isinstance(getattr(response, "content", None), (bytes, bytearray)) \
            else str(getattr(response, "text", "")).encode("utf-8")
        if len(body) > MAX_BYTES:
            raise core.SourceError(f"{url} is larger than {MAX_BYTES // 1_000_000} MB; skipped")
        final = str(getattr(response, "url", "") or url)
        return final, body.decode(getattr(response, "encoding", None) or "utf-8", errors="replace")

    def google_api(self, url, params):
        """A Google API GET (YouTube Data API). Classifies quota and key errors. Returns parsed JSON."""
        response = self._request(url, params=params)
        status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if status == 200:
            return payload
        error = (payload or {}).get("error") or {}
        reasons = {str(e.get("reason", "")) for e in error.get("errors", []) if isinstance(e, dict)}
        reasons.add(str(error.get("status", "")))
        if status == 429 or reasons & {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded",
                                         "userRateLimitExceeded", "RESOURCE_EXHAUSTED"}:
            raise core.QuotaError("YouTube API quota used up for today (it resets at midnight Pacific time)")
        if status in (400, 401, 403) and reasons & {"keyInvalid", "keyExpired", "API_KEY_INVALID", "forbidden",
                                                     "accessNotConfigured", "PERMISSION_DENIED", "INVALID_ARGUMENT",
                                                     "ipRefererBlocked", "SERVICE_DISABLED"}:
            raise core.AuthError("YouTube API rejected the key (check YOUTUBE_API_KEY and that YouTube Data API v3 "
                                 "is enabled in its Google Cloud project)")
        raise core.SourceError(f"YouTube API answered HTTP {status}")


def html_to_text(html):
    """Visible-ish text of an HTML page, good enough to check that a phrase or an address is written on it."""
    html = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return " ".join(unescape(text).split())


def page_title(html):
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    return core.clean_text(unescape(match.group(1)), 200) if match else ""


def mailto_addresses(html):
    """Addresses in mailto: links (published contact links, written out in the page source)."""
    return [core.normalize_email(m) for m in re.findall(r"(?i)href=[\"']mailto:([^\"'?]+)", html or "")]
