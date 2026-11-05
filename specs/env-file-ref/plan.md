# Plan: .env 단일 참조와 공통 환경변수 해석

명세: [spec.md](spec.md). 작업 목록: [tasks.md](tasks.md).

## 흐름

```text
웹 화면 → 웹 서버(BFF, 운영자 키) → POST /env-files            (ENV-01)
                                    ← env_file_ref{id, version, sha256}
웹 서버 → POST /migrations {…, env_file_ref}                     (ENV-02)
MigrationWorkflow
  ├─ 분석: resolve_env_names(env_file_ref) + 코드 대조 → EnvPlan   (ENV-05)
  ├─ 샌드박스: render_env(EnvPlan, env_file_ref, 샌드박스 DB 출력)   (ENV-06)
  └─ 구축: render_env(EnvPlan, env_file_ref, 대상 DB 출력)          (ENV-04)
```

## 배치

| 구성 | 위치(제안) | 담당 |
| --- | --- | --- |
| 계약: `EnvPlanEntry`, `EnvPlan`, `RenderedEnv`, `EnvDecision` | `packages/contracts` (`cmp_contracts.env_plan`) | 민서 |
| 저장소 읽기 인터페이스 `EnvFileStore`(read_names·read_values)와 InMemory 구현. 쓰기·삭제는 ENV-01 구현 몫 | `packages/providers` (`cmp_providers.env_file`) | 민서 |
| `resolve_env_names`, `render_env`, DB 연결 Secret 이름·데이터 | `cmp_providers.env_file` | 민서 |
| 분석용 EnvPlan 빌더 `build_env_plan`(편의 함수, 분석 모듈 미수정) | `cmp_providers.env_plan_builder` | 민서(최종 규칙은 박승철님) |
| 업로드·삭제 API, 암호화 저장 구현 | `apps/api` | 민서 |
| `MigrationRequest.env_file_ref`와 Workflow 전달 | `cmp_contracts.migration`, `apps/worker` Migration Workflow | 민서(#60과 조율) |
| 구축 렌더 전환 | `cmp_worker.plan_publish`, `k8s_workloads`, ON_PREM 작업 | 민서 |
| 분석 EnvPlan 출력 | 분석 Activity·`cmp_scanners.assembly` | 박승철님 |
| 샌드박스 렌더 전환 | `cmp_worker.sandbox_deploy` | 샌드박스 담당 |

## 설계 메모

- 값을 읽는 함수는 `render_env` 하나다. 분석은 `resolve_env_names`만 쓴다(값을 읽을 권한이 필요 없다).
- `render_env`는 순수 함수에 가깝게 둔다. 저장소에서 값을 읽는 부분만 주입한다(`EnvFileStore`). 실제 클러스터에 Secret을 만드는 것은 호출하는 실행 주체(샌드박스 Activity, 구축 Activity, ON_PREM Agent)의 몫이다.
- ON_PREM은 Agent가 클러스터 안에서 Secret을 만든다. 값은 작업 본문에 넣지 않고 Agent 인증 API로 작업별로 받는다(구체 경로는 ENV-04에서 결정).
- Secret 이름 규칙: 앱 env는 `<앱 release>-env`, DB 출력은 기존 `<DB release>-auth`(비밀번호)·`<DB release>-url`(19번 URL)을 쓴다. `host`·`port`·`name`·`username`은 새 DB 연결 Secret `<DB release>-conn`의 같은 이름 키로 참조한다(ENV-03 D3 확정). 이 Secret은 ENV-04·ENV-06이 `connection_secret_data(outputs)`로 만든다.
- EnvPlan은 분석 결과 산출물로 저장하고, 승인 대상 Plan의 입력 Digest에 포함한다(승인 후 변경 감지).

## 검증

- 단위: InMemory 저장소로 `resolve_env_names`·`render_env`의 분류별 결과, 같은 입력의 바이트 동일성, 결과·repr·로그에 값이 없는지.
- 통합: 기존 가짜 클러스터(09·11·12)로 구축이 미리 만든 Secret 없이 성공하는지.
- 계약: 웹이 쓰는 `EnvExceptions`가 EnvPlan에서 같은 값으로 계산되는지.
