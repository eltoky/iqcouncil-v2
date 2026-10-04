// The conformance suite.
//
// Every adapter passes this before it is allowed to serve a tenant.
// These assert BEHAVIOUR, not plumbing - an adapter that compiles against the
// contract but fails these is not safe to put in front of a customer.
//
// All nine fail until an adapter implements them. That is the intended
// starting state of this repo.

import { describe, it, expect } from 'vitest';
import type { HalAdapter } from '@iqcouncil/hal';

export function runConformance(makeAdapter: () => Promise<HalAdapter>): void {
  describe('HAL conformance', () => {

    it('1. a decision binds to one revision; a changed revision forces re-review', async () => {
      // Propose, review revision A, mutate to revision B, then decide on A.
      // MUST fail with code 'revision-changed'.
      expect.fail('not implemented');
    });

    it('2. a critical injection finding blocks application', async () => {
      // evaluate() returns a critical injection finding; decide('apply')
      // MUST fail with 'injection-finding-critical'.
      expect.fail('not implemented');
    });

    it('3. an interrupted apply leaves no partial state', async () => {
      // Kill the adapter mid-apply. The target is either fully at the old
      // revision or fully at the new one. Never between.
      expect.fail('not implemented');
    });

    it('4. a seat cannot be given a write capability', async () => {
      // define() an agent with mode 'seat' and a write capability in its
      // allowlist. MUST fail with 'write-from-seat'.
      // Then: a write attempted at runtime from seat mode also fails.
      expect.fail('not implemented');
    });

    it('5. compile then lift round-trips an authored edit without loss', async () => {
      // Compile a neutral source, edit an authored region natively,
      // lift it back. The proposal must contain exactly that edit.
      expect.fail('not implemented');
    });

    it('6. a patch touching a generated region is rejected and alerted', async () => {
      // Edit inside a generated region, then lift. MUST reject - it means
      // either a compiler defect or a skill rewriting its own plumbing.
      expect.fail('not implemented');
    });

    it('7. no tenant content appears in any cross-boundary payload', async () => {
      // Emit a fleet signal carrying an extra free-text field.
      // MUST fail with 'free-text-in-boundary-payload'.
      // Property test: no reachable code path produces a boundary payload
      // containing a string outside the closed vocabulary.
      expect.fail('not implemented');
    });

    it('8. no action reaches the harness without an authenticated actor and scope', async () => {
      // Submit a turn with a missing or unauthenticated actor.
      // MUST fail with 'unscoped-call' or 'actor-not-authenticated',
      // and the harness MUST NOT have been called.
      expect.fail('not implemented');
    });

    it('9. no operation resolves across two ventures in a single call', async () => {
      // Reference a second venture in any operation.
      // MUST fail with 'cross-venture-reference'.
      // Also: an identity-layer read returns no venture-derived data.
      expect.fail('not implemented');
    });

  });
}
