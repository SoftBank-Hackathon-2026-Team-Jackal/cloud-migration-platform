# Project

Cloud Migration Platform moves an AWS EKS application to EKS, GKE, or K3s. FastAPI handles HTTP, Temporal coordinates long-running work, and Python workers run Activities. Terraform prepares cloud infrastructure; Helm deploys Kubernetes workloads. Current code is a scaffold, not real migration execution.

# Source of truth

1. Read `docs/product/overview.md` and `docs/product/policies.md` for product scope and policy.
2. Read `docs/architecture/overview.md`, `domain-boundaries.md`, and `temporal-flow.md` for responsibilities and execution flow.
3. For Provisioning, read `specs/provisioning/spec.md`, then `plan.md`, then `tasks.md` and the selected `tickets/provisioning/` ticket.
4. Read relevant `docs/decisions/` ADRs. `docs/contracts/openapi.yaml` covers external HTTP only; current paths are a scaffold.

# Before implementation

- Check ticket status, blockers, scope, acceptance criteria, and proposed public test seam. Draft tickets require user review before implementation.
- After review, create or reuse one GitHub Issue for the selected Provisioning ticket; record its `#number` in `specs/provisioning/tasks.md` before implementation. Never create duplicate Issues for the same ticket.
- If documents disagree or a product policy, API contract, or responsibility boundary must change, report the conflict and obtain a decision before affected implementation. Do not silently choose a policy.
- Keep other teammates' features and tickets outside the Provisioning workflow.

# Development rules

- Prefer one observable behavior at a time: failing test (RED), minimum implementation (GREEN), then separate review/refactoring. Agree on the test seam before writing tests.
- Keep Workflow deterministic. External I/O belongs in Activities. Make Terraform, Helm, and Agent execution retry-aware; never treat scaffold Activities as real cloud execution.
- Never commit credentials, `.env`, Terraform plan/state, uploaded source archives, or generated secrets.

# Documentation consistency and completion

- When requirements, design, or decisions change, update the corresponding SDD document and ticket before marking DONE or opening a PR. Record “no documentation change” when behavior and contracts truly did not change.
- A ticket is DONE only after its acceptance criteria, relevant tests, regression check, code/document alignment, and review pass. Record unmet checks as BLOCKED or pending decision.
- Run `make check` after Python changes. For HTTP route changes, regenerate and review `make openapi`; for Temporal changes, run `make smoke-temporal` with local infrastructure when available. Add ticket-specific Terraform/Helm/cluster checks where relevant.
- Commit only coherent changes. For Issue-backed work, include `#number` in commit messages and PR title/body; use `Closes #number` only when the PR completes its acceptance criteria. Push and create a PR only for a logically complete scope with tests and docs aligned, and only when the task authorizes remote actions.

# Detailed rules

- [Development workflow](docs/engineering/development-workflow.md)
- [Testing strategy](docs/engineering/testing-strategy.md)
- [Provisioning ticket index](specs/provisioning/tasks.md)
