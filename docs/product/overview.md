# Product overview

The intended platform manages deployment, updates, and migration of stateful applications across clouds. The agreed baseline is an application first deployed and managed by this platform, with an AWS-to-GCP PostgreSQL migration demonstration. External arbitrary AWS application discovery and on-premises migration appear in a later proposal but are not confirmed as the baseline here.

The desired migration flow is analysis → architecture planning and build → sandbox verification → approved target provisioning → data replication → HTTP canary → DB cutover → stabilization. This repository currently implements only a **local scaffold** of the orchestration sequence. It does not deploy, copy data, or shift traffic.

The frontend is a separate repository and is excluded here. See `docs/product/policies.md` for current policy and `docs/architecture/overview.md` for the technical boundaries.
