# Architecture overview

```text
External frontend → FastAPI → Temporal Server → Workflow Worker
                                     ├─ analysis Task Queue → Analysis Activity
                                     ├─ build Task Queue → Build Activity
                                     ├─ provisioning Task Queue → Plan / Provision Activities
                                     └─ migration-activities Task Queue → Data / Traffic / Verify Activities
FastAPI → PostgreSQL (future business records)
Temporal Server → separate PostgreSQL persistence
```

`apps/api` owns HTTP, Signals, Queries, and SSE. `apps/worker/workflows` owns ordering, approval wait, retry policy, and state. `apps/worker/activities` owns external effects; all current Activities are inert examples. `packages/contracts` holds data exchanged across boundaries. `packages/providers` will translate approved target architecture into provider-specific Terraform inputs. `infra/terraform` and `infra/helm` hold reusable deployment templates once requirements are approved.

The long-term logical components include Platform API, Application Analysis & Build, AI Architecture Agent, Sandbox Manager, Execution Engine, Infrastructure Management, Monitoring & State Store, and External Integration. These are responsibilities, not one service each. The central management K3s cluster and tenant workload K3s clusters are independent. See `domain-boundaries.md` and `temporal-flow.md`.
