"""
Deterministic preference retrieval for founder-memory injection.

Retrieves active founder preferences matching a message context using
scope-keyword and word-overlap scoring.  Read-only — never mutates
preference records.

Only ESTABLISHED and EXPLICIT preferences with active=True are eligible.
EMERGING, OBSERVED, and CONFLICTED preferences are never returned.
"""

import re
from database import supabase


# ── safety gate ──────────────────────────────────────────────────────

LIVE_STATUSES = {"ESTABLISHED", "EXPLICIT"}

# Relevance threshold — preferences below this score are discarded.
MIN_RELEVANCE = 0.35

# Maximum preferences returned; also enforced as a hard cap.
DEFAULT_LIMIT = 5
MAX_LIMIT = 10


# ── scope → keyword maps ─────────────────────────────────────────────

SCOPE_KEYWORDS = {
    "pricing": [
        "pricing", "price", "quote", "quotation", "discount",
        "wholesale", "bulk", "cost", "rate",
    ],
    "urgent": [
        "urgent", "immediately", "today", "emergency",
        "third follow-up", "third follow up", "complaint",
        "escalate", "publicly", "legal",
    ],
    "first_time": [
        "first-time", "first time", "new", "first",
    ],
    "attention_routing": [
        "review", "approve", "handle", "attention", "decision",
    ],
    "authority_boundary": [
        "authority", "boundary", "approval", "permission", "delegate",
    ],
    "communication_style": [
        "tone", "voice", "style", "formal", "casual", "friendly",
    ],
}

# Allow multi-word keywords to match in running text.
# Pre-compiled: we join the keyword tokens and look for them in the
# cleaned message text.


# ── text normalisation ────────────────────────────────────────────────

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
}


def normalize_text(text):
    """
    Deterministic text normalisation: lowercased, punctuation-stripped,
    whitespace-normalised, stop words removed.

    Returns a set of token strings.
    """
    if not text:
        return set()
    cleaned = re.sub(r"[^\w\s]", "", text.lower())
    tokens = cleaned.split()
    return {t for t in tokens if t and t not in STOP_WORDS}


def _message_text(message_context):
    """Build a single searchable string from the message context."""
    parts = []
    for key in ("subject", "body", "sender", "channel", "intent", "product_service"):
        val = (message_context or {}).get(key)
        if val:
            parts.append(str(val))
    return " ".join(parts)


# ── relevance scoring ─────────────────────────────────────────────────

def score_relevance(preference, message_context):
    """
    Compute a deterministic relevance score between a preference and a
    message context.  Returns a float in [0.0, 1.0].

    Formula (documented):
        scope_match  = fraction of scope-keywords found in message text
        word_overlap = Jaccard similarity between rule tokens and
                       message tokens
        lifecycle_bonus = 0.10 for EXPLICIT, 0.00 for ESTABLISHED

        raw = scope_match × 0.50 + word_overlap × 0.40 + lifecycle_bonus

    The result is clamped to [0.0, 1.0].
    """
    scope = preference.get("scope") or ""
    rule_text = preference.get("rule") or ""
    message_raw = _message_text(message_context)

    # ── scope keyword match ──────────────────────────────────────
    scope_lower = scope.lower()
    msg_lower = message_raw.lower()

    # Find all matching scope tags and pick the most specific
    # one — the tag that appears closest to the end of the scope
    # string.  E.g. for "attention_routing_pricing", "pricing"
    # (at position 18) is more specific than "attention_routing"
    # (at position 0).
    matching = [
        (scope_lower.rfind(tag), tag)
        for tag in SCOPE_KEYWORDS
        if tag in scope_lower
    ]
    matching.sort(key=lambda x: x[0], reverse=True)
    best_tag = matching[0][1] if matching else None

    scope_hits = 0
    scope_total = 0

    if best_tag:
        keywords = SCOPE_KEYWORDS[best_tag]
        scope_total = len(keywords)
        for kw in keywords:
            if kw in msg_lower:
                scope_hits += 1

    scope_match = (scope_hits / scope_total) if scope_total > 0 else 0.0

    # ── word overlap ─────────────────────────────────────────────
    # What fraction of the rule's signal words appear in the
    # message?  Using |intersection| / |rule_tokens| instead of
    # Jaccard avoids penalising long messages (which would make
    # the union huge and suppress all scores).
    rule_tokens = normalize_text(rule_text)
    message_tokens = normalize_text(message_raw)

    if rule_tokens:
        intersection = rule_tokens & message_tokens
        word_overlap = len(intersection) / len(rule_tokens)
    else:
        word_overlap = 0.0

    # ── lifecycle bonus ───────────────────────────────────────────
    status = preference.get("memory_status", "")
    lifecycle_bonus = 0.10 if status == "EXPLICIT" else 0.0

    # ── combine ───────────────────────────────────────────────────
    raw = scope_match * 0.50 + word_overlap * 0.40 + lifecycle_bonus
    return min(max(raw, 0.0), 1.0)


# ── main retrieval ────────────────────────────────────────────────────

def get_active_preferences(founder_id=None):
    """
    Return all live-routing-eligible preferences from Supabase.

    Safety gate: only rows with ``active = True`` AND
    ``memory_status IN ('ESTABLISHED', 'EXPLICIT')`` are returned.

    Founder isolation:
        - founder_id=None  → only preferences where founder_id IS NULL
        - founder_id=<uuid> → only preferences matching that founder_id

    Ordered: EXPLICIT first, then ESTABLISHED, then by confidence,
    supporting_observations, and last_reinforced_at.
    """
    query = (
        supabase
        .table("founder_preferences")
        .select("*")
        .eq("active", True)
        .in_("memory_status", list(LIVE_STATUSES))
    )

    # Founder isolation — never mix founders
    if founder_id is None:
        query = query.is_("founder_id", "null")
    else:
        query = query.eq("founder_id", founder_id)

    response = query.execute()
    prefs = response.data or []

    # Sort: EXPLICIT > ESTABLISHED, then high > medium > low confidence,
    # then observations descending, then last_reinforced_at descending.
    # Python sort is stable, so we apply keys in reverse priority order.
    status_rank = {"EXPLICIT": 0, "ESTABLISHED": 1}
    conf_rank = {"high": 0, "medium": 1, "low": 2}

    prefs.sort(
        key=lambda p: p.get("last_reinforced_at") or p.get("created_at") or "",
        reverse=True,
    )
    prefs.sort(
        key=lambda p: (
            status_rank.get(p.get("memory_status", ""), 99),
            conf_rank.get(p.get("confidence", "low"), 99),
            -(p.get("supporting_observations") or 0),
        )
    )

    return prefs


def get_relevant_preferences(message_context, founder_id=None, limit=None):
    """
    Retrieve, score, rank, and filter active preferences against a
    message context.

    Parameters
    ----------
    message_context : dict
        Keys may include subject, body, sender, channel, intent,
        product_service.
    founder_id : str | None
        Isolate to a single founder (or NULL-founder when None).
    limit : int | None
        Max results.  Default 5, hard-cap 10.

    Returns
    -------
    dict
        {
            "count": int,
            "preferences": [...],
            "memory_conflict": bool | None,
            "conflicting_preference_ids": [...] | None,
        }
    """
    if limit is None:
        limit = DEFAULT_LIMIT
    limit = min(limit, MAX_LIMIT)

    # ── 1. fetch active preferences ───────────────────────────────
    candidates = get_active_preferences(founder_id=founder_id)

    # ── 2. score each candidate ───────────────────────────────────
    scored = []
    for pref in candidates:
        relevance = score_relevance(pref, message_context)
        if relevance >= MIN_RELEVANCE:
            scored.append((pref, relevance))

    # Sort by relevance descending, then status (EXPLICIT > ESTABLISHED)
    status_rank = {"EXPLICIT": 0, "ESTABLISHED": 1}
    scored.sort(
        key=lambda item: (
            -item[1],
            status_rank.get(item[0].get("memory_status", ""), 99),
        )
    )

    # ── 3. detect conflicts among top candidates ──────────────────
    top_prefs = [p for p, _ in scored[:limit]]
    conflict_result = detect_conflicts(top_prefs)

    if conflict_result["memory_conflict"]:
        # Exclude conflicting preferences from normal list
        conflict_ids = set(conflict_result["conflicting_preference_ids"])
        clean = [p for p in top_prefs if p.get("id") not in conflict_ids]
    else:
        conflict_ids = None
        clean = top_prefs

    # ── 4. build compact response ─────────────────────────────────
    result_prefs = []
    for pref in clean:
        # Attach the relevance score we computed
        matching_score = next(
            (s for p, s in scored if p.get("id") == pref.get("id")), 0.0
        )
        result_prefs.append({
            "id": pref.get("id"),
            "rule": pref.get("rule"),
            "scope": pref.get("scope"),
            "memory_status": pref.get("memory_status"),
            "confidence": pref.get("confidence"),
            "supporting_observations": pref.get("supporting_observations"),
            "relevance_score": round(matching_score, 4),
        })

    return {
        "count": len(result_prefs),
        "preferences": result_prefs,
        "memory_conflict": conflict_result["memory_conflict"],
        "conflicting_preference_ids": conflict_ids,
    }


# ── conflict detection ────────────────────────────────────────────────

def detect_conflicts(preferences):
    """
    Check whether any pair of active preferences appear to contradict
    each other.

    If a conflict is found, both conflicting preference IDs are listed
    and excluded from the normal preferences list.  No database write
    occurs — retrieval is read-only.

    Returns
    -------
    dict
        {
            "memory_conflict": bool,
            "conflicting_preference_ids": [str, str] | None,
        }
    """
    if len(preferences) < 2:
        return {"memory_conflict": False, "conflicting_preference_ids": None}

    negation_signals = [
        "no longer", "don't want", "do not want", "stop",
        "never mind", "changed my mind", "scratch that", "forget",
    ]

    for i in range(len(preferences)):
        for j in range(i + 1, len(preferences)):
            rule_a = (preferences[i].get("rule") or "").lower()
            rule_b = (preferences[j].get("rule") or "").lower()

            # Only flag if one rule has negation signals and both rules
            # are in the same scope
            scope_a = preferences[i].get("scope", "")
            scope_b = preferences[j].get("scope", "")

            # Broader conflict: also flag if scopes overlap and one rule
            # negates.
            scopes_related = (
                scope_a == scope_b
                or scope_a.startswith(scope_b)
                or scope_b.startswith(scope_a)
            )

            if not scopes_related:
                continue

            has_negation_a = any(s in rule_a for s in negation_signals)
            has_negation_b = any(s in rule_b for s in negation_signals)

            if has_negation_a or has_negation_b:
                return {
                    "memory_conflict": True,
                    "conflicting_preference_ids": [
                        preferences[i].get("id"),
                        preferences[j].get("id"),
                    ],
                }

    return {"memory_conflict": False, "conflicting_preference_ids": None}
