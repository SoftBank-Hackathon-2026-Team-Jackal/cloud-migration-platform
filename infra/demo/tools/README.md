# 데모 도구 (부하·장애 주입·전환 시연·원본 DB 시드)

데모 서버와 중계 서버를 **운영자 PC에서** 다루는 스크립트 모음이다. 서비스 코드가 아니고, 발표·리허설용이다. 비밀값은 다루지 않는다(AWS는 SSO 프로필로 접속).

## 준비

- Windows Git Bash + AWS CLI v2(`C:\Program Files\Amazon\AWSCLIV2\aws.exe`). 다른 경로면 스크립트 윗부분의 `AWS=`를 고친다.
- `aws sso login --profile cmp-aws-prod` (계정 714066696240, 서울).
- 서버 ID는 스크립트에 박혀 있다: 데모 서버 `i-091464203346381b5`, 중계 서버 `i-017c5ae4b2c8894d8`. 서버를 새로 만들면 고친다.

## 스크립트

| 파일 | 용도 |
|---|---|
| `demo-load.sh` | 중계 서버에서 부하 생성(`start`·`stop`·`status`, 초당 20건·10분 뒤 자동 종료), 장애 주입(`fault block\|reject\|loss [초]`, `fault clear\|status`), 데모 앱 AI 채팅 에이전트 안내(`bots`) |
| `demo-traffic-shift.sh` | 트래픽 전환 라이브 시연(`forward`: 10→50→100%, `rollback`: 50%에서 취소 신호, `status`, `reset`: 부하 끄고 원본 100%) |
| `seed/seed.sql`, `seed/unseed.sql` | 데모 원본 DB(`chat-source/chat-db-0`)에 화면용 표 10개·약 5.7만 행을 넣고 지움 |

`DRY_RUN=1`을 붙이면 `demo-load.sh`가 실제로 보내지 않고 명령만 출력한다.

## 장애 주입의 안전장치

- 대상은 `FAULT_TARGET`(기본 `10.200.0.4`, 온프레 앱의 터널 주소)로 가는 길뿐이다(중계 서버의 iptables).
- **최대 300초 뒤 자동 해제**된다. 바로 풀려면 `fault clear`.
- 중계 서버에 `tc`가 없어 지연 주입은 못 하고, 패킷 일부 손실(`LOSS=1~99`)로 대신한다.
- 실서버 시험: `fault block 10`으로 대상 헬스가 `failed_active_hc`로 바뀌고 자동 해제 뒤 복구되는 것을 확인했다.

## 시드(원본 DB 화면용 데이터)

```bash
kubectl -n chat-source exec -i chat-db-0 -- psql -U chat -d chat -v ON_ERROR_STOP=1 < seed/seed.sql
# 되돌리기
kubectl -n chat-source exec -i chat-db-0 -- psql -U chat -d chat -v ON_ERROR_STOP=1 < seed/unseed.sql
```

- **데이터 이전을 시작하기 전에만** 실행한다(복제 도중에 표를 만들면 대상에 표가 없어 막힌다). 이전이 도는 동안 `unseed.sql`도 실행하지 않는다.
- 규모는 `-v scale=2`처럼 조절한다(기본 1).
- 이 스크립트가 만든 표에는 `app_seed_info` 표시가 있어, 같은 이름의 다른 표가 있으면 아무것도 하지 않고 멈춘다.
- 데모 서버의 워커 컨테이너에서 데이터 이전용 역할로 `kubectl exec` 하면 이 namespace에서는 쓰기도 된다.

## 주의

- 전환 시연은 사용자가 없는 데모 서버 기준이다. 실제 운영 대상에 쓰지 않는다.
- 트래픽 단계는 최소 요청 수(데모 설정 10건)가 들어와야 올라가므로 전환 직전에 `demo-load.sh start`를 한다.
