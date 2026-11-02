# Provisioning development workflow

## Scope and documents

This workflow governs Codex work on Provisioning tickets only. Other teammates may use their own ticket process. Before selecting work, read [product policies](../product/policies.md), [architecture boundaries](../architecture/domain-boundaries.md), the [Provisioning spec](../../specs/provisioning/spec.md), [implementation plan](../../specs/provisioning/plan.md), [ticket index](../../specs/provisioning/tasks.md), the selected ticket, and relevant ADRs. The product policy sets the scope; the spec says what must work; the plan proposes how; the ticket defines the current slice. The [OpenAPI document](../contracts/openapi.yaml) describes only agreed/implemented external HTTP interfaces, not Worker contracts. The current API/Temporal code remains a scaffold.

## Current working preference (2026-11-03)

Work sequentially, one implementation ticket at a time. Include related policy/design/ticket documentation in the implementation Issue and PR; do not create documentation-only Issues/PRs by default. Parallel-work preparation is not an independent delivery goal. Existing historical parallel guidance is inactive unless the user explicitly requests parallel work again.

## Ticket selection and start

1. Pick a `TODO` ticket whose `Blocked by` tickets are `DONE`. The 7th-stage drafts remain `DRAFT` until user review. Do not start implementing them merely because files exist.
2. Check its linked `PROV` IDs, expected observable outcome, excluded scope, target environment, and proposed test seam. Confirm the seam with the user before tests; describe what that seam catches and what it misses. Record the chosen seam in the ticket.
3. Create or reuse a GitHub Issue for this reviewed ticket **before implementation**. Search by ticket ID/title first to avoid duplicates; include the ticket link, `PROV` IDs, acceptance criteria, blockers, and test seam in the Issue. Record the Issue `#number` in `tasks.md`. If the ticket is revised, update the existing Issue rather than creating another. This Issue process applies to Provisioning work, not other teammates' areas.
4. Compare the ticket with current docs and code. If an assumption is undecided, isolate unaffected work and mark the dependent work `BLOCKED` with the needed decision. Do not use a fixture or scaffold behavior as proof of production support.
5. Keep the branch and change set focused on the selected Issue and logical slice. Existing unrelated working-tree changes remain outside its commit.

## Development cycle

For behavior that can be exercised in a local or isolated environment, use one RED → GREEN cycle at a time: choose an externally observable behavior at the agreed seam, write a failing test, verify it fails for the intended reason, implement the minimum behavior, and verify the test passes. Repeat for the next behavior. Review and refactor after a passing slice, then run relevant regression checks. Avoid writing a complete test suite before implementation or asserting internal mock call counts. Infrastructure work that cannot be made into a meaningful RED/GREEN loop still needs static checks, a reviewable plan, isolated integration evidence where possible, and explicit unverified items.

## Design changes and decisions

**A ticket is not complete while a changed requirement, design, or decision remains absent from its SDD source.** When implementation reveals a difference, map the affected `PROV` criteria and downstream tickets. Update `docs/product/` for product scope/policy, `docs/architecture/` for system/Worker/Temporal boundaries, `spec.md` for behavior, `plan.md` for implementation design, `docs/contracts/openapi.yaml` for agreed or implemented external HTTP interfaces, the relevant ADR for a material technical choice, and `tasks.md` plus tickets for scope/dependency changes. Do not regenerate all tickets: edit affected ones, add a new one if necessary, preserve IDs and history, and mark obsolete work explicitly.

An implementation detail within an agreed boundary can be documented and changed locally. A change to product policy, public API, responsibility ownership, or a conflicting ADR needs a user/owner decision before dependent implementation; record it as `BLOCKED` or `PENDING_DECISION`. For a pure refactor with no contract effect, record “no documentation change” and why. Review existing tests affected by any contract change. The existing ADR-001 covers a Temporal learning scaffold, while a full FastAPI + Temporal adoption ADR has not yet been assigned; do not silently rewrite it.

## Verification and completion

For each ticket, record commands, environment, pass/fail evidence, unmet criteria, and any real resources created. Run `make check` after Python changes. After HTTP route changes, run `make openapi` and review the generated contract against FastAPI and agreed behavior; preserve or reconcile the SDD extension section. For Temporal changes, use `make smoke-temporal` when local infrastructure is available. Run Helm lint/template, Terraform fmt/validate, isolated Kubernetes or Provider checks according to the [testing strategy](testing-strategy.md). A static check is not evidence of a successful EKS/GKE deployment. Do not mark an unexecuted cloud test as passed.

Before `DONE`, check: ticket acceptance criteria; relevant tests; existing behavior regression; requirements/design changes and SDD updates; implementation/contract alignment; open conflicts; review and final verification. Update ticket evidence and index status together. A decision still pending, failed test, or unverifiable required behavior keeps the ticket out of `DONE`.

## Git and PR

Commit coherent changes on the task branch; small TDD commits are allowed. Keep unrelated files and secrets out of commits. For work with a GitHub Issue, put its number in **every related commit message** (for example, `feat: validate ProvisioningSpec (#12)`) and in the PR title/body. Search for an existing PR before opening another for the same Issue. A PR is for a **logically complete, reviewable** slice with relevant tests and documents aligned. Do not open one with unmet acceptance criteria, failed required checks, an unrecorded design change, or unresolved blocking dependency. A PR description should name the Issue and ticket, `PROV` IDs, behavior delivered, commands/results, changed SDD documents, remaining unverified items, and material limitations. Use `Closes #12` only when merging that PR should close a fully satisfied Issue; otherwise use `Refs #12`. Push/PR actions require a task that authorizes external GitHub changes. If the user explicitly exempts a documentation-only change from Issue creation, record that exception in its PR and do not invent an Issue number for the commit. This document itself does not impose an automatic GitHub gate; future CI, PR templates, and branch protection would be separate work.

## Sources used for the workflow

The local ticket approach adapts the [to-tickets guide](https://github.com/mattpocock/skills/blob/main/docs/engineering/to-tickets.md) to reviewable vertical slices with explicit blockers. The test loop and seam guidance adapt the [TDD guide](https://github.com/mattpocock/skills/blob/main/skills/engineering/tdd/SKILL.md) and [test examples](https://github.com/mattpocock/skills/blob/main/skills/engineering/tdd/tests.md). These references do not override the product SDD or a user's explicit instructions.
