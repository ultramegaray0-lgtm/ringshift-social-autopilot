"""Rules every outreach message must pass, applied when a draft is made AND again right before delivery.

They encode the brief: honest, specific, no fake familiarity, no unapproved promises, no invented facts, the verified
demo link and nothing else, an easy opt-out. A failure is 'blocking': such a draft can never be approved.
"""

import re

from . import core

PROMISES = re.compile(
    r"\b(pay|paid|payment|paying|sponsor\w*|compensat\w*|fees?|rewards?|giveaways?|prizes?|free (copy|copies|key|keys)|"
    r"steam keys?|game keys?|review keys?|keys? for you|exclusive\w*|early access|affiliate|commission|revenue share|"
    r"discount\w*|gift ?cards?|merch|bonus|guarantee\w*|in exchange)\b", re.I)
FAKE_FAMILIARITY = re.compile(
    r"\b(i (just |really |recently )?(watched|loved|enjoyed|binged|saw|adored|read)|big fan|huge fan|long[- ]?time "
    r"(fan|viewer|subscriber|reader)|i('ve| have) been (following|watching|reading)|as (a|your) (fan|subscriber)|"
    r"we (spoke|talked|met)|as (we )?discussed|following up on our|great to (chat|talk)|you might remember me|"
    r"my favou?rite (channel|creator|video))\b", re.I)
INVENTED_CLAIMS = re.compile(
    r"(\b\d[\d,.]*\s*(k\s*)?(players|downloads|wishlists|reviews|sales|copies|users|views|fans|levels|puzzles|hours)\b|"
    r"\b(award\w*|viral|best[- ]selling|bestselling|#1|number one|critically acclaimed|top[- ]rated|"
    r"release date|launch(es|ing)? on|out now on|coming (soon )?to (steam|switch|ps\d|xbox|ios|android)|"
    r"on steam|wishlist|multiplayer|co-?op|level editor|leaderboards?|soundtrack|procedural\w*|story mode|"
    r"thousands of|hundreds of|millions of)\b)", re.I)
HYPE = re.compile(r"\b(revolutionary|game[- ]changer|the future of gaming|must[- ]play|mind[- ]?blowing|"
                  r"unlike anything|you('ll| will) love|perfect for your (channel|audience))\b", re.I)
URL_RE = re.compile(r"(https?://[^\s<>\"')]+|www\.[^\s<>\"')]+)", re.I)
DECEPTIVE_SUBJECT = re.compile(r"^\s*(re|fw|fwd|aw|tr)\s*:|\b(urgent|important|action required|invoice|"
                               r"payment|your account|final notice)\b|!!", re.I)
OPT_OUT_LINE = "reply \"no thanks\""
NO_REPLY_LINE = "no reply is needed"


def check(subject, body, *, demo_url, channel, evidence_title=None, require_evidence=True):
    """List of blocking problems (empty = passes)."""
    problems = []
    subject, body = str(subject or ""), str(body or "")
    if channel == "email":
        if not 10 <= len(subject) <= 90:
            problems.append("subject must be 10-90 characters")
        if DECEPTIVE_SUBJECT.search(subject):
            problems.append("subject looks like a reply/forward or uses alarm words")
        if "ringshift" not in subject.lower():
            problems.append("subject must name Ringshift so the email is not disguised")
        letters = [c for c in subject if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.5:
            problems.append("subject is mostly capital letters")
    if not 250 <= len(body) <= 1600:
        problems.append(f"body length {len(body)} is outside 250-1600 characters")
    urls = [u.rstrip(".,;:!?") for u in URL_RE.findall(body)]
    if urls.count(demo_url) != 1:
        problems.append("body must contain the verified demo link exactly once")
    if any(u != demo_url for u in urls):
        problems.append("body contains a link other than the verified demo link")
    if core.EMAIL_RE.search(body):
        problems.append("body contains an email address")
    # Their own title is quoted and attributed to them, so its words ("Top 10 puzzles") are not our claims.
    scanned = body
    if evidence_title:
        for quoted in (f"\u201c{evidence_title}\u201d", f'"{evidence_title}"'):
            scanned = scanned.replace(quoted, "\u201c\u2026\u201d")
    for name, pattern in (("promise or incentive", PROMISES), ("claim of familiarity", FAKE_FAMILIARITY),
                          ("unverified claim", INVENTED_CLAIMS), ("hype", HYPE)):
        for text, where in ((subject, "subject"), (scanned, "body")):
            match = pattern.search(text)
            if match:
                problems.append(f"{where} has a {name}: '{match.group(0)}'")
    if "indie" not in body.lower():
        problems.append("body must say this is from an indie developer")
    if channel == "email" and OPT_OUT_LINE not in body.lower():
        problems.append("body must contain the opt-out line")
    if channel == "email" and NO_REPLY_LINE not in body.lower():
        problems.append("body must say no reply is needed")
    if require_evidence:
        if not evidence_title:
            problems.append("no evidence title to reference")
        elif f"“{evidence_title}”" not in body and f'"{evidence_title}"' not in body:
            problems.append("body does not reference the evidence by its exact title")
    if "[" in body or "]" in body or "{" in body or "}" in body:
        problems.append("body contains an unfilled placeholder")
    return problems


def check_sentence(sentence):
    """Extra rules for the one optional Gemini sentence. Empty list = usable."""
    problems = []
    text = core.clean_text(sentence)
    if not 40 <= len(text) <= 260:
        problems.append("length")
    if len(re.findall(r"[.!?](\s|$)", text)) > 1 or "\n" in str(sentence).strip():
        problems.append("more than one sentence")
    if URL_RE.search(text) or core.EMAIL_RE.search(text) or "@" in text:
        problems.append("link, address or handle")
    if re.search(r"\d", text):
        problems.append("number")
    if re.search(r"[\"“”\[\]{}*#]", text):
        problems.append("quotes or markup")
    for pattern in (PROMISES, FAKE_FAMILIARITY, INVENTED_CLAIMS, HYPE):
        if pattern.search(text):
            problems.append("disallowed claim")
    if re.search(r"\b(you|your)\b.*\b(great|amazing|awesome|incredible|brilliant|love)\b", text, re.I):
        problems.append("flattery")
    if re.search(r"\b(level|levels|mode|modes|feature|features|power|powers|theme|themes|ufo|"
                 r"rocket|meteor|orchestra)\b", text, re.I):
        problems.append("describes game features (the fixed text does that)")
    return problems
