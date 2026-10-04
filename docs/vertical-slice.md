# Vertical slice - the phase 0 acceptance test

## Why this slice

It is the cheapest honest test of **S2**: *does a multi-seat council with recorded dissent produce a materially better decision than one well-prompted model?*

S2 is the premise the company rests on, and premises are what nobody tests. If it fails, the right response is a much simpler product - and we want to know that in week six, not month eight.

The slice is also a full integration pass: it touches tenancy, agent definition, turn execution, approvals, metering and record writing. Anything that will not compose shows up here.

## Explicitly out of scope

No UI. No mobile app. No web client. No channels. No industry packs. No fleet learning. No succession. Drive a CLI or the harness's own interface.

## The slice

1. **Provision a venture** - archetype `physical-product`, industry `food-and-beverage`, market `vietnam`, one Owner identity. The security controls in `docs/security-gate.md` are enabled at provisioning, not afterwards.
2. **A lane does one real task** - Finance reads a supplied document and drafts a payment instruction. Read-only tools, real content.
3. **An approval is raised** - the value exceeds the Owner's ceiling, so it escalates. The request carries content, reasoning, impact and cost.
4. **The Owner approves** - through an authenticated, attributed action. The approval is recorded.
5. **A council convenes** - three seats, **on at least two distinct model providers**, each answering independently with no sight of the others. Positions use the fixed schema.
6. **Divergence is measured** - structured comparison plus a graded judgement. If the seats diverge, a second round runs. If they converge, synthesis happens immediately.
7. **A record is written** - recommendation, each seat's position, dissent verbatim and attributed, falsifiers, and the context stamp.

## Acceptance criteria

| # | Criterion |
|---|---|
| A1 | The venture provisions unattended and passes the security checklist |
| A2 | No call reaches the harness without an authenticated actor and a resolved venture scope |
| A3 | Seats have no write capability bound. A write attempted from seat mode fails at the adapter |
| A4 | Round 1 positions are produced in parallel with no cross-visibility |
| A5 | At least two distinct model providers are used, and the mix is recorded |
| A6 | A divergent council runs round 2; a converged council does not |
| A7 | Dissent appears in the record verbatim and attributed, never summarised or averaged |
| A8 | Every model call, tool call and container-hour emits a billable event |
| A9 | Billable events reconcile against the provider invoice within 5% |
| A10 | No cross-boundary payload contains a free-text field |
| A11 | The record carries a complete context stamp |
| A12 | The whole slice runs from a single command against a clean environment |

## The S2 measurement

Acceptance is not the point. The measurement is.

- Run **the same ten decisions** through the council and through a single strong model given the same context and a good prompt.
- Have someone with real domain experience grade both, **blind to which produced which**.
- Grade on: did it surface a consideration the other missed; was the recommendation sound; would acting on it have been better.

Record the result honestly, including if it is null. A null result is the most valuable thing phase 0 can produce.
