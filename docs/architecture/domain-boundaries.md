# Domain boundaries

| Area | Owns | Does not own |
| --- | --- | --- |
| API | HTTP contracts, workflow start, approval/cancel Signals, state/SSE | Long running cloud work |
| Analyzer | Dockerfile/Compose and source execution requirements | Target architecture choice |
| Build/Sandbox | Image and isolated test evidence | Production cutover decision |
| AI planner | Target architecture and change proposal | Provider API execution |
| Provider adapter | Provider-specific Terraform/Helm inputs | Redesign of approved architecture |
| Workflow | Ordering, wait, retries, phase and failure state | Direct network, DB, or cloud I/O |
| Activities | External operations and their results | End-to-end orchestration policy |
| State store | User/project records and approved plans | Temporal event history |

At present the Analyzer, Build, Adapter, and Migration Activities are scaffold examples only. Keep a boundary-specific Pydantic contract for each real handoff; do not introduce a universal cloud intermediate language by default.
