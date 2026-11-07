-- seed.sql이 만든 표만 지운다(app_seed_info가 있을 때만). 채팅 앱의 표는 건드리지 않는다.
-- 실행: kubectl -n chat-source exec -i chat-db-0 -- psql -U chat -d chat -v ON_ERROR_STOP=1 < unseed.sql
-- 데이터 이전이 돌고 있는 동안에는 실행하지 않는다(발행·구독이 표를 기다리고 있을 수 있다).

BEGIN;
DO $$
BEGIN
  IF to_regclass('public.app_seed_info') IS NULL THEN
    RAISE NOTICE 'app_seed_info가 없어 지울 것이 없습니다.';
    RETURN;
  END IF;
  DROP TABLE IF EXISTS audit_logs, events, reviews, payments, order_items, orders, products, addresses, users, categories, app_seed_info;
END $$;
COMMIT;

SELECT count(*) AS remaining FROM pg_tables
WHERE schemaname = 'public' AND tablename IN ('categories','users','addresses','products','orders','order_items','payments','reviews','events','audit_logs','app_seed_info');
