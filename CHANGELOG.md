# Changelog

## 1.0.0

- Initial `settle_intent` adjudication pipeline: goal/claim/evidence intake, `run_nondet`-based leader/validator consensus, strict verdict schema.
- Evidence enrichment: URL, IPFS, and screenshot (multimodal) evidence types alongside plain text.
- Escrow integration: `settle_intent` is payable and executes its own verdict's financial consequence (release, partial payout, slash, refund, or hold) deterministically.
- `resolve_escrow` for funder-directed resolution of escalated cases.
- Optional per-agent reputation tracking via `context.agent_id`.
- First adversarial audit: fixed a calldata float-return crash, a settlement-id front-running/fund-stranding path, unbounded input lengths, and verdict consistency gaps. Switched from `run_nondet_unsafe` to `run_nondet` for proper validator-error sandboxing.
- Second adversarial audit: fixed a partial-payout escrow-drain bypass (the most severe finding across both audits), added an externally-verifiable-evidence requirement and a minimum confidence threshold for full escrow release, added a reasoning-substantiveness floor, and added a permissionless `resolve_stale_escrow` fallback for escrow that would otherwise be locked indefinitely.
- 77 direct-mode tests, `genvm-lint` clean, deployed and live-verified on GenLayer Bradbury.
