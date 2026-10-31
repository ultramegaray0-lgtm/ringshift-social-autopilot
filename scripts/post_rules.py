"""Deterministic checks applied to every generated post before it can be queued.

Nothing here talks to the network or to an AI model. These rules are a safety
net underneath the AI critic: they catch the mistakes that are cheap to detect
mechanically (too long once X's weighting is applied, invented social proof,
near-copies of earlier posts) so a bad post is rejected even if the critic
waves it through.
"""

import difflib
import re

X_MAX_WEIGHTED_LENGTH = 280
X_URL_WEIGHT = 23
MAX_HASHTAGS = 5

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
HASHTAG_RE = re.compile(r"(?<![\w&])#\w+", re.UNICODE)
MENTION_RE = re.compile(r"(?<![\w@])@\w+", re.UNICODE)

# Code point ranges that X counts as a single character. Everything else
# (CJK, emoji, most symbols) counts double. This follows the public
# twitter-text configuration; it is deliberately conservative, so a post that
# passes here will not be rejected by X for length.
_SINGLE_WEIGHT_RANGES = (
    (0, 4351),
    (8192, 8205),
    (8208, 8223),
    (8242, 8247),
)


def weighted_length(text):
    """Length of `text` the way X counts it: URLs cost 23, wide characters cost 2."""
    text = str(text)
    total = 0

    def add_plain(chunk):
        count = 0
        for char in chunk:
            code = ord(char)
            if any(lo <= code <= hi for lo, hi in _SINGLE_WEIGHT_RANGES):
                count += 1
            else:
                count += 2
        return count

    position = 0
    for match in URL_RE.finditer(text):
        total += add_plain(text[position:match.start()])
        total += X_URL_WEIGHT
        position = match.end()
    total += add_plain(text[position:])

    return total


def count_hashtags(text):
    return len(HASHTAG_RE.findall(str(text)))


# Claims the project must never make unless the owner adds them to
# RINGSHIFT_KNOWLEDGE.md. Matching is on lowercase text.
_UNSUPPORTED_CLAIM_PATTERNS = [
    (r"\b\d[\d,.]*\s*\+?\s*(k|m)?\s*(players|downloads|wishlists?|sales|copies|reviews?|users|followers|fans)\b",
     "number attached to players/sales/reviews"),
    (r"\b(award|awards|award-winning|best-selling|bestselling|bestseller|top-rated|critically acclaimed)\b",
     "award or ranking claim"),
    (r"\b(viral|trending|going viral|blowing up)\b", "popularity claim"),
    (r"\b(players (love|are loving|say|said|think|keep)|people (love|are loving|are saying)|everyone (loves|is))\b",
     "claim about what players think"),
    (r"\b(5[- ]?star|five[- ]?star|rave reviews?|glowing reviews?)\b", "review claim"),
    (r"\b(release date|out now|now available|available now|launching|launch day|launches on|coming soon|coming out)\b",
     "release or availability claim"),
    (r"\b(steam|itch\.io|app store|google play|epic games|gog)\b", "storefront or platform not in the knowledge base"),
    (r"\b(sold out|selling fast|limited time|free to play|free-to-play)\b", "commercial claim"),
    (r"\b(patch notes?|version \d|v\d+\.\d+|update \d+)\b", "version or update claim"),
]
_UNSUPPORTED_CLAIMS = [(re.compile(p, re.IGNORECASE), why) for p, why in _UNSUPPORTED_CLAIM_PATTERNS]

BANNED_OPENER_RE = re.compile(r"^(equip|try|check out|ringshift is)\b", re.IGNORECASE)


def find_unsupported_claims(text):
    """Reasons `text` looks like it asserts something the knowledge base never said."""
    found = []
    for pattern, why in _UNSUPPORTED_CLAIMS:
        if pattern.search(str(text)):
            found.append(why)
    return found


def validate_post(text, allow_urls=False):
    """Return a list of human-readable problems; an empty list means the post is acceptable."""
    problems = []
    text = str(text or "").strip()

    if not text:
        return ["post is empty"]

    length = weighted_length(text)
    if length > X_MAX_WEIGHTED_LENGTH:
        problems.append(f"too long for X ({length}/{X_MAX_WEIGHTED_LENGTH} weighted characters)")

    hashtag_count = count_hashtags(text)
    if hashtag_count > MAX_HASHTAGS:
        problems.append(f"more than {MAX_HASHTAGS} hashtags")

    if MENTION_RE.search(text):
        problems.append("contains an @mention (automatic posts never tag other accounts)")

    if URL_RE.search(text) and not allow_urls:
        problems.append("contains a URL but no project URL is configured")

    if BANNED_OPENER_RE.match(text.lstrip("\"'“”‘’ ")):
        problems.append("starts with a banned opener")

    for why in find_unsupported_claims(text):
        problems.append(f"unsupported claim: {why}")

    return problems


# ---- duplicate / near-duplicate detection -----------------------------------

_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "you", "your", "are", "was", "but",
    "not", "can", "has", "have", "had", "its", "it's", "just", "than", "then", "them",
    "they", "from", "into", "out", "all", "any", "one", "how", "who", "why", "what",
    "when", "where", "does", "did", "get", "got", "would", "could", "should", "there",
    "ringshift", "indiegames", "indiedev",
}


def squash(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _tokens(text):
    return {
        word for word in squash(text).split()
        if len(word) > 2 and word not in _STOPWORDS
    }


def similarity(a, b):
    """0..1 score; high when two posts share wording or the same core idea."""
    squashed_a, squashed_b = squash(a), squash(b)

    if not squashed_a or not squashed_b:
        return 0.0

    sequence = difflib.SequenceMatcher(None, squashed_a, squashed_b).ratio()

    tokens_a, tokens_b = _tokens(a), _tokens(b)
    if tokens_a and tokens_b:
        overlap = len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b))
        jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    else:
        overlap = jaccard = 0.0

    # Containment of a short post inside a long one is a duplicate even if the
    # sequence ratio is low.
    contained = 1.0 if (len(squashed_a) > 12 and squashed_a in squashed_b) else 0.0

    return max(sequence, jaccard, contained, overlap * 0.9)


def most_similar(post, existing_posts):
    """(score, text) of the closest earlier post, or (0.0, None)."""
    best = (0.0, None)
    for other in existing_posts:
        score = similarity(post, other)
        if score > best[0]:
            best = (score, other)
    return best


def is_near_duplicate(post, existing_posts, threshold=0.7):
    score, other = most_similar(post, existing_posts)
    return score >= threshold, score, other


# ---- reading earlier posts out of the markdown files -------------------------

def extract_generated_posts(markdown):
    """Post texts from content/generated_posts.md ("## Post NNN" ... "### Critic")."""
    posts = []
    # The text before the first "## Post NNN" header is the file title, not a post.
    # The very first historical entry was written with an escaped header ("\## Post 001").
    blocks = re.split(r"(?m)^\\?#{2}\s*Post\s+\d+\s*$", str(markdown or ""))
    for block in blocks[1:]:
        body = re.split(r"(?m)^\\?#{2,3}\s*Critic\b", block, maxsplit=1)[0].strip()
        if body:
            posts.append(body)
    return posts


def extract_analytics_posts(markdown):
    """Post texts from data/analytics.md ("### Post" ... "### Metrics")."""
    posts = []
    for match in re.finditer(r"(?ms)^### Post\s*\n(.*?)(?=^### Metrics)", str(markdown or "")):
        body = match.group(1).strip()
        if body:
            posts.append(body)
    return posts
