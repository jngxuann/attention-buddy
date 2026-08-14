"""
Unit tests for the deterministic Business Knowledge layer (configurable
business-specific vocabulary).

Covers:
  - token normalisation (boundary-safe, no substring false positives)
  - phrase matching (multi-word, contiguous, case/normalization-safe)
  - singular/plural exact-variant behaviour (documented)
  - universal vs business-specific vocabulary separation
  - database keywords contributing to category_match (no fifth component)
  - active / effective-date filtering
  - founder isolation (knowledge AND its keywords)
  - inactive knowledge never resurrected by keywords
  - priority alone never creates relevance
  - empty retrieval, limits, return shape
  - Box & Bloom product query, Sarah public complaint, Daniel bulk order,
    tuition-centre (business-agnostic), bouquet-vs-lawyer
  - pipeline business_context construction

All Supabase access is mocked; these tests do not touch the network.
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import business_knowledge as bk
import pipeline


# ── helpers ──────────────────────────────────────────────────────────

def _resp(data):
    m = MagicMock()
    m.data = data
    return m


def _chain_mock(execute_results):
    """Return a chainable mock Supabase client.  Each element of
    ``execute_results`` is the ``.data`` list returned by successive
    ``.execute()`` calls."""
    chain = MagicMock()
    chain.table.return_value = chain
    chain.select.return_value = chain
    chain.eq.return_value = chain
    chain.is_.return_value = chain
    chain.in_.return_value = chain
    chain.limit.return_value = chain
    chain.execute.side_effect = [_resp(d) for d in execute_results]
    return chain


def _signals(subject="", body=""):
    return bk._build_signals(subject, body)


def _record(**overrides):
    rec = {
        "id": "bk-001",
        "founder_id": None,
        "category": "PRICING",
        "title": "Bulk pricing eligibility",
        "content": "Orders of 200 units or more may qualify for bulk pricing.",
        "applies_to": ["bulk", "order"],
        "keywords": ["bulk order", "wholesale"],
        "priority": 80,
        "active": True,
        "source_type": "MANUAL",
        "source_reference": None,
        "effective_from": None,
        "effective_until": None,
    }
    rec.update(overrides)
    return rec


# ── token normalisation ──────────────────────────────────────────────

class TokenNormalisationTests(unittest.TestCase):
    def test_lowercase_and_punctuation_stripped(self):
        self.assertEqual(
            bk.normalize_tokens("Hello, WORLD! 123"),
            ["hello", "world", "123"],
        )

    def test_hyphen_and_underscore_split(self):
        self.assertEqual(
            bk.normalize_tokens("follow-up opening_hours"),
            ["follow", "up", "opening", "hours"],
        )

    def test_tokenize_removes_stopwords(self):
        self.assertEqual(bk.tokenize("the open hours"), {"open", "hours"})

    def test_empty_input(self):
        self.assertEqual(bk.normalize_tokens(""), [])
        self.assertEqual(bk.tokenize(""), set())
        self.assertEqual(bk.normalize_tokens(None), [])


# ── phrase matching ──────────────────────────────────────────────────

class PhraseMatchingTests(unittest.TestCase):
    def test_social_media_phrase_match(self):
        sig = _signals("", "I will post about this on social media.")
        self.assertTrue(bk._term_matches("social media", sig["set"], sig["seq"]))

    def test_logo_printing_phrase_match(self):
        sig = _signals("", "Do you provide logo printing for gift boxes?")
        self.assertTrue(bk._term_matches("logo printing", sig["set"], sig["seq"]))

    def test_gift_box_does_not_match_gift_boxes(self):
        """Exact variant matching: 'gift box' != 'gift boxes'."""
        sig = _signals("", "We need customised gift boxes.")
        self.assertFalse(bk._term_matches("gift box", sig["set"], sig["seq"]))

    def test_gift_boxes_matches(self):
        sig = _signals("", "We need customised gift boxes.")
        self.assertTrue(bk._term_matches("gift boxes", sig["set"], sig["seq"]))

    def test_phrase_requires_adjacency(self):
        sig = _signals("", "I need a gift and a box separately.")
        self.assertFalse(bk._term_matches("gift box", sig["set"], sig["seq"]))


# ── substring safety ─────────────────────────────────────────────────

class SubstringSafetyTests(unittest.TestCase):
    def test_rate_does_not_match_inside_corporate(self):
        sig = _signals("", "Corporate event")
        self.assertFalse(bk._term_matches("rate", sig["set"], sig["seq"]))

    def test_art_does_not_match_party(self):
        sig = _signals("", "party")
        self.assertFalse(bk._term_matches("art", sig["set"], sig["seq"]))

    def test_rate_matches_standalone(self):
        sig = _signals("", "corporate rate")
        self.assertTrue(bk._term_matches("rate", sig["set"], sig["seq"]))


# ── vocabulary separation ────────────────────────────────────────────

class VocabularySeparationTests(unittest.TestCase):
    def test_no_business_specific_terms_in_python(self):
        business_terms = [
            "gift", "gifts", "gift box", "gift boxes", "hamper", "hampers",
            "bouquet", "bouquets", "artwork", "sleeve", "sleeves",
            "logo", "logos", "logo printing", "packaging",
            "wholesale", "bulk", "bulk order", "corporate gifting",
        ]
        for category, keywords in bk.CATEGORY_KEYWORDS.items():
            for term in business_terms:
                self.assertNotIn(
                    term, keywords,
                    f"{term!r} must not be hard-coded in {category}",
                )

    def test_product_service_is_generic(self):
        self.assertEqual(
            bk.CATEGORY_KEYWORDS["PRODUCT_SERVICE"],
            {"product", "products", "service", "services"},
        )


# ── scoring components ───────────────────────────────────────────────

class ScoringTests(unittest.TestCase):
    def test_category_match_universal(self):
        c = bk.score_components(_record(category="PRICING"), _signals("", "pricing"))
        self.assertEqual(c["category_match"], 1.0)
        self.assertIn("pricing", c["matched_category_keywords"])

    def test_db_keyword_sets_category_match(self):
        rec = _record(
            category="OTHER",
            applies_to=[],
            keywords=["replacement class"],
            content="Make-up lessons must be arranged with the centre.",
        )
        c = bk.score_components(
            rec, _signals("", "My child missed class. Can we arrange a replacement class?")
        )
        self.assertEqual(c["category_match"], 1.0)
        self.assertIn("replacement class", c["matched_business_keywords"])
        self.assertEqual(c["matched_category_keywords"], [])

    def test_applies_to_match_fraction(self):
        rec = _record(applies_to=["pricing", "wholesale", "bulk", "corporate"])
        c = bk.score_components(rec, _signals("", "bulk pricing"))
        self.assertAlmostEqual(c["applies_to_match"], 2 / 4)

    def test_priority_bonus(self):
        c = bk.score_components(_record(priority=80), _signals(""))
        self.assertAlmostEqual(c["priority_bonus"], 0.8)

    def test_priority_bonus_clamped(self):
        c = bk.score_components(_record(priority=500), _signals(""))
        self.assertAlmostEqual(c["priority_bonus"], 1.0)

    def test_final_score_clamped(self):
        rec = _record(
            category="PRICING", applies_to=["pricing"], content="pricing", priority=9999,
            keywords=["pricing"],
        )
        s = bk.score_relevance(rec, _signals("", "pricing"))
        self.assertLessEqual(s, 1.0)
        self.assertGreaterEqual(s, 0.0)

    def test_relevance_threshold_constant(self):
        self.assertEqual(bk.MIN_RELEVANCE, 0.30)

    def test_irrelevant_high_priority_below_threshold(self):
        """Priority alone must never make unrelated knowledge relevant."""
        rec = _record(
            category="OTHER",
            title="Unrelated",
            content="nothing to match here",
            applies_to=[],
            keywords=[],
            priority=100,
        )
        c = bk.score_components(rec, _signals("", "are you open this saturday"))
        self.assertLess(c["relevance_score"], bk.MIN_RELEVANCE)
        self.assertAlmostEqual(c["relevance_score"], 0.10)

    def test_no_double_count_keyword_vs_applies_to(self):
        """A term present as both applies_to and keyword is not double counted."""
        rec = _record(
            category="PRICING",
            applies_to=["bulk"],
            keywords=["bulk"],
            content="zzzz",
        )
        c = bk.score_components(rec, _signals("", "bulk"))
        # 'bulk' is counted as applies_to (matched_applies_to), not as a
        # business keyword (matched_business_keywords).
        self.assertIn("bulk", c["matched_applies_to"])
        self.assertNotIn("bulk", c["matched_business_keywords"])


# ── effective-date filtering ─────────────────────────────────────────

class EffectiveDateTests(unittest.TestCase):
    NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_future_effective_from_excluded(self):
        self.assertFalse(bk._is_effective(
            {"effective_from": "2026-06-01T00:00:00+00:00"}, now=self.NOW,
        ))

    def test_past_effective_until_excluded(self):
        self.assertFalse(bk._is_effective(
            {"effective_until": "2025-06-01T00:00:00+00:00"}, now=self.NOW,
        ))

    def test_currently_valid_period_included(self):
        self.assertTrue(bk._is_effective({
            "effective_from": "2025-06-01T00:00:00+00:00",
            "effective_until": "2026-06-01T00:00:00+00:00",
        }, now=self.NOW))

    def test_no_dates_included(self):
        self.assertTrue(bk._is_effective({}, now=self.NOW))

    def test_naive_datetime_assumed_utc(self):
        self.assertTrue(bk._is_effective(
            {"effective_from": "2025-01-01T00:00:00"}, now=self.NOW,
        ))


# ── active retrieval (mocked Supabase) ───────────────────────────────

class ActiveRetrievalTests(unittest.TestCase):
    def test_active_filter_applied_at_query(self):
        chain = _chain_mock([[], []])
        with patch("business_knowledge.supabase") as sb:
            sb.table.return_value = chain
            bk.get_active_business_knowledge(founder_id=None)
        chain.eq.assert_any_call("active", True)

    def test_founder_isolation_null(self):
        chain = _chain_mock([[], []])
        with patch("business_knowledge.supabase") as sb:
            sb.table.return_value = chain
            bk.get_active_business_knowledge(founder_id=None)
        chain.is_.assert_called_with("founder_id", "null")
        eq_calls = [c[0] for c in chain.eq.call_args_list]
        self.assertNotIn(("founder_id", "some-uuid"), eq_calls)

    def test_founder_isolation_specific(self):
        chain = _chain_mock([[], []])
        with patch("business_knowledge.supabase") as sb:
            sb.table.return_value = chain
            bk.get_active_business_knowledge(founder_id="some-uuid")
        chain.eq.assert_any_call("founder_id", "some-uuid")
        self.assertEqual(chain.is_.call_count, 0)

    def test_effective_period_filtered(self):
        rows = [
            _record(id="future", effective_from="2999-01-01T00:00:00+00:00"),
            _record(id="expired", effective_until="2000-01-01T00:00:00+00:00"),
            _record(id="valid"),
        ]
        chain = _chain_mock([rows, []])
        with patch("business_knowledge.supabase") as sb:
            sb.table.return_value = chain
            result = bk.get_active_business_knowledge(founder_id=None)
        self.assertEqual({r["id"] for r in result}, {"valid"})

    def test_keywords_attached_to_records(self):
        chain = _chain_mock([
            [
                {"id": "k1", "category": "OTHER", "title": "a", "content": "b",
                 "applies_to": [], "priority": 50, "active": True},
                {"id": "k2", "category": "OTHER", "title": "a", "content": "b",
                 "applies_to": [], "priority": 50, "active": True},
            ],
            [
                {"knowledge_id": "k1", "keyword": "gift box"},
                {"knowledge_id": "k1", "keyword": "hamper"},
            ],
        ])
        with patch("business_knowledge.supabase") as sb:
            sb.table.return_value = chain
            result = bk.get_active_business_knowledge(founder_id=None)
        by_id = {r["id"]: r for r in result}
        self.assertEqual(by_id["k1"]["keywords"], ["gift box", "hamper"])
        self.assertEqual(by_id["k2"]["keywords"], [])

    def test_keyword_query_scoped_to_eligible_ids(self):
        """Ineffective knowledge is excluded from the keyword query."""
        future = {"id": "future", "category": "OTHER", "title": "x", "content": "y",
                  "applies_to": [], "priority": 50, "active": True,
                  "effective_from": "2999-01-01T00:00:00+00:00"}
        valid = {"id": "valid", "category": "OTHER", "title": "x", "content": "y",
                 "applies_to": [], "priority": 50, "active": True}
        chain = _chain_mock([
            [future, valid],
            [{"knowledge_id": "valid", "keyword": "super-special-test-keyword"}],
        ])
        with patch("business_knowledge.supabase") as sb:
            sb.table.return_value = chain
            result = bk.get_active_business_knowledge(founder_id=None)
        self.assertEqual([r["id"] for r in result], ["valid"])
        self.assertEqual(chain.in_.call_args[0][1], ["valid"])

    def test_founder_isolation_scopes_keywords(self):
        """Keyword query is scoped to the founder's own knowledge ids."""
        chain = _chain_mock([
            [{"id": "ka", "founder_id": "founder-a", "category": "OTHER",
              "title": "a", "content": "b", "applies_to": [], "priority": 50,
              "active": True}],
            [],
        ])
        with patch("business_knowledge.supabase") as sb:
            sb.table.return_value = chain
            bk.get_active_business_knowledge(founder_id="founder-a")
        chain.in_.assert_called_once()
        self.assertEqual(chain.in_.call_args[0][0], "knowledge_id")
        self.assertEqual(chain.in_.call_args[0][1], ["ka"])


# ── relevant retrieval (mocked active retrieval) ─────────────────────

class RelevantRetrievalTests(unittest.TestCase):
    def test_empty_retrieval_when_no_match(self):
        with patch(
            "business_knowledge.get_active_business_knowledge",
            return_value=[_record(keywords=[], applies_to=[], category="OTHER", content="zzzz")],
        ):
            result = bk.get_relevant_business_knowledge(
                subject="unrelated", body="gibberish"
            )
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["knowledge"], [])

    def test_max_limit_enforced(self):
        records = [
            _record(
                id=f"bk-{i}",
                category="PRICING",
                applies_to=["pricing"],
                keywords=[],
                content="pricing",
                priority=80,
            )
            for i in range(20)
        ]
        with patch(
            "business_knowledge.get_active_business_knowledge",
            return_value=records,
        ):
            result = bk.get_relevant_business_knowledge(subject="pricing", limit=100)
        self.assertEqual(result["count"], bk.MAX_LIMIT)
        self.assertEqual(len(result["knowledge"]), bk.MAX_LIMIT)

    def test_default_limit(self):
        records = [
            _record(
                id=f"bk-{i}",
                category="PRICING",
                applies_to=["pricing"],
                keywords=[],
                content="pricing",
                priority=80,
            )
            for i in range(20)
        ]
        with patch(
            "business_knowledge.get_active_business_knowledge",
            return_value=records,
        ):
            result = bk.get_relevant_business_knowledge(subject="pricing")
        self.assertEqual(result["count"], bk.DEFAULT_LIMIT)

    def test_relevant_return_shape(self):
        with patch(
            "business_knowledge.get_active_business_knowledge",
            return_value=[_record(keywords=["bulk"], applies_to=["pricing"])],
        ):
            result = bk.get_relevant_business_knowledge(
                subject="bulk pricing", body="quote"
            )
        self.assertIn("count", result)
        self.assertIn("knowledge", result)
        for item in result["knowledge"]:
            for key in [
                "knowledge_id", "category", "title", "content",
                "priority", "relevance_score", "source_type",
                "source_reference", "matched_keywords",
            ]:
                self.assertIn(key, item)

    def test_matched_keywords_in_output(self):
        with patch(
            "business_knowledge.get_active_business_knowledge",
            return_value=[_record(
                keywords=["bulk order", "wholesale"],
                applies_to=[],
                category="PRICING",
                content="bulk pricing",
            )],
        ):
            result = bk.get_relevant_business_knowledge(
                subject="", body="can I get a bulk order quote?"
            )
        item = result["knowledge"][0]
        self.assertIn("bulk order", item["matched_keywords"])
        self.assertNotIn("wholesale", item["matched_keywords"])


# ── integration test cases (spec sections 18-22) ────────────────────

class IntegrationTestCaseScoring(unittest.TestCase):
    def test_box_bloom_product_query(self):
        """Test 18: logo printing / gift boxes should retrieve customisation."""
        rec = _record(
            category="PRODUCT_SERVICE",
            title="Customisation options",
            content="Customers can customise gift boxes with logo printing and custom sleeves.",
            applies_to=["customise", "customisation"],
            keywords=["gift box", "gift boxes", "logo printing", "custom sleeves", "packaging"],
            priority=80,
        )
        c = bk.score_components(
            rec, _signals("", "Do you provide logo printing for gift boxes?")
        )
        self.assertGreaterEqual(c["relevance_score"], bk.MIN_RELEVANCE)
        self.assertEqual(c["category_match"], 1.0)
        self.assertIn("logo printing", c["matched_business_keywords"])
        self.assertIn("gift boxes", c["matched_business_keywords"])

    def test_sarah_public_complaint(self):
        """Test 19: universal vocabulary now covers public/post/publicly."""
        rec = _record(
            category="ESCALATION",
            title="Public complaint escalation",
            content="Repeated unresolved complaints involving a threat of public or social-media escalation require founder review.",
            applies_to=["complaint", "follow-up", "urgent"],
            keywords=["post publicly", "public complaint", "social media"],
            priority=100,
        )
        body = ("This is my third follow-up about order #A1234. It still hasn't arrived. "
                "If nobody gets back to me today, I'm going to post about this publicly.")
        c = bk.score_components(rec, _signals("Order #A1234 still hasn't arrived", body))
        self.assertGreaterEqual(c["relevance_score"], bk.MIN_RELEVANCE)
        self.assertEqual(c["category_match"], 1.0)
        self.assertIn("publicly", c["matched_category_keywords"])

    def test_daniel_bulk_order(self):
        """Test 20: bulk pricing rule retrieves (subject matter relevant), but
        retrieval is not qualification — content still says 200+ units."""
        rec = _record(
            category="PRICING",
            title="Bulk pricing eligibility",
            content="Orders of 200 units or more may qualify for bulk pricing.",
            applies_to=["order", "quantity"],
            keywords=["bulk", "bulk order", "wholesale"],
            priority=80,
        )
        body = ("Hi, we're looking to order 100 customised gift boxes for our company event "
                "next month. Could you offer us a bulk discount?")
        c = bk.score_components(rec, _signals("Bulk pricing enquiry", body))
        self.assertGreaterEqual(c["relevance_score"], bk.MIN_RELEVANCE)
        self.assertIn("bulk", c["matched_business_keywords"])

    def test_tuition_centre_without_python_change(self):
        """Test 21: a different business retrieves using DATABASE keywords only."""
        rec = _record(
            category="OTHER",
            title="Make-up lessons must be arranged with the centre.",
            content="Make-up lessons must be arranged with the centre.",
            applies_to=[],
            keywords=["make-up lesson", "replacement class", "student"],
            priority=70,
        )
        c = bk.score_components(
            rec, _signals("", "My child missed class. Can we arrange a replacement class?")
        )
        self.assertGreaterEqual(c["relevance_score"], bk.MIN_RELEVANCE)
        self.assertIn("replacement class", c["matched_business_keywords"])
        self.assertEqual(c["matched_category_keywords"], [])

    def test_bouquet_vs_lawyer(self):
        """Test 22: irrelevant business vocabulary contributes nothing."""
        rec = _record(
            category="OTHER",
            title="Bouquet customisation",
            content="Custom bouquets are available.",
            applies_to=[],
            keywords=["bouquet", "bouquets"],
            priority=80,
        )
        c = bk.score_components(rec, _signals("", "My lawyer will contact you."))
        self.assertEqual(c["matched_business_keywords"], [])
        self.assertEqual(c["category_match"], 0.0)
        self.assertLess(c["relevance_score"], bk.MIN_RELEVANCE)


# ── record-specific relevance gate ───────────────────────────────────

class RecordSpecificGateTests(unittest.TestCase):
    """
    A record must carry at least ONE record-specific signal (matched
    business keyword, applies_to tag, or title/content token) to be
    returned.  A generic category keyword match + priority must never
    make an unrelated record eligible on their own.
    """

    def _public_complaint(self, id="bk-public"):
        return _record(
            id=id,
            category="ESCALATION",
            title="Public complaint escalation",
            content="Threats to post a complaint publicly or on social media require founder review.",
            applies_to=["complaint", "follow-up", "urgent"],
            keywords=["post publicly", "public complaint", "social media"],
            priority=100,
        )

    def _legal_threat(self, id="bk-legal"):
        return _record(
            id=id,
            category="ESCALATION",
            title="Legal threat escalation",
            content="Threats of legal action or litigation require founder review.",
            applies_to=["legal", "lawyer"],
            keywords=["legal action", "lawsuit", "lawyer", "court"],
            priority=100,
        )

    def test_legal_threat_excluded_for_sarah(self):
        """Sarah's public-complaint message must NOT surface the legal rule.

        The legal record scores >= MIN_RELEVANCE from category_match
        (ESCALATION via 'post'/'publicly') + priority, but has NO
        record-specific signal, so the gate must exclude it.
        """
        legal = self._legal_threat()
        subject = "Order #A1234 still hasn't arrived"
        body = ("This is my third follow-up about order #A1234. It still hasn't arrived. "
                "If nobody gets back to me today, I'm going to post about this publicly.")
        c = bk.score_components(legal, _signals(subject, body))
        # Proves it is the gate (not the threshold) that excludes it.
        self.assertGreaterEqual(c["relevance_score"], bk.MIN_RELEVANCE)
        self.assertEqual(c["category_match"], 1.0)
        self.assertFalse(c["has_record_specific_match"])
        self.assertEqual(c["matched_business_keywords"], [])
        self.assertEqual(c["matched_applies_to"], [])
        self.assertEqual(c["matched_content_tokens"], [])

    def test_sarah_full_retrieval(self):
        """End-to-end: Sarah retrieves repeated/public/delivery, never legal."""
        repeated = _record(
            id="bk-repeated",
            category="ESCALATION",
            title="Repeated unresolved complaints",
            content="Repeated unresolved complaints require founder review.",
            applies_to=["complaint", "follow-up"],
            keywords=["third follow-up", "repeated complaint"],
            priority=100,
        )
        delivery = _record(
            id="bk-delivery",
            category="SHIPPING",
            title="Delivery replacement verification",
            content="Verify delivery before issuing a replacement.",
            applies_to=["delivery", "replacement"],
            keywords=["hasn't arrived", "not delivered", "non-arrival"],
            priority=80,
        )
        records = [
            repeated,
            self._public_complaint(),
            self._legal_threat(),
            delivery,
        ]
        subject = "Order #A1234 still hasn't arrived"
        body = ("This is my third follow-up about order #A1234. It still hasn't arrived. "
                "If nobody gets back to me today, I'm going to post about this publicly.")
        with patch(
            "business_knowledge.get_active_business_knowledge",
            return_value=records,
        ):
            result = bk.get_relevant_business_knowledge(subject=subject, body=body)

        titles = [k["title"] for k in result["knowledge"]]
        self.assertIn("Repeated unresolved complaints", titles)
        self.assertIn("Public complaint escalation", titles)
        self.assertIn("Delivery replacement verification", titles)
        self.assertNotIn("Legal threat escalation", titles)

    def test_broad_category_public_vs_legal(self):
        """Section 12: 'post publicly' -> public rule eligible, legal excluded."""
        public = self._public_complaint()
        legal = self._legal_threat()
        c_public = bk.score_components(public, _signals("", "I will post about this publicly."))
        c_legal = bk.score_components(legal, _signals("", "I will post about this publicly."))
        self.assertTrue(c_public["has_record_specific_match"])
        self.assertFalse(c_legal["has_record_specific_match"])

    def test_broad_category_lawyer_positive(self):
        """Section 12: 'my lawyer will contact you' -> legal rule eligible."""
        legal = self._legal_threat()
        c = bk.score_components(legal, _signals("", "My lawyer will contact you."))
        self.assertTrue(c["has_record_specific_match"])
        self.assertGreaterEqual(c["relevance_score"], bk.MIN_RELEVANCE)
        self.assertIn("lawyer", c["matched_applies_to"])

    def test_actual_legal_threat_returned(self):
        """Section 9: explicit legal threat must reach the legal rule."""
        legal = self._legal_threat()
        c = bk.score_components(
            legal,
            _signals("", "If you don't refund me today, my lawyer will contact you and we will consider legal action."),
        )
        self.assertTrue(c["has_record_specific_match"])
        self.assertGreaterEqual(c["relevance_score"], bk.MIN_RELEVANCE)
        self.assertIn("lawyer", c["matched_applies_to"])
        self.assertIn("legal action", c["matched_business_keywords"])

    def test_pricing_category_false_positive(self):
        """Section 13: generic PRICING terms alone must not return an
        unrelated record that lacks record-specific evidence."""
        bulk = _record(
            id="bk-bulk",
            category="PRICING",
            title="Bulk pricing eligibility",
            content="Orders of 200 units or more may qualify for bulk pricing.",
            applies_to=["order", "quantity"],
            keywords=["bulk", "bulk order", "wholesale"],
            priority=80,
        )
        final_quote = _record(
            id="bk-final-quote",
            category="PRICING",
            title="Final quotation requirements",
            content="A final quotation must be approved before sending.",
            applies_to=[],
            keywords=[],
            priority=90,
        )
        c_final = bk.score_components(final_quote, _signals("", "What is the price?"))
        # Generic PRICING category matches and priority are high...
        self.assertEqual(c_final["category_match"], 1.0)
        self.assertGreaterEqual(c_final["relevance_score"], bk.MIN_RELEVANCE)
        # ...but there is no record-specific evidence, so it is gated out.
        self.assertFalse(c_final["has_record_specific_match"])

        with patch(
            "business_knowledge.get_active_business_knowledge",
            return_value=[bulk, final_quote],
        ):
            result = bk.get_relevant_business_knowledge(body="What is the price?")
        titles = [k["title"] for k in result["knowledge"]]
        self.assertNotIn("Final quotation requirements", titles)

    def test_refund_category_false_positive(self):
        """Section 14: a damaged-item rule must not return for a plain refund
        request merely because both live in REFUND_RETURN."""
        refund_verify = _record(
            id="bk-refund-verify",
            category="REFUND_RETURN",
            title="Refund verification",
            content="Verify the order before processing a refund.",
            applies_to=["refund"],
            keywords=["refund request", "process refund"],
            priority=80,
        )
        damaged_photo = _record(
            id="bk-damaged-photo",
            category="REFUND_RETURN",
            title="Damaged item photo required",
            content="A photo of the damaged item is required.",
            applies_to=["damaged", "photo"],
            keywords=["damaged item", "photo", "proof"],
            priority=90,
        )
        c_damaged = bk.score_components(damaged_photo, _signals("", "I want a refund."))
        self.assertEqual(c_damaged["category_match"], 1.0)
        self.assertGreaterEqual(c_damaged["relevance_score"], bk.MIN_RELEVANCE)
        self.assertFalse(c_damaged["has_record_specific_match"])

        with patch(
            "business_knowledge.get_active_business_knowledge",
            return_value=[refund_verify, damaged_photo],
        ):
            result = bk.get_relevant_business_knowledge(body="I want a refund.")
        titles = [k["title"] for k in result["knowledge"]]
        self.assertIn("Refund verification", titles)
        self.assertNotIn("Damaged item photo required", titles)


# ── content-overlap false positive (temporal function words) ─────────

class ContentOverlapFalsePositiveTests(unittest.TestCase):
    """
    Regression: temporal/relational function words ("before", "after", ...)
    must never count as record-specific title/content evidence.  A message
    and an unrelated rule that share only such a word must not pass the
    record-specific relevance gate.
    """

    TEMPORAL_WORDS = [
        "before", "after", "during", "until", "between", "since", "while",
    ]

    def _discount_authorization(self):
        return _record(
            id="87e11003-4e5b-4e94-ac8d-64d3971f8bed",
            category="AUTHORITY",
            title="Non-standard discount authorization",
            content=(
                "Non-standard discounts require founder authorization "
                "before they are communicated to the customer."
            ),
            applies_to=["discount", "special discount", "pricing"],
            keywords=[
                "bulk discount", "custom discount",
                "non-standard discount", "special discount",
                "wholesale discount",
            ],
            priority=100,
        )

    def test_temporal_words_in_stop_words(self):
        for word in self.TEMPORAL_WORDS:
            self.assertIn(word, bk.STOP_WORDS)
        # Topical words must remain evidence-bearing.
        for word in ("order", "customer", "change", "approved",
                     "discount", "pricing"):
            self.assertNotIn(word, bk.STOP_WORDS)

    def test_artwork_change_excludes_discount_authorization(self):
        """TEST A: '...logo before printing' must not match the discount
        rule whose content contains '...authorization before...'."""
        rec = self._discount_authorization()
        subject = "Change approved artwork"
        body = ("We already approved the artwork yesterday, but can you "
                "change our logo before printing?")
        c = bk.score_components(rec, _signals(subject, body))

        # The confirmed false-positive token must no longer be counted.
        self.assertNotIn("before", c["matched_content_tokens"])
        self.assertEqual(c["matched_content_tokens"], [])
        # Category + priority still cross the threshold...
        self.assertEqual(c["category_match"], 1.0)
        self.assertGreaterEqual(c["relevance_score"], bk.MIN_RELEVANCE)
        # ...but there is no record-specific signal, so the gate excludes it.
        self.assertFalse(c["has_record_specific_match"])

        with patch(
            "business_knowledge.get_active_business_knowledge",
            return_value=[rec],
        ):
            result = bk.get_relevant_business_knowledge(
                subject=subject, body=body
            )
        self.assertEqual(result["count"], 0)

    def test_special_discount_request_still_retrieves(self):
        """TEST B: a genuine discount request must still retrieve the rule."""
        rec = self._discount_authorization()
        subject = "Special discount"
        body = "Can you give us a special discount for this order?"
        c = bk.score_components(rec, _signals(subject, body))

        self.assertTrue(c["has_record_specific_match"])
        self.assertIn("special discount", c["matched_applies_to"])
        self.assertGreaterEqual(c["relevance_score"], bk.MIN_RELEVANCE)

        with patch(
            "business_knowledge.get_active_business_knowledge",
            return_value=[rec],
        ):
            result = bk.get_relevant_business_knowledge(
                subject=subject, body=body
            )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["knowledge"][0]["knowledge_id"], rec["id"])

    def test_temporal_word_only_overlap_is_not_evidence(self):
        """TEST C: sharing only a temporal word counts as no content evidence."""
        for word in self.TEMPORAL_WORDS:
            rec = _record(
                category="OTHER",
                title="Some rule",
                content=f"Something must happen {word} proceeding.",
                applies_to=[],
                keywords=[],
                priority=100,
            )
            c = bk.score_components(
                rec, _signals("", f"Please {word} do this.")
            )
            self.assertNotIn(word, c["matched_content_tokens"], word)
            self.assertEqual(c["matched_content_tokens"], [], word)
            self.assertFalse(c["has_record_specific_match"], word)


# ── Test 3: quantity-context signal (MOQ) ────────────────────────────

class QuantityContextSignalTests(unittest.TestCase):
    """Generic numeric-quantity + customisation context signals a
    minimum-order / MOQ rule.  A bare number, a year, or an order id
    must never do so, and unrelated records must not be flagged."""

    def _moq(self):
        return _record(
            id="d2e14590-bfd1-4869-8441-7b22b9bc1892",
            category="PRODUCT_SERVICE",
            title="Minimum customised order",
            content="The minimum order quantity for customised products is 30 units.",
            applies_to=["customised", "customization", "minimum order", "quantity"],
            keywords=[
                "minimum order", "minimum quantity", "moq", "customised order",
                "customized order", "minimum order quantity", "custom order",
                "personalised order", "personalized order",
                "customised products", "customized products", "order quantity",
            ],
            priority=85,
        )

    def _bulk_pricing(self):
        return _record(
            id="b34c84e3-15e3-4357-b583-da70b3e2ba02",
            category="PRICING",
            title="Bulk pricing eligibility",
            content="Orders of 200 units or more may qualify for bulk pricing.",
            applies_to=["bulk pricing", "bulk order", "wholesale",
                        "corporate order", "quantity"],
            keywords=[],
            priority=85,
        )

    def test_numeric_quantities_detection(self):
        self.assertEqual(
            bk._numeric_quantities(["20", "2026", "a1234", "250", "0"]),
            ["20", "250"],
        )

    def test_quantity_rule_identification_is_data_driven(self):
        self.assertTrue(bk._is_quantity_rule(self._moq()))
        # "corporate order" + "quantity" tags must NOT read as
        # "order quantity" and wrongly flag a bulk-pricing rule.
        self.assertFalse(bk._is_quantity_rule(self._bulk_pricing()))

    def test_moq_returns_for_quantity_and_custom(self):
        c = bk.score_components(
            self._moq(),
            _signals(
                "20 customised gift boxes",
                "I only need 20 customised gift boxes with our company logo. Can you do that?",
            ),
        )
        self.assertTrue(c["quantity_context_match"])
        self.assertEqual(c["matched_quantity"], ["20"])
        self.assertIn("customised", c["matched_custom_context"])
        self.assertEqual(c["category_match"], 1.0)
        self.assertTrue(c["has_record_specific_match"])
        self.assertGreaterEqual(c["relevance_score"], bk.MIN_RELEVANCE)

    def test_moq_generalizes_variants(self):
        for body in ("Can you do 20 customised boxes?",
                     "Can I order 15 personalised hampers?"):
            c = bk.score_components(self._moq(), _signals("", body))
            self.assertTrue(c["quantity_context_match"], body)
            self.assertTrue(c["has_record_specific_match"], body)

    def test_moq_requires_quantity(self):
        c = bk.score_components(
            self._moq(),
            _signals("", "What customisation options do you offer?"),
        )
        self.assertFalse(c["quantity_context_match"])
        self.assertFalse(c["has_record_specific_match"])

    def test_moq_order_id_is_not_quantity(self):
        c = bk.score_components(
            self._moq(), _signals("", "My order number is A1234.")
        )
        self.assertEqual(c["matched_quantity"], [])
        self.assertFalse(c["quantity_context_match"])

    def test_moq_year_is_not_quantity(self):
        c = bk.score_components(
            self._moq(), _signals("", "The event is in 2026.")
        )
        self.assertEqual(c["matched_quantity"], [])
        self.assertFalse(c["quantity_context_match"])

    def test_bulk_pricing_not_flagged_by_number(self):
        c = bk.score_components(
            self._bulk_pricing(), _signals("", "Can you do 20 customised boxes?")
        )
        self.assertFalse(c["quantity_context_match"])
        self.assertFalse(c["has_record_specific_match"])


# ── Test 14: repeated-complaint gate (narrowed applies_to) ───────────

class RepeatedComplaintGateTests(unittest.TestCase):
    """A 'Repeated unresolved complaints' rule must require repeat/prior
    contact evidence, not a generic 'complaint'/'unresolved'/'follow-up'
    tag.  These tests use the corrected (narrowed) record vocabulary."""

    def _repeated(self):
        return _record(
            id="b6764d57-7829-47d5-a5e8-0218d89eae1b",
            category="ESCALATION",
            title="Repeated unresolved complaints",
            content="Repeated unresolved customer complaints require higher-priority handling.",
            applies_to=["repeated complaint"],
            keywords=["repeated complaint", "multiple complaints",
                      "third follow-up", "second follow-up",
                      "unresolved complaint"],
            priority=95,
        )

    def _public(self):
        return _record(
            id="3fd9493c-8991-4121-b5a3-23a8a4e59d07",
            category="ESCALATION",
            title="Public complaint escalation",
            content="Threats to post a complaint publicly or on social media require founder review.",
            applies_to=["public complaint", "social media", "post publicly",
                        "founder review"],
            keywords=["post publicly", "public complaint", "social media",
                      "instagram", "facebook", "tiktok", "online review",
                      "bad review"],
            priority=100,
        )

    def _legal(self):
        return _record(
            id="afc4165a-1094-4678-a00b-1d01cb2da8e9",
            category="ESCALATION",
            title="Legal threat escalation",
            content="Customer legal threats require immediate escalation.",
            applies_to=["legal threat", "lawyer", "lawsuit", "legal",
                        "escalation"],
            keywords=["legal threat", "lawyer", "lawyers", "attorney",
                      "lawsuit", "sue", "court"],
            priority=100,
        )

    def test_generic_complaint_is_not_record_specific_evidence(self):
        c = bk.score_components(
            self._repeated(),
            _signals(
                "Complaint going public",
                "If nobody responds today, I'm posting this on Instagram and telling everyone about your service.",
            ),
        )
        self.assertEqual(c["matched_applies_to"], [])
        self.assertNotIn("complaint", c["matched_applies_to"])
        self.assertFalse(c["has_record_specific_match"])

    def test_first_public_threat_returns_only_public(self):
        records = [self._repeated(), self._public(), self._legal()]
        with patch(
            "business_knowledge.get_active_business_knowledge",
            return_value=records,
        ):
            result = bk.get_relevant_business_knowledge(
                subject="Complaint going public",
                body="If nobody responds today, I'm posting this on Instagram and telling everyone about your service.",
            )
        titles = [k["title"] for k in result["knowledge"]]
        self.assertIn("Public complaint escalation", titles)
        self.assertNotIn("Repeated unresolved complaints", titles)
        self.assertNotIn("Legal threat escalation", titles)

    def test_third_followup_matches_repeated(self):
        c = bk.score_components(
            self._repeated(),
            _signals("", "This is my third follow-up. Nobody has resolved my missing order."),
        )
        self.assertIn("third follow-up", c["matched_business_keywords"])
        self.assertTrue(c["has_record_specific_match"])
        self.assertGreaterEqual(c["relevance_score"], bk.MIN_RELEVANCE)

    def test_first_ordinary_complaint_excluded(self):
        c = bk.score_components(
            self._repeated(), _signals("", "My order has not arrived. Please help.")
        )
        self.assertFalse(c["has_record_specific_match"])


# ── pipeline business_context construction ───────────────────────────

class PipelineBusinessContextTests(unittest.TestCase):
    def test_build_business_context_shape(self):
        with patch(
            "pipeline.get_relevant_business_knowledge",
            return_value={
                "count": 2,
                "knowledge": [
                    {"knowledge_id": "k1", "title": "A"},
                    {"knowledge_id": "k2", "title": "B"},
                ],
            },
        ):
            ctx = pipeline.build_business_context(
                {"subject": "Bulk pricing", "body_verbatim": "discount 100 boxes"},
                founder_id=None,
            )
        self.assertEqual(ctx["retrieval_status"], "COMPLETE")
        self.assertEqual(len(ctx["knowledge"]), 2)

    def test_build_business_context_empty_is_valid(self):
        with patch(
            "pipeline.get_relevant_business_knowledge",
            return_value={"count": 0, "knowledge": []},
        ):
            ctx = pipeline.build_business_context(
                {"subject": "hi", "body_verbatim": "hello"},
            )
        self.assertEqual(ctx["retrieval_status"], "COMPLETE")
        self.assertEqual(ctx["knowledge"], [])

    def test_pipeline_business_knowledge_limit_is_respected(self):
        """
        Regression: the pipeline must request PIPELINE_BUSINESS_KNOWLEDGE_LIMIT
        (7) records and pass all of them through — it must not hardcode 5.
        """
        fake_knowledge = [
            {"knowledge_id": f"bk-{i}", "title": f"Item {i}"}
            for i in range(pipeline.PIPELINE_BUSINESS_KNOWLEDGE_LIMIT)
        ]
        with patch(
            "pipeline.get_relevant_business_knowledge",
            return_value={"count": len(fake_knowledge), "knowledge": fake_knowledge},
        ) as mock_retrieval:
            ctx = pipeline.build_business_context(
                {"subject": "Corporate gift box enquiry",
                 "body_verbatim": "We need 150 customised gift boxes."},
                founder_id=None,
            )

        # The retrieval was asked for exactly the pipeline limit (not 5).
        mock_retrieval.assert_called_once()
        self.assertEqual(
            mock_retrieval.call_args.kwargs["limit"],
            pipeline.PIPELINE_BUSINESS_KNOWLEDGE_LIMIT,
        )
        # And every record flows through to the business_context block.
        self.assertEqual(len(ctx["knowledge"]), pipeline.PIPELINE_BUSINESS_KNOWLEDGE_LIMIT)
        self.assertEqual(ctx["knowledge"][-1]["knowledge_id"],
                         f"bk-{pipeline.PIPELINE_BUSINESS_KNOWLEDGE_LIMIT - 1}")

    def test_build_pipeline_input_includes_business_context(self):
        with patch(
            "pipeline.get_relevant_preferences",
            return_value={
                "preferences": [],
                "memory_conflict": False,
                "conflicting_preference_ids": [],
            },
        ), patch(
            "pipeline.get_relevant_business_knowledge",
            return_value={
                "count": 1,
                "knowledge": [
                    {"knowledge_id": "bk-1", "title": "Wholesale pricing",
                     "category": "PRICING", "relevance_score": 0.68,
                     "matched_keywords": ["bulk"]}
                ],
            },
        ):
            msg = {
                "id": "m1", "channel": None, "sender_name": "X",
                "sender_address": "x@example.com", "subject": "s",
                "body_verbatim": "b", "received_at": None,
            }
            result = pipeline.build_pipeline_input(msg, founder_id=None)

        self.assertEqual(result["schema_version"], "attention_buddy_input.v1")
        self.assertEqual(result["business_context"]["retrieval_status"], "COMPLETE")
        self.assertEqual(
            result["business_context"]["knowledge"][0]["knowledge_id"], "bk-1"
        )
        self.assertEqual(
            result["founder_memory_context"]["retrieval_status"], "COMPLETE"
        )
        self.assertIsNone(result["message"]["received_at"])

    def test_process_message_routes_business_context_to_atlas_and_clio(self):
        chain = _chain_mock([[
            {
                "id": "m1", "channel": None, "sender_name": "X",
                "sender_address": "x@example.com", "subject": "s",
                "body_verbatim": "b", "received_at": None,
            }
        ]])
        atlas_output = {
            "schema_version": "ae.v1", "attention_decision": "AUTO_HANDLE",
            "attention_score": 0.2, "founder_input_required": False,
            "evidence": {"message_evidence": [], "business_rule_evidence": [], "founder_memory_evidence": []},
            "response_plan": {"required_founder_decisions": [], "optional_recommendations": [], "missing_information": []},
        }
        clio_output = {
            "schema_version": "cl.v1", "action": "DRAFT", "draft": {"subject": "", "body": "Hello"},
            "grounding": {"business_rules_used": [], "founder_preferences_used": []},
            "approval_required": False, "unresolved_items": [],
        }
        with patch("pipeline.supabase") as sb, patch(
            "pipeline.get_relevant_preferences",
            return_value={
                "preferences": [],
                "memory_conflict": False,
                "conflicting_preference_ids": [],
            },
        ), patch(
            "pipeline.get_relevant_business_knowledge",
            return_value={
                "count": 1,
                "knowledge": [
                    {"knowledge_id": "bk-1", "title": "Wholesale pricing",
                     "category": "PRICING", "relevance_score": 0.6,
                     "matched_keywords": ["bulk"]}
                ],
            },
        ), patch("pipeline.run_atlas", return_value=(atlas_output, {})) as atlas_run, \
                patch("pipeline.run_clio", return_value=(clio_output, {})) as clio_run, \
                patch("pipeline.persist_pipeline_run", return_value={"id": "run"}):
            sb.table.return_value = chain
            result = pipeline.process_message("m1", founder_id=None)

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["pipeline_run"], {"id": "run"})
        self.assertIsNone(result["validation_errors"])
        self.assertEqual(
            result["pipeline_input"]["business_context"]["retrieval_status"],
            "COMPLETE",
        )
        self.assertEqual(
            len(result["pipeline_input"]["business_context"]["knowledge"]), 1
        )
        atlas_input = atlas_run.call_args.args[0]
        self.assertEqual(atlas_input["business_context"]["knowledge"][0]["title"], "Wholesale pricing")
        clio_run.assert_called_once_with(atlas_input, atlas_output)


if __name__ == "__main__":
    unittest.main()
