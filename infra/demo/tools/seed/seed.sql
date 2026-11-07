-- 데모 원본 DB(chat-source/chat-db-0, DB·사용자 chat)에 화면용 테이블 10개와 샘플 데이터를 넣는다.
-- 실행: kubectl -n chat-source exec -i chat-db-0 -- psql -U chat -d chat -v ON_ERROR_STOP=1 < seed.sql
-- 규모 조절: -v scale=2 처럼 주면 행 수가 배로 늘어난다(기본 1, 약 57,000행). 소수도 된다(-v scale=0.5).
-- 반드시 데이터 이전을 시작하기 전에 실행한다(복제 도중에 표를 만들면 대상에 표가 없어 복제가 막힌다).
-- 안전장치: 같은 이름의 표가 이미 있고 이 스크립트가 만든 것이 아니면 아무것도 하지 않고 멈춘다. 다시 실행해도 안전하다.
-- 되돌리기: unseed.sql

\if :{?scale}
\else
  \set scale 1
\endif

BEGIN;

-- 이 스크립트가 만든 표인지 알아보는 표시. 있으면 이미 넣은 것으로 보고 건너뛴다.
DO $$
DECLARE
  names text[] := ARRAY['categories','users','addresses','products','orders','order_items','payments','reviews','events','audit_logs'];
  found text;
BEGIN
  IF to_regclass('public.app_seed_info') IS NOT NULL THEN
    RAISE NOTICE 'app_seed_info가 있어 이미 넣은 것으로 보고 건너뜁니다(되돌리려면 unseed.sql).';
    RETURN;
  END IF;
  SELECT string_agg(n, ', ') INTO found FROM unnest(names) AS n WHERE to_regclass('public.' || n) IS NOT NULL;
  IF found IS NOT NULL THEN
    RAISE EXCEPTION '이미 같은 이름의 표가 있습니다(%). 아무것도 바꾸지 않고 멈춥니다.', found;
  END IF;
END $$;

SELECT to_regclass('public.app_seed_info') IS NULL AS fresh \gset
\if :fresh

SELECT setseed(0.42);

CREATE TABLE app_seed_info (id int PRIMARY KEY, note text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO app_seed_info VALUES (1, '화면용 샘플 데이터(seed.sql)');

CREATE TABLE categories (id int PRIMARY KEY, name text NOT NULL, parent_id int);
CREATE TABLE users (
  id int PRIMARY KEY, email text NOT NULL, name text NOT NULL, status text NOT NULL, created_at timestamptz NOT NULL
);
CREATE TABLE addresses (
  id int PRIMARY KEY, user_id int NOT NULL, city text NOT NULL, postcode text NOT NULL, is_default boolean NOT NULL
);
CREATE TABLE products (
  id int PRIMARY KEY, category_id int NOT NULL, sku text NOT NULL, name text NOT NULL,
  price_cents int NOT NULL, stock int NOT NULL, created_at timestamptz NOT NULL
);
CREATE TABLE orders (
  id int PRIMARY KEY, user_id int NOT NULL, status text NOT NULL, total_cents int NOT NULL, created_at timestamptz NOT NULL
);
CREATE TABLE order_items (
  id int PRIMARY KEY, order_id int NOT NULL, product_id int NOT NULL, qty int NOT NULL, unit_price_cents int NOT NULL
);
CREATE TABLE payments (
  id int PRIMARY KEY, order_id int NOT NULL, method text NOT NULL, amount_cents int NOT NULL, status text NOT NULL, paid_at timestamptz NOT NULL
);
CREATE TABLE reviews (
  id int PRIMARY KEY, user_id int NOT NULL, product_id int NOT NULL, rating int NOT NULL, body text NOT NULL, created_at timestamptz NOT NULL
);
CREATE TABLE events (
  id bigint PRIMARY KEY, user_id int NOT NULL, kind text NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL
);
CREATE TABLE audit_logs (
  id bigint PRIMARY KEY, actor text NOT NULL, action text NOT NULL, target text NOT NULL, created_at timestamptz NOT NULL
);

-- 개수(scale 반영, 최소 1)
SELECT greatest(1, round(20 * :scale)::int)    AS n_categories,
       greatest(1, round(2000 * :scale)::int)  AS n_users,
       greatest(1, round(2500 * :scale)::int)  AS n_addresses,
       greatest(1, round(500 * :scale)::int)   AS n_products,
       greatest(1, round(8000 * :scale)::int)  AS n_orders,
       greatest(1, round(20000 * :scale)::int) AS n_items,
       greatest(1, round(8000 * :scale)::int)  AS n_payments,
       greatest(1, round(3000 * :scale)::int)  AS n_reviews,
       greatest(1, round(10000 * :scale)::int) AS n_events,
       greatest(1, round(3000 * :scale)::int)  AS n_audit \gset

INSERT INTO categories
SELECT g, 'category-' || g, CASE WHEN g > 5 THEN 1 + (g % 5) END FROM generate_series(1, :n_categories) g;

INSERT INTO users
SELECT g, 'user' || g || '@example.com', 'User ' || g,
       (ARRAY['active','active','active','inactive','pending'])[1 + (g % 5)],
       now() - (g % 700 || ' days')::interval - (g % 86400 || ' seconds')::interval
FROM generate_series(1, :n_users) g;

INSERT INTO addresses
SELECT g, 1 + (g % :n_users),
       (ARRAY['Seoul','Busan','Incheon','Daegu','Daejeon','Gwangju','Ulsan','Suwon'])[1 + (g % 8)],
       lpad(((g * 37) % 100000)::text, 5, '0'), (g % 3 = 0)
FROM generate_series(1, :n_addresses) g;

INSERT INTO products
SELECT g, 1 + (g % :n_categories), 'SKU-' || lpad(g::text, 6, '0'), 'Product ' || g,
       500 + ((g * 137) % 99500), (g * 7) % 500,
       now() - (g % 400 || ' days')::interval
FROM generate_series(1, :n_products) g;

INSERT INTO orders
SELECT g, 1 + (g % :n_users),
       (ARRAY['paid','paid','paid','shipped','delivered','cancelled'])[1 + (g % 6)],
       1000 + ((g * 211) % 200000),
       now() - (g % 365 || ' days')::interval - (g % 86400 || ' seconds')::interval
FROM generate_series(1, :n_orders) g;

INSERT INTO order_items
SELECT g, 1 + (g % :n_orders), 1 + ((g * 13) % :n_products), 1 + (g % 4), 500 + ((g * 137) % 99500)
FROM generate_series(1, :n_items) g;

INSERT INTO payments
SELECT g, g, (ARRAY['card','card','card','transfer','point'])[1 + (g % 5)],
       1000 + ((g * 211) % 200000), (ARRAY['captured','captured','captured','refunded'])[1 + (g % 4)],
       now() - (g % 365 || ' days')::interval
FROM generate_series(1, :n_payments) g;

INSERT INTO reviews
SELECT g, 1 + (g % :n_users), 1 + ((g * 7) % :n_products), 1 + (g % 5),
       'Review text ' || g || ' ' || md5(g::text), now() - (g % 300 || ' days')::interval
FROM generate_series(1, :n_reviews) g;

INSERT INTO events
SELECT g, 1 + (g % :n_users), (ARRAY['page_view','add_to_cart','checkout','login','search'])[1 + (g % 5)],
       jsonb_build_object('session', md5((g % 997)::text), 'seq', g), now() - (g % 30 || ' days')::interval - (g % 86400 || ' seconds')::interval
FROM generate_series(1, :n_events) g;

INSERT INTO audit_logs
SELECT g, (ARRAY['system','admin','batch'])[1 + (g % 3)],
       (ARRAY['create','update','delete','login'])[1 + (g % 4)], 'target-' || (g % 500),
       now() - (g % 90 || ' days')::interval - (g % 86400 || ' seconds')::interval
FROM generate_series(1, :n_audit) g;

ANALYZE;
\endif

COMMIT;

-- 확인
SELECT 'categories' AS tbl, count(*) FROM categories UNION ALL
SELECT 'users', count(*) FROM users UNION ALL
SELECT 'addresses', count(*) FROM addresses UNION ALL
SELECT 'products', count(*) FROM products UNION ALL
SELECT 'orders', count(*) FROM orders UNION ALL
SELECT 'order_items', count(*) FROM order_items UNION ALL
SELECT 'payments', count(*) FROM payments UNION ALL
SELECT 'reviews', count(*) FROM reviews UNION ALL
SELECT 'events', count(*) FROM events UNION ALL
SELECT 'audit_logs', count(*) FROM audit_logs;
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;
