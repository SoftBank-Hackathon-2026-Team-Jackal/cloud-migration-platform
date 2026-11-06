<div align="center">

<p align="center"><b>English</b> · <a href="README.ja.md">日本語</a> · <a href="README.ko.md">한국어</a></p>

# Cloud Migration Platform

### Move a running EKS app and its database — without stopping it, at the same security level

To on-premises (K3s) or another cloud — AI analyzes it and tries it in a sandbox first, and only a human-approved configuration is deployed.

![Python](https://img.shields.io/badge/Python_3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Temporal](https://img.shields.io/badge/Temporal-000000?logo=temporal&logoColor=white)
![AWS](https://img.shields.io/badge/AWS-232F3E?logo=amazonwebservices&logoColor=white)
![Google Cloud](https://img.shields.io/badge/Google_Cloud-4285F4?logo=googlecloud&logoColor=white)
![Kubernetes](https://img.shields.io/badge/K3s·EKS·GKE-326CE5?logo=kubernetes&logoColor=white)
![WireGuard](https://img.shields.io/badge/WireGuard-88171A?logo=wireguard&logoColor=white)
![Claude](https://img.shields.io/badge/Claude-D97757?logo=anthropic&logoColor=white)

</div>

<!-- TODO: web E2E recording (GIF, or an mp4 dropped into the GitHub editor) -->
<p align="center"><i>Demo recording goes here — one migration in the web UI, from analysis to traffic shift</i></p>

---

## At a glance

| The challenge's question | Our answer |
| --- | --- |
| Can a running app move to another environment **without stopping**? | The database catches up through logical replication, then writes pause only for milliseconds at cutover. Traffic moves in steps (10→50→100%) and rolls back automatically on trouble |
| Can security and governance stay **at the same level everywhere**? | We extract "who connects to whom" from the source security groups and manifests, and rebuild it in the target as NetworkPolicy: deny by default, open only the paths the app used |
| On-premises, how do you reproduce **what the cloud gave you for free**? | An Agent in the user's cluster deploys while only connecting outward (polling + WireGuard), so no inbound port is opened. Load balancers and managed DBs become standard building blocks |
| How far can **AI be trusted** to inspect and fix? | AI tests and fixes only inside a sandbox with outbound traffic blocked. Production gets only the Revision a human approved, and the approved image and config digests are re-checked at run time |

## Why we built it

An app that ran well in the cloud **quietly gets weaker** the moment it moves. Security groups disappear, managed databases and load balancers are gone, and nobody remembers which ports had to be open. So migrations are usually matched by hand, piece by piece, and the service is often stopped while it moves.

We turned this into one flow: **analyze → verify → approve → execute**. People only make decisions; everything that can be checked, the platform checks.

---

## How it flows

<p align="center">
  <img src="docs/readme/temporal-flow.en.svg" alt="One migration in 10 steps, from analysis to done" width="100%">
</p>

One migration is one [Temporal](https://temporal.io) workflow, `MigrationWorkflow`. Whether it takes days, the server restarts, or it waits for a human approval, it never loses its state and continues from where it stopped.

| Building block | In this platform |
| --- | --- |
| **Workflow** — order and waiting | `MigrationWorkflow` calls child workflows in turn (analysis · sandbox · approval · data · traffic). After a restart it replays its history and continues |
| **Activity** — the real outside work | clone, AWS reads, buildx, kubectl·Helm, DB writes. Failures are retried automatically, so each one is safe to run twice |
| **Worker** — polls a queue and runs code | `worker` on the platform VM serves six queues; `relay-worker` on the relay server serves `relay-1` |
| **Signal · Query** — the human link | approval `approve`, DB cutover `switchover`, rollback·retry / state `status` |

Every step emits progress events (`analysis.*` `sandbox.*` `plan.*` `provision.*` `data.*` `traffic.*` `cutover.*`), and the web UI receives them live from `GET /migrations/{id}/events` (SSE).

---

## Feature flows

<details open>
<summary><b>① Connect and analyze</b> — find out what has to move</summary>

```mermaid
flowchart TB
  U[User] -->|repo·commit / .env / AWS account| W[Web]
  W --> API[API]
  API -->|.env stored encrypted, only a reference moves on| ENV[(env store)]
  API --> AN[AnalysisWorkflow]
  AN --> F[fetch_source<br/>clone at a pinned commit]
  AN --> C[scan_code<br/>DB driver · env vars · routes]
  AN --> M[scan_manifests<br/>Deployment · Service · NetworkPolicy]
  AN --> I[scan_aws_infra<br/>VPC · security groups · LB · IAM · CloudTrail]
  F & C & M & I --> R[intermediate spec]
  R --> AI[Claude mapping<br/>fit to standard resources]
```

- The AWS account is connected through a **read-only role** created with CloudFormation; the scope is chosen per EKS namespace and workload.
- `.env` values are encrypted once; analysis reads **key names only**.
- The result is an **intermediate spec**: what the app uses (DB, config, exposure, connections) and how each maps to our standard resources (as-is / must change).

</details>

<details>
<summary><b>② Build the image</b> — pin the same source to the same image</summary>

```mermaid
flowchart LR
  SRC[pinned commit] -->|upload bundle| S3[(S3: source · logs)]
  S3 --> BX[Docker on the platform VM<br/>buildx · amd64+arm64]
  BX -->|push| ECR[(ECR)]
  ECR -->|sha256 digest| NEXT[later steps refer to the image only by digest]
```

- During the build, the instance metadata service (credentials) is blocked, so user code cannot steal platform permissions.
- Sandbox and production use the **same digest**, so the image that was tested is the image that ships.

</details>

<details>
<summary><b>③ Sandbox verification</b> — AI runs it first and fixes it</summary>

```mermaid
flowchart LR
  REQ[intermediate spec + digest] --> RD[shared renderer]
  RD --> K3D[k3d sandbox<br/>temporary namespace · no outbound traffic · cleaned after 2h]
  K3D --> T[tests<br/>health · DB write→read · survives restart<br/>+ AI-generated tests]
  T -->|pass| OK[PASSED]
  T -->|fail| FX[AI Fixer] -->|up to 3 times| RD
```

- Sandbox and production use the **same renderer** (`shared_deployment_v2`).
- AI changes things freely only in this isolated space, and every fix must pass the tests again before moving on.

</details>

<details>
<summary><b>④ Plan and approval</b> — a human approves a plan fitted to the target</summary>

```mermaid
sequenceDiagram
  participant WF as MigrationWorkflow
  participant P as Plan publish
  participant A as Approver (web)
  WF->>P: intermediate spec + target profile (on-prem / AWS / GCP)
  P->>P: resolve into Namespace · NetworkPolicy · DB · App
  P-->>A: preview (manifest digest)
  A->>WF: approve (a specific Revision)
  WF->>WF: re-check the approved digest at run time
```

- **NetworkPolicy** built from connection intent: deny everything by default, open only the paths the source used, such as app → DB.
- An approval is valid for one Revision only; if the digest differs at run time (`ARTIFACT_DIGEST_MISMATCH`), nothing is deployed.

</details>

<details>
<summary><b>⑤ On-premises provisioning</b> — deploy into the user's cluster without opening a port</summary>

```mermaid
sequenceDiagram
  participant AG as Agent (user's K3s)
  participant API as Platform API
  participant RL as Relay server
  AG->>API: enroll with a one-time token → own credential
  AG->>API: register WireGuard public key → tunnel IP
  RL->>API: sync peer list
  AG-->>RL: outbound handshake (no inbound port on the user side)
  AG->>API: poll — receive the approved job
  AG->>AG: Namespace → NetworkPolicy → DB → App
  AG->>API: report progress → pod · DB · app · through-tunnel health checks
```

- The user installs the Agent Helm chart **once**. On failure it restores the previous Revision.

</details>

<details>
<summary><b>⑥ Data replication and DB cutover</b> — writes barely stop</summary>

```mermaid
flowchart LR
  SRC[(source DB)] -->|logical replication · WireGuard data tunnel| TGT[(target DB)]
  TGT --> ST[catch up on changes]
  ST --> AP{DB cutover approval}
  AP --> PB[hold writes briefly in PgBouncer<br/>sync sequences · verify row counts and checksums]
  PB --> SW[target becomes the writer]
```

- Cross-cloud check (EKS→GKE): writes paused for **83 ms**, row counts and checksums matched, zero app write errors.

</details>

<details>
<summary><b>⑦ Traffic shift</b> — move in steps, roll back on trouble</summary>

```mermaid
flowchart LR
  USER[user requests] --> EV[Envoy relay server]
  EV --> O[source]
  EV -->|10% → 50% → 100%| T[target · through the tunnel]
  EV --> G{watch each step<br/>requests · failures · latency}
  G -->|pass| NEXT[next step]
  G -->|fail| RB[automatic rollback → source 100%]
```

</details>

---

## Architecture

```mermaid
flowchart TB
  subgraph WEB[Web]
    UI[cloud-migration-web]
  end
  subgraph CP[Platform VM · EC2]
    EDGE[Caddy] --> API[FastAPI]
    API --> PDB[(PostgreSQL)]
    API --> TS[Temporal]
    WK[Worker] --> TS
    WK --> DK[Docker buildx]
    WK --> SB[k3d sandbox]
  end
  subgraph SRC[Source · AWS]
    EKS[EKS app · DB]
    ECR[(ECR)]
    S3[(S3)]
  end
  subgraph RELAY[Relay server · EC2]
    ENV[Envoy] --- RW[relay-worker]
    WG[WireGuard]
  end
  subgraph DST[Target]
    AG[On-prem Agent] --> K3S[K3s app · DB]
    GKE[GKE · AWS]
  end
  UI --> EDGE
  WK -->|read-only role| EKS
  DK --> ECR
  WK --> S3
  RW --> TS
  AG -->|polling| EDGE
  AG <-->|tunnel| WG
  ENV --> EKS
  ENV -->|tunnel| K3S
```

## Cloud services we use

| Cloud | Services | Used for |
| --- | --- | --- |
| AWS | EC2 · SSM · CloudFormation | running the platform and relay, SSH-less deploys, connection-role templates |
| AWS | EKS · IAM roles and access entries · CloudTrail · VPC | read-only source analysis, a separate least-privilege role for data migration |
| AWS | ECR · S3 | digest-pinned images, source bundles and logs |
| Google Cloud | GKE · Terraform | cloud targets and cross-cloud data migration checks |
| On-premises | K3s · Helm · WireGuard | Agent deployment, outbound-only tunnel |

## Where AI is used, and its guardrails

| Where | What | Guardrail |
| --- | --- | --- |
| Analysis | Maps the source setup to standard resources | Every judgment keeps its evidence (file:line), and a human reviews it in the plan preview |
| Sandbox | Generates tests, fixes failures (Fixer) | Only in a temporary space with outbound traffic blocked; a fix must pass the tests again |
| Everywhere | Claude calls | Cost caps enforced in code ($3 per migration · $20 per day · $100 per month) and a usage ledger. The model receives only masked evidence snippets — never `.env` values or the whole codebase ([ADR-004](docs/decisions/ADR-004.md)) |

## What works today

> What the demo shows is not a mockup — it runs on the demo server. Parts still in progress are marked as such.

| Stage | Status |
| --- | --- |
| Connection · scope selection, analysis, image build, sandbox verification | ✅ Runs for real on the demo server |
| Plan · approval, on-premises provisioning (Agent · tunnel) | ✅ Runs for real — app and DB deployed to the user's K3s, all four checks passing |
| Traffic shift | 🟡 Being verified in real runs — entry into the stepped weight shift confirmed |
| Data replication · DB cutover | 🟡 Verified cross-cloud (EKS→GKE) · on-premises path in progress ([ADR-007](docs/decisions/ADR-007.md)) |

## Technical decisions we thought hard about

- **Never lose a long-running job** — Temporal survives hours-long approval waits and server restarts. When code changes while migrations are running, patch markers separate old runs from new ones ([ADR-001](docs/decisions/ADR-001.md)).
- **Deploy only what was verified** — sandbox and production share the image digest and the renderer, and the approved digest is re-checked at run time ([ADR-003](docs/decisions/ADR-003.md)).
- **Never punch holes on the user's side** — the on-prem Agent and its tunnel only connect outward. The data tunnel direction follows the same rule ([ADR-007](docs/decisions/ADR-007.md)).
- **Never leak secrets** — `.env` is stored encrypted and only referenced afterwards; Agent and approver tokens are stored as digests only.
- **Split permissions** — the source connection is a read-only role; data migration uses a separate role limited to two namespaces.

## How the team worked

- Decisions live in **7 ADRs** and per-area specs (`specs/`); work was split into **136 tickets** (`tickets/`) that record alternatives, the reason for each choice, and the checks done.
- Every change went through branch → PR → CI (ruff, about **3,000** pytest tests) → squash merge; about 160 PRs are merged.
- Each area (analysis·verification / provisioning / data migration / traffic / web) had an owner, and areas agreed through `packages/contracts` and the event schema.

---

## Repository layout

| Path | Role |
| --- | --- |
| `apps/api` | FastAPI — start·read·approve migrations, connections, Agent·tunnel, events (SSE), report |
| `apps/worker` | Temporal workflows · activities · workers |
| `packages/contracts` | Contracts — events, task queues, input models |
| `packages/scanners` | Code · manifest · AWS analysis |
| `packages/agents` | AI mapping · test generation · Fixer |
| `packages/testsuite` | Sandbox test runner |
| `packages/providers` | Renderer, NetworkPolicy, per-target provisioning, on-prem Agent·tunnel |
| `packages/data` | Data replication · DB cutover |
| `packages/traffic` | Envoy config · traffic shift |
| `infra/` | Demo server, Agent Helm chart, relay server, connection templates, Terraform |
| `docs/` · `specs/` · `tickets/` | Design decisions, specs, work records |

## Getting started

Requirements: Python 3.11, [uv](https://docs.astral.sh/uv/), Docker Compose. Full local setup, Agent enrollment, and a file-by-file guide are in the [development guide](docs/development.md) (Korean).

```bash
cp .env.example .env
make setup
make infra-up    # PostgreSQL, Temporal
make workers     # in another terminal
make api         # in another terminal → http://localhost:8000/docs
```

## Further reading

Most design documents are written in Korean.

[Development guide](docs/development.md) · [Design decisions (ADR)](docs/decisions/) · [Temporal guide](docs/architecture/temporal-flow.md) · [Events guide](docs/engineering/events-guide.md) · [`.env` reference guide](docs/engineering/env-file-guide.md) · [On-prem Agent install·operations](specs/provisioning/onprem-agent-install.md) · [Demo server operations](infra/demo/README.md)

<!-- TODO: team -->
