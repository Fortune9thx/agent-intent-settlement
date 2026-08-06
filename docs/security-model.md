# Security model

AgentIntentSettlement moves real value based on an LLM-derived judgment, so its trust boundaries and the fixes applied to close them are documented here rather than assumed obvious.

## Trust boundaries

- **The agent's claim is never evidence.** The adjudication prompt explicitly frames the claim as an unproven hypothesis to be checked against submitted evidence, never accepted on its own.
- **Evidence content is data, never instructions.** Fetched URL/IPFS text and submitter-supplied text are explicitly labeled as untrusted data in the prompt; the adjudicator is instructed to ignore any embedded instructions directed at it and to record an attempted manipulation as a violation. This applies uniformly to the goal, the claim, the evidence, and `context_json` — not just evidence, since all four are equally caller-controlled.
- **The LLM's raw output is never trusted directly.** Every field is validated for type and range, and cross-field consistency rules (see the README's "Verdict consistency guarantees") are enforced deterministically on both the leader's proposal and every validator's independent check, so an internally-inconsistent or manipulated verdict cannot reach consensus.
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

## Live verification

Both audits' highest-severity fixes were verified against the deployed Bradbury contract with real `settle_intent` transactions, not only the local test suite. In particular, a settlement funded with detailed, plausible, self-authored-only evidence (deliberately crafted to give a full release its best chance of firing if the evidence-verifiability guard were absent) returned `fulfilled: false` and `recommended_action: "reject"` — the adjudicating model independently reasoned that self-reported evidence "is not independently verifiable and could be fabricated," converging with the contract's own code-level guard.

## Out of scope

- **LLM judgment quality.** No code-level fix eliminates the possibility of sufficiently well-crafted evidence influencing a given model's judgment. GenLayer's network-level validator diversity (Greyboxing) is the intended mitigation for this, not something a single contract can control.
- **On-chain events.** The GenVM build this contract is deployed against does not expose a native event/log primitive. Downstream integrators should poll `get_settlement` / `has_settlement` / `get_escrow`.
- **Upgradeability.** Like any immutable contract, a future change to the adjudication prompt or consistency rules requires a new deployment at a new address; there is no in-place upgrade or state-migration path.

## Reporting a vulnerability

Report vulnerabilities privately to the repository maintainer before public disclosure. Do not include private evidence or credentials in reports.
