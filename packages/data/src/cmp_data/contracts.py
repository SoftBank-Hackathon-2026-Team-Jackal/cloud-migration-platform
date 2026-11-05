"""데이터 이전(CDC) 판단 입력·출력 계약. 외부 I/O 없이 검증할 수 있는 순수 모델이다.

값의 출처는 원본·대상 PostgreSQL에서 읽은 카탈로그 값이다(조회 SQL은 Activity 쪽 책임).
기준값은 예선·본선 PoC(`Softbank-Hackathon-2026-Finals/poc/cdc`)에서 쓴 값이다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# pg_subscription_rel.srsubstate: i 초기화, d 복사 중, f 복사 끝, s 동기화됨, r 준비 완료
SubState = Literal["i", "d", "f", "s", "r"]
SyncPhase = Literal["schema", "copy", "catchup", "streaming"]
Direction = Literal["forward", "reverse"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadinessPolicy(Strict):
    """전환 가능 시점 기준(`poc/cdc/scripts/readiness.py`와 같은 값)."""

    # 쓰기 중단은 진행 중인 트랜잭션이 끝나기를 기다리므로, 이보다 긴 트랜잭션이 있으면 기다린다.
    max_xact_ms: int = Field(default=200, ge=0)
    # 이 이하의 슬롯 지연이면 실시간 동기화로 본다(1 MiB).
    lag_ok_bytes: int = Field(default=1_048_576, ge=0)


class PrecheckFacts(Strict):
    """원본 DB 사전 점검 값(`poc/cdc/scripts/precheck.sql`과 같은 항목)."""

    server_version_num: int = Field(ge=0)
    wal_level: str
    max_replication_slots: int = Field(ge=0)
    slots_in_use: int = Field(ge=0)
    # PK가 없고 REPLICA IDENTITY가 default/nothing인 테이블(`schema.table`)
    tables_without_identity: list[str] = Field(default_factory=list)
    sequences: list[str] = Field(default_factory=list)
    large_objects: int = Field(default=0, ge=0)
    unlogged_tables: int = Field(default=0, ge=0)
    extensions: list[str] = Field(default_factory=list)


class PrecheckResult(Strict):
    ok: bool
    blockers: list[str]
    warnings: list[str]
    # 복제를 시작하기 전에 원본에 적용할 조치: (조치 코드, 대상)
    actions: list[tuple[str, str]]


class ReplicationNames(Strict):
    publication: str
    subscription: str
    slot: str


class TableSync(Strict):
    name: str
    state: SubState
    rows_copied: int = Field(default=0, ge=0)
    rows_total: int = Field(default=0, ge=0)


class SyncSample(Strict):
    """대상 구독 상태와 원본 슬롯 지연 한 번의 샘플."""

    tables: list[TableSync]
    # 원본 슬롯의 pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn). 슬롯이 없으면 None.
    lag_bytes: int | None = Field(ge=0)
    source_lsn: str = ""
    target_lsn: str | None = None
    lag_ms: float | None = Field(default=None, ge=0)


class ReadinessSample(Strict):
    """원본(현재 쓰기 주체)의 전환 가능 시점 신호. 이 도구 자신의 접속은 빼고 잰다."""

    longest_xact_ms: int = Field(ge=0)
    lock_waiters: int = Field(ge=0)
    lag_bytes: int | None = Field(ge=0)
    commits_per_sec: float = Field(default=0, ge=0)


class DbEndpoint(Strict):
    """DB 하나에 닿는 방법(비밀값 없음). SQL은 Worker가 이 Pod 안에서 psql로 실행한다(DM-001).

    - KUBECONFIG: Worker 설정 디렉터리의 `<kubeconfig_ref>.yaml`(로컬·온프레미스·시험)
    - AWS_EKS: `aws eks update-kubeconfig`(cluster_name, region)
    - GCP_GKE: gcloud(project_id, cluster_name, location)
    """

    access: Literal["KUBECONFIG", "AWS_EKS", "GCP_GKE"]
    kubeconfig_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]+$")
    cluster_name: str | None = None
    region: str | None = None
    project_id: str | None = None
    location: str | None = None
    namespace: str
    pod: str
    container: str = "postgres"
    # 상대 DB가 이 DB에 접속할 주소(WireGuard 터널 쪽 주소, DM-011)와 DB 이름
    peer_host: str
    peer_port: int = Field(default=5432, ge=1, le=65535)
    dbname: str


class DataMigrationRequest(Strict):
    migration_id: str
    source: DbEndpoint
    target: DbEndpoint
    policy: ReadinessPolicy = Field(default_factory=ReadinessPolicy)
    direction: Direction = "forward"
