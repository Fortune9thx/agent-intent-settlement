# Changelog

## 1.2.0

- Fifth adversarial pass (final pre-resubmission, targeting the "bind every fund-affecting output to independent assessment" requirement directly): identified that `resolve_escrow` let the original funder manually override an `"escalate"` outcome (`release_escrow`/`partial_payout`/`slash`/`reject`) on their own escrowed funds without a fresh independent evidence assessment -- a fund-affecting output produced by discretion, not consensus.
- **Removed `resolve_escrow` entirely.** Every fund movement is now either (a) bound to the independently-assessed `settle_intent` verdict, or (b) `resolve_stale_escrow`'s permissionless, strictly refund-only safety valve after a 30-day timeout on an unresolved escalation. No discretionary path remains. `_execute_escrow_action`'s `"escalate"` branch is unchanged -- it still only holds funds; there is simply no longer any earlier manual exit.
- Removed the four `resolve_escrow`-specific tests (three of which only "passed" because calling a now-nonexistent method raises an exception, not because they proved the intended behavior). Added `test_no_discretionary_resolve_escrow_method_exists` and `test_escalated_escrow_only_movable_via_stale_timeout`. 85 direct-mode tests (down from 87 -- net two fewer), `genvm-lint` clean, 7 public methods (2 writes, down from 3).
- Redeployed to `0xEcB0951a3d7361A01998936D34DC2DBc9DE72Dbc` on GenLayer Studio Network. Deploy and one post-change `settle_intent` call both reached `ACCEPTED`/`MAJORITY_AGREE` (3/5 `AGREE`). A second call meant to demonstrate an escalate outcome live hit Studio's hourly RPC rate limit before submission -- disclosed rather than fabricated; the escalate/stale-refund path is proven deterministically via direct-mode time-travel tests instead, which a live run can't practically demonstrate anyway (30 real days).
- A designated arbiter/DAO role for time-sensitive escalations remains a documented, explicitly out-of-scope future direction -- not added under this pass, to avoid reintroducing an unbound path under time pressure.

## 1.1.4

- Fourth adversarial pass (final pre-resubmission, explicitly not re-litigating the closed validator-independence finding): constructed and closed a concrete counter-example where `MAX_FETCHED_ITEMS = 1` let a submitter deterministically bury a contradicting-but-verifiable evidence item past the fetch cap via array ordering, since every validator applies the same first-N-in-order rule independently. Fixed by rewriting the skip note in `_fetch_evidence` to explicitly warn that a skipped item's content is unknown, not neutral, and that `evidence_quality` must not be rated "strong" while unfetched verifiable-type items remain -- disclosed as a mitigation, not a full structural close, since any fixed selection rule below the submitted item count remains order-gameable in principle.
- Documented (not a fund-safety bug, but a real observability gap): `resolve_escrow`/`resolve_stale_escrow` update `escrow.status` but never rewrite the stored `verdict` or adjust reputation. `escrow.status` is now explicitly documented as the source of truth for funds; `verdict` as an immutable record of the original automated assessment only.
- Redeployed to `0xb906fa472d044470B1c28461120610De4EA877ce` on GenLayer Studio Network. Live-verified the exact buried-evidence counter-example against the real model: correctly returned `fulfilled: false` / `escalate`, `ACCEPTED`/`MAJORITY_AGREE` with 2/5 genuine `DISAGREE` votes.
- 87 direct-mode tests (up from 85) -- including a behavioral test that only passes if the new caution wording is actually present in the leader/validator's input, not merely in the source. `genvm-lint` clean.

## 1.1.3

- No contract code changes. Bradbury testnet's transaction-activation queue was confirmed (via the explorer's own queue-position display, and terminal `CANCELED`/`NOT_VOTED`/`numOfRounds: 0` receipts) to be backlogged network-wide, unrelated to contract correctness or payload size. The identical `1.1.2` contract source was deployed to GenLayer Studio Network (chain id 61999) for a clean verification run.
- Deployed to `0xb906fa472d044470B1c28461120610De4EA877ce`. Deploy: `FINALIZED`/`MAJORITY_AGREE`. Three independent `settle_intent` calls, all `ACCEPTED`/`MAJORITY_AGREE` (3/5 `AGREE` each, including one round with a genuine `DISAGREE` vote), with verdicts genuinely reactive to each validator's own independent fetch outcome (a successful fetch, a failed fetch, and an HTTP 403) rather than to the submitted claim text. Reputation aggregation verified correct across two settlements for the same agent.
- This is now the primary reference deployment; see `docs/security-model.md` for the full verification record.

## 1.1.2

- Further liveness tuning: `MAX_FETCHED_ITEMS` 3 → 1, evidence/prompt ceilings cut again (`MAX_EVIDENCE_ITEMS` 10 → 5, char limits roughly halved), required reasoning length shortened. Redeployed to `0xdC53EACBD7685a8dbd4fe1E889ed50dB272766a6`.
- Live verification at this deployment hit sustained `PENDING`/pre-round-activity stalls on Bradbury rather than the round-level `TIMEOUT`/`DETERMINISTIC_VIOLATION` pattern seen previously -- disclosed as a signal that the remaining bottleneck for those specific attempts was network/validator-availability conditions, not contract payload size. `has_settlement` confirmed no state was written for any of the four non-finalized attempts. The prior deployment (`0x5ED018A0893209f02E3Ad721d90a3132ed024dc7`) remains the reference for a clean, live, majority-`AGREE` result under the same design.
- 85 direct-mode tests, `genvm-lint` clean.

## 1.1.1

- Liveness tuning in response to the validator-timeout pattern disclosed in 1.1.0: cut per-validator independent workload without weakening what is independently re-acquired/re-judged -- tighter evidence-item/char/prompt-field ceilings, a new per-call fetch cap (`MAX_FETCHED_ITEMS`, default 3) so validator fetch count no longer scales with submitted evidence volume, and a shorter adjudication prompt.
- Removed redundant duplication where plain-text evidence content was rendered twice (once in the evidence summary, once again in the "enriched evidence" section).
- Documented, and live-verified, why insufficient timely consensus votes cannot leave funds/state ambiguous: `settle_intent`'s escrow/reputation/storage-write code only runs after `prompt_non_comparative` returns an agreed value, so an unfinalized round writes no state at all and the same `settlement_id` can always be safely retried -- confirmed via `has_settlement` returning `false` for three non-finalized live transactions.
- Redeployed to `0x5ED018A0893209f02E3Ad721d90a3132ed024dc7`. Live testing after tuning produced one clean majority `AGREE` (3/5 validators, correct evidence-reactive verdict on a real HTTP 403) alongside continued liveness variability, including a `DETERMINISTIC_VIOLATION` vote recurring intermittently within an otherwise-mixed round -- disclosed in `docs/security-model.md` as not fully eliminated, only reduced to a minority vote a same-round majority can still out-vote.
- 85 direct-mode tests (up from 83), `genvm-lint` clean.

## 1.1.0

- Redesigned the adjudication pipeline around `gl.eq_principle.prompt_non_comparative`, GenLayer's own SDK primitive for comparative validation, in response to a Portal steward review: every validator now independently re-fetches evidence and re-derives its own judgment before accepting the leader's output, rather than only checking structure/ranges.
- An intermediate hand-rolled `run_nondet` + manual Python comparison design was tried first, unit-tested, and then found to trigger a real `DETERMINISTIC_VIOLATION` on live Bradbury consensus -- documented in `docs/security-model.md` alongside why the SDK-native primitive was chosen instead.
- Trade-off from the new primitive: no `response_format="json"` enforcement (mitigated by existing robust parsing/fallback) and no multimodal images parameter (screenshot evidence is now fetched as rendered text, not attached as a real image).
- 83 direct-mode tests (up from 77), `genvm-lint` clean, redeployed and live-verified on GenLayer Bradbury -- including a disclosed liveness trade-off observed during verification (see `docs/security-model.md`).

## 1.0.0

- Initial `settle_intent` adjudication pipeline: goal/claim/evidence intake, `run_nondet`-based leader/validator consensus, strict verdict schema.
- Evidence enrichment: URL, IPFS, and screenshot (multimodal) evidence types alongside plain text.
- Escrow integration: `settle_intent` is payable and executes its own verdict's financial consequence (release, partial payout, slash, refund, or hold) deterministically.
- `resolve_escrow` for funder-directed resolution of escalated cases.
- Optional per-agent reputation tracking via `context.agent_id`.
- First adversarial audit: fixed a calldata float-return crash, a settlement-id front-running/fund-stranding path, unbounded input lengths, and verdict consistency gaps. Switched from `run_nondet_unsafe` to `run_nondet` for proper validator-error sandboxing.
- Second adversarial audit: fixed a partial-payout escrow-drain bypass (the most severe finding across both audits), added an externally-verifiable-evidence requirement and a minimum confidence threshold for full escrow release, added a reasoning-substantiveness floor, and added a permissionless `resolve_stale_escrow` fallback for escrow that would otherwise be locked indefinitely.
- 77 direct-mode tests, `genvm-lint` clean, deployed and live-verified on GenLayer Bradbury.
