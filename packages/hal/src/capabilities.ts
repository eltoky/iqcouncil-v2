// Capability flags. An absent capability reports itself absent so callers
// degrade visibly. A silent no-op is a bug - see CLAUDE.md.

export const CAPABILITY_DOMAINS = [
  'session',       // 1  create, resume, fork, close, ownership
  'turn',          // 2  submit, stream, interrupt, steer, cancel
  'agent',         // 3  persona, contract, model binding, tool allowlist
  'tool',          // 4  declare, authorise, invoke, capture
  'skill',         // 5  compile, lift, install, propose, evaluate, decide
  'memory',        // 6  write, search, provenance, export, hard delete
  'approval',      // 7  request, present, decide, audit
  'workspace',     // 8  read, write, list, sync
  'schedule',      // 9  create, list, pause, delete
  'channel',       // 10 inbound admission, outbound delivery
  'telemetry',     // 11 billable event stream
  'tenancy',       // 12 scope to one venture and one authenticated actor
] as const;

export type CapabilityDomain = (typeof CAPABILITY_DOMAINS)[number];

/** Phase 0 scope - D6: everything beta needs, flagged stubs for the rest. */
export const PHASE_0_REQUIRED: readonly CapabilityDomain[] = [
  'session', 'turn', 'agent', 'approval', 'telemetry', 'tenancy',
];

export const PHASE_0_PARTIAL: readonly CapabilityDomain[] = [
  'tool', 'skill', 'workspace',
];

export const PHASE_0_STUBBED: readonly CapabilityDomain[] = [
  'memory', 'schedule', 'channel',
];

export interface CapabilityReport {
  readonly domain: CapabilityDomain;
  readonly available: boolean;
  /** Present when partially implemented; names the operations that work. */
  readonly operations?: readonly string[];
  /** Why absent. Shown in the operator console, never to a founder. */
  readonly absentReason?: string;
}

export interface AdapterIdentity {
  readonly adapterName: string;
  readonly contractVersion: string;
  readonly harnessVersion: string;
}
