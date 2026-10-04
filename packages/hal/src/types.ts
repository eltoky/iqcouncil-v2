// Shared domain types. No harness concepts appear here - see R1.

declare const brand: unique symbol;
type Brand<T, B> = T & { readonly [brand]: B };

export type IdentityId = Brand<string, 'IdentityId'>;
export type AccountId = Brand<string, 'AccountId'>;
export type VentureId = Brand<string, 'VentureId'>;
export type SessionId = Brand<string, 'SessionId'>;
export type AgentId = Brand<string, 'AgentId'>;
export type SkillId = Brand<string, 'SkillId'>;
export type RevisionId = Brand<string, 'RevisionId'>;
export type RecordId = Brand<string, 'RecordId'>;
export type ApprovalId = Brand<string, 'ApprovalId'>;
export type CouncilId = Brand<string, 'CouncilId'>;

// --- Venture shape -----------------------------------------------------

export type Archetype =
  | 'digital-product'
  | 'physical-product'
  | 'services'
  | 'trading-distribution';

export type Industry =
  | 'manufacturing-industrial'
  | 'food-and-beverage'
  | 'software-saas'
  | 'it-technology-services';

/** A named geographic scope covering one or more territories. */
export type MarketId = Brand<string, 'MarketId'>;

/** Markets are typed by role: a venture may produce in one and sell into others. */
export type MarketRole = 'production' | 'sales';

export interface MarketBinding {
  readonly market: MarketId;
  readonly role: MarketRole;
  readonly effectiveFrom: string; // ISO date
  readonly effectiveTo?: string;  // absent = current
}

export interface VentureContext {
  readonly venture: VentureId;
  readonly archetype: Archetype;
  readonly industry: Industry;
  readonly markets: readonly MarketBinding[];
  readonly packVersions: Readonly<Record<string, string>>;
}

// --- Actors and authority ---------------------------------------------

export type Role = 'owner' | 'steward' | 'operator' | 'contributor' | 'observer';

/**
 * Every operation carries one. There is no unscoped call.
 * `human` is a person acting; `agent` is a lane or seat acting on their behalf.
 */
export interface Actor {
  readonly kind: 'human' | 'agent';
  readonly identity: IdentityId;
  readonly venture: VentureId;
  readonly role: Role;
  /** Domains this actor may touch. Empty means all domains for the role. */
  readonly domainScope: readonly string[];
}

/** Money is never a number. Ceilings are in currency, and we operate in VND, JPY, USD and EUR. */
export interface Money {
  readonly currency: 'VND' | 'JPY' | 'USD' | 'EUR' | 'THB' | 'KRW' | 'AED' | 'SGD' | 'CNY';
  /** Minor units, as an integer. VND has no minor unit; store whole dong. */
  readonly minorUnits: number;
}

// --- Agents ------------------------------------------------------------

export type Domain =
  | 'finance' | 'legal' | 'product' | 'design-research'
  | 'build' | 'marketing' | 'customer-sales' | 'procurement';

/**
 * The same agent runs in one of two modes.
 * A seat has NO write capability bound - enforced at the adapter, not by prompt.
 */
export type AgentMode = 'lane' | 'seat';

export interface AgentDefinition {
  readonly id: AgentId;
  readonly domain: Domain;
  readonly mode: AgentMode;
  readonly modelBinding: ModelBinding;
  /** Capability ids this agent may invoke. A seat's list contains no write capability. */
  readonly toolAllowlist: readonly string[];
  readonly contractRef: SkillId;
}

export interface ModelBinding {
  readonly provider: string;
  readonly model: string;
}

// --- Errors ------------------------------------------------------------

export type HalErrorCode =
  | 'unscoped-call'
  | 'actor-not-authenticated'
  | 'cross-venture-reference'
  | 'capability-absent'
  | 'write-from-seat'
  | 'approval-required'
  | 'ceiling-exceeded'
  | 'free-text-in-boundary-payload'
  | 'revision-changed'
  | 'injection-finding-critical';

export class HalError extends Error {
  constructor(
    readonly code: HalErrorCode,
    message: string,
    readonly detail?: Readonly<Record<string, string | number>>,
  ) {
    super(message);
    this.name = 'HalError';
  }
}
