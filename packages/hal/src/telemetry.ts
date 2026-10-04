// The closed vocabulary. See R8.
//
// Cross-boundary payloads may contain ONLY values drawn from these enums,
// plus identifiers and counts. No free text. No summaries. No runtime-authored
// strings. Leakage is structurally impossible rather than filter-dependent.
//
// Extending the dictionary is a deliberate act informed by the `unclassified`
// counter. The tenant runtime never invents an entry at run time.

export const OUTCOME_CLASS = [
  'completed',
  'completed-after-correction',
  'abandoned-mid-run',
  'failed-tool-error',
  'failed-timeout',
  'failed-budget',
  'blocked-approval-denied',
  'unclassified',
] as const;
export type OutcomeClass = (typeof OUTCOME_CLASS)[number];

export const CORRECTION_CLASS = [
  'scope-narrowed',
  'scope-widened',
  'figure-corrected',
  'tone-adjusted',
  'wrong-recipient',
  'wrong-source',
  'step-reordered',
  'step-skipped',
  'unclassified',
] as const;
export type CorrectionClass = (typeof CORRECTION_CLASS)[number];

/**
 * The ONLY shape permitted across the tenant boundary.
 * Note the absence of any free-form string field. That absence is the design.
 */
export interface FleetSignal {
  readonly skillId: string;
  readonly skillRevision: string;
  readonly stepId: string;
  readonly outcome: OutcomeClass;
  readonly correction?: CorrectionClass;
  readonly count: number;
}

/** Billable events are venture-scoped and never cross into the fleet store. */
export interface BillableEvent {
  readonly venture: string;
  readonly kind: 'model-call' | 'tool-call' | 'container-hour' | 'monitor-check';
  readonly provider?: string;
  readonly model?: string;
  readonly inputUnits?: number;
  readonly outputUnits?: number;
  readonly credits: number;
  readonly at: string; // ISO timestamp
}

/** Fails closed: anything not in the vocabulary is rejected, not coerced. */
export function isValidFleetSignal(value: unknown): value is FleetSignal {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  const stringFields = ['skillId', 'skillRevision', 'stepId'];
  const allowed = new Set([...stringFields, 'outcome', 'correction', 'count']);
  for (const key of Object.keys(v)) if (!allowed.has(key)) return false;
  for (const f of stringFields) if (typeof v[f] !== 'string') return false;
  if (!OUTCOME_CLASS.includes(v.outcome as OutcomeClass)) return false;
  if (v.correction !== undefined && !CORRECTION_CLASS.includes(v.correction as CorrectionClass)) return false;
  return typeof v.count === 'number';
}
