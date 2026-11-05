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
| Terraform static | `terraform fmt -check`, `terraform init -backend=false`, `terraform validate`, variable/module checks per target. Every `.terraform.lock.hcl` records `linux_amd64`, `linux_arm64`, `darwin_arm64` and `darwin_amd64` hashes (`terraform providers lock -platform=...`), because Plan/Apply run `init -lockfile=readonly` in the Linux worker image; run the check inside that image (ticket 43) | Does not prove live Provider acceptance |
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

On this workstation, AWS checks use `cmp-aws-prod` and gcloud CLI checks use `cmp-gcp-prod` as directed by the user. Inspect the account/project identity before use. The gcloud configuration does not supply Terraform/SDK ADC by itself; verify ADC independently. A production-named profile is an authentication route, not permission to Apply or destroy. This v2 Planning contract task performs no cloud calls.

05/06의 Plan 전용 개발 검증에는 계정/프로젝트를 확인한 명시적 프로필과 접근을 제한한 임시 로컬 State를 사용할 수 있다. Apply 없이 실제 Provider Plan·민감정보 제거 Preview 생성을 확인하되, 이 산출물은 승인 불가로 표시한다. 실제 클라우드 승인 전에는 대상 계정/프로젝트, 중앙 S3 State·잠금, 새로 생성한 최종 저장 Plan·Preview를 확인한다. 로컬 검증 Plan을 Apply 산출물로 취급하지 않는다.

## Commands and evidence

Use `make check` for Python lint/tests, `make smoke-temporal` for the existing local scaffold when available, `helm lint`/`helm template` for Chart tickets, and target-specific Terraform static checks when modules exist. For an implemented API route, regenerate and inspect `make openapi`. Ticket evidence includes commands, test environment, results, artifacts or masked excerpts, and any unverified target behavior. A failing required test blocks `DONE`; a test that could not run remains explicitly unverified rather than assumed successful. Current scaffold smoke tests do not establish real Provisioning support.
