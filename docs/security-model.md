# Security model

AgentIntentSettlement moves real value based on an LLM-derived judgment, so its trust boundaries and the fixes applied to close them are documented here rather than assumed obvious.

## Trust boundaries

- **The agent's claim is never evidence.** The adjudication prompt explicitly frames the claim as an unproven hypothesis to be checked against submitted evidence, never accepted on its own.
- **Evidence content is data, never instructions.** Fetched URL/IPFS text and submitter-supplied text are explicitly labeled as untrusted data in the prompt; the adjudicator is instructed to ignore any embedded instructions directed at it and to record an attempted manipulation as a violation. This applies uniformly to the goal, the claim, the evidence, and `context_json` — not just evidence, since all four are equally caller-controlled.
- **The LLM's raw output is never trusted directly.** Every field is validated for type and range, and cross-field consistency rules (see the README's "Verdict consistency guarantees") are re-applied deterministically by contract code on the agreed output regardless of what either LLM call produced.
- **A validator that only checks shape isn't validation.** The adjudication pipeline uses `gl.eq_principle.prompt_non_comparative` specifically so that every validator independently re-acquires evidence and re-derives its own judgment before accepting the leader's output — see "Portal steward review" below for the finding this closes.
- **Calldata safety.** GenVM's calldata encoding has no floating-point type. Every value a public method returns or stores — verdict fields, reputation scores — is represented as a string, never a raw Python float, since a float anywhere in a returned structure crashes the call.

## First audit: production-breaking and fund-safety defects

- **Calldata float-return crash.** Traced GenVM's return path (`contract_return` → calldata encoding) and confirmed every public method returning a verdict or reputation dict with a raw float would crash in production — invisible to mocked tests, which never roundtrip return values through calldata encoding. Fixed by stringifying every numeric field before it is stored or returned.
- **Settlement-id front-running.** A third party could claim a `settlement_id` with a free, zero-value call before the intended funder's real, valued call arrived; the real call would then silently return the squatter's verdict, with its attached value absorbed into the contract and never moved. Fixed with an ownership check on the idempotent resubmission path — a different sender reusing a claimed id is rejected outright, and any legitimate resubmission's newly attached value is refunded rather than absorbed.
- **Unbounded input.** No length limits on the goal, claim, criteria, context, or evidence content — a catastrophic-prompt-size griefing vector. Fixed with explicit ceilings on every caller-controlled string.
- **Verdict consistency gaps.** `release_escrow` could fire on `fulfilled = false`, and `fulfilled = true` could pair with `partial_payout`. Fixed with the consistency rules described in the README, mirrored on both the leader and validator side.

## Second audit: partial-payout drain bypass and liveness

- **Partial-payout drain bypass (the most severe finding across both audits).** The first audit's consistency rules constrained enum-value combinations but not the continuous `partial_credit` value. A verdict with `fulfilled = false`, `evidence_quality = "weak"`, `recommended_action = "partial_payout"`, and `partial_credit` near `1.0` passed every existing rule and would pay out nearly the full escrow through the "partial" path — functionally a full release that bypassed `release_escrow`'s stronger evidence requirement entirely. Fixed by capping `partial_credit` well below full value whenever evidence quality is only `"weak"`.
- **No requirement for externally verifiable evidence.** `release_escrow` could fire on evidence consisting entirely of submitter-authored text, unverifiable by construction and written by the same party who benefits from a positive verdict. Fixed by requiring at least one fetched URL, IPFS, or screenshot evidence item for full release.
- **No confidence floor for full release.** A verdict could claim `fulfilled = true` with `evidence_quality = "strong"` while reporting near-zero self-confidence and still release funds. Fixed with a minimum confidence threshold.
- **Escrow could be locked indefinitely.** If a funder never called `resolve_escrow` — lost keys, an abandoned agent, a calling contract with no forwarding path — an escalated escrow had no recovery path at all. Fixed with a permissionless, refund-only `resolve_stale_escrow`, callable by anyone after 30 days, safe to leave open because refunding a funder's own money to themselves can never be an unfair outcome for any party.

## Portal steward review: validator independence

A GenLayer Portal steward reviewing an earlier deployment (`0xB66722841c1b4C8A91b420f78eBB20a586c1D7f6`) correctly identified that its `validator_fn` only parsed the leader's JSON and checked structure/ranges — it never independently re-acquired evidence or independently judged fulfillment, so two conflicting substantive verdicts on the same evidence could both pass validation.

Two design iterations were tried in response:

1. **A hand-rolled `gl.vm.run_nondet(leader_fn, validator_fn)`** where `validator_fn` made its own second `gl.nondet.exec_prompt` call (independently re-fetching evidence and re-deriving a full verdict) and compared the two verdicts in plain Python. This was architecturally sound and unit-tested, but a live Bradbury transaction returned a genuine `DETERMINISTIC_VIOLATION` from 4 of 5 validators — GenVM's consensus protocol rejected this specific pattern of a validator making a free-standing second non-deterministic call and reconciling it in hand-written Python, rather than through the platform's own consensus-tracked mechanism.
2. **The current design: `gl.eq_principle.prompt_non_comparative`**, GenLayer's own SDK primitive for exactly this pattern. Both leader and validator independently call the same evidence-gathering function; the validator's *acceptance* of the leader's output is judged by the validator's own LLM call via the platform's internal `EqNonComparativeValidator` protocol path, not a hand-rolled Python comparison. This is the platform-sanctioned mechanism, and it does not use `response_format="json"` or a multimodal images parameter (unlike `gl.nondet.exec_prompt`) — JSON compliance now rests on prompt wording, mitigated by `_coerce_verdict`'s robust parsing and fallback-to-safe-defaults, and screenshot evidence is fetched as rendered page text rather than attached as a real image.

## Live verification

Every fix above was verified against a real deployed Bradbury contract with real `settle_intent` transactions, not only the local test suite:

- The first and second audits' highest-severity fixes: a settlement funded with detailed, plausible, self-authored-only evidence (deliberately crafted to give a full release its best chance of firing if the evidence-verifiability guard were absent) returned `fulfilled: false` and `recommended_action: "reject"` — the adjudicating model independently reasoned that self-reported evidence "is not independently verifiable and could be fabricated," converging with the contract's own code-level guard.
- The `prompt_non_comparative` redesign: two separate live transactions on the current deployment both completed without any `DETERMINISTIC_VIOLATION` (confirming that specific failure mode is closed), and in each case the leader's verdict was genuinely reactive to real conditions (correctly escalating when a URL fetch returned a real HTTP 404, correctly recommending release when evidence was strong). One transaction reached a validator `AGREE` vote that matched the leader's output exactly, confirming the comparative mechanism itself works end to end.

## Known liveness trade-off (disclosed, not hidden)

Across the two live verification transactions for the `prompt_non_comparative` redesign, most validators returned `TIMEOUT` rather than a normal `AGREE`/`DISAGREE` vote (one transaction needed 4 consensus rounds before a `LEADER_TIMEOUT`; a retry succeeded on round 0 but with 4 of 5 validators timing out and only 1 validator completing in time to vote `AGREE`). Each validator under this design performs a full independent evidence fetch plus its own LLM call, which is genuinely more work than the prior structural-only check, and this appears to routinely approach or exceed GenVM's per-validator round timeout on Bradbury under current network/LLM latency conditions. This is a liveness concern, not a correctness or security one — when a round does collect enough timely votes, the mechanism resolves to the correct, evidence-grounded outcome. It is disclosed here rather than smoothed over, and is a reasonable next area of investigation (e.g. whether round-timeout configuration or prompt/evidence-fetch latency can be tuned) independent of the correctness of the design itself.

## Liveness tuning pass (redeployment at `0x5ED018A0893209f02E3Ad721d90a3132ed024dc7`)

In response to the timeout pattern above, the per-validator independent workload was deliberately cut without weakening what is independently re-acquired or re-judged:

- `MAX_EVIDENCE_ITEMS` 25 → 10, `MAX_EVIDENCE_ITEM_CHARS` 8000 → 2000, `MAX_FETCHED_URL_CHARS` 6000 → 1500, and `MAX_GOAL_CHARS`/`MAX_CLAIM_CHARS`/`MAX_CRITERIA_CHARS`/`MAX_CONTEXT_JSON_CHARS` 4000 → 1500 each.
- A new `MAX_FETCHED_ITEMS` (3) caps how many evidence items actually trigger a network fetch per call, regardless of how many fetchable items are submitted — fetch count, not just prompt size, is a direct per-validator wall-clock cost since every validator repeats every fetch independently. Items beyond the cap are marked "not fetched" rather than silently dropped from the evidence record.
- `_fetch_evidence` no longer re-renders plain-text evidence items in the "enriched evidence" section — they were already shown in full in `evidence_summary`, so this was pure duplication of the same content in the prompt.
- `ADJUDICATION_TASK` and the equivalence-criteria rules text were both shortened, keeping every enforced rule and the evidence-first/conservative framing intact.
- Two new direct-mode tests (`TestFetchCap`) assert the fetch cap is enforced against gltest's mock-hit tracking and that settlement still succeeds correctly with evidence beyond the cap. 85 direct-mode tests total (up from 83), `genvm-lint` clean.

**Safe handling of insufficient timely votes.** No contract-level "escalate on timeout" code was needed, because the existing structure already guarantees it: `settle_intent`'s escrow transfer, reputation update, and settlement storage write all happen only *after* `gl.eq_principle.prompt_non_comparative` returns an agreed value. If a round cannot collect enough timely/matching votes, GenVM does not finalize an agreed return for that call, so none of that code ever executes — no partial state, no ambiguous escrow position. This was verified live, not just argued: three transactions in this round of testing resolved to `UNDETERMINED`→`FINALIZED`/`NO_MAJORITY`, a long-stalled `PENDING`, and `CANCELED` respectively, and `has_settlement` for all three returned `false` — confirming no state was ever written for a non-agreed round. Because no settlement record exists in that case, the same `settlement_id` can always be safely retried.

**Live results after tuning.** Four `settle_intent` calls were submitted against the retuned contract. One reached a clean majority: 3 of 5 validators voted `AGREE` (one `TIMEOUT`, one `DETERMINISTIC_VIOLATION`), the transaction finalized as `ACCEPTED`/`AGREE`, and the stored verdict was genuinely evidence-reactive — the leader detected a real HTTP 403 on the fetched URL and correctly returned `fulfilled: false` / `recommended_action: "escalate"` rather than trusting the claim. The other three did not reach a timely majority (`NO_MAJORITY`, a long `PENDING` stall, and `CANCELED`), with `TIMEOUT` and, in one round, a recurring `DETERMINISTIC_VIOLATION` vote alongside `AGREE`/`TIMEOUT` votes from other validators in the *same* round — meaning `DETERMINISTIC_VIOLATION` is not fully eliminated by the `prompt_non_comparative` primitive, only reduced to an intermittent per-validator outcome that a same-round majority can still out-vote, as it did in the successful transaction. This, together with the sustained `PENDING` stall on an isolated retry, points to some of the remaining liveness variability being current Bradbury network/validator-availability conditions rather than purely contract-side payload size — disclosed here rather than overstated as fully resolved.

## Out of scope

- **LLM judgment quality.** No code-level fix eliminates the possibility of sufficiently well-crafted evidence influencing a given model's judgment. GenLayer's network-level validator diversity (Greyboxing) is the intended mitigation for this, not something a single contract can control.
- **On-chain events.** The GenVM build this contract is deployed against does not expose a native event/log primitive. Downstream integrators should poll `get_settlement` / `has_settlement` / `get_escrow`.
- **Upgradeability.** Like any immutable contract, a future change to the adjudication prompt or consistency rules requires a new deployment at a new address; there is no in-place upgrade or state-migration path.

## Reporting a vulnerability

Report vulnerabilities privately to the repository maintainer before public disclosure. Do not include private evidence or credentials in reports.
