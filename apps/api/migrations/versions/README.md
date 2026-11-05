# Business DB revisions

Create a revision after defining an approved SQLAlchemy model: `uv run alembic revision --autogenerate -m "description"`. Review the generated migration before applying it with `uv run alembic upgrade head`. Revision `20261103_01` creates minimal Agent owner, enrollment, credential digest and work-reference tables; it does not create user accounts or approve Production work.
Revision `20261105_04` (ticket 18) adds `provisioning_planning_inputs` (per-Migration Planning input registration) and `provisioning_plan_publications` (input Digest per published Plan Revision).
Revision `20261105_06` (ticket 24) adds the append-only `migration_events` (per-Migration `seq`, idempotency key) and `migration_logs`. It chains onto `20261105_04`; ticket 23's `20261105_05` is re-chained at merge.
