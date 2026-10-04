// L2 - tenancy, metering, policy, billing.
//
// Binds to the HAL contract and to nothing harness-specific (R1).
// If you find yourself importing from packages/adapter-*, stop.
//
// Policy that stays ours regardless of harness or control plane:
//   - approval ceilings, expressed in currency
//   - separation of duties
//   - the restricted-domain flag
// A platform IAM authorises actions on resources. It does not express
// "this member may approve up to 100,000,000 VND".

export const PLACEHOLDER = true;
