// The HAL capability contract, v0.1.
//
// This is the single seam between the product and any harness. Everything
// above L1 binds to these interfaces and to nothing else (R1).
//
// An adapter implements this. The conformance suite proves it did so correctly
// before it is allowed to serve a tenant.

import type {
  Actor, AgentDefinition, AgentId, ApprovalId, CouncilId, HalError, Money,
  RecordId, RevisionId, SessionId, SkillId, VentureContext, VentureId,
} from './types.js';
import type { AdapterIdentity, CapabilityDomain, CapabilityReport } from './capabilities.js';
import type { BillableEvent, FleetSignal } from './telemetry.js';

/** Every call carries this. There is no unscoped operation. */
export interface Scope {
  readonly actor: Actor;
  readonly venture: VentureId;
}

// --- 1. Session --------------------------------------------------------

export interface SessionCapability {
  create(scope: Scope, opts: { agent: AgentId; title?: string }): Promise<SessionId>;
  resume(scope: Scope, session: SessionId): Promise<void>;
  fork(scope: Scope, session: SessionId): Promise<SessionId>;
  close(scope: Scope, session: SessionId): Promise<void>;
}

// --- 2. Turn -----------------------------------------------------------

export interface TurnResult {
  readonly text: string;
  readonly provider: string;
  readonly model: string;
  readonly events: readonly BillableEvent[];
}

export interface TurnCapability {
  submit(scope: Scope, session: SessionId, input: string): Promise<TurnResult>;
  stream(scope: Scope, session: SessionId, input: string): AsyncIterable<string>;
  interrupt(scope: Scope, session: SessionId): Promise<void>;
  cancel(scope: Scope, session: SessionId): Promise<void>;
}

// --- 3. Agent ----------------------------------------------------------

export interface AgentCapability {
  define(scope: Scope, def: AgentDefinition): Promise<AgentId>;
  /**
   * MUST reject a definition whose mode is 'seat' and whose toolAllowlist
   * contains any write capability. Assertion 4 covers this.
   */
  list(scope: Scope): Promise<readonly AgentDefinition[]>;
}

// --- 5. Skill ----------------------------------------------------------

export interface CompiledSkill {
  readonly skill: SkillId;
  readonly revision: RevisionId;
  /** Regions the compiler generated. A patch touching one is rejected. */
  readonly generatedRegions: readonly { start: number; end: number }[];
}

export interface SkillProposal {
  readonly skill: SkillId;
  readonly revision: RevisionId;
  readonly origin: 'learned' | 'past-work-scan' | 'human' | 'fleet';
  readonly diff: string;
}

export interface EvaluationResult {
  readonly findings: readonly {
    severity: 'critical' | 'high' | 'medium' | 'low';
    kind: 'injection' | 'grader' | 'benchmark' | 'scanner';
    detail: string;
  }[];
}

export interface SkillCapability {
  compile(scope: Scope, source: string, target: string): Promise<CompiledSkill>;
  /** The inverse of compile. Rejects patches touching a generated region. */
  lift(scope: Scope, skill: SkillId, nativeDiff: string): Promise<SkillProposal>;
  install(scope: Scope, compiled: CompiledSkill): Promise<void>;
  propose(scope: Scope, proposal: SkillProposal): Promise<RevisionId>;
  evaluate(scope: Scope, revision: RevisionId): Promise<EvaluationResult>;
  /**
   * A decision binds to ONE revision. If the revision changed since review,
   * this MUST fail with 'revision-changed'. Assertion 1.
   * A critical injection finding MUST block apply. Assertion 2.
   */
  decide(
    scope: Scope,
    revision: RevisionId,
    decision: 'apply' | 'reject' | 'quarantine',
    reason: string,
  ): Promise<void>;
  history(scope: Scope, skill: SkillId): Promise<readonly SkillProposal[]>;
}

// --- 7. Approval -------------------------------------------------------

export interface ApprovalRequest {
  readonly id: ApprovalId;
  readonly summary: string;
  readonly value?: Money;
  readonly reasoning: string;
  readonly impact: string;
  readonly creditCost: number;
  readonly requestedBy: AgentId;
}

export interface ApprovalCapability {
  request(scope: Scope, req: Omit<ApprovalRequest, 'id'>): Promise<ApprovalId>;
  /** MUST reject when the actor's ceiling is below the value. */
  decide(scope: Scope, id: ApprovalId, decision: 'approve' | 'reject', note?: string): Promise<void>;
  pending(scope: Scope): Promise<readonly ApprovalRequest[]>;
}

// --- 11. Telemetry -----------------------------------------------------

export interface TelemetryCapability {
  /** Billable events stay venture-scoped. */
  billable(scope: Scope): AsyncIterable<BillableEvent>;
  /**
   * The ONLY path across the tenant boundary. Implementations MUST validate
   * against the closed vocabulary and reject anything else. Assertion 7.
   */
  emitFleetSignal(scope: Scope, signal: FleetSignal): Promise<void>;
}

// --- 12. Tenancy -------------------------------------------------------

export interface TenancyCapability {
  context(scope: Scope): Promise<VentureContext>;
  /** MUST reject any operation naming two ventures. Assertion 9. */
  assertSingleVenture(scope: Scope, referenced: readonly VentureId[]): void;
}

// --- Council (L3 consumes HAL; declared here so the contract is complete) --

export interface SeatPosition {
  readonly agent: AgentId;
  readonly recommendation: string;
  readonly confidence: 'low' | 'medium' | 'high';
  readonly reasoning: readonly string[];
  readonly wouldChangeMyMind: readonly string[];
  readonly risksOwned: readonly string[];
  readonly doNotKnow: readonly string[];
  readonly provider: string;
}

export interface DecisionRecord {
  readonly id: RecordId;
  readonly council: CouncilId;
  readonly question: string;
  readonly context: VentureContext;
  readonly recommendation: string;
  readonly positions: readonly SeatPosition[];
  /** Verbatim and attributed. Never summarised, never averaged. Assertion 5. */
  readonly dissent: readonly SeatPosition[];
  readonly falsifiers: readonly { condition: string; owner: AgentId }[];
  readonly unverifiedAssumptions: readonly string[];
  readonly reviewDate: string;
}

// --- The adapter -------------------------------------------------------

export interface HalAdapter {
  readonly identity: AdapterIdentity;
  capabilities(): Promise<readonly CapabilityReport[]>;
  supports(domain: CapabilityDomain): Promise<boolean>;

  readonly session: SessionCapability;
  readonly turn: TurnCapability;
  readonly agent: AgentCapability;
  readonly skill: SkillCapability;
  readonly approval: ApprovalCapability;
  readonly telemetry: TelemetryCapability;
  readonly tenancy: TenancyCapability;
}

export type { HalError };
