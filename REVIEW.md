# Review policy

Applies to every review lane and to intent-pr-review.

- **Passes:** each lane reviews the diff once; the consolidator dedupes into one fix ledger.
- **Important vs nit:** Important = correctness, security, a broken R1–R8 rule, a contradicted register claim, a seat binding a write capability, money as `number`, free text in a cross-boundary payload. Everything else is a nit.
- **Nit cap:** at most 5 nits per lane per PR; surplus is dropped, not queued.
- **Exclusions:** generated files, lockfiles, `docs/intent/**` generated artifacts, vendored code.
- **Phase 0:** no UI. A PR adding UI is rejected regardless of quality.
