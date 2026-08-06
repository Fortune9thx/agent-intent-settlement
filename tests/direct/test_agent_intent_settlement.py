"""
Direct-mode tests for AgentIntentSettlement.

Uses gltest's in-process WASI-mock VM (no localnet/simulator needed):
  - direct_deploy -> deploys contracts/AgentIntentSettlement.py, returns a
                     proxy whose public methods are called directly.
  - direct_vm     -> Foundry-style cheatcodes: vm.mock_llm(pattern, response)
                     stubs gl.nondet.exec_prompt; vm.mock_web(pattern, body)
                     stubs gl.nondet.web.render for url-type evidence.

settle_intent is @gl.public.write.payable but direct-mode calls it like any
other write method (no on-chain value semantics to simulate here).
"""
import json
import re

import pytest

CONTRACT_PATH = "contracts/AgentIntentSettlement.py"


def re_escape(url: str) -> str:
    return re.escape(url)


FULFILLED_VERDICT = {
    "fulfilled": True,
    "confidence": "0.92",
    "reasoning": (
        "Evidence item 0 shows the on-chain transfer of 500 USDC to the "
        "recipient address stated in the goal, matching the agent's claim "
        "exactly."
    ),
    "partial_credit": "1.0",
    "evidence_quality": "strong",
    "violations": [],
    "recommended_action": "release_escrow",
}

INSUFFICIENT_VERDICT = {
    "fulfilled": False,
    "confidence": "0.3",
    "reasoning": (
        "No evidence items corroborate the claimed delivery; only the "
        "agent's own assertion is present."
    ),
    "partial_credit": "0.0",
    "evidence_quality": "insufficient",
    "violations": ["claim unsupported by any evidence item"],
    "recommended_action": "escalate",
}

PARTIAL_VERDICT = {
    "fulfilled": False,
    "confidence": "0.6",
    "reasoning": (
        "Evidence shows 2 of the 3 requested files were delivered; the "
        "third (README) is absent from the evidence."
    ),
    "partial_credit": "0.66",
    "evidence_quality": "weak",
    "violations": ["missing README as required by the goal"],
    "recommended_action": "partial_payout",
}

SLASH_VERDICT = {
    "fulfilled": False,
    "confidence": "0.9",
    "reasoning": (
        "Evidence directly contradicts the agent's claim -- the funds were "
        "sent to an unauthorized address, not the recipient stated in the "
        "goal."
    ),
    "partial_credit": "0.0",
    "evidence_quality": "strong",
    "violations": ["funds sent to wrong recipient"],
    "recommended_action": "slash",
}

REJECT_VERDICT = {
    "fulfilled": False,
    "confidence": "0.7",
    "reasoning": "The goal was simply not accomplished; no violation occurred.",
    "partial_credit": "0.0",
    "evidence_quality": "strong",
    "violations": [],
    "recommended_action": "reject",
}


@pytest.fixture
def contract(direct_deploy):
    # Pinned to the exact genvm release matching the contract's own
    # "Depends": "py-genlayer:..." header.
    return direct_deploy(CONTRACT_PATH, sdk_version="v0.2.16")


def _mock_llm(direct_vm, verdict: dict):
    direct_vm.clear_mocks()
    direct_vm.mock_llm(".*", json.dumps(verdict))


class TestHappyPath:
    def test_fulfilled_intent_releases_escrow(self, contract, direct_vm):
        _mock_llm(direct_vm, FULFILLED_VERDICT)

        verdict = contract.settle_intent(
            settlement_id="s-1",
            natural_language_goal="Transfer 500 USDC to 0xRecipient",
            agent_claim="I transferred 500 USDC to 0xRecipient in tx 0xabc",
            evidence_json=json.dumps(
                [
                    "on-chain log: transfer 500 USDC to 0xRecipient, tx 0xabc",
                    {"type": "url", "content": "https://explorer.example.com/tx/0xabc"},
                ]
            ),
        )

        assert verdict["fulfilled"] is True
        assert verdict["recommended_action"] == "release_escrow"
        assert verdict["evidence_quality"] == "strong"

        stored = contract.get_settlement(settlement_id="s-1")
        assert stored["verdict"]["fulfilled"] is True
        assert stored["settlement_id"] == "s-1"

    def test_has_settlement(self, contract, direct_vm):
        assert contract.has_settlement(settlement_id="s-none") is False
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        contract.settle_intent(
            settlement_id="s-2",
            natural_language_goal="Deliver goal",
            agent_claim="Done",
            evidence_json=json.dumps(["proof"]),
        )
        assert contract.has_settlement(settlement_id="s-2") is True

    def test_idempotent_on_settlement_id(self, contract, direct_vm):
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        first = contract.settle_intent(
            settlement_id="s-3",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(["evidence"]),
        )

        # Change the mock; a second call with the same id must NOT re-run
        # adjudication -- it should return the original stored verdict.
        _mock_llm(direct_vm, INSUFFICIENT_VERDICT)
        second = contract.settle_intent(
            settlement_id="s-3",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(["evidence"]),
        )

        assert first == second
        assert second["fulfilled"] is True


class TestInsufficientAndPartialEvidence:
    def test_insufficient_evidence_escalates(self, contract, direct_vm):
        _mock_llm(direct_vm, INSUFFICIENT_VERDICT)

        verdict = contract.settle_intent(
            settlement_id="s-4",
            natural_language_goal="Deliver the signed contract",
            agent_claim="I delivered the signed contract",
            evidence_json=json.dumps([]),
        )

        assert verdict["fulfilled"] is False
        assert verdict["evidence_quality"] == "insufficient"
        assert verdict["recommended_action"] == "escalate"

    def test_partial_fulfillment(self, contract, direct_vm):
        _mock_llm(direct_vm, PARTIAL_VERDICT)

        verdict = contract.settle_intent(
            settlement_id="s-5",
            natural_language_goal="Deliver 3 files including a README",
            agent_claim="I delivered all 3 requested files",
            evidence_json=json.dumps(["file1.txt present", "file2.txt present"]),
        )

        assert verdict["fulfilled"] is False
        assert 0.0 < float(verdict["partial_credit"]) < 1.0
        assert verdict["recommended_action"] == "partial_payout"


class TestConsistencyGuards:
    def test_release_escrow_downgraded_when_evidence_not_strong(self, contract, direct_vm):
        inconsistent = dict(FULFILLED_VERDICT)
        inconsistent["evidence_quality"] = "weak"
        _mock_llm(direct_vm, inconsistent)

        verdict = contract.settle_intent(
            settlement_id="s-6",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(["some weak evidence"]),
        )

        # release_escrow + weak evidence is an inconsistent LLM output --
        # the contract must collapse it to escalate rather than trust it.
        assert verdict["recommended_action"] == "escalate"

    def test_fulfilled_true_with_slash_is_downgraded_to_escalate(self, contract, direct_vm):
        inconsistent = dict(FULFILLED_VERDICT)
        inconsistent["recommended_action"] = "slash"
        _mock_llm(direct_vm, inconsistent)

        verdict = contract.settle_intent(
            settlement_id="s-7",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(["evidence"]),
        )

        assert verdict["recommended_action"] == "escalate"

    def test_malformed_llm_json_falls_back_to_safe_defaults(self, contract, direct_vm):
        direct_vm.clear_mocks()
        direct_vm.mock_llm(".*", "not valid json at all")

        verdict = contract.settle_intent(
            settlement_id="s-8",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(["evidence"]),
        )

        assert verdict["fulfilled"] is False
        assert verdict["evidence_quality"] == "insufficient"
        assert verdict["recommended_action"] in ("escalate", "reject")
        assert 0.0 <= float(verdict["confidence"]) <= 1.0
        assert 0.0 <= float(verdict["partial_credit"]) <= 1.0

    def test_out_of_range_confidence_and_partial_credit_are_clamped(self, contract, direct_vm):
        bad = dict(FULFILLED_VERDICT)
        bad["confidence"] = "5.0"
        bad["partial_credit"] = "-2.0"
        bad["evidence_quality"] = "strong"
        _mock_llm(direct_vm, bad)

        verdict = contract.settle_intent(
            settlement_id="s-9",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(["evidence"]),
        )

        assert float(verdict["confidence"]) == 1.0
        assert float(verdict["partial_credit"]) == 0.0
        assert isinstance(verdict["confidence"], str)
        assert isinstance(verdict["partial_credit"], str)

    def test_unknown_evidence_quality_and_action_fall_back(self, contract, direct_vm):
        bad = dict(INSUFFICIENT_VERDICT)
        bad["evidence_quality"] = "made_up_value"
        bad["recommended_action"] = "made_up_action"
        _mock_llm(direct_vm, bad)

        verdict = contract.settle_intent(
            settlement_id="s-10",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(["evidence"]),
        )

        assert verdict["evidence_quality"] == "insufficient"
        assert verdict["recommended_action"] == "escalate"


class TestEvidenceEnrichment:
    def test_url_evidence_is_fetched(self, contract, direct_vm):
        direct_vm.clear_mocks()
        direct_vm.mock_web(
            re_escape("https://explorer.example.com/tx/0xabc"),
            {"body": "Status: Success. Transferred 500 USDC to 0xRecipient."},
        )
        direct_vm.mock_llm(".*", json.dumps(FULFILLED_VERDICT))

        verdict = contract.settle_intent(
            settlement_id="s-11",
            natural_language_goal="Transfer 500 USDC to 0xRecipient",
            agent_claim="Transferred as requested",
            evidence_json=json.dumps(
                [{"type": "url", "content": "https://explorer.example.com/tx/0xabc"}]
            ),
        )

        assert verdict["fulfilled"] is True

    def test_failed_url_fetch_does_not_abort_settlement(self, contract, direct_vm):
        direct_vm.clear_mocks()
        # No mock_web registered for this URL -> fetch raises inside
        # _fetch_evidence, which must be caught and reported as
        # unverifiable rather than reverting the whole settlement.
        direct_vm.mock_llm(".*", json.dumps(INSUFFICIENT_VERDICT))

        verdict = contract.settle_intent(
            settlement_id="s-12",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(
                [{"type": "url", "content": "https://unreachable.example.com/x"}]
            ),
        )

        assert verdict["recommended_action"] == "escalate"


class TestEscrow:
    def test_no_escrow_when_no_value_sent(self, contract, direct_vm):
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        contract.settle_intent(
            settlement_id="e-1",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(["evidence"]),
        )
        escrow = contract.get_escrow(settlement_id="e-1")
        assert escrow["status"] == "no_escrow"
        assert escrow["held_amount"] == "0"

    def test_release_escrow_pays_beneficiary(self, contract, direct_vm, direct_alice):
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        direct_vm.value = 1000
        try:
            contract.settle_intent(
                settlement_id="e-2",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps(
                    ["strong evidence", {"type": "url", "content": "https://example.com/proof"}]
                ),
                context_json=json.dumps({"beneficiary_address": str(direct_alice)}),
            )
        finally:
            direct_vm.value = 0

        escrow = contract.get_escrow(settlement_id="e-2")
        assert escrow["status"] == "released"
        assert escrow["transferred_to_beneficiary"] == "1000"
        assert escrow["refunded_to_sender"] == "0"
        assert escrow["beneficiary"] == str(direct_alice)

    def test_release_escrow_without_beneficiary_refunds_sender(self, contract, direct_vm):
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        direct_vm.value = 500
        try:
            contract.settle_intent(
                settlement_id="e-3",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps(
                    ["strong evidence", {"type": "url", "content": "https://example.com/proof"}]
                ),
            )
        finally:
            direct_vm.value = 0

        escrow = contract.get_escrow(settlement_id="e-3")
        assert escrow["status"] == "refunded_no_beneficiary"
        assert escrow["refunded_to_sender"] == "500"

    def test_partial_payout_splits_between_beneficiary_and_sender(
        self, contract, direct_vm, direct_alice
    ):
        _mock_llm(direct_vm, PARTIAL_VERDICT)
        direct_vm.value = 1000
        try:
            contract.settle_intent(
                settlement_id="e-4",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps(["partial evidence"]),
                context_json=json.dumps({"beneficiary_address": str(direct_alice)}),
            )
        finally:
            direct_vm.value = 0

        escrow = contract.get_escrow(settlement_id="e-4")
        assert escrow["status"] == "partial_paid"
        # PARTIAL_VERDICT.partial_credit == 0.66 -> 660 to beneficiary, 340 refunded
        assert escrow["transferred_to_beneficiary"] == "660"
        assert escrow["refunded_to_sender"] == "340"

    def test_slash_with_treasury_transfers_funds(self, contract, direct_vm, direct_bob):
        _mock_llm(direct_vm, SLASH_VERDICT)
        direct_vm.value = 750
        try:
            contract.settle_intent(
                settlement_id="e-5",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps(["contradicting evidence"]),
                context_json=json.dumps({"treasury_address": str(direct_bob)}),
            )
        finally:
            direct_vm.value = 0

        escrow = contract.get_escrow(settlement_id="e-5")
        assert escrow["status"] == "slashed"
        assert escrow["transferred_to_treasury"] == "750"

    def test_slash_without_treasury_holds_funds(self, contract, direct_vm):
        _mock_llm(direct_vm, SLASH_VERDICT)
        direct_vm.value = 750
        try:
            contract.settle_intent(
                settlement_id="e-6",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps(["contradicting evidence"]),
            )
        finally:
            direct_vm.value = 0

        escrow = contract.get_escrow(settlement_id="e-6")
        assert escrow["status"] == "slashed_no_treasury_held"
        assert escrow["transferred_to_treasury"] == "0"

    def test_reject_refunds_sender_in_full(self, contract, direct_vm):
        _mock_llm(direct_vm, REJECT_VERDICT)
        direct_vm.value = 300
        try:
            contract.settle_intent(
                settlement_id="e-7",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps(["evidence showing no accomplishment"]),
            )
        finally:
            direct_vm.value = 0

        escrow = contract.get_escrow(settlement_id="e-7")
        assert escrow["status"] == "refunded"
        assert escrow["refunded_to_sender"] == "300"

    def test_escalate_holds_funds(self, contract, direct_vm):
        _mock_llm(direct_vm, INSUFFICIENT_VERDICT)
        direct_vm.value = 400
        try:
            contract.settle_intent(
                settlement_id="e-8",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps([]),
            )
        finally:
            direct_vm.value = 0

        escrow = contract.get_escrow(settlement_id="e-8")
        assert escrow["status"] == "held_pending_escalation"
        assert escrow["held_amount"] == "400"

    def test_resolve_escrow_by_original_funder_succeeds(
        self, contract, direct_vm, direct_alice
    ):
        _mock_llm(direct_vm, INSUFFICIENT_VERDICT)
        direct_vm.value = 400
        try:
            contract.settle_intent(
                settlement_id="e-9",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps([]),
            )
        finally:
            direct_vm.value = 0

        resolved = contract.resolve_escrow(
            settlement_id="e-9",
            action="release_escrow",
            beneficiary_address=str(direct_alice),
        )
        assert resolved["status"] == "released"
        assert resolved["transferred_to_beneficiary"] == "400"

        escrow = contract.get_escrow(settlement_id="e-9")
        assert escrow["status"] == "released"

    def test_resolve_escrow_by_non_funder_raises(self, contract, direct_vm, direct_bob):
        _mock_llm(direct_vm, INSUFFICIENT_VERDICT)
        direct_vm.value = 400
        try:
            contract.settle_intent(
                settlement_id="e-10",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps([]),
            )
        finally:
            direct_vm.value = 0

        with direct_vm.prank(direct_bob):
            with pytest.raises(Exception):
                contract.resolve_escrow(settlement_id="e-10", action="reject")

    def test_resolve_escrow_when_not_escalated_raises(self, contract, direct_vm):
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        direct_vm.value = 100
        try:
            contract.settle_intent(
                settlement_id="e-11",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps(
                    ["strong evidence", {"type": "url", "content": "https://example.com/proof"}]
                ),
            )
        finally:
            direct_vm.value = 0

        with pytest.raises(Exception):
            contract.resolve_escrow(settlement_id="e-11", action="reject")

    def test_resolve_escrow_invalid_action_raises(self, contract, direct_vm):
        _mock_llm(direct_vm, INSUFFICIENT_VERDICT)
        direct_vm.value = 100
        try:
            contract.settle_intent(
                settlement_id="e-12",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps([]),
            )
        finally:
            direct_vm.value = 0

        with pytest.raises(Exception):
            contract.resolve_escrow(settlement_id="e-12", action="escalate")

    def test_get_escrow_unknown_settlement_raises(self, contract):
        with pytest.raises(Exception):
            contract.get_escrow(settlement_id="does-not-exist")


class TestValidationErrors:
    def test_empty_settlement_id_raises(self, contract):
        with pytest.raises(Exception):
            contract.settle_intent(
                settlement_id="",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json="[]",
            )

    def test_empty_goal_raises(self, contract):
        with pytest.raises(Exception):
            contract.settle_intent(
                settlement_id="s-13",
                natural_language_goal="",
                agent_claim="Claim",
                evidence_json="[]",
            )

    def test_invalid_evidence_json_raises(self, contract):
        with pytest.raises(Exception):
            contract.settle_intent(
                settlement_id="s-14",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json="not json",
            )

    def test_evidence_json_not_a_list_raises(self, contract):
        with pytest.raises(Exception):
            contract.settle_intent(
                settlement_id="s-15",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps({"not": "a list"}),
            )

    def test_too_many_evidence_items_raises(self, contract):
        with pytest.raises(Exception):
            contract.settle_intent(
                settlement_id="s-16",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps(["item"] * 100),
            )

    def test_get_settlement_unknown_id_raises(self, contract):
        with pytest.raises(Exception):
            contract.get_settlement(settlement_id="does-not-exist")

    def test_invalid_context_json_raises(self, contract):
        with pytest.raises(Exception):
            contract.settle_intent(
                settlement_id="s-17",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json="[]",
                context_json="not json",
            )
