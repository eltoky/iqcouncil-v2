# Security gate - beta preconditions

Every control that ships disabled by default upstream is enabled here. **None of this is backlog.** No tenant reaches beta until all items pass, verified on a freshly provisioned tenant from the production pipeline - not a hand-configured instance.

| # | Control | Requirement |
|---|---|---|
| 1 | Code sandbox | Enabled in the tenant image, enforced by configuration, not user-toggleable |
| 2 | Durable secrets | KMS-backed store; short-lived credentials injected at boot; nothing durable in the harness secret store |
| 3 | Workspace restriction | Filesystem access anchored to the venture workspace; no path escape |
| 4 | Session permission mode | Strictest available by default; loosening is per-session and audited |
| 5 | Public session links | Disabled at image level |
| 6 | Gateway auth | Shared-secret or identity-bearing auth mandatory; `none` prohibited on any ingress |
| 7 | Device pairing | Required for every non-loopback client |
| 8 | Plugin and MCP installation | Allowlisted catalogue per tier; no open installation |
| 9 | Skill provenance | Tracked, audited sources only; capability consent recorded |
| 10 | Network egress | Per-container allowlist tied to authorised connectors |
| 11 | Write actions | All external writes gated by approval |
| 12 | Transport | TLS everywhere; no plaintext inter-component traffic |
| 13 | Audit | Every approval, tool call and credential use recorded and exportable per venture |
| 14 | Deletion | Verified hard-delete path including transcripts and memory |
| 15 | Tenant data boundary | Cross-boundary payloads validated against the closed vocabulary; free-text fields rejected at the adapter |
| 16 | Succession scope | Import only between ventures under one account; one-time transfer; connectors re-authorised, never token-copied |
| 17 | Member access path | No member receives direct gateway, CLI or shell access |
| 18 | Actor attribution | Every action carries an authenticated actor; unscoped requests rejected |
| 19 | Identity isolation | Identity layer holds stated preferences only; promotion of a learned preference requires explicit confirmation |
| 20 | Owner MFA | Enforced for any identity holding Owner authority anywhere; not user-disableable |
| 21 | Operator console isolation | Console exposes profile metadata and operational state, never produced content |
| 22 | Operator console hardening | Separate auth domain, mandatory MFA, no standing production access, two-person approval for destructive operations |

## Observability warning

OpenTelemetry spans carry prompts and payloads as attributes by default. That places venture content in the observability backend and voids control 15.

**A redaction processor with an attribute allowlist is required before the first trace is emitted.** Not retrofitted.
