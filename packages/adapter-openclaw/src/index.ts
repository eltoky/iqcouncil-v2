// The OpenClaw adapter. The ONLY place in this repo where harness specifics
// may appear (R1). Nothing here is exported upward except the HalAdapter.
//
// Phase 0 target: pass all nine conformance assertions.
//
// NOTE - D13 is open. OpenClaw Enterprise may become the integration target
// instead of the harness protocol directly, which would split this adapter
// along a control-plane / data-plane seam. Keep provisioning logic shallow
// so that change is cheap.

import type { HalAdapter } from '@iqcouncil/hal';

export function createOpenClawAdapter(): HalAdapter {
  throw new Error('not implemented - phase 0');
}
