# IQCouncil

An AI operating layer for single decision-makers running real businesses.

**Start with [`CLAUDE.md`](./CLAUDE.md).** It holds the conventions, the boundary rules and the vocabulary.

## Phase 0 scope

| Deliverable | Where | Gate |
|---|---|---|
| HAL capability contract | `packages/hal` | Compiles, every domain declared |
| Conformance suite | `packages/hal-conformance` | 9 behavioural assertions, all failing initially |
| OpenClaw adapter | `packages/adapter-openclaw` | Passes the suite |
| Vertical slice | `docs/vertical-slice.md` | One decision, end to end, no UI |

## Layers

```
L4  Surfaces            not in phase 0
L3  Council and skills  skills/
L2  Control plane       packages/control-plane
L1  HAL                 packages/hal, packages/adapter-*
L0  Harness             rented, pinned, never forked
```

Nothing above L1 may know which harness is running. See R1.

## Running

```bash
npm install
npm run build
npm run conformance     # expect failures until the adapter is implemented
```
