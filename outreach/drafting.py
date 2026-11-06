"""Write one specific, honest message per qualified prospect. Writes text only; it cannot send anything.

Structure is fixed and truthful (who you are, how you found them, what the game is, the verified demo link, an
easy out). The one personal sentence ("why this might suit your audience") comes from Gemini when it is available and
passes checker.check_sentence; otherwise from a deterministic sentence built from the evidence keywords.
Gemini never receives a name, an email address, a URL or a handle (Gemini's free-tier terms: no personal data).
"""

import re
import secrets

from . import approval, checker, core, scoring, store

GEMINI_PROMPT = """You help an indie game developer write ONE sentence for a short, honest email to a {audience}.

The {kind} they found: title "{title}". Public description excerpt: "{excerpt}".
The game: {pitch} {mechanics}

Write ONE sentence (15-35 words) saying why this game might suit the people who follow that {kind}, based only on
the title and excerpt above.
Rules: do not claim to have watched, read or enjoyed anything; no praise or flattery; no numbers; no links; no
quotes; do not describe features, levels or modes of the game; do not promise anything; no hype words.
Output only the sentence."""


def _topic(prospect, keywords):
    """What the evidence's own title (or, failing that, its description) says it is about."""
    words = keywords["puzzle"] + keywords["indie"]
    found = set(scoring.hits(prospect["evidence"].get("title", ""), words)) or \
        set(scoring.hits(prospect["evidence"].get("excerpt", ""), words))
    if "thinky" in found:
        return "thinky puzzle games"
    if "logic" in found or "logic puzzles" in found:
        return "logic puzzles"
    if found & {"spatial reasoning", "spatial puzzle", "spatial puzzles"}:
        return "spatial puzzles"
    if found & {"puzzle", "puzzles", "puzzler", "brain teaser", "brainteaser", "brain teasers", "brainteasers"}:
        return "puzzle games"
    if found & {"demo", "demos", "steam next fest"}:
        return "indie game demos"
    if found & {"unusual", "weird games", "strange games", "hidden gem", "hidden gems"}:
        return "unusual indie games"
    return "indie games"


def reference(prospect, config):
    """The video or article a first message names. The evidence (usually the latest upload) when its title is about
    puzzles; otherwise the newest dated item of the channel's researched puzzle coverage, so a message never opens
    with an unrelated upload (a draft once cited "I'm Organizing Orgies." for a logic-puzzle channel)."""
    evidence = prospect["evidence"]
    words = config["keywords"]["puzzle"]
    if scoring.hits(evidence.get("title", ""), words):
        return evidence
    coverage = [c for c in ((prospect.get("research") or {}).get("similar_coverage") or [])
                if c.get("title") and c.get("url") and c.get("date") and scoring.hits(c["title"], words)]
    if not coverage:
        return evidence
    best = max(coverage, key=lambda c: c["date"])
    return {"url": best["url"], "title": best["title"], "date": best["date"], "source": "researched coverage"}


def template_sentence(prospect, keywords, hook="a puzzle game about steering beams through rotating rings"):
    topic = _topic(prospect, keywords)
    category = prospect["category"]
    coverage = [c for c in ((prospect.get("research") or {}).get("similar_coverage") or []) if c.get("game")]
    if category == "youtube" and coverage:
        return (f"Your channel has covered {coverage[0]['game']} and other puzzle games, so I thought {hook} might "
                "suit the people who watch it.")
    if category == "youtube":
        return f"Since that video is about {topic}, I thought {hook} might suit the people who watch your channel."
    if category == "blog":
        return f"Since that article is about {topic}, I thought {hook} might interest your readers."
    if category == "directory":
        return f"Your site publishes browser games, so I am submitting {hook}."
    return f"Your site invites game suggestions, so I am sending {hook}."


def personal_sentence(prospect, config, writer):
    """(sentence, how it was made): a researched angle first, then Gemini (only for a real video or article), then
    the fixed template. Every non-template sentence must pass checker.check_sentence."""
    angle = (prospect.get("research") or {}).get("angle")
    if angle and not checker.check_sentence(angle):
        return angle, "researched angle (sources.json)", None
    note = None
    if writer and prospect["category"] in ("youtube", "blog"):
        sentence, how = writer.sentence(prospect, config)
        if sentence:
            return sentence, how, None
        note = how
    elif writer:
        note = "gemini not used for a submission page"
    template = template_sentence(prospect, config["keywords"], config["game"].get("hook") or
                                 "a puzzle game about steering beams through rotating rings")
    return template, f"template ({note or 'gemini not used'})", note


def _public_excerpt(text):
    text = core.EMAIL_RE.sub(" ", str(text or ""))
    text = checker.URL_RE.sub(" ", text)
    text = re.sub(r"@\w[\w.]*", " ", text)
    return core.clean_text(text.replace('"', "'"), 200)


class GeminiWriter:
    """One sentence per prospect, at most `budget` calls per run. Quota/auth errors switch it off for the run."""

    def __init__(self, api_key, models, client_factory=None, budget=10):
        self.models, self.budget, self.disabled, self.notes = list(models), budget, None, []
        self._key, self._factory, self._client = api_key, client_factory, None

    def _get_client(self):
        if self._client is None:
            if self._factory:
                self._client = self._factory(self._key)
            else:
                from google import genai
                self._client = genai.Client(api_key=self._key)
        return self._client

    def sentence(self, prospect, config):
        if self.disabled or self.budget <= 0:
            return None, self.disabled or "Gemini budget for this run used"
        kind = {"youtube": "video", "blog": "article"}.get(prospect["category"], "page")
        audience = {"youtube": "YouTube creator", "blog": "blog writer"}.get(prospect["category"], "game curator")
        prompt = GEMINI_PROMPT.format(audience=audience, kind=kind,
                                      title=_public_excerpt(prospect["evidence"].get("title")),
                                      excerpt=_public_excerpt(prospect["evidence"].get("excerpt")),
                                      pitch=config["game"]["pitch"], mechanics=config["game"]["mechanics_line"])
        last = "no model answered"
        for model in self.models:
            self.budget -= 1
            try:
                response = self._get_client().models.generate_content(model=model, contents=prompt)
                text = core.clean_text(getattr(response, "text", "") or "")
            except Exception as error:
                message = str(error)
                if re.search(r"429|RESOURCE_EXHAUSTED|quota", message, re.I):
                    self.disabled = "Gemini free-tier quota used up; template sentences used"
                    return None, self.disabled
                if re.search(r"API[_ ]KEY|PERMISSION_DENIED|401|403|invalid.*key", message, re.I):
                    self.disabled = "Gemini rejected the API key; template sentences used"
                    return None, self.disabled
                last = f"{model}: {type(error).__name__}"
                continue
            text = text.strip().strip('"').strip()
            if text and not text.endswith((".", "!", "?")):
                text += "."
            problems = checker.check_sentence(text)
            if not problems:
                return text, f"gemini:{model}"
            last = f"{model} sentence rejected ({', '.join(problems)})"
        return None, last


def greeting(name):
    """'Hi Olexa,' for a person-like name; 'Hello,' when the channel name is a phrase ('I Dream of Indie Games')."""
    return f"Hi {name}," if len(str(name).split()) <= 2 else "Hello,"


def _human_date(value):
    moment = core.parse_time(value)
    return f"{moment:%B} {moment.day}, {moment.year}" if moment else None


def compose(prospect, config, sentence):
    """(channel, recipient, subject, body, require_evidence) for a first message."""
    game, sender = config["game"], config["sender"]["name"]
    evidence = reference(prospect, config)
    contact = prospect.get("contact") or {}
    name = core.clean_text(prospect["name"], 60)
    facts = f"{game['pitch']} {game['mechanics_line']}"
    craft = game.get("craft_line") or ""
    demo = f"{game['demo_line']}\n{config['demo']['url']}"
    title = core.clean_text(evidence.get("title"), 160)
    when = _human_date(evidence.get("date"))

    if contact.get("email"):
        if prospect["category"] in ("youtube", "blog"):
            kind = "video" if prospect["category"] == "youtube" else "article"
            found = (f"I'm the indie developer of Ringshift, a puzzle game with a free browser demo, and I'm writing "
                     f"because of your {kind} “{title}” ({when}). {sentence}")
        else:
            found = (f"I'm the indie developer of Ringshift, a puzzle game with a free browser demo. I found {name} "
                     f"through your page “{title}”, which invites game suggestions. {sentence}")
        subject = f"Ringshift, a rotating-ring beam puzzle with a browser demo, for {core.clean_text(name, 30)}"
        body = (f"{greeting(name)}\n\n{found}\n\n{facts}\n\n" + (f"{craft}\n\n" if craft else "") + f"{demo}\n\n"
                f"If it looks like your kind of puzzle, you are welcome to play it. No reply is needed if it's not "
                f"for you.\n\nThanks,\n{sender}\n\n--\nThis is a one-time message from an independent developer. "
                f"If you'd prefer not to hear from me, reply \"no thanks\" and I won't contact you again.")
        return "email", contact["email"], subject, body, True
    if contact.get("submission_url"):
        subject = f"Game suggestion: Ringshift ({game['short']})"
        body = (f"Ringshift is {game['short']} with a free browser demo. {facts}\n\n" + (f"{craft}\n\n" if craft else "")
                + f"{demo}\n\n{sentence}\n\nThanks for considering it.\n{sender}")
        return "form", contact["submission_url"], subject, body, False
    raise core.OutreachError("no contact route")


def compose_followup(prospect, config, original):
    game, sender = config["game"], config["sender"]["name"]
    sent = _human_date(original.get("sent_at")) or "a couple of weeks ago"
    subject = f"Ringshift browser demo (one follow-up), for {core.clean_text(prospect['name'], 40)}"
    body = (f"Hi {core.clean_text(prospect['name'], 60)},\n\nI wrote on {sent} about Ringshift, {game['short']}, "
            f"after finding your work through “{core.clean_text(original['evidence'].get('title'), 160)}”. "
            f"This is my only follow-up, in case the first email got buried.\n\n{game['pitch']}\n\n"
            f"{game['demo_line']}\n{config['demo']['url']}\n\n"
            f"No reply is needed if it's not for you, and I won't write again.\n\n{sender}\n\n--\n"
            f"This is a message from an independent developer. If you'd prefer not to hear from me, reply "
            f"\"no thanks\" and I won't contact you again.")
    return subject, body


def _new_draft(state, prospect, *, kind, channel, recipient, subject, body, evidence, personalization, warnings,
               require_evidence, now, demo, config, replaces=None):
    draft_id = "od-" + secrets.token_hex(4)
    while draft_id in state["drafts"]:
        draft_id = "od-" + secrets.token_hex(4)
    blocking = checker.check(subject, body, demo_url=config["demo"]["url"], channel=channel,
                             evidence_title=evidence.get("title"), require_evidence=require_evidence)
    draft = {"draft_id": draft_id, "prospect_id": prospect["id"], "kind": kind, "channel": channel,
             "recipient": recipient, "subject": subject, "body": body, "evidence": evidence,
             "contact": prospect.get("contact") or {}, "personalization": personalization, "warnings": warnings,
             "blocking": blocking, "require_evidence": require_evidence, "created_at": core.iso(now),
             "status": core.D_AWAITING, "demo_check": demo, "replaces": replaces}
    draft["approval_code"] = approval.code_for(draft)
    state["drafts"][draft_id] = draft
    return draft


def expire_drafts(state, config, now):
    """Retire waiting drafts that expired, or that link a demo URL the config no longer uses (they could never pass
    the delivery check, and their prospect should be drafted again with the current link)."""
    expired = []
    demo_url = config["demo"]["url"]
    for draft in state["drafts"].values():
        created = core.parse_time(draft["created_at"])
        if draft["status"] != core.D_AWAITING:
            continue
        reason = None
        if created and core.days_between(created, now) > config["limits"]["draft_expiry_days"]:
            reason = "expired before approval"
        elif demo_url not in str(draft.get("body") or ""):
            reason = "the demo link changed; redrafted with the current link"
        if reason:
            draft["status"], draft["status_reason"] = core.D_SUPERSEDED, reason
            prospect = state["prospects"].get(draft["prospect_id"])
            if prospect and prospect["status"] in (core.DRAFTED, core.NEEDS_REVIEW) and draft["kind"] == "initial":
                store.set_status(prospect, core.QUALIFIED, now, "draft expired; will be re-checked")
            expired.append(draft["draft_id"])
    return expired


def active_draft(state, prospect_id, kind=None):
    for draft in state["drafts"].values():
        if draft["prospect_id"] == prospect_id and draft["status"] in (core.D_AWAITING, core.D_APPROVED) \
                and (kind is None or draft["kind"] == kind):
            return draft
    return None


def make_drafts(state, config, now, *, demo, writer=None):
    """Draft the best-scoring QUALIFIED prospects (up to the daily limit) and queue due follow-ups.

    `demo` is the result of checking the demo link this run; with a failing check no draft is made."""
    result = {"created": [], "followups": [], "expired": expire_drafts(state, config, now), "notes": []}
    if not demo.get("ok"):
        result["notes"].append(f"no drafts: the demo link failed its check ({demo.get('reason')})")
        return result
    if not config["sender"]["name"]:
        result["notes"].append("no drafts: set the sign-off name (repository variable OUTREACH_SENDER_NAME)")
        return result

    queue = sorted((p for p in state["prospects"].values()
                    if p["status"] == core.QUALIFIED and not active_draft(state, p["id"])),
                   key=lambda p: (-p["score"], p["first_discovered"]))
    for prospect in queue[:max(0, int(config["limits"]["max_new_drafts_per_run"]))]:
        email = (prospect.get("contact") or {}).get("email")
        if email and store.is_suppressed(state, email=email):
            store.set_status(prospect, core.SUPPRESSED, now, "address is on the suppression list")
            continue
        if email and store.ledger_for(state, email):
            result["notes"].append(f"{prospect['id']}: address was already contacted under another listing; skipped")
            continue
        sentence, personalization, note = personal_sentence(prospect, config, writer)
        if note and writer and note not in result["notes"] and "submission page" not in note:
            result["notes"].append(note)
        try:
            channel, recipient, subject, body, require_evidence = compose(prospect, config, sentence)
        except core.OutreachError as error:
            result["notes"].append(f"{prospect['id']}: {error}")
            continue
        warnings = []
        contact, meta = prospect.get("contact") or {}, prospect.get("source_meta") or {}
        if channel == "form" and contact.get("route_kind") == "upload":
            warnings.append("developer-portal upload: create the listing on their portal yourself (use this text as "
                            "the description), then mark it sent")
        elif channel == "form":
            warnings.append("submission form: paste this text into their form yourself, then mark it sent")
        if meta.get("uncertain"):
            warnings.append(f"eligibility '{meta.get('status')}' (checked {meta.get('last_checked')}); confirm on "
                            f"their page first: " + "; ".join(u.rstrip(". ") for u in meta["uncertain"][:3]))
        if (prospect.get("contact") or {}).get("source_kind") == "owner":
            warnings.append("contact address was entered by you; double-check it on its source page")
        if (prospect.get("contact") or {}).get("source_kind") == "research":
            warnings.append("contact address was recorded by research and not re-checked this run (no "
                            "YOUTUBE_API_KEY): open its source link and confirm it before approving")
        research = prospect.get("research") or {}
        if research.get("verification") == "research-record":
            warnings.append("channel activity was not re-checked through the YouTube API this run: open the "
                            "evidence link and the channel before approving")
        if research.get("language") and research["language"] != "en":
            warnings.append(f"the channel publishes in '{research['language']}'; this draft is in English")
        draft = _new_draft(state, prospect, kind="initial", channel=channel, recipient=recipient, subject=subject,
                           body=body, evidence=reference(prospect, config), personalization=personalization,
                           warnings=warnings, require_evidence=require_evidence, now=now, demo=demo, config=config)
        status = core.DRAFTED if not draft["blocking"] and not warnings else core.NEEDS_REVIEW
        store.set_status(prospect, status, now, f"draft {draft['draft_id']}")
        result["created"].append(draft["draft_id"])

    for prospect in state["prospects"].values():
        if prospect["status"] != core.SENT or active_draft(state, prospect["id"], "followup"):
            continue
        if any(d["prospect_id"] == prospect["id"] and d["kind"] == "followup" for d in state["drafts"].values()):
            continue                                   # one follow-up ever (also when you rejected it)
        originals = [d for d in state["drafts"].values() if d["prospect_id"] == prospect["id"]
                     and d["kind"] == "initial" and d["status"] == core.D_SENT and d["channel"] == "email"]
        last = core.parse_time(prospect.get("last_contact"))
        if not originals or not last or core.days_between(last, now) < config["limits"]["followup_after_days"]:
            continue
        original = originals[-1]
        subject, body = compose_followup(prospect, config, original)
        synced = state["sync"].get("last_ok_at")
        warning = (f"no reply recorded as of the last Gmail check ({synced[:10]})" if synced
                   else "replies were never checked automatically: look in your inbox before approving")
        draft = _new_draft(state, prospect, kind="followup", channel="email", recipient=original["recipient"],
                           subject=subject, body=body, evidence=original["evidence"], personalization="template",
                           warnings=[warning], require_evidence=True, now=now, demo=demo, config=config,
                           replaces=original["draft_id"])
        result["followups"].append(draft["draft_id"])
    store.event(state, "drafting", now, created=len(result["created"]), followups=len(result["followups"]),
                expired=len(result["expired"]))
    return result
