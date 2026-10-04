# IQCouncil — working conventions

Read this before writing code. It is short on purpose; everything in it is load-bearing.

## What this is

An AI operating layer for a single decision-maker running a real business. Eight domain agents work either as **lanes** (execute work, can write, approval-gated) or as **seats** (judge a question, structurally read-only). When a decision is material, cross-domain and hard to reverse, a **council** convenes: seats answer independently, a second round runs only if they disagree, and a **decision record** captures the recommendation with dissent preserved verbatim.

The full design pack lives outside this repo. `docs/` holds only what implementation needs.

## Current phase

**Phase 0** — HAL contract, conformance suite, first adapter, and the vertical slice in `docs/vertical-slice.md`.

There is no UI in this phase. Do not build one.

## The rules that matter most

**R1 — nothing above L1 knows which harness is running.** No harness types, SDKs, protocol clients, config formats or error semantics may appear in `control-plane/`, in skills, or in any future client. The adapter is the only place with that knowledge.

*This is the rule you will break by accident.* If you are importing anything harness-specific outside `packages/adapter-*`, stop and extend the contract instead.

**R2 — integrate at documented contracts only.** Published APIs, CLIs, config and file formats. No patching internals, no undocumented behaviour.

**R3 — never fork the harness or its UI.** Our clients are separate applications speaking the HAL contract.

**R4 — pin the version.** Never track `main`. Upgrades are deliberate and validated in a staging tenant.

**R5 — contribute upstream rather than diverge.** Wanting to change harness behaviour means we have mislocated the feature.

**R6 — security posture is ours.** Every control that ships disabled upstream is enabled here. See `docs/security-gate.md`.

**R7 — ownership governs mutation.** Vendor-owned skills are never modified autonomously; proposals only.

**R8 — tenant content never leaves the container.** Cross-boundary payloads carry only values from the closed vocabulary in `packages/hal/src/telemetry.ts`. No free text, no summaries, no runtime-authored strings. A string field in a cross-boundary payload is a bug, not a convenience.

## Vocabulary — use these exactly

| Term | Means | Not |
|---|---|---|
| **Venture** | The unit of tenancy. One business, one container, one workspace | "tenant", "org", "workspace" alone |
| **Identity** | One human, globally. Holds preferences and memberships | "user" |
| **Account** | The billing entity. Owns ventures | - |
| **Archetype** | How the venture produces and delivers | Not the industry |
| **Industry** | The sector it sells into | Not the archetype |
| **Market** | A named geographic scope, one or more territories | Not necessarily a country |
| **Lane** | An agent executing work | "worker", "task agent" |
| **Seat** | The same agent judging a question, read-only | "persona", "advisor" |
| **Council** | A convened deliberation | "meeting", "debate" |
| **Chair** | Frames, selects, synthesizes. Does not vote | "orchestrator", "conductor" |
| **Record** | A decision record | "report", "output" |

Prefer the domain word over the technical one in code, comments and commits.

## Repo layout

```
packages/hal/              The capability contract. Types only - no implementation
packages/hal-conformance/  The suite every adapter must pass before serving a tenant
packages/adapter-openclaw/ First adapter. The ONLY place harness specifics live
packages/control-plane/    L2 - tenancy, metering, policy. Binds to HAL, never a harness
skills/                    Neutral skill sources, compiled per harness at provisioning
docs/                      What implementation needs. The design pack lives elsewhere
```

## Conventions

- **TypeScript, strict.** `any` needs a comment explaining why.
- **Branded ID types.** `VentureId`, `IdentityId` and friends are not interchangeable strings.
- **Money is never a number.** Use the `Money` type. Approval ceilings are in currency and the product operates in VND, JPY, USD and EUR.
- **Every operation carries an actor and a venture scope.** Unscoped calls are rejected at the adapter, not deeper.
- **Capability flags, never silent no-ops.** An unimplemented capability reports itself absent so callers degrade visibly.
- **Errors are typed and domain-shaped.** Not `throw new Error(string)`.
- Tests sit beside the code. The conformance suite is the exception and lives in its own package.

## What not to do

- Do not build UI in phase 0.
- Do not implement a capability by calling the harness from outside the adapter.
- Do not add a capability to the contract without a conformance assertion for it.
- Do not put free text in a telemetry payload.
- Do not let a seat bind a write capability.
- Do not write "vertical" when you mean "industry".
- Do not add dependencies without saying why in the commit.

## Commits

Imperative, scoped by package, one concern each. Reference the rule or assertion when a change is driven by one, for example `hal: reject unscoped turn submission (assertion 8)`.
