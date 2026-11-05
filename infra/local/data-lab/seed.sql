-- 데모 chat 앱과 비슷한 스키마. audit_log는 PK가 없어 REPLICA IDENTITY 조치 대상이다.
create table if not exists rooms (id bigserial primary key, name text not null);
create table if not exists messages (
  id bigserial primary key, room_id bigint not null references rooms(id),
  body text not null, created_at timestamptz not null default now());
create table if not exists audit_log (at timestamptz not null default now(), what text);
-- 다시 실행해도 데이터가 늘지 않게 비어 있을 때만 넣는다.
insert into rooms(name) select 'room-' || g from generate_series(1, 5) g
  where not exists (select 1 from rooms);
insert into messages(room_id, body) select 1 + g % 5, md5(g::text) from generate_series(1, 20000) g
  where not exists (select 1 from messages);
insert into audit_log(what) select 'seed-' || g from generate_series(1, 100) g
  where not exists (select 1 from audit_log);
