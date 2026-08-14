"""Mocked contract and orchestration tests for Atlas + Clio (no live Groq)."""
import unittest
from unittest.mock import MagicMock, patch

import pipeline
from agents.atlas import build_atlas_payload, validate_ae_v1, run_atlas
from agents.clio import build_clio_payload, validate_cl_v1, run_clio
from groq_client import GroqError


def ae(decision="AUTO_HANDLE"):
    return {"schema_version": "ae.v1", "attention_decision": decision,
            "attention_score": .5, "founder_input_required": decision != "AUTO_HANDLE",
            "evidence": {"message_evidence": [], "business_rule_evidence": [], "founder_memory_evidence": []},
            "response_plan": {"required_founder_decisions": ["Approve discount"] if decision == "APPROVAL_REQUIRED" else [], "optional_recommendations": [], "missing_information": []}}


def cl(action="DRAFT", body="Hello"):
    return {"schema_version": "cl.v1", "action": action, "draft": {"subject": "Re: Test", "body": body},
            "grounding": {"business_rules_used": [], "founder_preferences_used": []},
            "approval_required": action == "HOLD", "unresolved_items": []}


INPUT = {"schema_version": "attention_buddy_input.v1", "message": {"id": "m1", "channel": "email", "sender_name": "Jamie", "sender_address": "j@example.com", "subject": "Collection", "body_verbatim": "Saturday?", "received_at": "now"}, "founder_memory_context": {"preferences": [{"preference_id": "p1", "rule": "Be concise"}], "memory_conflict": False, "conflicting_preference_ids": []}, "business_context": {"knowledge": [{"id": "db-id", "category": "HOURS", "title": "Saturday collection", "content": "10 AM-4 PM", "created_at": "secret", "matched_keywords": ["Saturday"]}]}}


class AtlasContractTests(unittest.TestCase):
    @patch("agents.atlas.generate_json_with_usage", return_value=({"result": ae()}, {"total_tokens": 3}))
    def test_run_atlas_unwraps_json_result_before_validation(self, groq):
        output, usage = run_atlas(INPUT)
        self.assertEqual(validate_ae_v1(output), [])
        self.assertEqual(usage, {"total_tokens": 3})

    @patch("agents.atlas.generate_json_with_usage", return_value=(ae(), {"total_tokens": 7}))
    def test_run_atlas_passes_plain_ae_v1_through_and_keeps_usage_separate(self, groq):
        # The live Groq contract (after the prompt fix) is the ae.v1 object
        # directly. It must reach the validator unchanged and usage must stay
        # out of the validated object.
        output, usage = run_atlas(INPUT)
        self.assertEqual(output, ae())
        self.assertEqual(validate_ae_v1(output), [])
        self.assertEqual(usage, {"total_tokens": 7})
        self.assertNotIn("usage", output)
        self.assertNotIn("total_tokens", output)

    def test_rejects_response_wrapper_shape(self):
        # The original live failure: Groq guessed {"response": {...}} because
        # the schema was never specified. The validator must reject it rather
        # than silently pass a non-ae.v1 object.
        broken = {"response": {"decision": "AUTO_HANDLE", "message": "hi"}}
        self.assertTrue(validate_ae_v1(broken))

    def test_all_valid_routes(self):
        for route in ("AUTO_HANDLE", "APPROVAL_REQUIRED", "ESCALATE_NOW"):
            self.assertEqual(validate_ae_v1(ae(route)), [])

    def test_rejects_invalid_decision_and_score(self):
        bad = ae("AWAITING_FOUNDER"); self.assertTrue(validate_ae_v1(bad))
        bad = ae(); bad["attention_score"] = 1.1; self.assertTrue(validate_ae_v1(bad))

    def test_compact_payload_includes_grounding_not_metadata(self):
        payload = build_atlas_payload(INPUT)
        self.assertEqual(payload["business_context"], [{"category": "HOURS", "title": "Saturday collection", "content": "10 AM-4 PM"}])
        self.assertEqual(payload["founder_memory_context"]["preferences"][0]["rule"], "Be concise")
        self.assertNotIn("id", payload["business_context"][0])
        self.assertNotIn("created_at", payload["business_context"][0])


class ClioContractTests(unittest.TestCase):
    @patch("agents.clio.generate_json_with_usage", return_value=({"data": cl()}, {"total_tokens": 2}))
    def test_run_clio_unwraps_json_data_before_validation(self, groq):
        output, usage = run_clio(INPUT, ae())
        self.assertEqual(validate_cl_v1(output), [])
        self.assertEqual(usage, {"total_tokens": 2})

    @patch("agents.clio.generate_json_with_usage", return_value=(cl(), {"total_tokens": 6}))
    def test_run_clio_passes_plain_cl_v1_through_and_keeps_usage_separate(self, groq):
        output, usage = run_clio(INPUT, ae())
        self.assertEqual(output, cl())
        self.assertEqual(validate_cl_v1(output), [])
        self.assertEqual(usage, {"total_tokens": 6})
        self.assertNotIn("usage", output)
        self.assertNotIn("total_tokens", output)

    def test_draft_and_hold_are_valid_and_invalid_is_rejected(self):
        self.assertEqual(validate_cl_v1(cl()), [])
        self.assertEqual(validate_cl_v1(cl("HOLD", "Reviewing your request.")), [])
        bad = cl(); bad["action"] = "SEND"; self.assertTrue(validate_cl_v1(bad))

    def test_compact_payload_preserves_atlas_plan_without_metadata(self):
        payload = build_clio_payload(INPUT, ae("APPROVAL_REQUIRED"))
        self.assertEqual(payload["atlas"]["response_plan"]["required_founder_decisions"], ["Approve discount"])
        self.assertEqual(payload["business_context"], [{"category": "HOURS", "title": "Saturday collection", "content": "10 AM-4 PM"}])
        self.assertNotIn("id", payload["business_context"][0])


class AssemblyTests(unittest.TestCase):
    def test_status_and_draft_semantics(self):
        out = pipeline.assemble_pipeline_v1(INPUT, ae(), cl())
        self.assertEqual(out["communication_status"], "READY"); self.assertTrue(out["ui_summary"]["draft_available"])
        out = pipeline.assemble_pipeline_v1(INPUT, ae("APPROVAL_REQUIRED"), cl("HOLD", "Reviewing"))
        self.assertEqual(out["communication_status"], "AWAITING_APPROVAL"); self.assertTrue(out["ui_summary"]["draft_available"])
        out = pipeline.assemble_pipeline_v1(INPUT, ae("ESCALATE_NOW"), cl("HOLD", "We are reviewing this."))
        self.assertEqual(out["communication_status"], "HELD")

    def _message_chain(self):
        response = MagicMock(); response.data = [INPUT["message"]]
        chain = MagicMock(); chain.table.return_value = chain; chain.select.return_value = chain; chain.eq.return_value = chain; chain.limit.return_value = chain; chain.execute.return_value = response
        return chain

    @patch("pipeline.build_pipeline_input", return_value=INPUT)
    @patch("pipeline.supabase")
    @patch("pipeline.run_clio")
    @patch("pipeline.run_atlas", side_effect=GroqError("down"))
    def test_atlas_failure_stops_clio(self, atlas_run, clio_run, supabase, build):
        supabase.table.return_value = self._message_chain()
        self.assertEqual(pipeline.process_message("m1")["status"], "ATLAS_FAILED")
        clio_run.assert_not_called()

    @patch("pipeline.persist_pipeline_run")
    @patch("pipeline.build_pipeline_input", return_value=INPUT)
    @patch("pipeline.supabase")
    @patch("pipeline.run_clio", side_effect=GroqError("down"))
    @patch("pipeline.run_atlas", return_value=(ae(), {}))
    def test_clio_failure_does_not_persist(self, atlas_run, clio_run, supabase, build, persist):
        supabase.table.return_value = self._message_chain()
        self.assertEqual(pipeline.process_message("m1")["status"], "CLIO_FAILED")
        persist.assert_not_called()

    @patch("pipeline.persist_pipeline_run", return_value={"id": "run"})
    @patch("pipeline.build_pipeline_input", return_value=INPUT)
    @patch("pipeline.supabase")
    @patch("pipeline.run_clio", return_value=(cl(), {}))
    @patch("pipeline.run_atlas", return_value=(ae(), {}))
    def test_success_persists_all_artifacts(self, atlas_run, clio_run, supabase, build, persist):
        supabase.table.return_value = self._message_chain()
        result = pipeline.process_message("m1")
        saved = persist.call_args.args[1]
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(saved["ae_v1"]["schema_version"], "ae.v1")
        self.assertEqual(saved["cl_v1"]["schema_version"], "cl.v1")
        self.assertEqual(saved["schema_version"], "pipeline.v1")
        self.assertTrue(saved["ui_summary"]["draft_available"])
        self.assertIsNone(saved["mie_v1"])


class DiagnosticEndpointTests(unittest.TestCase):
    @patch("main.run_atlas", return_value=(ae(), {}))
    @patch("main.build_attention_buddy_input", return_value=INPUT)
    def test_atlas_test_is_non_persistent(self, build, run):
        import main
        result = main.atlas_test(main.PipelinePreviewRequest(message_id="m1"))
        self.assertEqual(result["ae_v1"]["schema_version"], "ae.v1")

    @patch("main.run_clio", return_value=(cl(), {}))
    @patch("main.run_atlas", return_value=(ae(), {}))
    @patch("main.build_attention_buddy_input", return_value=INPUT)
    def test_clio_test_is_non_persistent(self, build, atlas_run, clio_run):
        import main
        result = main.clio_test(main.PipelinePreviewRequest(message_id="m1"))
        self.assertEqual(result["cl_v1"]["schema_version"], "cl.v1")

    def test_no_destructive_gmail_write_methods(self):
        from pathlib import Path
        source = Path(__file__).with_name("gmail.py").read_text(encoding="utf-8")
        # Sending is the one intentional write (manual-only). Destructive
        # operations remain prohibited.
        for prohibited in ("drafts().create", ".modify(", ".trash(", ".delete("):
            self.assertNotIn(prohibited, source)


class PromptSchemaTests(unittest.TestCase):
    """The actual root cause was that the prompts never defined the ae.v1 /
    cl.v1 schemas, so Groq improvised a wrong shape. Lock in that both
    prompts now spell out every required field."""

    def _prompt(self, name):
        from pathlib import Path
        return Path(__file__).parent.joinpath("agents", "prompts", name).read_text(encoding="utf-8")

    def test_atlas_prompt_defines_ae_v1_schema(self):
        text = self._prompt("atlas.md")
        for token in ("ae.v1", "schema_version", "attention_decision",
                      "attention_score", "founder_input_required",
                      "message_evidence", "business_rule_evidence",
                      "founder_memory_evidence", "required_founder_decisions",
                      "optional_recommendations", "missing_information"):
            self.assertIn(token, text)

    def test_clio_prompt_defines_cl_v1_schema(self):
        text = self._prompt("clio.md")
        for token in ("cl.v1", "schema_version", "action", "draft",
                      "grounding", "business_rules_used",
                      "founder_preferences_used", "approval_required",
                      "unresolved_items"):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
