"""
Regression tests for the second serious-probe audit on AgentIntentSettlement
(hostile red-team pass on top of the already-audited contract). Each class
targets one specific new finding, proving it is closed with a concrete
attack payload or failure mode, not just "still passes."

New findings covered:
  N1 (HIGH)   -- partial_payout could drain ~100% of escrow on merely
                 "weak" evidence via partial_credit close to 1.0,
                 completely bypassing release_escrow's evidence_quality
                 == "strong" gate. This was the most serious finding of
                 the probe: a second-order bug the first audit's own
                 tightened consistency rules did not close, because they
                 only fenced off enum-value combinations, not the
                 continuous partial_credit value.
  N2 (MEDIUM) -- release_escrow could fire on evidence consisting entirely
                 of submitter-authored "text" (unverifiable by
                 construction), with no requirement for any externally
                 fetched corroboration.
  N3 (MEDIUM) -- release_escrow had no minimum confidence floor; a verdict
                 could claim "fulfilled, strong evidence" while reporting
                 near-zero self-confidence and still release funds.
  N4 (MEDIUM, economic) -- an escalated escrow whose funder never calls
                 resolve_escrow (lost keys, abandoned bot, a calling
                 contract with no forwarding path -- all realistic,
                 non-adversarial scenarios) was locked forever with no
                 recovery path at all.
  N5 (LOW, observability) -- no floor on how substantive `reasoning` had
                 to be; a technically-valid one-word reasoning provided no
                 real transparency to a downstream integrator.
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


VERIFIABLE_EVIDENCE = json.dumps(
    ["some text", {"type": "url", "content": "https://example.com/proof"}]
)
TEXT_ONLY_EVIDENCE = json.dumps(["submitter-authored text, nothing external"])


class TestPartialPayoutDrainBypass:
    """N1 -- the most important finding of this probe."""

    def test_weak_evidence_partial_payout_is_capped_well_below_full(
        self, contract, direct_vm
    ):
        drain_attempt = {
            "fulfilled": False,
            "confidence": "0.55",
            "reasoning": (
                "The evidence is weak and self-authored, but I am reporting "
                "high partial credit anyway to test the cap."
            ),
            "partial_credit": "0.99",
            "evidence_quality": "weak",
            "violations": [],
            "recommended_action": "partial_payout",
        }
        _mock_llm(direct_vm, drain_attempt)

        direct_vm.value = 100_000
        try:
            verdict = contract.settle_intent(
                settlement_id="drain-1",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=TEXT_ONLY_EVIDENCE,
            )
        finally:
            direct_vm.value = 0

        # partial_credit itself must be clamped in the stored verdict...
        assert float(verdict["partial_credit"]) <= 0.7 + 1e-9

        # ...and the ACTUAL fund movement must respect that cap -- this is
        # the part that matters. Before the fix, this would transfer
        # 99,000 of the 100,000 attached (99% "partial" payout) on weak,
        # self-authored evidence alone.
        escrow = contract.get_escrow(settlement_id="drain-1")
        transferred = int(escrow["transferred_to_beneficiary"]) + int(
            escrow["refunded_to_sender"]
        )
        beneficiary_share = int(escrow["transferred_to_beneficiary"])
        assert beneficiary_share <= int(100_000 * 0.7) + 1
        assert transferred == 100_000  # no funds silently vanish either

    def test_strong_evidence_partial_payout_is_not_capped(self, contract, direct_vm):
        """The cap must only bite on "weak" evidence -- genuine strong
        partial-fulfillment evidence should still be payable near-fully if
        the LLM genuinely assesses it that way (partial_payout doesn't
        require the release_escrow-only "strong" gate -- it's the "weak"
        tier specifically that's capped, since insufficient/conflicting
        are already blocked entirely and strong should ordinarily route to
        release_escrow, but the contract doesn't forbid an LLM choosing
        partial_payout with strong evidence for a genuinely partial goal)."""
        high_credit_strong = {
            "fulfilled": False,
            "confidence": "0.9",
            "reasoning": "Strong evidence shows 90% of a multi-part goal was completed.",
            "partial_credit": "0.9",
            "evidence_quality": "strong",
            "violations": [],
            "recommended_action": "partial_payout",
        }
        _mock_llm(direct_vm, high_credit_strong)
        verdict = contract.settle_intent(
            settlement_id="drain-2",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=VERIFIABLE_EVIDENCE,
        )
        assert float(verdict["partial_credit"]) == pytest.approx(0.9)


class TestReleaseEscrowRequiresVerifiableEvidence:
    """N2"""

    def test_release_escrow_downgraded_on_text_only_evidence(self, contract, direct_vm):
        fulfilled_strong = {
            "fulfilled": True,
            "confidence": "0.95",
            "reasoning": "The self-authored evidence text is detailed and specific.",
            "partial_credit": "1.0",
            "evidence_quality": "strong",
            "violations": [],
            "recommended_action": "release_escrow",
        }
        _mock_llm(direct_vm, fulfilled_strong)
        verdict = contract.settle_intent(
            settlement_id="verif-1",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=TEXT_ONLY_EVIDENCE,
        )
        assert verdict["recommended_action"] == "escalate"

    def test_release_escrow_succeeds_with_url_evidence_present(self, contract, direct_vm):
        fulfilled_strong = {
            "fulfilled": True,
            "confidence": "0.95",
            "reasoning": "The fetched URL evidence directly corroborates the goal.",
            "partial_credit": "1.0",
            "evidence_quality": "strong",
            "violations": [],
            "recommended_action": "release_escrow",
        }
        _mock_llm(direct_vm, fulfilled_strong)
        verdict = contract.settle_intent(
            settlement_id="verif-2",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=VERIFIABLE_EVIDENCE,
        )
        assert verdict["recommended_action"] == "release_escrow"

    def test_release_escrow_succeeds_with_screenshot_evidence_present(
        self, contract, direct_vm
    ):
        fulfilled_strong = {
            "fulfilled": True,
            "confidence": "0.95",
            "reasoning": "The screenshot evidence directly corroborates the goal.",
            "partial_credit": "1.0",
            "evidence_quality": "strong",
            "violations": [],
            "recommended_action": "release_escrow",
        }
        _mock_llm(direct_vm, fulfilled_strong)
        verdict = contract.settle_intent(
            settlement_id="verif-3",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(
                [{"type": "screenshot", "content": "https://example.com/shot.png"}]
            ),
        )
        assert verdict["recommended_action"] == "release_escrow"

    def test_release_escrow_succeeds_with_ipfs_evidence_present(self, contract, direct_vm):
        fulfilled_strong = {
            "fulfilled": True,
            "confidence": "0.95",
            "reasoning": "The IPFS-hosted evidence directly corroborates the goal.",
            "partial_credit": "1.0",
            "evidence_quality": "strong",
            "violations": [],
            "recommended_action": "release_escrow",
        }
        _mock_llm(direct_vm, fulfilled_strong)
        verdict = contract.settle_intent(
            settlement_id="verif-4",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(
                [{"type": "ipfs", "content": "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"}]
            ),
        )
        assert verdict["recommended_action"] == "release_escrow"


class TestReleaseEscrowMinimumConfidence:
    """N3"""

    def test_low_confidence_blocks_release_escrow(self, contract, direct_vm):
        low_confidence = {
            "fulfilled": True,
            "confidence": "0.2",
            "reasoning": "Technically I found this fulfilled but I am not very sure.",
            "partial_credit": "1.0",
            "evidence_quality": "strong",
            "violations": [],
            "recommended_action": "release_escrow",
        }
        _mock_llm(direct_vm, low_confidence)
        verdict = contract.settle_intent(
            settlement_id="conf-1",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=VERIFIABLE_EVIDENCE,
        )
        assert verdict["recommended_action"] == "escalate"

    def test_confidence_at_threshold_allows_release_escrow(self, contract, direct_vm):
        at_threshold = {
            "fulfilled": True,
            "confidence": "0.6",
            "reasoning": "Confident this is fulfilled based on strong external evidence.",
            "partial_credit": "1.0",
            "evidence_quality": "strong",
            "violations": [],
            "recommended_action": "release_escrow",
        }
        _mock_llm(direct_vm, at_threshold)
        verdict = contract.settle_intent(
            settlement_id="conf-2",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=VERIFIABLE_EVIDENCE,
        )
        assert verdict["recommended_action"] == "release_escrow"


class TestReasoningSubstantivenessFloor:
    """N5"""

    def test_extremely_short_reasoning_is_padded_not_silently_accepted(
        self, contract, direct_vm
    ):
        terse = {
            "fulfilled": False,
            "confidence": "0.5",
            "reasoning": "ok",
            "partial_credit": "0.0",
            "evidence_quality": "insufficient",
            "violations": [],
            "recommended_action": "escalate",
        }
        _mock_llm(direct_vm, terse)
        verdict = contract.settle_intent(
            settlement_id="reason-1",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps([]),
        )
        assert len(verdict["reasoning"]) >= 20


class TestStaleEscrowFallback:
    """N4 -- economic/liveness probe: can funds be locked forever?"""

    def test_cannot_resolve_stale_before_timeout(self, contract, direct_vm):
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
        direct_vm.value = 1000
        try:
            contract.settle_intent(
                settlement_id="stale-1",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps([]),
            )
        finally:
            direct_vm.value = 0

        with pytest.raises(Exception):
            contract.resolve_stale_escrow(settlement_id="stale-1")

    def test_permissionless_refund_after_timeout(self, contract, direct_vm, direct_bob):
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
        direct_vm.value = 5000
        try:
            contract.settle_intent(
                settlement_id="stale-2",
                natural_language_goal="Goal",
                agent_claim="Claim",
                evidence_json=json.dumps([]),
            )
        finally:
            direct_vm.value = 0

        escrow_before = contract.get_escrow(settlement_id="stale-2")
        assert escrow_before["status"] == "held_pending_escalation"

        # Fast-forward past the timeout (31 days) and warp the mocked clock.
        import datetime

        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=31)
        direct_vm.warp(future.strftime("%Y-%m-%dT%H:%M:%SZ"))

        # A totally unrelated third party (bob, never the funder) triggers
        # the fallback -- this must succeed precisely because it's
        # refund-only and permissionless by design.
        with direct_vm.prank(direct_bob):
            result = contract.resolve_stale_escrow(settlement_id="stale-2")

        assert result["status"] == "refunded_stale"
        assert result["refunded_to_sender"] == "5000"

        escrow_after = contract.get_escrow(settlement_id="stale-2")
        assert escrow_after["status"] == "refunded_stale"

    def test_resolve_stale_escrow_on_non_escalated_settlement_raises(
        self, contract, direct_vm
    ):
        fulfilled_strong = {
            "fulfilled": True,
            "confidence": "0.9",
            "reasoning": "Strong evidence directly corroborates the goal.",
            "partial_credit": "1.0",
            "evidence_quality": "strong",
            "violations": [],
            "recommended_action": "release_escrow",
        }
        _mock_llm(direct_vm, fulfilled_strong)
        contract.settle_intent(
            settlement_id="stale-3",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=VERIFIABLE_EVIDENCE,
        )
        with pytest.raises(Exception):
            contract.resolve_stale_escrow(settlement_id="stale-3")
