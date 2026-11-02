# Provisioning testing strategy

## Goal and test seams

Test observable Provisioning behavior at a public boundary, not a private helper. Each ticket proposes a seam and records what it catches and misses; confirm the seam before writing tests. Prefer a real contract/adapter/rendered artifact, a Kubernetes or Agent boundary, or a Workflow result over mocked internal call counts. Expected results come from spec examples, known fixtures, or an independently reviewed plan, not the production algorithm restated in the test. Use small RED → GREEN cycles where meaningful, followed by review/refactoring. Test affected paths again when design changes.

## Default layers

| Layer | Seam and evidence | Limit |
| --- | --- | --- |
| Pydantic v2 contract | `ProvisioningSpec` validation of AWS/GCP/ON_PREM requirements, invalid CIDR, incompatible image architecture, and missing references | Does not prove cloud permissions |
| Provider adapter | Spec → provider configuration; inspect network, cluster/node, DB/Ingress decisions and unsupported-feature errors | Does not prove resource creation |
| `.env`/Secret binding | File reference metadata + target result → ConfigMap/Secret references; assert plaintext absent from Preview/Result/History | Does not prove actual Secret store access |
| Helm render | `helm lint` and `helm template`; inspect Deployment, PostgreSQL StatefulSet, Service, PVC, ports, image Digest and Secret references | Does not prove Pod Ready or PVC Bound |
| Terraform static | `terraform fmt -check`, `terraform init -backend=false`, `terraform validate`, variable/module checks per target | Does not prove live Provider acceptance |
| Temporal execution | Test environment with safe Activity substitutes: approval wait, stale Revision rejection, retry, cancellation, result, duplicate completion | Does not prove Terraform/Helm effects |
| Kubernetes integration | Isolated K3s/test cluster: DB/app deploy, Service DNS, Secret mapping, PVC Bound, DB connect, health, cleanup | Does not prove EKS/GKE IAM, CSI, or LB |
| Cloud Provider integration | Explicit isolated AWS/GCP plan/apply: actual EKS/GKE, Node, controller/CSI, Ingress/LB, PVC and health | Costs money and changes resources; separate opt-in run |

Use integration-style tests around the meaningful boundary and focused unit tests for pure validation edge cases. Avoid private-method tests, tautological expected values, snapshots of huge generated plans, and tests that only count mock calls. Replace external Activities in Temporal tests but keep the real Workflow boundary. For Terraform Plan, use a dedicated account/project and read-only or constrained credentials as appropriate; a plan can still read sensitive metadata and must be protected. The ordinary local test command must never invoke cloud Apply.

## Minimum behavior matrix

- **Input/adapter:** unsupported Provider, provider-specific required fields, `CREATE` versus existing network, CIDR overlap, AWS/GCP topology differences, ON_PREM Agent/K3s requirements, Node versus Pod resources, and P0 object-storage omission.
- **Environment/render:** separate `.env` reference, static versus dynamic DB bindings, user versus generated Secret, missing key, mismatched ConfigMap/Secret reference, image Digest and architecture, common Chart with distinct Sandbox/Production Values.
- **Preview/approval:** no target creation before approval; summary redacts secrets; Plan ID/Revision and artifact Digest match; stale, duplicate, rejected, or modified Plan cannot Apply; Signal receipt is not execution completion.
- **Execution/recovery:** Terraform partial Apply and Worker death trigger State/resource inspection; Helm response loss triggers Release inspection; Agent disconnect/reconnect preserves job ID and avoids duplicate submission; cancel does not imply destroy.
- **Verification/result:** Node Ready, PVC Bound and usable, DB connect, Pod Ready, Service/Ingress address, app health; any required failure prevents Provisioning `READY`; Result gives DB/endpoint references to Data/Traffic without claiming migration completion.

## Environment and safety

Local contract/render/Workflow tests use fixtures and temporary files with no real credentials. Kubernetes tests use a named isolated namespace and explicitly owned test resources; record teardown evidence. Never point tests at a user's production cluster or account by default. Actual AWS/GCP Apply tests require an explicit task and environment/cost authorization, scoped credentials, resource names/tags, a reviewed plan, and a cleanup decision. Do not destroy partially created or data-bearing resources automatically. Record which checks were not run and why. Secret plaintext, `.env`, Terraform Plan/State, and uploaded sources must not appear in test logs or committed fixtures.

## Commands and evidence

Use `make check` for Python lint/tests, `make smoke-temporal` for the existing local scaffold when available, `helm lint`/`helm template` for Chart tickets, and target-specific Terraform static checks when modules exist. For an implemented API route, regenerate and inspect `make openapi`. Ticket evidence includes commands, test environment, results, artifacts or masked excerpts, and any unverified target behavior. A failing required test blocks `DONE`; a test that could not run remains explicitly unverified rather than assumed successful. Current scaffold smoke tests do not establish real Provisioning support.
