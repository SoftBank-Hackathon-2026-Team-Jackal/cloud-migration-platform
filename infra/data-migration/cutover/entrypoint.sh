#!/bin/sh
# 처음 시작할 때만 Secret의 연결 대상(route)을 runtime 파일로 복사한다. 전환 뒤에는 Activity가
# Secret도 새 대상으로 바꾸므로, Pod를 다시 만들어도 전환 전 DB로 돌아가지 않는다.
set -e
RUNTIME=/etc/pgbouncer/runtime/databases.ini
if [ ! -f "$RUNTIME" ]; then
  printf '[databases]\n%s\n' "$(cat /etc/pgbouncer/secret/route)" > "$RUNTIME"
fi
exec pgbouncer /etc/pgbouncer/pgbouncer.ini
