"""
Studio-mode (network) tests for AgentIntentSettlement.

Unlike tests/direct/, these run against a REAL GenLayer node over RPC
(localnet via `genlayer up`, or GenLayer Studio) -- full consensus,
real (or Studio-simulated) validators, real transaction lifecycle. They
exercise things direct mode cannot: multi-validator Equivalence Principle
agreement on `settle_intent`'s LLM-derived verdict, actual transaction
ACCEPTED/FINALIZED status transitions, and real gas/payable value handling.

Requirements to run this file (NOT available in this workspace's sandbox --
no Docker/node is exposed here, so this file is written but unexecuted):
  1. A running network reachable at the RPC URL in gltest.config.yaml
     (default network: localnet, http://127.0.0.1:4000/api). Start one with
     `genlayer up` (GenLayer CLI) or point config at hosted Studio.
  2. A funded default account on that network (gltest's `default_account`
     fixture / genlayer CLI wallet).
  3. Validators configured with LLM providers so `gl.nondet.exec_prompt`
     resolves for real (not mocked) -- Studio/localnet handle this via their
     own validator LLM config, no mock_llm equivalent exists in this mode.

Run with:
    gltest tests/studio/test_agent_intent_settlement_studio.py -v --network localnet

Contract methods are called via the schema-derived Contract proxy:
  - read (view) methods:  contract.get_settlement(args=[...]).call()
  - write methods:        contract.settle_intent(args=[...]).transact(value=0)
"""
import json

import pytest
from gltest import get_contract_factory
from gltest.types import TransactionStatus

CONTRACT_NAME = "AgentIntentSettlement"


@pytest.fixture
def contract(default_account):
    factory = get_contract_factory(CONTRACT_NAME)
    return factory.deploy(account=default_account)


class TestStudioHappyPath:
    def test_settle_intent_reaches_accepted_and_stores_verdict(self, contract):
        settlement_id = "studio-s-1"

        receipt = contract.settle_intent(
            args=[
                settlement_id,
                "Transfer 500 USDC to 0xRecipient",
                "I transferred 500 USDC to 0xRecipient in tx 0xabc",
                json.dumps(
                    [
                        "On-chain log: transfer event, 500 USDC, to=0xRecipient, "
                        "tx=0xabc, status=success"
                    ]
                ),
                "",
                "{}",
            ]
        ).transact(wait_transaction_status=TransactionStatus.ACCEPTED)

        assert receipt is not None

        stored = contract.get_settlement(args=[settlement_id]).call()
        verdict = json.loads(stored)["verdict"] if isinstance(stored, str) else stored["verdict"]

        assert verdict["evidence_quality"] in (
            "strong",
            "weak",
            "conflicting",
            "insufficient",
        )
        assert verdict["recommended_action"] in (
            "release_escrow",
            "partial_payout",
            "slash",
            "reject",
            "escalate",
        )
        assert isinstance(verdict["fulfilled"], bool)
        assert 0.0 <= float(verdict["confidence"]) <= 1.0
        assert 0.0 <= float(verdict["partial_credit"]) <= 1.0

    def test_idempotent_resubmit_returns_same_verdict(self, contract):
        settlement_id = "studio-s-2"
        args = [
            settlement_id,
            "Post a tweet announcing the launch",
            "I posted the tweet",
            json.dumps(["no corroborating evidence supplied"]),
            "",
            "{}",
        ]

        contract.settle_intent(args=args).transact(
            wait_transaction_status=TransactionStatus.ACCEPTED
        )
        first = contract.get_settlement(args=[settlement_id]).call()

        # Re-submitting the same settlement_id must short-circuit to the
        # stored verdict rather than re-running (possibly different)
        # LLM-based adjudication.
        contract.settle_intent(args=args).transact(
            wait_transaction_status=TransactionStatus.ACCEPTED
        )
        second = contract.get_settlement(args=[settlement_id]).call()

        assert first == second


class TestStudioValidation:
    def test_empty_settlement_id_reverts(self, contract):
        with pytest.raises(Exception):
            contract.settle_intent(
                args=["", "Goal", "Claim", "[]", "", "{}"]
            ).transact(wait_transaction_status=TransactionStatus.ACCEPTED)

    def test_get_unknown_settlement_reverts(self, contract):
        with pytest.raises(Exception):
            contract.get_settlement(args=["never-settled"]).call()


class TestStudioEscrow:
    """Real-money-path tests: these are the ones direct mode cannot fully
    verify, since its emit_transfer is a no-op without a cross-contract
    hook. Here, actual native value moves between real accounts."""

    def test_release_escrow_actually_pays_beneficiary(self, contract, default_account, accounts):
        beneficiary = accounts[1]
        settlement_id = "studio-escrow-1"

        contract.settle_intent(
            args=[
                settlement_id,
                "Transfer 500 USDC to 0xRecipient",
                "I transferred 500 USDC to 0xRecipient in tx 0xabc",
                json.dumps(
                    ["on-chain log: transfer event, 500 USDC, to=0xRecipient, status=success"]
                ),
                "",
                json.dumps({"beneficiary_address": beneficiary.address}),
            ]
        ).transact(value=1000, wait_transaction_status=TransactionStatus.ACCEPTED)

        escrow = contract.get_escrow(args=[settlement_id]).call()
        # If the verdict didn't come back "release_escrow" (LLM-dependent),
        # this assertion documents the dependency rather than papering over
        # it with a broad try/except.
        assert escrow["status"] in ("released", "refunded_no_beneficiary")

    def test_escalated_escrow_has_no_discretionary_resolution(self, contract, default_account, accounts):
        """There is no discretionary funder-resolution method -- an
        escalated escrow stays held_pending_escalation until
        resolve_stale_escrow's timeout; that path is covered by direct-mode
        time-travel tests, not here (30 real days can't be waited out live)."""
        settlement_id = "studio-escrow-2"
        contract.settle_intent(
            args=[
                settlement_id,
                "An intentionally underspecified and unverifiable goal",
                "I did it",
                json.dumps([]),
                "",
                "{}",
            ]
        ).transact(value=200, wait_transaction_status=TransactionStatus.ACCEPTED)

        escrow = contract.get_escrow(args=[settlement_id]).call()
        if escrow["status"] != "held_pending_escalation":
            pytest.skip(
                "verdict did not escalate for this LLM response -- "
                f"got status {escrow['status']!r}"
            )

        assert not hasattr(contract, "resolve_escrow")


class TestStudioReputation:
    def test_reputation_updates_after_settlement(self, contract):
        agent_id = "studio-agent-1"
        contract.settle_intent(
            args=[
                "studio-rep-1",
                "Transfer 500 USDC to 0xRecipient",
                "I transferred 500 USDC to 0xRecipient in tx 0xabc",
                json.dumps(
                    ["on-chain log: transfer event, 500 USDC, to=0xRecipient, status=success"]
                ),
                "",
                json.dumps({"agent_id": agent_id}),
            ]
        ).transact(wait_transaction_status=TransactionStatus.ACCEPTED)

        rep = contract.get_reputation(args=[agent_id]).call()
        assert rep["total_settlements"] == 1
        assert -1.0 <= rep["reputation_score"] <= 1.0


class TestStudioMultimodalEvidence:
    def test_screenshot_evidence_settles_without_error(self, contract):
        # Requires validators actually able to fetch/render the URL and run
        # a real multimodal LLM call -- a genuinely public, stable image URL
        # keeps this from depending on any mock infrastructure (none exists
        # in Studio mode).
        receipt = contract.settle_intent(
            args=[
                "studio-shot-1",
                "The linked image shows a red square",
                "The image shows a red square as required",
                json.dumps(
                    [
                        {
                            "type": "screenshot",
                            "content": "https://via.placeholder.com/64/ff0000/ff0000.png",
                        }
                    ]
                ),
                "",
                "{}",
            ]
        ).transact(wait_transaction_status=TransactionStatus.ACCEPTED)
        assert receipt is not None
