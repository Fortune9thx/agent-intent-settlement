"""
Direct-mode tests for the two most recently added features:
  - Reputation side-effects (context.agent_id -> get_reputation)
  - Multimodal/screenshot + IPFS evidence enrichment

Note on "screenshot" evidence: after switching the adjudication pipeline
to gl.eq_principle.prompt_non_comparative (see AgentIntentSettlement.py's
module docstring for why), screenshot-type evidence is fetched as rendered
page TEXT via gl.nondet.web.render(mode="text"), not as a real attached
image -- prompt_non_comparative's underlying call has no multimodal images
parameter. It still counts as VERIFIABLE_EVIDENCE_TYPES since a genuine
third-party fetch happens.
"""
import json
import re

import pytest

CONTRACT_PATH = "contracts/AgentIntentSettlement.py"


def re_escape(url: str) -> str:
    return re.escape(url)


FULFILLED_VERDICT = {
    "fulfilled": True,
    "confidence": "0.9",
    "reasoning": "Screenshot evidence clearly shows the requested state.",
    "partial_credit": "1.0",
    "evidence_quality": "strong",
    "violations": [],
    "recommended_action": "release_escrow",
}

PARTIAL_VERDICT = {
    "fulfilled": False,
    "confidence": "0.6",
    "reasoning": "Only part of the goal is corroborated by evidence.",
    "partial_credit": "0.5",
    "evidence_quality": "weak",
    "violations": [],
    "recommended_action": "partial_payout",
}

SLASH_VERDICT = {
    "fulfilled": False,
    "confidence": "0.9",
    "reasoning": "Evidence contradicts the claim.",
    "partial_credit": "0.0",
    "evidence_quality": "strong",
    "violations": ["contradicted claim"],
    "recommended_action": "slash",
}

INSUFFICIENT_VERDICT = {
    "fulfilled": False,
    "confidence": "0.3",
    "reasoning": "No corroborating evidence.",
    "partial_credit": "0.0",
    "evidence_quality": "insufficient",
    "violations": [],
    "recommended_action": "escalate",
}


@pytest.fixture
def contract(direct_deploy):
    return direct_deploy(CONTRACT_PATH, sdk_version="v0.2.16")


def _mock_llm(direct_vm, verdict: dict):
    direct_vm.clear_mocks()
    direct_vm.mock_llm(".*", json.dumps(verdict))


class TestScreenshotEvidence:
    def test_screenshot_evidence_is_fetched_and_counts_as_verifiable(
        self, contract, direct_vm
    ):
        direct_vm.clear_mocks()
        direct_vm.mock_web(
            re_escape("https://dashboard.example.com/status"),
            {"body": "All systems operational. Status: green."},
        )
        direct_vm.mock_llm(".*", json.dumps(FULFILLED_VERDICT))

        verdict = contract.settle_intent(
            settlement_id="m-1",
            natural_language_goal="Dashboard shows all systems green",
            agent_claim="All systems are green",
            evidence_json=json.dumps(
                [{"type": "screenshot", "content": "https://dashboard.example.com/status"}]
            ),
        )

        assert verdict["fulfilled"] is True
        assert verdict["recommended_action"] == "release_escrow"

    def test_screenshot_fetch_failure_does_not_abort_settlement(self, contract, direct_vm):
        direct_vm.clear_mocks()
        # No mock_web registered -> render() raises inside _fetch_evidence,
        # which must degrade gracefully rather than crash settle_intent.
        direct_vm.mock_llm(".*", json.dumps(INSUFFICIENT_VERDICT))

        verdict = contract.settle_intent(
            settlement_id="m-2",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(
                [{"type": "screenshot", "content": "https://unreachable.example.com/x.png"}]
            ),
        )

        assert verdict["recommended_action"] == "escalate"

    def test_many_screenshot_items_within_evidence_item_limit_all_process(
        self, contract, direct_vm
    ):
        direct_vm.clear_mocks()
        direct_vm.mock_web(".*", {"body": "page content"})
        direct_vm.mock_llm(".*", json.dumps(FULFILLED_VERDICT))

        items = [
            {"type": "screenshot", "content": f"https://example.com/shot{i}.png"}
            for i in range(5)
        ]
        verdict = contract.settle_intent(
            settlement_id="m-3",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(items),
        )
        assert verdict["fulfilled"] is True


class TestFetchCap:
    """Liveness tuning: MAX_FETCHED_ITEMS caps how many evidence items
    trigger a real network fetch per settle_intent call, regardless of how
    many fetchable items are submitted (within MAX_EVIDENCE_ITEMS) -- every
    validator repeats these fetches independently, so fetch count is a
    direct per-validator wall-clock cost."""

    def test_fetch_count_is_capped_even_with_more_fetchable_items(
        self, contract, direct_vm
    ):
        direct_vm.clear_mocks()
        for i in range(5):
            direct_vm.mock_web(
                re_escape(f"https://example.com/cap{i}"), {"body": f"content {i}"}
            )
        direct_vm.mock_llm(".*", json.dumps(FULFILLED_VERDICT))

        items = [
            {"type": "url", "content": f"https://example.com/cap{i}"}
            for i in range(5)
        ]
        contract.settle_intent(
            settlement_id="fc-1",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(items),
        )

        # Only MAX_FETCHED_ITEMS (1) of the 5 registered per-URL mocks
        # should ever have been hit -- items beyond the cap are never
        # fetched at all, not merely truncated after fetching.
        assert len(direct_vm._web_mocks_hit) == 1

    def test_settlement_still_succeeds_with_items_beyond_fetch_cap(
        self, contract, direct_vm
    ):
        direct_vm.clear_mocks()
        direct_vm.mock_web(".*", {"body": "content"})
        direct_vm.mock_llm(".*", json.dumps(FULFILLED_VERDICT))

        items = [
            {"type": "url", "content": f"https://example.com/beyond{i}"}
            for i in range(5)
        ]
        verdict = contract.settle_intent(
            settlement_id="fc-2",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(items),
        )
        assert verdict["fulfilled"] is True
        assert verdict["recommended_action"] == "release_escrow"


class TestIpfsEvidence:
    def test_ipfs_cid_is_resolved_via_gateway(self, contract, direct_vm):
        cid = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
        direct_vm.clear_mocks()
        direct_vm.mock_web(
            re_escape(f"https://ipfs.io/ipfs/{cid}"),
            {"body": "Delivery confirmation: package received, signed by recipient."},
        )
        direct_vm.mock_llm(".*", json.dumps(FULFILLED_VERDICT))

        verdict = contract.settle_intent(
            settlement_id="m-4",
            natural_language_goal="Deliver the package",
            agent_claim="Delivered and signed for",
            evidence_json=json.dumps([{"type": "ipfs", "content": cid}]),
        )
        assert verdict["fulfilled"] is True

    def test_ipfs_uri_scheme_is_normalized(self, contract, direct_vm):
        cid = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
        direct_vm.clear_mocks()
        direct_vm.mock_web(
            re_escape(f"https://ipfs.io/ipfs/{cid}"),
            {"body": "some content"},
        )
        direct_vm.mock_llm(".*", json.dumps(FULFILLED_VERDICT))

        verdict = contract.settle_intent(
            settlement_id="m-5",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps([{"type": "ipfs", "content": f"ipfs://{cid}"}]),
        )
        assert verdict["fulfilled"] is True

    def test_ipfs_fetch_failure_does_not_abort_settlement(self, contract, direct_vm):
        direct_vm.clear_mocks()
        direct_vm.mock_llm(".*", json.dumps(INSUFFICIENT_VERDICT))

        verdict = contract.settle_intent(
            settlement_id="m-6",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps([{"type": "ipfs", "content": "unresolvable-cid"}]),
        )
        assert verdict["recommended_action"] == "escalate"


class TestReputation:
    def test_no_reputation_tracked_without_agent_id(self, contract, direct_vm):
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        contract.settle_intent(
            settlement_id="r-1",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(["evidence"]),
        )
        assert contract.has_reputation(agent_id="agent-x") is False

    def test_release_escrow_improves_reputation(self, contract, direct_vm):
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        contract.settle_intent(
            settlement_id="r-2",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(
                ["evidence", {"type": "url", "content": "https://example.com/proof"}]
            ),
            context_json=json.dumps({"agent_id": "agent-alpha"}),
        )

        rep = contract.get_reputation(agent_id="agent-alpha")
        assert rep["total_settlements"] == 1
        assert rep["released_count"] == 1
        assert float(rep["reputation_score"]) == 1.0
        assert isinstance(rep["reputation_score"], str)

    def test_slash_lowers_reputation(self, contract, direct_vm):
        _mock_llm(direct_vm, SLASH_VERDICT)
        contract.settle_intent(
            settlement_id="r-3",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(["evidence"]),
            context_json=json.dumps({"agent_id": "agent-beta"}),
        )

        rep = contract.get_reputation(agent_id="agent-beta")
        assert rep["slashed_count"] == 1
        assert float(rep["reputation_score"]) == -1.0

    def test_reputation_accumulates_across_settlements(self, contract, direct_vm):
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        contract.settle_intent(
            settlement_id="r-4a",
            natural_language_goal="Goal A",
            agent_claim="Claim A",
            evidence_json=json.dumps(
                ["evidence", {"type": "url", "content": "https://example.com/proof"}]
            ),
            context_json=json.dumps({"agent_id": "agent-gamma"}),
        )

        _mock_llm(direct_vm, PARTIAL_VERDICT)
        contract.settle_intent(
            settlement_id="r-4b",
            natural_language_goal="Goal B",
            agent_claim="Claim B",
            evidence_json=json.dumps(["evidence"]),
            context_json=json.dumps({"agent_id": "agent-gamma"}),
        )

        rep = contract.get_reputation(agent_id="agent-gamma")
        assert rep["total_settlements"] == 2
        assert rep["released_count"] == 1
        assert rep["partial_count"] == 1
        # score_sum = 1.0 (release) + 0.5 (partial_credit) = 1.5 / 2
        assert float(rep["reputation_score"]) == pytest.approx(0.75)

    def test_get_reputation_unknown_agent_raises(self, contract):
        with pytest.raises(Exception):
            contract.get_reputation(agent_id="never-seen")

    def test_blank_agent_id_is_treated_as_absent(self, contract, direct_vm):
        _mock_llm(direct_vm, FULFILLED_VERDICT)
        contract.settle_intent(
            settlement_id="r-5",
            natural_language_goal="Goal",
            agent_claim="Claim",
            evidence_json=json.dumps(["evidence"]),
            context_json=json.dumps({"agent_id": "   "}),
        )
        assert contract.has_reputation(agent_id="   ") is False
