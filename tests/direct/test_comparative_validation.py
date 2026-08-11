"""
Tests for the comparative-validation redesign, responding to the GenLayer
Portal steward's rejection: the original validator_fn only parsed the
leader's JSON and checked structure/ranges -- it never independently
re-acquired evidence or independently judged fulfillment, so two
conflicting substantive verdicts could both pass validation.

Two design iterations happened in response:
  1. A hand-rolled gl.vm.run_nondet(leader_fn, validator_fn) where
     validator_fn made its own second gl.nondet.exec_prompt call and
     compared verdicts in Python. This was architecturally sound and unit-
     tested (13 tests, all passing), but hit a real live-network
     DETERMINISTIC_VIOLATION on Bradbury -- GenVM's consensus protocol
     rejected this pattern of a validator making its own free-standing
     nondet call and comparing results in plain Python.
  2. The current design: gl.eq_principle.prompt_non_comparative, the
     SDK's own built-in primitive for exactly this pattern. Both leader
     and validator independently call the same evidence-gathering
     function (_build_input, calling _fetch_evidence fresh each time);
     the validator's ACCEPTANCE of the leader's output is then judged by
     the validator's own LLM call via the platform's internal
     "EqNonComparativeValidator" protocol path, not a hand-rolled Python
     comparison. This is the platform-sanctioned mechanism for the
     redesign the steward asked for.

Direct-mode testing limitation (same as always): gltest's WASI mock
(gltest.direct.wasi_mock._handle_run_nondet) always runs only the leader
function and never validator_fn -- there is no way to simulate an actual
validator disagreement in direct mode. What these tests CAN and do prove:
  1. The leader path works correctly end-to-end through
     prompt_non_comparative (requiring the ExecPromptTemplate mock patch
     in tests/direct/conftest.py, since gltest has no built-in handler for
     that gl_call type -- ExecPrompt and ExecPromptTemplate are distinct
     request types).
  2. _coerce_verdict correctly parses a PLAIN STRING response (not a
     pre-parsed dict), since prompt_non_comparative returns str, unlike
     gl.nondet.exec_prompt(response_format="json")'s auto-parsing.
  3. Every consistency guard from prior audits still holds on top of this
     new adjudication pipeline.
The actual validator-side consensus behavior is verified live on Bradbury
(see docs/security-model.md for the deployment/verification record), not
here, since it requires real multi-validator consensus no local mock can
substitute for honestly.
"""
import json

import pytest

CONTRACT_PATH = "contracts/AgentIntentSettlement.py"


@pytest.fixture
def contract(direct_deploy):
    return direct_deploy(CONTRACT_PATH, sdk_version="v0.2.16")


def _mock_llm(direct_vm, verdict: dict):
    direct_vm.clear_mocks()
    direct_vm.mock_llm(".*", json.dumps(verdict))


FULFILLED_VERDICT = {
    "fulfilled": True,
    "confidence": "0.9",
    "reasoning": "Strong external evidence directly corroborates the stated goal.",
    "partial_credit": "1.0",
    "evidence_quality": "strong",
    "violations": [],
    "recommended_action": "release_escrow",
}

VERIFIABLE_EVIDENCE = json.dumps(
    ["some text", {"type": "url", "content": "https://example.com/proof"}]
)


class TestPromptNonComparativeLeaderPath:
    """Proves the ExecPromptTemplate mock patch works and the leader path
    through the new primitive produces correct verdicts, exercising every
    major branch."""

    def test_release_escrow_end_to_end(self, contract, direct_vm):
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        verdict = contract.settle_intent(
            settlement_id="cmp-1",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=VERIFIABLE_EVIDENCE,
        )
        assert verdict["fulfilled"] is True
        assert verdict["recommended_action"] == "release_escrow"
        assert isinstance(verdict["confidence"], str)
        assert isinstance(verdict["partial_credit"], str)

    def test_escalate_end_to_end(self, contract, direct_vm):
        insufficient = {
            "fulfilled": False,
            "confidence": "0.3",
            "reasoning": "No corroborating evidence was supplied for this claim.",
            "partial_credit": "0.0",
            "evidence_quality": "insufficient",
            "violations": [],
            "recommended_action": "escalate",
        }
        _mock_llm(direct_vm, insufficient)
        verdict = contract.settle_intent(
            settlement_id="cmp-2",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps([]),
        )
        assert verdict["recommended_action"] == "escalate"

    def test_malformed_llm_string_response_falls_back_safely(self, contract, direct_vm):
        """prompt_non_comparative returns a plain str with no
        response_format="json" enforcement -- _coerce_verdict's own
        parsing/fallback must carry the full weight of handling a
        non-JSON or garbage response, unlike the old exec_prompt path
        which had the SDK's own JSON auto-parse as a first line of
        defense."""
        direct_vm.clear_mocks()
        direct_vm.mock_llm(".*", "this is not JSON at all, just prose")

        verdict = contract.settle_intent(
            settlement_id="cmp-3",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=VERIFIABLE_EVIDENCE,
        )
        assert verdict["fulfilled"] is False
        assert verdict["evidence_quality"] == "insufficient"
        assert verdict["recommended_action"] == "escalate"
        assert 0.0 <= float(verdict["confidence"]) <= 1.0
        assert 0.0 <= float(verdict["partial_credit"]) <= 1.0

    def test_consistency_guards_still_hold_on_new_pipeline(self, contract, direct_vm):
        """Spot-check that the audited consistency guards (release_escrow
        requires fulfilled+strong+verifiable+confidence>=0.6) still apply
        after the adjudication mechanism changed underneath them."""
        inconsistent = {
            "fulfilled": False,
            "confidence": "0.95",
            "reasoning": "Evidence is strong but does not show fulfillment.",
            "partial_credit": "0.0",
            "evidence_quality": "strong",
            "violations": [],
            "recommended_action": "release_escrow",
        }
        _mock_llm(direct_vm, inconsistent)
        verdict = contract.settle_intent(
            settlement_id="cmp-4",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=VERIFIABLE_EVIDENCE,
        )
        assert verdict["recommended_action"] == "escalate"

    def test_release_escrow_still_requires_verifiable_evidence(self, contract, direct_vm):
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        verdict = contract.settle_intent(
            settlement_id="cmp-5",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(["only self-authored text"]),
        )
        assert verdict["recommended_action"] == "escalate"

    def test_idempotency_still_works(self, contract, direct_vm):
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        first = contract.settle_intent(
            settlement_id="cmp-6",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=VERIFIABLE_EVIDENCE,
        )
        second = contract.settle_intent(
            settlement_id="cmp-6",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=VERIFIABLE_EVIDENCE,
        )
        assert first == second
