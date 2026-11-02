# Product policies

These policies reflect the project's agreed design snapshot. They describe intended behavior, not completed implementation. Changes require a deliberate decision and corresponding documentation updates.

1. The baseline migration target is an application initially deployed and managed by this platform.
2. Tenant is a logical ownership boundary. A tenant can have a separate K3s cluster per cloud; projects are isolated by Kubernetes Namespace within a cluster.
3. Each project owns its PostgreSQL instance and persistent volume. Cleaning one project must preserve other projects and shared cluster resources.
4. A sandbox uses test data and does not mutate the production database. Sandbox success alone does not prove a lossless production cutover.
5. During HTTP canary, the original PostgreSQL remains the only write source, including for the new application instance.
6. DB cutover controls new writes, waits for final synchronization and consistency checks, then promotes the target DB. A brief write pause may occur.
7. After new writes occur on the target DB, traffic-only rollback to the old environment is unsafe. Retain the old environment until stabilization and recovery conditions are met.

## Unresolved against the supplied proposal

The supplied proposal also covers external AWS scanning, on-premises targets, Route 53 weighted routing, and WireGuard. Existing design documents instead use platform-managed applications, AWS → GCP as the baseline, and Cloudflare HTTP traffic switching. This scaffold keeps integration boundaries without choosing those conflicting implementations. Temporal is adopted for this repository's orchestration scaffold; production deployment details are still open.

Source design snapshot: `product/policies.md`, `product/overview.md`, and `architecture/migration.md` in the SoftBank project reference set.
