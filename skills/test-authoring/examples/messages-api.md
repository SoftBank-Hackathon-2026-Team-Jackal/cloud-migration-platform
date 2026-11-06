# 예시: 메시지 API

## 입력
찾은 경로:
- POST /api/messages (cmd/server.go:12)
- GET /api/messages (cmd/server.go:13)

`get_snippet`으로 읽은 내용: `POST /api/messages`는 `client_msg_id`, `author`, `body`를 받아 새로 만들면 201과 저장된 행(`id` 포함)을, 같은 `client_msg_id`로 다시 보내면 200과 같은 행을 돌려준다. `GET /api/messages`는 `items` 목록과 조회 시각(`served_at`)을 돌려준다.

## 기대 출력
`submit_tests`에 아래를 낸다.

shapes:
- id `messages`, write_path `/api/messages`, write_body `{"client_msg_id": "std-{{run_id}}", "author": "cmp-test", "body": "value-{{run_id}}"}`, write_status 201, id_from_response `id`, value_field `body`, read_back `REPEAT_WRITE`, evidence `cmd/server.go:12`

probes:
- id `messages`, path `/api/messages`, volatile `["served_at"]`, evidence `cmd/server.go:13`

기대값은 한 줄도 쓰지 않는다. 쓰기 모양에서 만든 테스트는 쓴 값을 그대로 읽는지 보고, 읽기 요청은 플랫폼이 기록한 응답과 같은지 본다.
