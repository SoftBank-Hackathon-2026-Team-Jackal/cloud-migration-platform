# Ticket B-01 — 공통 이미지 빌드와 통과 Digest 재사용

## 상태
TODO — [Issue #17](https://github.com/SoftBank-Hackathon-2026-Team-Jackal/cloud-migration-platform/issues/17). 이번 PR #16에서는 결정과 착수 기준만 기록하며 코드는 구현하지 않는다.

## 결정과 책임

[ADR-003](../../docs/decisions/ADR-003.md)에 따라 `코드 수정 → Build → 같은 Digest로 Sandbox 검증`을 반복하고 통과 Digest를 Production에 재사용한다. 민서 담당 공통 모듈은 **준비된 소스·Dockerfile의 빌드·push·Digest 반환**까지다. Sandbox/변환 담당자가 수정 루프·호출·검증·통과 결정을 맡는다.

## 선행 조건

- [01](01-provider-spec-validation.md)의 이전 내부 v1 계약과 새 정책의 차이를 B-01에서 실제 코드/fixture로 해소한다.
- 빌드 실행 위치·격리/권한, Registry 테스트 환경, Sandbox 담당자의 호출 계약을 확인한다. 기존 Dockerfile의 안전한 빌드 범위로 시작한다.

## 수용 기준

- [ ] 소스·Dockerfile/빌드 설정·플랫폼·Registry/인증 참조·시도 ID를 받아 build/push하고 성공/실패·Digest 고정 URI·플랫폼·출처를 반환한다.
- [ ] 격리된 Builder/Registry에서 실제 build→push→Digest 조회·재시도/응답 유실을 검증한다. 비밀값은 빌드 컨텍스트·이미지 레이어·일반 로그에 넣지 않는다.
- [ ] BuildResult는 Sandbox 결과를 선행 요구하지 않고 SandboxResult는 실제 검증한 Build 참조·이미지 Digest·플랫폼/설정 범위를 기록한다.
- [ ] Planning이 PASSED와 소스/설정·Build/검증/배포 Digest 및 플랫폼 범위를 검사한다. 이전 v1을 새 정책 충족으로 간주하지 않는다.
- [ ] 수정 후 이미지 A 실패 → 새 이미지 B Sandbox 통과 → B 그대로 계획/배포, A 또는 이후 재빌드 C 혼입 차단을 공개 경계에서 검증한다.
- [ ] 같은 Chart의 이미지 교체가 가능한 조건을 검사한다. 포트·환경변수·DB 요구 변경은 Values/환경 재평가를 요구한다.
- [ ] 관련 SDD·회귀 테스트와 실제 검증 증거를 같은 구현 PR에 반영한다.

## 제외

Dockerfile 자동 생성, 임의 언어 자동 지원/코드 수정, Sandbox 검증 루프의 소유권, 실제 Production 배포.

## Test seam

공통 빌드 모듈 공개 입력/결과 + 격리 Registry의 실제 Digest + Planning 계약/불일치 거절. 사용자가 승인한 이미지가 실제 대상에서 Pull/Ready가 되는지는 배포 티켓에서 별도로 검증한다.
