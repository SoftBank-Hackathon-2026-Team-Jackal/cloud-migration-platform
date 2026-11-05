# Ticket 02 — kubectl exec psql 실행기와 사전 점검·스키마·구독 Activity

## 상태

DRAFT — 사용자 검토 전. 복제 경로는 WireGuard(ticket 06), Q3(원본 접근 참조) 결정 필요

## 목적

Worker가 원본·대상 DB Pod 안에서 SQL을 실행해 사전 점검, 스키마 복사, publication·subscription 생성을 한다(DM-001).

## 포함 범위

- `PsqlPort`(SQL → 행) 인터페이스와 `kubectl exec` 구현. 비밀번호를 명령줄·로그에 남기지 않는다.
- `dm_precheck`(precheck SQL → `PrecheckFacts` → `evaluate_precheck`), REPLICA IDENTITY FULL 조치 적용
- `dm_copy_schema`(`pg_dump --schema-only` 원본 Pod → 대상 Pod `psql`), marker 테이블
- `dm_subscribe`(이름 고정, 있으면 건너뜀)

## 수용 기준(초안)

- 가짜 `PsqlPort`로 Activity 결과와 멱등성(재실행 시 생성 없음)을 시험한다.
- 실제 EKS chat-source → 대상 클러스터에서 구독이 만들어지고 초기 복사가 시작된다.
