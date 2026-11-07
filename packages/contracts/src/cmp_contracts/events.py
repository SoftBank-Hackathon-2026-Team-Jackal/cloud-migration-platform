"""Migration event envelope and per-type `data` contracts (ticket 24).

Names follow the web contract (`cloud-migration-web` `src/lib/migration/events.ts`,
`contract/events.schema.json`; copy in `docs/contracts/events.schema.json`). Backend names that
differ are translated by `to_web` before validation. Events carry values and codes only: no
user-facing text, no secret values (PROV-021).

The `*Data` models below mirror the web schema one-to-one; `tests/test_event_contract.py` compares
`event_json_schema()` with the web copy, so edit both together.
"""

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

BackendPhase = Literal[
    "analysis",
    "build",
    "planning",
    "awaiting_approval",
    "provisioning",
    "data_copy",
    "traffic",
    "verification",
]
EventStatus = Literal["running", "done", "failed", "waiting"]
MigrationStatus = Literal["idle", "running", "waiting", "done", "failed", "cancelled"]
PHASES: tuple[str, ...] = BackendPhase.__args__
STATUSES: tuple[str, ...] = EventStatus.__args__


class WebModel(BaseModel):
    """Strict web-contract model. Optional fields left as None are omitted when serialized."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, hide_input_in_errors=True)

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler):
        data = handler(self)
        for name, field in type(self).model_fields.items():
            key = field.alias or name
            if not field.is_required() and data.get(key, 0) is None:
                data.pop(key)
        return data


# ---- generated from the web schema (see module docstring), reviewed by hand ----


class AnalysisFindingData(WebModel):
    id: str
    stream: Literal["infrastructure", "code"]
    key: str
    via: list[Literal["config", "code", "iam", "trail"]]
    progress: float = Field(ge=0, le=100)
    message: str
    detail: str


class AnalysisDoneData(WebModel):
    pass


class AnalysisScanItem(WebModel):
    key: str
    n: float = Field(ge=0)
    sku: str | None = None


class AnalysisScanEvidence(WebModel):
    """`cmp_contracts.analysis.Evidence` (web `$defs/evidence`). Cloud read results stay unverified."""

    kind: Literal["CODE", "MANIFEST", "CLOUD_RESOURCE", "IAM_POLICY", "CLOUDTRAIL", "CONFIG"]
    locator: str
    verified: bool


class AnalysisScanData(WebModel):
    """One per finished scan Activity: counts only (`items`), `completed`/`total` per stream."""

    id: str
    activity: str
    stream: Literal["infrastructure", "code"]
    completed: int = Field(ge=0)
    total: int = Field(ge=1)
    items: list[AnalysisScanItem]
    evidence: list[AnalysisScanEvidence]


class BuildCheckData(WebModel):
    id: Literal["build", "health", "db", "render", "secret", "scope"]
    state: Literal["running", "failed", "fixing", "recheck", "done"]
    fixed: int | None = Field(default=None, ge=0)
    attempt: int | None = Field(default=None, ge=1)


class BuildFixLinesItem(WebModel):
    kind: Literal["del", "add"]
    text: str


class BuildFixData(WebModel):
    id: str
    check: str
    lines: list[BuildFixLinesItem]
    why: str
    evidence: str
    file: str | None = Field(default=None)


class PlanStepData(WebModel):
    id: str
    group: Literal["infrastructure", "code"]
    grade: Literal["DIRECT", "API_DIFF", "PARTIAL", "NONE"]
    before: str
    after: str
    evidence: str
    change: str
    action: Literal["PROCEED", "APPROVAL", "BLOCK"]
    keep_source: bool
    verification: Literal["UNVERIFIED", "EVIDENCE_CHECKED", "SANDBOX_VERIFIED"]
    kind: (
        Literal[
            "ingress",
            "network",
            "compute",
            "proxy",
            "database",
            "cache",
            "queue",
            "storage",
            "config",
            "other",
        ]
        | None
    ) = Field(default=None)


class BuildResultData(WebModel):
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    status: Literal["SUCCEEDED", "FAILED"]
    digest: str | None
    image_uri: str | None
    reused: bool
    error_code: (
        Literal["SOURCE_NOT_FOUND", "DOCKERFILE_MISSING", "BUILD_FAILED", "PUSH_FAILED", "TIMEOUT"]
        | None
    )


class BuildReviewData(WebModel):
    reason: Literal["max_attempts"]
    attempts: int = Field(ge=1)


class PlanReadyData(WebModel):
    plan_id: str | None = Field(default=None)
    revision: int | None = Field(default=None, ge=1)
    template_version: str | None = Field(default=None)
    built_digest: str | None = Field(default=None)
    verified_digest: str | None = Field(default=None)
    manifest_digest: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    preview_digest: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    # The Revision's approval scope (`PlanManifest.scope`), the value the approval API compares;
    # the web sends it back unchanged on APPROVE/REJECT. Optional: older events omit it.
    scope: Literal["INFRASTRUCTURE", "WORKLOADS", "FULL"] | None = Field(default=None)


class PlanPreviewSource(WebModel):
    provider: str
    platform: str
    location: str


class PlanPreviewTarget(WebModel):
    provider: str
    platform: str
    location: str


class PlanPreviewResourcesItem(WebModel):
    id: str
    group: Literal["network", "compute", "data", "other"]
    name: str
    detail: str
    size: str
    size_detail: str | None = Field(default=None)
    change: Literal["CREATE", "PINNED", "KEEP_SOURCE", "UPDATE", "DELETE", "EXISTING"]
    kind: (
        Literal[
            "ingress",
            "network",
            "compute",
            "proxy",
            "database",
            "cache",
            "queue",
            "storage",
            "config",
            "other",
        ]
        | None
    ) = Field(default=None)
    warning: str | None = Field(default=None)
    resource_type: str | None = Field(default=None)
    quantity: float | None = Field(default=None, ge=0)
    backend_name: str | None = Field(default=None)
    defaulted: bool | None = Field(default=None)


class PlanPreviewEnvRemapItem(WebModel):
    key: str
    from_: str = Field(alias="from")
    to: str
    secret: bool
    keep: bool | None = Field(default=None)


class PlanPreviewSecurityRulesItem(WebModel):
    from_: str = Field(alias="from")
    to: str
    port: int | None = Field(ge=0, le=65535)
    protocol: Literal["TCP", "UDP", "ANY"]
    action: Literal["ALLOW", "DENY"]


class PlanPreviewValidationErrorsItem(WebModel):
    id: str
    resource: str | None = Field(default=None)
    message: str


class PlanPreviewValidationWarningsItem(WebModel):
    id: str
    resource: str | None = Field(default=None)
    message: str


class PlanPreviewValidation(WebModel):
    errors: list[PlanPreviewValidationErrorsItem]
    warnings: list[PlanPreviewValidationWarningsItem]


class PlanPreviewRollout(WebModel):
    """전환 방법 [가정: 백엔드 계약 없음]. traffic_method는 정해졌을 때만 온다(Route 53 가중치·중계 프록시 미정)."""

    traffic_steps: list[Annotated[float, Field(le=100, gt=0)]] = Field(min_length=1)
    traffic_method: str | None = Field(default=None)
    expected_write_pause_ms: float | None = Field(default=None, ge=0)
    data_method: str | None = Field(default=None)


class PlanPreviewCostLinesItem(WebModel):
    id: str
    label: str | None = Field(default=None)
    source_usd: float | None = Field(ge=0)
    target_usd: float | None = Field(ge=0)
    excluded: bool | None = Field(default=None)


class PlanPreviewCost(WebModel):
    """고정 요금 추정(USD/월) [가정]. 값이 null이면 그쪽은 아직 추정하지 못했다."""

    currency: Literal["USD"]
    lines: list[PlanPreviewCostLinesItem]
    credits_usd: float | None = Field(default=None, ge=0)
    basis: str | None = Field(default=None)


class PlanPreviewData(WebModel):
    """PROV-014 계획 미리보기 [가정: 이벤트 이름·필드는 백엔드와 미정]. 비밀값 원문은 담지 않는다."""

    plan_id: str
    revision: int = Field(ge=1)
    source: PlanPreviewSource
    target: PlanPreviewTarget
    resources: list[PlanPreviewResourcesItem]
    env_remap: list[PlanPreviewEnvRemapItem]
    security_rules: list[PlanPreviewSecurityRulesItem]
    validation: PlanPreviewValidation
    pending_decisions: list[PlanPreviewValidationErrorsItem] | None = Field(default=None)
    manifest_digest: str | None = Field(default=None)
    rollout: PlanPreviewRollout | None = Field(default=None)
    cost: PlanPreviewCost | None = Field(default=None)


# Optional *and* nullable on the web side (`type: [x, "null"]`): keep the null branch.
NULLABLE = {"x-nullable": True}


class PreviewChange(WebModel):
    """cmp_contracts.plan.ResourceChange. size는 검토된 표시용 문자열."""

    resource_type: str
    action: Literal["CREATE", "UPDATE", "DELETE", "READ", "NO_CHANGE"]
    name: str
    quantity: int | None = Field(default=None, ge=1, json_schema_extra=NULLABLE)
    size: str | None = Field(default=None, json_schema_extra=NULLABLE)


class ValidationIssue(WebModel):
    """cmp_contracts.provisioning.ValidationIssue"""

    code: str
    path: str
    message: str


class PlanPreviewBackendData(WebModel):
    """백엔드 Preview(cmp_contracts.plan.Preview) 그대로 + 계획 식별자·대상·보안 규칙(티켓 60).

    웹은 `changes`가 배열이면 이 모양으로 읽는다(`isBackendPreview`)."""

    contract_version: Literal["1"] | None = Field(default=None)
    manifest_digest: str
    changes: list[PreviewChange]
    applied_defaults: list[PreviewChange]
    warnings: list[ValidationIssue]
    errors: list[ValidationIssue]
    pending_decisions: list[ValidationIssue]
    plan_id: str | None = Field(default=None)
    revision: int | None = Field(default=None, ge=1)
    source: PlanPreviewSource | None = Field(default=None)
    target: PlanPreviewTarget | None = Field(default=None)
    env_remap: list[PlanPreviewEnvRemapItem] | None = Field(default=None)
    security_rules: list[PlanPreviewSecurityRulesItem] | None = Field(default=None)


class ApprovalRequiredData(WebModel):
    excluded: list[str]
    rollback_policy: str
    pending_decisions: list[ValidationIssue] | None = Field(default=None)
    gate: Literal["cutover"] | None = Field(default=None)


class ApprovalDecisionData(WebModel):
    """승인 결정의 접수·전달·실행 상태 [가정: SSE 이벤트 이름]. 필드는 백엔드 ApprovalResponse와 같다. 전달(DELIVERED) 전에는 완료로 보이지 않는다(ADR-002)."""

    approval_id: str
    plan_id: str
    revision: int = Field(ge=1)
    decision: Literal["APPROVE", "REJECT"]
    scope: Literal["INFRASTRUCTURE", "WORKLOADS", "FULL"]
    delivery_status: Literal["PENDING", "DELIVERED", "FAILED"]
    delivery_attempts: int = Field(ge=0)
    execution_status: Literal["NOT_STARTED", "RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN"]
    workflow_state: str | None = Field(default=None)


class ProvisionResourceData(WebModel):
    id: str
    key: str
    state: Literal["running", "done", "failed"]
    progress: float = Field(ge=0, le=100)
    name: str | None = Field(default=None)
    detail: str | None = Field(default=None)


class ProvisionCheckData(WebModel):
    name: str
    state: Literal["running", "done", "failed"]
    passed: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)


class DataProgressData(WebModel):
    step: Literal["connect", "dump", "cdc", "ready"]
    rows: int = Field(ge=0)
    total_rows: int = Field(ge=0)
    lag_ms: float | None = Field(ge=0)
    writes_per_sec: float
    dump_percent: float = Field(ge=0, le=100)


class DataVerifyData(WebModel):
    name: Literal["rows", "checksum"]
    state: Literal["running", "done", "failed"]
    source: int | None = Field(default=None, ge=0)
    target: int | None = Field(default=None, ge=0)


class DataPreflightChecksItemStepsItem(WebModel):
    name: Literal["DNS", "TCP", "TLS", "AUTH", "QUERY"]
    status: Literal["PENDING", "PASS", "FAIL", "UNKNOWN"]


class DataPreflightChecksItem(WebModel):
    name: Literal["cidr_overlap", "route", "db_path", "large_transfer"]
    status: Literal["PENDING", "RUNNING", "PASS", "FAIL", "UNKNOWN", "SKIPPED"]
    code: str | None
    evidence: str
    checked_at: str | None = Field(default=None)
    tested_endpoint: str | None = Field(default=None)
    steps: list[DataPreflightChecksItemStepsItem] | None = Field(default=None)
    bytes_done: int | None = Field(default=None, ge=0)
    bytes_total: int | None = Field(default=None, ge=0)
    elapsed_ms: float | None = Field(default=None, ge=0)
    mtu: Literal["NOT_ATTEMPTED", "PASS", "FAIL"] | None = Field(default=None)


class DataPreflightNetworkPath(WebModel):
    kind: Literal["wireguard", "cloud_vpn", "other"]
    name: str
    cidr: str | None


class DataPreflightNetwork(WebModel):
    source_cidr: str
    target_cidr: str
    path: DataPreflightNetworkPath
    source_db: str
    source_addr: str
    replication_node: str
    target_db: str
    target_db_detail: str


class DataPreflightData(WebModel):
    status: Literal["RUNNING", "PASS", "FAIL", "UNKNOWN"]
    checks: list[DataPreflightChecksItem]
    probe_location: str
    input_fingerprint: str | None
    checked_at: str | None
    may_start: bool
    network: DataPreflightNetwork


class DataPhaseData(WebModel):
    # "tunnel" (Provisioning 77): dm_tunnel is building the replication tunnel (source NLB 2-3 min).
    phase: Literal["tunnel", "preflight", "blocked", "schema", "copy", "catchup", "streaming"]


class DataLsnData(WebModel):
    source_lsn: str
    target_lsn: str | None
    lag_bytes: int = Field(ge=0)
    lag_ms: float | None = Field(ge=0)


class DataTableData(WebModel):
    name: str
    rows_copied: int = Field(ge=0)
    rows_total: int = Field(ge=0)
    state: Literal["waiting", "copying", "copied", "matched"]


class DataReadinessData(WebModel):
    longest_tx_ms: float = Field(ge=0)
    lock_waits: int = Field(ge=0)
    commits_per_sec: float = Field(ge=0)
    ready: bool


class TrafficReadyData(WebModel):
    state: Literal["running", "done", "waiting"]
    lag_ms: float = Field(ge=0)
    longest_tx_ms: float = Field(ge=0)
    commits_per_sec: float = Field(ge=0)


class TrafficSnapshotWeights(WebModel):
    origin: float
    target: float


class TrafficSnapshotGate(WebModel):
    failed_requests: float
    max_failed_requests: float
    p99_ratio: float
    max_p99_ratio: float
    observe_left_seconds: float
    observe_seconds: float | None = Field(default=None, ge=0)


class TrafficSnapshotResidual(WebModel):
    requests_per_sec: float
    streams: int


class TrafficSnapshotNotesItem(WebModel):
    key: Literal[
        "prepare",
        "apply",
        "observe",
        "gatePass",
        "gateFail",
        "rollback",
        "reached",
        "manual",
        "inject",
        "drained",
        "restart",
        "idleProbe",
    ]
    params: dict[str, str | float] | None = Field(default=None)
    activity: str | None = Field(default=None)


class TrafficSnapshotBackendsOrigin(WebModel):
    requests_per_sec: float = Field(ge=0)
    p99_ms: float = Field(ge=0)
    failed_requests: float = Field(ge=0)


class TrafficSnapshotBackendsTarget(WebModel):
    requests_per_sec: float = Field(ge=0)
    p99_ms: float = Field(ge=0)
    failed_requests: float = Field(ge=0)


class TrafficSnapshotBackends(WebModel):
    """[가정] 백엔드별 지표"""

    origin: TrafficSnapshotBackendsOrigin
    target: TrafficSnapshotBackendsTarget


class TrafficSnapshotResultsItem(WebModel):
    step: float = Field(ge=0, le=100)
    verdict: Literal["pass", "rollback"]
    failed_requests: float = Field(ge=0)
    p99_ratio: float = Field(ge=0)
    observed_seconds: float | None = Field(default=None, ge=0)


class TrafficSnapshotReplication(WebModel):
    """[가정] 논리 복제 지연"""

    lag_ms: float | None = Field(ge=0)


class TrafficSnapshotData(WebModel):
    phase: Literal["applying", "observing", "gate", "complete", "rolledback"]
    weights: TrafficSnapshotWeights
    step: float = Field(ge=0, le=100)
    gate: TrafficSnapshotGate
    residual: TrafficSnapshotResidual
    decision: Literal["advance", "rollback", "done"] | None = Field(default=None)
    notes: list[TrafficSnapshotNotesItem] | None = Field(default=None)
    steps: list[Annotated[float, Field(ge=0, le=100)]] | None = Field(default=None, min_length=1)
    backends: TrafficSnapshotBackends | None = Field(default=None)
    results: list[TrafficSnapshotResultsItem] | None = Field(default=None)
    replication: TrafficSnapshotReplication | None = Field(default=None)


class TrafficPrepareChecksItem(WebModel):
    id: str
    state: Literal["running", "done", "failed"]
    value: str | None = Field(default=None)


class TrafficPrepareRelay(WebModel):
    name: str
    platform: str | None = Field(default=None)
    location: str | None = Field(default=None)
    mode: str | None = Field(default=None)


class TrafficPrepareOrigin(WebModel):
    name: str
    platform: str | None = Field(default=None)
    location: str | None = Field(default=None)


class TrafficPrepareTargetTunnel(WebModel):
    interface: str
    mtu: int = Field(ge=576)


class TrafficPrepareTarget(WebModel):
    name: str
    platform: str | None = Field(default=None)
    location: str | None = Field(default=None)
    external_endpoint: str | None = Field(default=None)
    tunnel: TrafficPrepareTargetTunnel | None = Field(default=None)


class TrafficPrepareDataPath(WebModel):
    pooler: str
    source_db: str
    target_db: str


class TrafficPreparePolicy(WebModel):
    steps: list[Annotated[float, Field(ge=0, le=100)]] = Field(min_length=1)
    observe_seconds: float = Field(ge=0)
    max_failed_requests: float = Field(ge=0)
    max_p99_ratio: float = Field(ge=0)


class TrafficPrepareData(WebModel):
    """[가정] traffic_prepare 사전 확인과 연결 정보"""

    state: Literal["running", "done", "failed"]
    checks: list[TrafficPrepareChecksItem]
    reason: str | None = Field(default=None)
    entry_host: str | None = Field(default=None)
    relay: TrafficPrepareRelay | None = Field(default=None)
    origin: TrafficPrepareOrigin | None = Field(default=None)
    target: TrafficPrepareTarget | None = Field(default=None)
    data_path: TrafficPrepareDataPath | None = Field(default=None)
    policy: TrafficPreparePolicy | None = Field(default=None)


CutoverMethod = Literal["fast", "classic"]
CutoverWindow = Literal["pre", "pause"]


class CutoverStepPlan(WebModel):
    id: str
    ms: float = Field(ge=0)
    start_ms: float | None = Field(default=None, ge=0)
    window: CutoverWindow | None = Field(default=None)


class CutoverPlanExpectedPause(WebModel):
    min: float = Field(ge=0)
    max: float = Field(ge=0)


class CutoverPlanRehearsalMethod(WebModel):
    method: CutoverMethod
    pause_ms: float = Field(ge=0)


class CutoverPlanRehearsal(WebModel):
    source: str
    runs: int = Field(ge=0)
    methods: list[CutoverPlanRehearsalMethod]


class CutoverPlanPrecondition(WebModel):
    id: str
    state: Literal["pass", "wait", "fail"]
    value: str | None = Field(default=None)
    evidence: str | None = Field(default=None)
    proposal: bool | None = Field(default=None)


class CutoverPlanRollback(WebModel):
    standby_hours: float = Field(ge=0)


class CutoverPlanTopology(WebModel):
    app: str
    pooler: str
    source_db: str
    target_db: str
    source_where: str
    target_where: str
    writes_per_sec: float = Field(ge=0)


class CutoverPlanHandover(WebModel):
    item: Literal["dns", "origin", "access"]
    detail: str | None = Field(default=None)


class CutoverPlanData(WebModel):
    """[가정] 두 번째 승인 전 전환 계획"""

    method: CutoverMethod
    expected_pause_ms: CutoverPlanExpectedPause
    rehearsal: CutoverPlanRehearsal
    preconditions: list[CutoverPlanPrecondition]
    steps: list[CutoverStepPlan]
    rollback: CutoverPlanRollback
    topology: CutoverPlanTopology
    handover: list[CutoverPlanHandover]


class CutoverReadyLimits(WebModel):
    longest_tx_ms: float = Field(ge=0)
    lock_waits: int = Field(ge=0)


class CutoverReadyData(WebModel):
    """[가정] 전환 가능 시점 대기"""

    attempt: int = Field(ge=1)
    longest_tx_ms: float = Field(ge=0)
    lock_waits: int = Field(ge=0)
    commits_per_sec: float = Field(ge=0)
    ready: bool
    limits: CutoverReadyLimits
    waiting_on: str | None = Field(default=None)
    waited_ms: float | None = Field(default=None, ge=0)


class CutoverStepData(WebModel):
    n: int = Field(ge=1, le=32)
    state: Literal["running", "done", "verify"]
    id: str | None = Field(default=None)
    ms: float | None = Field(default=None, ge=0)
    method: CutoverMethod | None = Field(default=None)
    of: int | None = Field(default=None, ge=1, le=32)
    result: Literal["ok", "fail", "revert"] | None = Field(default=None)
    window: CutoverWindow | None = Field(default=None)


class CutoverOutcomeReplication(WebModel):
    direction: Literal["reverse", "forward"]
    lag_ms: float | None = Field(ge=0)


class CutoverOutcomeData(WebModel):
    """[가정] 전환 결과(통과·거부)"""

    result: Literal["switched", "refused"]
    active: Literal["source", "target"]
    pause_ms: float | None = Field(ge=0)
    replication: CutoverOutcomeReplication
    reason: str | None = Field(default=None)
    message: str | None = Field(default=None)
    steps: list[CutoverStepPlan] | None = Field(default=None)
    held_requests: int | None = Field(default=None, ge=0)
    failed_requests: int | None = Field(default=None, ge=0)


class CutoverVerifyData(WebModel):
    """[가정] 전환 순간 스냅샷 기준 테이블별 검증"""

    table: str
    source_rows: int = Field(ge=0)
    target_rows: int | None = Field(ge=0)
    checksum: Literal["match", "mismatch", "pending", "skipped"]


class CutoverResultData(WebModel):
    id: str
    value: str
    label: str | None = Field(default=None)
    ok: bool | None = Field(default=None)


class CutoverHandoverData(WebModel):
    item: Literal["dns", "origin", "access"]
    state: Literal["running", "done", "held"]
    detail: str | None = Field(default=None)


class ErrorData(WebModel):
    code: str
    message: str
    phase: (
        Literal[
            "analysis",
            "build",
            "planning",
            "awaiting_approval",
            "provisioning",
            "data_copy",
            "traffic",
            "verification",
        ]
        | None
    )
    retryable: bool


class SandboxEnvData(WebModel):
    namespace: str = Field(min_length=1)
    expires_minutes: int = Field(ge=1)
    egress_allowed: int = Field(ge=0)


class SandboxStepData(WebModel):
    run: int = Field(ge=1, le=6)
    attempt: int = Field(ge=1, le=3)
    step: Literal["render", "apply", "ready", "test", "judge"]
    state: Literal["running", "done", "failed"]


class SandboxTestsData(WebModel):
    total: int = Field(ge=0)
    catalog: int = Field(ge=0)
    original: int = Field(ge=0)
    generated: int = Field(ge=0)
    read_only: int = Field(ge=0)


class SandboxAttemptVerdict(WebModel):
    passed: bool
    reasons: list[str]


class SandboxAttemptChecks(WebModel):
    required_passed: int = Field(ge=0)
    required_total: int = Field(ge=0)
    unsupported_calls: int = Field(ge=0)
    calls_in_mapping: int = Field(ge=0)
    calls_total: int = Field(ge=0)
    blocked_egress: int = Field(ge=0)


class SandboxAttemptData(WebModel):
    run: int = Field(ge=1, le=6)
    attempt: int = Field(ge=1, le=3)
    max_attempts: int = Field(ge=1)
    verdict: SandboxAttemptVerdict
    failure_class: Literal["SPEC", "CODE", "BUILD", "RENDER", "INFRA"] | None
    checks: SandboxAttemptChecks
    fix_ids: list[str]


class SandboxDoneData(WebModel):
    status: Literal["PASSED", "FAILED", "NEEDS_REVIEW", "STOPPED_RENDER"]
    cleanup_status: Literal["CLEANED", "FAILED", "PENDING"]
    digest: str | None


DATA_MODELS: dict[str, type[WebModel]] = {
    "analysis.finding": AnalysisFindingData,
    "analysis.scan": AnalysisScanData,
    "analysis.done": AnalysisDoneData,
    "build.check": BuildCheckData,
    "build.fix": BuildFixData,
    "plan.step": PlanStepData,
    "build.result": BuildResultData,
    "build.review": BuildReviewData,
    "plan.ready": PlanReadyData,
    "plan.preview": PlanPreviewData,
    "approval.required": ApprovalRequiredData,
    "approval.decision": ApprovalDecisionData,
    "provision.resource": ProvisionResourceData,
    "provision.check": ProvisionCheckData,
    "data.progress": DataProgressData,
    "data.verify": DataVerifyData,
    "data.preflight": DataPreflightData,
    "data.phase": DataPhaseData,
    "data.lsn": DataLsnData,
    "data.table": DataTableData,
    "data.readiness": DataReadinessData,
    "traffic.ready": TrafficReadyData,
    "traffic.snapshot": TrafficSnapshotData,
    "traffic.prepare": TrafficPrepareData,
    "cutover.plan": CutoverPlanData,
    "cutover.ready": CutoverReadyData,
    "cutover.step": CutoverStepData,
    "cutover.outcome": CutoverOutcomeData,
    "cutover.verify": CutoverVerifyData,
    "cutover.result": CutoverResultData,
    "cutover.handover": CutoverHandoverData,
    "error": ErrorData,
    "sandbox.env": SandboxEnvData,
    "sandbox.step": SandboxStepData,
    "sandbox.tests": SandboxTestsData,
    "sandbox.attempt": SandboxAttemptData,
    "sandbox.done": SandboxDoneData,
}
# Types whose `data` has a second accepted shape (web `oneOf`), chosen when `data.changes` is a list (web `isBackendPreview`).
ALTERNATE_MODELS: dict[str, tuple[str, type[WebModel]]] = {
    "plan.preview": ("plan.preview.backend", PlanPreviewBackendData),
}
NAMED_DEFS: dict[str, type[WebModel]] = {
    "previewChange": PreviewChange,
    "validationIssue": ValidationIssue,
}


def _model_for(event_type: str, data: dict[str, Any]) -> type[WebModel] | None:
    alternate = ALTERNATE_MODELS.get(event_type)
    if alternate is not None and isinstance(data.get("changes"), list):
        return alternate[1]
    return DATA_MODELS.get(event_type)


# ---- envelope ----

EVENT_TYPES: tuple[str, ...] = tuple(DATA_MODELS)
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


class MigrationEvent(BaseModel):
    """Stored/JSON envelope. SSE omits `migration_id` (web schema) — see `sse_payload`."""

    model_config = ConfigDict(extra="forbid")

    migration_id: str
    seq: int = Field(ge=1)
    at: str
    phase: BackendPhase
    type: str
    status: EventStatus
    progress: float | None = None
    data: dict[str, Any]

    def sse_payload(self) -> dict[str, Any]:
        payload = self.model_dump(exclude={"migration_id"})
        if payload["progress"] is None:
            payload.pop("progress")
        return payload


def utc_now() -> str:
    """UTC ISO with milliseconds and `Z`, as the web schema pattern requires."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# ---- backend name -> web name ----

PREFLIGHT_CHECK_NAMES = {
    "check_cidr_overlap": "cidr_overlap",
    "check_route": "route",
    "probe_db_path": "db_path",
    "probe_large_transfer": "large_transfer",
}
DATA_PHASE_NAMES = {"copying": "copy", "catching_up": "catchup"}
_KEY_RENAMES = {
    "longest_xact_ms": "longest_tx_ms",
    "lock_waiters": "lock_waits",
    "image_digest": "digest",
    "bytes_received": "bytes_done",
    "bytes_expected": "bytes_total",
}


def _rename(value: Any) -> Any:
    if isinstance(value, list):
        return [_rename(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key == "seconds" and "elapsed_ms" not in value and isinstance(item, int | float):
            out["elapsed_ms"] = round(item * 1000, 3)
            continue
        out[_KEY_RENAMES.get(key, key)] = _rename(item)
    if isinstance(out.get("name"), str) and out["name"] in PREFLIGHT_CHECK_NAMES:
        out["name"] = PREFLIGHT_CHECK_NAMES[out["name"]]
    return out


def to_web(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Translate backend field names/values to the web contract (E1). Unknown keys pass through
    so validation can reject them."""
    data = _rename(data)
    if event_type == "data.phase" and data.get("phase") in DATA_PHASE_NAMES:
        data = {**data, "phase": DATA_PHASE_NAMES[data["phase"]]}
    # `data.table.state` keeps `copying` (a web value); only the `data.phase` value is renamed.
    return data


def validate_data(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Return web-shaped JSON for `data` or raise `ValueError` (unknown type, invalid fields)."""
    model = _model_for(event_type, data)
    if model is None:
        raise ValueError(f"unknown event type: {event_type}")
    return model.model_validate(to_web(event_type, data)).model_dump(mode="json", by_alias=True)


# ---- JSON Schema in the web layout ----


def _strip_optional_null(schema: dict) -> dict:
    """Optional fields are omitted when None, so their schema never needs a null branch."""
    for key, value in list(schema.items()):
        if isinstance(value, dict):
            _strip_optional_null(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _strip_optional_null(item)
    props = schema.get("properties")
    if isinstance(props, dict):
        required = set(schema.get("required", []))
        for name, prop in props.items():
            prop.pop("default", None)
            prop.pop("title", None)
            if prop.pop("x-nullable", False) or name in required:
                continue
            branches = prop.get("anyOf")
            if branches and any(b.get("type") == "null" for b in branches):
                rest = [b for b in branches if b.get("type") != "null"]
                prop.pop("anyOf")
                if len(rest) == 1:
                    prop.update(rest[0])
                else:
                    prop["anyOf"] = rest
    return schema


def event_json_schema() -> dict:
    """The web-layout schema: `$defs` `data:<type>`, `event:<type>`, `snapshotData`."""
    defs: dict[str, Any] = {"phase": {"enum": list(PHASES)}, "status": {"enum": list(STATUSES)}}
    one_of = []

    def add(name: str, model: type[WebModel]) -> None:
        schema = _strip_optional_null(model.model_json_schema(by_alias=True, mode="serialization"))
        nested = schema.pop("$defs", {})
        defs.update({f"model:{k}": v for k, v in nested.items()})  # already stripped
        defs[name] = schema

    for name, model in NAMED_DEFS.items():
        add(name, model)
    for event_type, model in DATA_MODELS.items():
        add(f"data:{event_type}", model)
        data_ref: Any = f"#/$defs/data:{event_type}"
        if event_type in ALTERNATE_MODELS:
            alternate, alternate_model = ALTERNATE_MODELS[event_type]
            add(f"data:{alternate}", alternate_model)
            data_ref = [data_ref, f"#/$defs/data:{alternate}"]
        defs[f"event:{event_type}"] = _envelope(event_type, data_ref)
        one_of.append({"$ref": f"#/$defs/event:{event_type}"})
    # The web schema names the shared Evidence object `evidence` (not `model:…`).
    if "model:AnalysisScanEvidence" in defs:
        defs["evidence"] = defs["model:AnalysisScanEvidence"]
    defs["nonSnapshotEvent"] = {"oneOf": list(one_of)}
    defs["snapshotData"] = {
        "type": "object",
        "properties": {
            "migration_id": {"type": "string"},
            "status": {"enum": list(MigrationStatus.__args__)},
            "reset": {"type": "boolean"},
            "events": {"type": "array", "items": {"$ref": "#/$defs/nonSnapshotEvent"}},
        },
        "required": ["migration_id", "status", "reset", "events"],
        "additionalProperties": False,
    }
    defs["event:snapshot"] = _envelope("snapshot", "#/$defs/snapshotData")
    one_of.append({"$ref": "#/$defs/event:snapshot"})
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "events.schema.json",
        "oneOf": one_of,
        "$defs": _prefix_model_refs(defs),
    }


def _prefix_model_refs(defs: dict) -> dict:
    """Pydantic nested refs point at `#/$defs/<Model>`; here they live under `model:<Model>`."""
    raw = json.dumps(defs, ensure_ascii=False)
    for name in [k.removeprefix("model:") for k in defs if k.startswith("model:")]:
        raw = raw.replace(f'"#/$defs/{name}"', f'"#/$defs/model:{name}"')
    return json.loads(raw)


def _envelope(event_type: str, data_ref: str | list[str]) -> dict:
    data = (
        {"$ref": data_ref}
        if isinstance(data_ref, str)
        else {"oneOf": [{"$ref": ref} for ref in data_ref]}
    )
    return {
        "type": "object",
        "properties": {
            "seq": {"type": "integer", "minimum": 0},
            "at": {
                "type": "string",
                "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$",
            },
            "phase": {"$ref": "#/$defs/phase"},
            "type": {"const": event_type},
            "status": {"$ref": "#/$defs/status"},
            "progress": {"type": "number"},
            "message": {"type": "string"},
            "data": data,
        },
        "required": ["seq", "at", "phase", "type", "status", "data"],
        "additionalProperties": False,
    }
