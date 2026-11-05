# cmp-cutover 이미지: PgBouncer(쓰기 중단·연결 대상 변경, DM-010)와 전환 실행기(switchover.py).
# 같은 이미지로 Pod 안에 컨테이너 두 개(pgbouncer, runner)를 띄운다.
FROM alpine:3.22
RUN apk add --no-cache pgbouncer postgresql16-client python3 py3-psycopg \
 && adduser -D -H -u 10001 cmp && mkdir -p /etc/pgbouncer/runtime && chown -R cmp /etc/pgbouncer/runtime
COPY pgbouncer.ini /etc/pgbouncer/pgbouncer.ini
COPY entrypoint.sh /usr/local/bin/cmp-pgbouncer
COPY switchover.py /opt/cmp/switchover.py
RUN chmod +x /usr/local/bin/cmp-pgbouncer
EXPOSE 6432
CMD ["cmp-pgbouncer"]
USER 10001
