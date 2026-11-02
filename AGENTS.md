# Repository working rules

1. Read `docs/product/policies.md` before changing product behavior. It is the policy source of truth for this repository.
2. Read `docs/architecture/overview.md` and `docs/architecture/domain-boundaries.md` before moving responsibilities between modules.
3. For a feature, create or update `specs/<feature>/spec.md`, `plan.md`, and `tasks.md`; keep acceptance criteria and completion evidence current.
4. Add HTTP contracts to `docs/contracts/openapi.yaml` only when an endpoint is implemented or agreed. Keep the FastAPI implementation and contract aligned.
5. Record material technical decisions in `docs/decisions/` with status, context, decision, consequences, and alternatives.
6. Keep Workflow code deterministic. External I/O belongs in Activities. Never label scaffold Activities as real cloud execution.
7. Run `make check` after Python changes. After HTTP route changes, run `make openapi` and check in the result. For Temporal changes, run `make smoke-temporal` with local infrastructure when available.
8. Never commit credentials, `.env`, Terraform state/plan files, uploaded source archives, or generated secrets.
