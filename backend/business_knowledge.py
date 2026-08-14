"""
Deterministic Business Knowledge retrieval for Attention Buddy.

Business Knowledge is FACTUAL information, policies, permissions,
constraints, and operational rules of the business.  It is kept strictly
separate from founder memory (how the founder prefers things handled) and
from the customer message itself.

This module owns Business Knowledge retrieval only.  It is read-only and
deterministic:

    * no WorkBuddy calls
    * no LLM / OpenAI calls
    * no external APIs
    * no vector databases or embedding services
    * no policy synthesis (it only ever returns *stored* rules)

Vocabulary is split into TWO layers so that onboarding a new business
does not require editing this file:

    1. UNIVERSAL CATEGORY VOCABULARY (``CATEGORY_KEYWORDS`` below)
       Generic terminology that applies across many businesses.
       Contributes to the ``category_match`` scoring component.

    2. BUSINESS-SPECIFIC / KNOWLEDGE-SPECIFIC KEYWORDS
       Stored in Supabase (``business_knowledge_keywords``), associated
       with an individual ``business_knowledge`` record, and retrieved
       dynamically.  They act as additional retrieval vocabulary for that
       individual record and contribute to ``category_match`` (OR'd in,
       exactly like a universal category keyword).  Box & Bloom terms such
       as "gift box", "hamper", "logo printing", etc. live HERE, never
       in Python.

Matching is deterministic and boundary-safe:
    * single-word terms  -> whole-token membership (never substring),
                            so "rate" never matches inside "corporate".
    * multi-word terms   -> normalized contiguous phrase matching, so
                            "social media" matches "...on social media".

Singular/plural variants are NOT auto-generated (no fuzzy matching):
legitimate variants must be stored explicitly (the universal vocabulary
and the recommended seed data both list them).

Scoring formula (unchanged):

    raw = category_match   * 0.35
        + applies_to_match * 0.30
        + content_overlap  * 0.25
        + priority_bonus   * 0.10

Database keywords contribute to ``category_match`` only (a keyword match
sets category_match = 1.0 exactly as a universal category keyword does).
The four weights never change and no fifth component is added.  A term
that also appears as an applies_to tag is not double-counted.

Retrieval eligibility (record-specific relevance gate, added):

    A record is only returned when BOTH:
        * relevance_score >= MIN_RELEVANCE, AND
        * it has at least one record-specific signal:
              matched_business_keywords
              OR matched_applies_to
              OR matched_content_tokens
              OR the quantity-context signal (see below)

Generic category keywords (``CATEGORY_KEYWORDS``) and the priority bonus
are NOT record-specific evidence: a high-priority record cannot be
returned solely because its category keywords happen to match.

Quantity-context signal (generic, record-specific):

    A minimum-order / MOQ / order-quantity rule is recognised when the
    message contains BOTH an explicit numeric quantity (e.g. "20",
    "250"; never a year like "2026" or an order id like "a1234") AND a
    custom/personalisation word (``CUSTOM_CONTEXT_WORDS``).  Which records
    qualify as quantity rules is determined data-driven from their own
    title/content/applies_to/keywords (``QUANTITY_RULE_TERMS``), never a
    hard-coded id.  The combined signal sets ``category_match = 1.0`` and
    satisfies the record-specific gate — it adds no new scoring weight.
"""

import re
from datetime import datetime, timezone

from database import supabase


# ── controlled vocabularies ──────────────────────────────────────────

VALID_CATEGORIES = {
    "BUSINESS_INFO",
    "OPENING_HOURS",
    "PRODUCT_SERVICE",
    "PRICING",
    "DISCOUNT",
    "SHIPPING",
    "REFUND_RETURN",
    "CUSTOMER_SERVICE",
    "ESCALATION",
    "AUTHORITY",
    "LEGAL_COMPLIANCE",
    "OTHER",
}

VALID_SOURCE_TYPES = {"MANUAL", "DOCUMENT", "SYSTEM", "IMPORT"}

# ── UNIVERSAL category keyword vocabulary ────────────────────────────
#
# Generic, cross-business terminology only.  Do NOT add business-specific
# terms (e.g. gift box, hamper, artwork, logo) here — those belong in the
# Supabase business_knowledge_keywords table.  Sets are used because order
# is irrelevant; matched keywords are sorted for deterministic output.

CATEGORY_KEYWORDS = {
    "OPENING_HOURS": {
        "open", "opening", "opened",
        "close", "closed", "closing",
        "hours", "weekday", "weekdays",
        "saturday", "saturdays", "sunday", "sundays",
        "weekend", "weekends", "holiday", "holidays",
    },
    "PRICING": {
        "price", "prices", "pricing",
        "quote", "quotes", "quotation", "quotations",
        "cost", "costs", "rate", "rates", "budget",
    },
    "DISCOUNT": {
        "discount", "discounts",
        "promotion", "promotions", "promo", "promos",
        "offer", "offers", "deal", "deals",
    },
    "SHIPPING": {
        "shipping", "ship", "shipped",
        "delivery", "deliver", "delivered",
        "tracking", "track", "carrier", "courier",
        "arrive", "arrived", "arrival",
        "late", "delay", "delayed",
    },
    "REFUND_RETURN": {
        "refund", "refunds", "refunded",
        "return", "returns", "returned",
        "exchange", "exchanges",
        "replacement", "replacements", "replace", "replaced",
        "damaged", "damage", "defective", "faulty",
    },
    "CUSTOMER_SERVICE": {
        "complaint", "complaints", "complain", "complained",
        "support", "customer", "customers",
        "follow-up", "followup",
        "issue", "issues", "problem", "problems", "help", "unresolved",
    },
    "ESCALATION": {
        "urgent", "urgently", "escalate", "escalation",
        "complaint", "complaints", "unresolved",
        "repeat", "repeated",
        "public", "publicly", "post", "posted", "posting",
        "social", "media",
        "legal", "lawyer", "lawyers", "attorney",
        "court", "sue", "lawsuit",
    },
    "AUTHORITY": {
        "approve", "approved", "approval",
        "authorise", "authorised", "authorize", "authorized",
        "refund", "refunds", "compensation",
        "exception", "exceptions",
    },
    "PRODUCT_SERVICE": {
        "product", "products", "service", "services",
    },
    "BUSINESS_INFO": {
        "business", "company", "location", "located",
        "address", "contact", "phone", "email",
    },
    "LEGAL_COMPLIANCE": {
        "privacy", "private", "personal", "data", "information",
        "legal", "compliance", "confidential", "confidentiality",
        "disclose", "disclosure",
    },
}

# ── scoring weights (unchanged) ──────────────────────────────────────

W_CATEGORY = 0.35
W_APPLIES_TO = 0.30
W_CONTENT = 0.25
W_PRIORITY = 0.10

MIN_RELEVANCE = 0.30

DEFAULT_LIMIT = 5
MAX_LIMIT = 10


# ── text normalisation ───────────────────────────────────────────────

STOP_WORDS = {
    "i", "me", "my", "we", "our", "the", "a", "an",
    "to", "for", "of", "in", "on", "at", "is", "are",
    "this", "that", "it", "be", "and", "or", "with",
    "you", "your", "he", "she", "they", "them", "us",
    "not", "no", "but", "so", "if", "then", "than",
    "was", "were", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should",
    "can", "may", "might", "shall", "just", "very", "really",
    "also", "too", "only", "still",
    "before", "after", "during", "until", "between", "since", "while",
}


def normalize_tokens(text):
    """
    Return an ORDERED list of lowercase word tokens.  Every run of
    non-alphanumeric characters (hyphens, underscores, punctuation) is
    treated as a token boundary.  Order is preserved so multi-word
    phrases can be matched contiguously.
    """
    if not text:
        return []
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(text).lower())
    return cleaned.split()


def tokenize(text):
    """
    Return a SET of tokens with stop words removed.  Used for content
    overlap, where common function words would otherwise inflate
    similarity.  Whole-token set membership is boundary-safe.
    """
    return set(normalize_tokens(text)) - STOP_WORDS


def normalize_text(text):
    """Alias kept for clarity: stop-word-stripped token set."""
    return tokenize(text)


# ── matching ─────────────────────────────────────────────────────────

def _phrase_in(phrase_tokens, seq):
    """True when ``phrase_tokens`` appears as a contiguous subsequence."""
    n = len(phrase_tokens)
    m = len(seq)
    if n == 0 or n > m:
        return False
    for i in range(m - n + 1):
        if seq[i:i + n] == phrase_tokens:
            return True
    return False


def _term_matches(term, message_set, message_seq):
    """
    Deterministic, boundary-safe term matching:
        * single-word term  -> whole-token membership (no substring)
        * multi-word term   -> contiguous normalized phrase match
    """
    term_tokens = normalize_tokens(term)
    if not term_tokens:
        return False
    if len(term_tokens) == 1:
        return term_tokens[0] in message_set
    return _phrase_in(term_tokens, message_seq)


def _as_list(value):
    """Normalise a possibly-scalar applies_to/keywords value into a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


# ── quantity-context signal (generic) ────────────────────────────────
#
# A message that states an explicit numeric quantity TOGETHER WITH a
# custom/personalisation context ("20 customised gift boxes") is
# relevant to a minimum-order / MOQ knowledge rule even though it does
# not literally contain that rule's phrases ("minimum order", "moq",
# "customised order").  This is a COMBINED signal: a bare number never
# makes a record relevant, and customisation wording without a number
# never makes a quantity rule relevant.
#
# The signal is available ONLY to records whose own title/content/
# applies_to/keywords clearly concern a minimum order or an order
# quantity (identified data-driven, never by hard-coded record id or
# business name).  This keeps unrelated records (e.g. bulk pricing,
# products-and-services) from matching merely because a number appears.

CUSTOM_CONTEXT_WORDS = {
    "custom", "customise", "customised", "customize", "customized",
    "customisation", "customization",
    "personalise", "personalised", "personalize", "personalized",
    "personalisation", "personalization",
}

QUANTITY_RULE_TERMS = (
    "minimum", "moq", "min order", "min quantity", "order quantity",
)


def _numeric_quantities(seq):
    """
    Return the tokens in ``seq`` that look like explicit order quantities.

    Only whole numeric tokens count.  Plausible years (1900-2100) and
    non-numeric tokens (order ids like "a1234") are excluded, so "2026"
    and "A1234" are never treated as quantity evidence.
    """
    quantities = []
    for tok in seq:
        if not re.fullmatch(r"\d+", tok):
            continue
        value = int(tok)
        if value < 1 or 1900 <= value <= 2100:
            continue
        if tok not in quantities:
            quantities.append(tok)
    return quantities


def _is_quantity_rule(record):
    """
    True when the record's own vocabulary concerns a minimum order or
    an order quantity.  Data-driven and business-agnostic: it inspects
    the record's title, content, applies_to tags and keywords, never a
    hard-coded id or business name.

    Each field/tag/keyword is checked INDEPENDENTLY so a multi-word
    term can never be formed by accidentally concatenating two adjacent
    fields (e.g. "corporate order" + "quantity" must not read as
    "order quantity").
    """
    units = [record.get("title") or "", record.get("content") or ""]
    units.extend(str(t) for t in _as_list(record.get("applies_to")))
    units.extend(str(t) for t in _as_list(record.get("keywords")))
    for unit in units:
        seq = normalize_tokens(unit)
        tokset = set(seq)
        if any(_term_matches(term, tokset, seq) for term in QUANTITY_RULE_TERMS):
            return True
    return False


# ── effective-period filtering ───────────────────────────────────────

def _parse_dt(value):
    """
    Parse an effective_from / effective_until value into a timezone-aware
    datetime.  Missing values return None.  Naive datetimes are assumed to
    be UTC (documented), so no timezone-naive comparison error can occur.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_effective(record, now=None):
    """
    True when ``now`` falls within the record's effective period (when the
    period is defined).  Records with no effective_from are eligible from
    the beginning of time; records with no effective_until never expire.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    effective_from = _parse_dt(record.get("effective_from"))
    effective_until = _parse_dt(record.get("effective_until"))
    if effective_from is not None and now < effective_from:
        return False
    if effective_until is not None and now > effective_until:
        return False
    return True


# ── knowledge-specific keyword loading ───────────────────────────────

def _load_keywords(knowledge_ids):
    """
    Load keywords for the given knowledge ids in ONE query (avoids N+1).

    Returns ``{knowledge_id: [keyword, ...]}``.

    Founder isolation is inherited from the caller, which passes only the
    ids of already-founder-isolated eligible knowledge records.  There is
    no founder_id column on business_knowledge_keywords, so ownership is
    determined exclusively by the parent business_knowledge row.
    """
    if not knowledge_ids:
        return {}
    response = (
        supabase
        .table("business_knowledge_keywords")
        .select("knowledge_id, keyword")
        .in_("knowledge_id", knowledge_ids)
        .execute()
    )
    result = {}
    for row in (response.data or []):
        kid = row.get("knowledge_id")
        kw = row.get("keyword")
        if kid is not None and kw:
            result.setdefault(kid, []).append(str(kw))
    return result


# ── active retrieval ─────────────────────────────────────────────────

def get_active_business_knowledge(founder_id=None):
    """
    Return active Business Knowledge rows from Supabase, each with its
    knowledge-specific keywords attached under ``record["keywords"]``.

    Eligibility (active flag + effective dates + founder isolation) is
    decided FIRST, before any keyword is considered.  Keywords therefore
    can never resurrect an ineligible record.

    Founder isolation (unchanged):
        * founder_id=None   -> only rows where founder_id IS NULL
        * founder_id=<uuid> -> only rows matching that founder
    No mixing, no fallback.
    """
    query = (
        supabase
        .table("business_knowledge")
        .select("*")
        .eq("active", True)
    )
    if founder_id is None:
        query = query.is_("founder_id", "null")
    else:
        query = query.eq("founder_id", founder_id)

    response = query.execute()
    rows = response.data or []

    now = datetime.now(timezone.utc)
    eligible = [r for r in rows if _is_effective(r, now)]

    keyword_map = _load_keywords([r.get("id") for r in eligible])
    for r in eligible:
        r["keywords"] = keyword_map.get(r.get("id"), [])

    return eligible


# ── relevance scoring ────────────────────────────────────────────────

def _build_signals(subject="", body=""):
    """Build the message token signals used by the scorer."""
    text = " ".join([str(subject or ""), str(body or "")])
    seq = normalize_tokens(text)
    return {
        "seq": seq,
        "set": set(seq),
        "set_sw": set(seq) - STOP_WORDS,
    }


def _compute_components(record, signals):
    """
    Compute every scoring component plus explainability diagnostics.

    Mapping of vocabulary to the four components (documented, no double
    counting, weights unchanged):

        * universal ``CATEGORY_KEYWORDS``  -> ``category_match``
        * ``record["keywords"]`` (Supabase) -> ``category_match``
          (a business-keyword match sets category_match = 1.0 exactly as
          a universal category keyword does — binary, never diluted)
        * quantity-context signal         -> ``category_match`` (and the
          record-specific gate).  When a quantity rule (minimum / MOQ /
          order-quantity vocabulary) sees an explicit numeric quantity
          plus custom/personalisation wording in the message, this counts
          as domain evidence — not as a new weighted component.
        * ``applies_to`` tags (row)        -> ``applies_to_match``
        * title/content tokens             -> ``content_overlap``
        * priority                         -> ``priority_bonus``

    A term identical to an applies_to tag is not also counted as a
    business keyword (no double counting across components).
    """
    category = record.get("category") or ""
    applies_to = _as_list(record.get("applies_to"))
    keywords = _as_list(record.get("keywords"))
    title = record.get("title") or ""
    content = record.get("content") or ""
    priority = record.get("priority")

    message_set = signals["set"]
    message_seq = signals["seq"]
    message_set_sw = signals["set_sw"]

    # ── 1. universal category keywords ───────────────────────────
    matched_category = sorted(
        kw for kw in CATEGORY_KEYWORDS.get(category, ())
        if _term_matches(kw, message_set, message_seq)
    )

    # ── 2. applies_to tags (unique, order-preserved) ─────────────
    applies_to_forms = set()
    applies_to_terms = []
    for tag in applies_to:
        form = " ".join(normalize_tokens(tag))
        if form and form not in applies_to_forms:
            applies_to_forms.add(form)
            applies_to_terms.append(tag)

    matched_applies_to = [
        t for t in applies_to_terms
        if _term_matches(t, message_set, message_seq)
    ]

    # ── 3. business-specific keywords (Supabase) ─────────────────
    matched_business_keywords = sorted(
        kw for kw in keywords
        if " ".join(normalize_tokens(kw)) not in applies_to_forms
        and _term_matches(kw, message_set, message_seq)
    )

    # ── 3b. quantity-context signal (generic) ─────────────────────
    matched_quantity = _numeric_quantities(message_seq)
    matched_custom_context = sorted(
        w for w in CUSTOM_CONTEXT_WORDS if w in message_set_sw
    )
    quantity_context = bool(
        matched_quantity
        and matched_custom_context
        and _is_quantity_rule(record)
    )

    # category_match is binary: universal category keyword, any
    # record-specific business keyword, or the quantity-context signal
    # marks this record's domain.
    category_match = (
        1.0 if (matched_category or matched_business_keywords
                or quantity_context) else 0.0
    )

    applies_to_match = (
        len(matched_applies_to) / len(applies_to_terms)
        if applies_to_terms else 0.0
    )

    # ── 4. title/content token overlap ───────────────────────────
    content_tokens = tokenize(f"{title} {content}")
    matched_content = sorted(content_tokens & message_set_sw)
    content_overlap = (
        len(matched_content) / len(content_tokens) if content_tokens else 0.0
    )

    # ── 5. priority bonus ────────────────────────────────────────
    try:
        priority_val = float(priority)
    except (TypeError, ValueError):
        priority_val = 0.0
    priority_bonus = min(max(priority_val, 0.0), 100.0) / 100.0

    raw = (
        W_CATEGORY * category_match
        + W_APPLIES_TO * applies_to_match
        + W_CONTENT * content_overlap
        + W_PRIORITY * priority_bonus
    )
    clamped = min(max(raw, 0.0), 1.0)

    # ── 6. record-specific relevance gate ────────────────────────
    # A record must carry at least ONE record-specific signal to be
    # eligible for final retrieval.  A generic category keyword match
    # (category_match) and priority are NOT record-specific evidence.
    # This prevents an unrelated high-priority record in the same broad
    # category from being returned solely on category + priority.
    has_record_specific_match = bool(
        matched_business_keywords or matched_applies_to or matched_content
        or quantity_context
    )

    return {
        "category_match": category_match,
        "applies_to_match": applies_to_match,
        "content_overlap": content_overlap,
        "priority_bonus": priority_bonus,
        "raw": raw,
        "relevance_score": clamped,
        "has_record_specific_match": has_record_specific_match,
        "matched_category_keywords": matched_category,
        "matched_business_keywords": matched_business_keywords,
        "matched_applies_to": matched_applies_to,
        "matched_content_tokens": matched_content,
        "quantity_context_match": quantity_context,
        "matched_quantity": matched_quantity,
        "matched_custom_context": matched_custom_context,
    }


def score_relevance(record, signals):
    """Return the clamped deterministic relevance score in [0.0, 1.0]."""
    return _compute_components(record, signals)["relevance_score"]


def score_components(record, signals):
    """
    Return the full scoring breakdown plus explainability diagnostics
    (matched_category_keywords, matched_business_keywords,
    matched_applies_to, matched_content_tokens).
    """
    return _compute_components(record, signals)


# ── main retrieval ───────────────────────────────────────────────────

def get_relevant_business_knowledge(
    subject="",
    body="",
    founder_id=None,
    limit=5,
):
    """
    Retrieve, score, rank, and filter active Business Knowledge against a
    message subject/body.

    Parameters
    ----------
    subject : str
    body : str
    founder_id : str | None
        Isolate to a single founder (or NULL-founder when None).
    limit : int
        Max results.  Default 5, hard-cap 10.

    Returns
    -------
    dict
        {
            "count": int,
            "knowledge": [
                {
                    "knowledge_id": str,
                    "category": str,
                    "title": str,
                    "content": str,
                    "priority": int,
                    "relevance_score": float,
                    "source_type": str,
                    "source_reference": str | None,
                    "matched_keywords": [str, ...],   # business keywords
                },
                ...
            ],
        }
    """
    if limit is None:
        limit = DEFAULT_LIMIT
    limit = min(int(limit), MAX_LIMIT)
    limit = max(limit, 1)

    candidates = get_active_business_knowledge(founder_id=founder_id)
    signals = _build_signals(subject, body)

    scored = []
    for record in candidates:
        components = _compute_components(record, signals)
        if (
            components["relevance_score"] >= MIN_RELEVANCE
            and components["has_record_specific_match"]
        ):
            scored.append((record, components))

    scored.sort(
        key=lambda item: (
            -item[1]["relevance_score"],
            -(item[0].get("priority") or 0),
        )
    )

    knowledge = []
    for record, components in scored[:limit]:
        knowledge.append({
            "knowledge_id": record.get("id"),
            "category": record.get("category"),
            "title": record.get("title"),
            "content": record.get("content"),
            "priority": record.get("priority"),
            "relevance_score": round(components["relevance_score"], 4),
            "source_type": record.get("source_type"),
            "source_reference": record.get("source_reference"),
            "matched_keywords": components["matched_business_keywords"],
        })

    return {
        "count": len(knowledge),
        "knowledge": knowledge,
    }
