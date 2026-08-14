"""
Unit tests for process_founder_feedback.

Covers all durability tiers and the preference lifecycle:
  - OBSERVED  (no explanation → learning event only)
  - EMERGING  (explanation, no apply_to_similar → candidate, inactive)
  - EXPLICIT  (apply_to_similar + explanation → active immediately)
  - Promotion: EMERGING → ESTABLISHED after 3+ observations

Also covers edge cases around empty explanations and
apply_to_similar without explanation.
"""

import unittest
from unittest.mock import patch, MagicMock

import learning


# ── helper: build a chainable mock Supabase client ───────────────────

def _mock_supabase_chain():
    """Return a MagicMock that supports chaining .table().select().eq()… .execute().

    Every method in the chain returns a new MagicMock whose .execute()
    returns a configurable response.  Callers can tune the final
    ``.execute.return_value`` after obtaining the mock.
    """
    chain = MagicMock()
    # Make every attribute / method call return the same chainable mock.
    chain.table.return_value = chain
    chain.select.return_value = chain
    chain.insert.return_value = chain
    chain.update.return_value = chain
    chain.eq.return_value = chain
    chain.is_.return_value = chain
    chain.limit.return_value = chain
    return chain


def _mock_response(data):
    """Return a MagicMock whose .data attribute is *data*."""
    m = MagicMock()
    m.data = data
    return m


# ── minimal feedback_row builders ────────────────────────────────────

def _base_feedback(**overrides):
    row = {
        "id": "fb-001",
        "pipeline_run_id": "run-001",
        "original_decision": "APPROVAL_REQUIRED",
        "final_decision": "AUTO_HANDLE",
        "action_type": "APPROVED",
        "founder_explanation": None,
        "apply_to_similar": False,
    }
    row.update(overrides)
    return row


# ── tests ────────────────────────────────────────────────────────────

class ObserveEmergingExplicitTiers(unittest.TestCase):
    """Core durability-tier tests for process_founder_feedback."""

    def setUp(self):
        # Every test patches database.supabase with a fresh chain mock.
        patcher = patch("learning.supabase")
        self.mock_supabase = patcher.start()
        self.addCleanup(patcher.stop)

        self.chain = _mock_supabase_chain()
        self.mock_supabase.table.return_value = self.chain

    # ── OBSERVED ─────────────────────────────────────────────────

    def test_observed_when_no_explanation(self):
        """No explanation → OBSERVED, low confidence, no preference."""
        # _resolve_founder_id returns default
        self.chain.execute.return_value = _mock_response([])

        result = learning.process_founder_feedback(_base_feedback())

        self.assertEqual(result["learning_event"]["memory_status"], "OBSERVED")
        self.assertEqual(result["learning_event"]["confidence"], "low")
        self.assertIsNone(result["preference_update"])

    def test_observed_when_explanation_is_whitespace_only(self):
        """Whitespace-only explanation is treated as no explanation."""
        self.chain.execute.return_value = _mock_response([])

        result = learning.process_founder_feedback(
            _base_feedback(founder_explanation="   \t  ")
        )

        self.assertEqual(result["learning_event"]["memory_status"], "OBSERVED")
        self.assertEqual(result["learning_event"]["confidence"], "low")
        self.assertIsNone(result["preference_update"])

    # ── EMERGING ─────────────────────────────────────────────────

    def test_emerging_with_explanation_no_apply(self):
        """Explanation present but apply_to_similar=False → EMERGING."""
        self.chain.execute.side_effect = [
            # _resolve_founder_id — pipeline_runs query
            _mock_response([
                {"message_id": "msg-001"},
            ]),
            # _resolve_founder_id — messages query
            _mock_response([
                {"founder_id": "founder-1"},
            ]),
            # learning_events insert — return empty so learning_event_row is used
            _mock_response([]),
            # _resolve_source_message_id — pipeline_runs query
            _mock_response([
                {"message_id": "msg-001"},
            ]),
            # learning_events insert — return empty so learning_event_row is used
            _mock_response([]),
            # _upsert_preference — existing preference query
            _mock_response([]),
            # _upsert_preference — insert new preference
            _mock_response([
                {"id": "pref-001", "rule": "Always escalate.", "memory_status": "EMERGING", "active": False},
            ]),
        ]

        result = learning.process_founder_feedback(
            _base_feedback(
                founder_explanation="Always escalate these.",
                apply_to_similar=False,
            )
        )

        self.assertEqual(result["learning_event"]["memory_status"], "EMERGING")
        self.assertEqual(result["learning_event"]["confidence"], "medium")
        self.assertIsNotNone(result["preference_update"])
        self.assertEqual(result["preference_update"]["action"], "created")
        self.assertFalse(result["preference_update"]["active"])

    def test_emerging_preference_is_inactive(self):
        """EMERGING creates a candidate preference that is NOT active."""
        self.chain.execute.side_effect = [
            _mock_response([]),                     # _resolve_founder_id — pipeline_runs
            _mock_response([]),                     # learning_events insert
            _mock_response([]),                     # _resolve_source_message_id — pipeline_runs
            _mock_response([]),                     # existing preference query
            _mock_response([{
                "id": "pref-candidate",
                "memory_status": "EMERGING",
                "active": False,
            }]),
        ]

        result = learning.process_founder_feedback(
            _base_feedback(
                founder_explanation="I prefer to review pricing emails first.",
                apply_to_similar=False,
                action_type="OVERRIDDEN",
            )
        )

        self.assertEqual(result["learning_event"]["memory_status"], "EMERGING")
        self.assertFalse(result["preference_update"]["active"])

    def test_emerging_creates_preference(self):
        """EMERGING feedback with explanation creates a founder_preference."""
        self.chain.execute.side_effect = [
            _mock_response([]),                     # _resolve_founder_id — pipeline_runs
            _mock_response([]),                     # learning_events insert
            _mock_response([]),                     # _resolve_source_message_id — pipeline_runs
            _mock_response([]),                     # existing preference query
            _mock_response([{"id": "pref-002"}]),   # insert new preference
        ]

        result = learning.process_founder_feedback(
            _base_feedback(
                founder_explanation="I prefer to review pricing emails first.",
                apply_to_similar=False,
                action_type="OVERRIDDEN",
            )
        )

        self.assertEqual(result["learning_event"]["memory_status"], "EMERGING")
        self.assertIsNone(result["learning_event"]["founder_id"])
        self.assertIsNotNone(result["preference_update"])
        self.assertEqual(result["preference_update"]["action"], "created")

    # ── EXPLICIT ─────────────────────────────────────────────────

    def test_explicit_when_apply_and_explanation(self):
        """apply_to_similar=True + explanation → EXPLICIT, high confidence."""
        self.chain.execute.side_effect = [
            _mock_response([]),                     # _resolve_founder_id — pipeline_runs
            _mock_response([]),                     # learning_events insert
            _mock_response([]),                     # _resolve_source_message_id — pipeline_runs
            _mock_response([]),                     # existing preference query
            _mock_response([{"id": "pref-003", "active": True}]),   # insert new preference
        ]

        result = learning.process_founder_feedback(
            _base_feedback(
                founder_explanation="Always auto-handle wholesale pricing inquiries.",
                apply_to_similar=True,
            )
        )

        self.assertEqual(result["learning_event"]["memory_status"], "EXPLICIT")
        self.assertEqual(result["learning_event"]["confidence"], "high")
        self.assertIsNotNone(result["preference_update"])
        self.assertTrue(result["preference_update"]["active"])

    def test_explicit_preference_is_active_immediately(self):
        """EXPLICIT preferences are active immediately."""
        self.chain.execute.side_effect = [
            _mock_response([]),                     # _resolve_founder_id — pipeline_runs
            _mock_response([]),                     # learning_events insert
            _mock_response([]),                     # _resolve_source_message_id — pipeline_runs
            _mock_response([]),                     # existing preference query
            _mock_response([{
                "id": "pref-active",
                "memory_status": "EXPLICIT",
                "active": True,
            }]),
        ]

        result = learning.process_founder_feedback(
            _base_feedback(
                founder_explanation="Always auto-handle wholesale pricing inquiries.",
                apply_to_similar=True,
            )
        )

        self.assertEqual(result["learning_event"]["memory_status"], "EXPLICIT")
        self.assertTrue(result["preference_update"]["active"])

    def test_explicit_creates_preference(self):
        """EXPLICIT feedback creates a founder_preference row."""
        self.chain.execute.side_effect = [
            _mock_response([]),                     # _resolve_founder_id — pipeline_runs
            _mock_response([]),                     # learning_events insert
            _mock_response([]),                     # _resolve_source_message_id — pipeline_runs
            _mock_response([]),                     # existing preference query
            _mock_response([{"id": "pref-004"}]),   # insert new preference
        ]

        result = learning.process_founder_feedback(
            _base_feedback(
                founder_explanation="First-time senders need approval.",
                apply_to_similar=True,
                action_type="OVERRIDDEN",
            )
        )

        self.assertEqual(result["learning_event"]["memory_status"], "EXPLICIT")
        self.assertEqual(result["preference_update"]["action"], "created")

    # ── EMERGING → ESTABLISHED promotion ────────────────────────

    def test_emerging_promotes_to_established_on_third_observation(self):
        """An inactive EMERGING candidate with 2 prior observations
        becomes ESTABLISHED and active on the 3rd supporting observation."""
        self.chain.execute.side_effect = [
            _mock_response([]),                     # _resolve_founder_id — pipeline_runs
            _mock_response([]),                     # learning_events insert
            _mock_response([]),                     # _resolve_source_message_id — pipeline_runs
            # Existing EMERGING preference with 2 observations
            _mock_response([{
                "id": "pref-existing",
                "rule": "Review pricing emails first.",
                "scope": "attention_routing_pricing",
                "memory_status": "EMERGING",
                "confidence": "medium",
                "supporting_observations": 2,
                "active": False,
                "source_learning_event_ids": ["le-a", "le-b"],
            }]),
            # Update call (no return data needed)
            _mock_response([]),
        ]

        result = learning.process_founder_feedback(
            _base_feedback(
                founder_explanation="I prefer to review pricing emails first.",
                apply_to_similar=False,
                action_type="OVERRIDDEN",
            )
        )

        self.assertEqual(result["learning_event"]["memory_status"], "EMERGING")
        self.assertIsNotNone(result["preference_update"])
        self.assertEqual(result["preference_update"]["action"], "reinforced")
        self.assertEqual(result["preference_update"]["memory_status"], "ESTABLISHED")
        self.assertTrue(result["preference_update"]["active"])
        self.assertEqual(result["preference_update"]["confidence"], "high")
        self.assertEqual(result["preference_update"]["supporting_observations"], 3)

    # ── edge cases ───────────────────────────────────────────────

    def test_apply_without_explanation_is_observed(self):
        """apply_to_similar=True but no explanation → OBSERVED (not EXPLICIT)."""
        self.chain.execute.return_value = _mock_response([])

        result = learning.process_founder_feedback(
            _base_feedback(
                founder_explanation=None,
                apply_to_similar=True,
            )
        )

        self.assertEqual(result["learning_event"]["memory_status"], "OBSERVED")
        self.assertIsNone(result["preference_update"])

    def test_observed_does_not_create_preference(self):
        """OBSERVED feedback never creates a founder_preference."""
        self.chain.execute.return_value = _mock_response([])

        result = learning.process_founder_feedback(
            _base_feedback(
                founder_explanation=None,
                apply_to_similar=False,
                action_type="EDITED",
            )
        )

        self.assertEqual(result["learning_event"]["memory_status"], "OBSERVED")
        self.assertIsNone(result["preference_update"])


# ── helper-function tests ────────────────────────────────────────────

class HelperFunctionTests(unittest.TestCase):
    """Unit tests for learning-internal helpers."""

    def test_infer_category_attention_preference(self):
        cat = learning._infer_learning_category({
            "action_type": "OVERRIDDEN",
        })
        self.assertEqual(cat, "ATTENTION_PREFERENCE")

    def test_infer_category_communication_preference(self):
        cat = learning._infer_learning_category({
            "action_type": "EDITED",
            "original_draft": "draft a",
            "final_draft": "draft b",
        })
        self.assertEqual(cat, "COMMUNICATION_PREFERENCE")

    def test_infer_category_authority_preference(self):
        cat = learning._infer_learning_category({
            "action_type": "APPROVED",
            "original_decision": "ESCALATE_NOW",
            "final_decision": "APPROVAL_REQUIRED",
        })
        self.assertEqual(cat, "AUTHORITY_PREFERENCE")

    def test_infer_category_observed_correction(self):
        cat = learning._infer_learning_category({
            "action_type": "APPROVED",
            "original_decision": "AUTO_HANDLE",
            "final_decision": "AUTO_HANDLE",
        })
        self.assertEqual(cat, "OBSERVED_CORRECTION")

    def test_build_rule_text_capitalizes_and_period(self):
        rule = learning._build_rule_text(
            "always escalate these", "ATTENTION_PREFERENCE"
        )
        self.assertEqual(rule, "Always escalate these.")

    def test_build_rule_text_already_capitalized(self):
        rule = learning._build_rule_text(
            "Always escalate these.", "ATTENTION_PREFERENCE"
        )
        self.assertEqual(rule, "Always escalate these.")

    def test_detect_contradiction_positive(self):
        self.assertTrue(
            learning._detect_contradiction(
                {"rule": "Always escalate."},
                "I no longer want to escalate.",
                "EXPLICIT",
            )
        )

    def test_detect_contradiction_not_explicit(self):
        self.assertFalse(
            learning._detect_contradiction(
                {"rule": "Always escalate."},
                "I no longer want to escalate.",
                "EMERGING",
            )
        )

    def test_derive_scope_pricing_hint(self):
        scope = learning._derive_scope(
            "ATTENTION_PREFERENCE",
            "Always escalate wholesale pricing emails.",
        )
        self.assertIn("pricing", scope)

    def test_derive_scope_first_time_hint(self):
        scope = learning._derive_scope(
            "AUTHORITY_PREFERENCE",
            "First-time senders need approval.",
        )
        self.assertIn("first_time", scope)

    # ── _rules_are_similar ──────────────────────────────────────

    def test_rules_similar_when_describing_same_pattern(self):
        """Two phrasings of the same rule should be considered similar."""
        self.assertTrue(
            learning._rules_are_similar(
                "I personally review wholesale pricing.",
                "I prefer to review wholesale pricing emails first.",
            )
        )

    def test_rules_not_similar_when_opposite_direction(self):
        """Review vs delegate in the same scope must NOT reinforce."""
        self.assertFalse(
            learning._rules_are_similar(
                "I personally review wholesale discounts.",
                "I delegate wholesale pricing.",
            )
        )

    def test_rules_not_similar_when_empty(self):
        self.assertFalse(learning._rules_are_similar("", ""))
        self.assertFalse(learning._rules_are_similar("Some rule.", ""))

    def test_rules_similar_exact_match(self):
        self.assertTrue(
            learning._rules_are_similar(
                "Auto-handle all first-time senders.",
                "Auto-handle all first-time senders.",
            )
        )

    # ── _resolve_founder_id ─────────────────────────────────────

    def test_resolve_founder_id_returns_none_without_pipeline(self):
        fid = learning._resolve_founder_id("")
        self.assertIsNone(fid)

    def test_resolve_founder_id_returns_none_when_no_match(self):
        fid = learning._resolve_founder_id(None)
        self.assertIsNone(fid)


if __name__ == "__main__":
    unittest.main()
