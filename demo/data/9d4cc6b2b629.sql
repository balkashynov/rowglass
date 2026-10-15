-- Rowglass isolated row-lineage replay. Original triggers, constraints and DDL are not copied.
-- CREATE SCHEMA fails if the destination already exists. Never replay into a production schema.
SET standard_conforming_strings = on;
SET TIME ZONE 'UTC';
BEGIN;
CREATE SCHEMA "rowglass_replay";
CREATE TABLE "rowglass_replay"."table_0" ("id" bigint, "customer" text, "amount" numeric, "status" text, "metadata" jsonb, "updated_at" timestamptz, PRIMARY KEY ("id"));
CREATE TABLE "rowglass_replay"."table_1" ("id" bigint, "order_id" bigint, "amount" numeric, "state" text, PRIMARY KEY ("id"));
INSERT INTO "rowglass_replay"."table_0" ("id", "customer", "amount", "status", "metadata", "updated_at") VALUES ('42', 'customer-42', '52.500042', 'pending', '{"region": "ca"}', '2026-01-01 00:00:00+00');
COMMIT;
-- Source XID 785, commit 0/19C0B28
BEGIN;
UPDATE "rowglass_replay"."table_0" SET "id"='42', "customer"='customer-42', "amount"='52.500043', "status"='paid', "metadata"='{"region": "ca"}', "updated_at"='2026-10-15 12:00:00+00' WHERE "id" IS NOT DISTINCT FROM '42';
INSERT INTO "rowglass_replay"."table_1" ("id", "order_id", "amount", "state") VALUES ('1792089477042', '42', '12.345678', 'captured');
COMMIT;
-- Source XID 844, commit 0/19C6980
BEGIN;
UPDATE "rowglass_replay"."table_0" SET "id"='42', "customer"='customer-42', "amount"='9007199254740993.123456', "status"='paid', "metadata"='{"region": "ca"}', "updated_at"='2026-10-15 12:00:00+00' WHERE "id" IS NOT DISTINCT FROM '42';
UPDATE "rowglass_replay"."table_1" SET "id"='1792089477042', "order_id"='42', "amount"='12.345678', "state"='refunded' WHERE "id" IS NOT DISTINCT FROM '1792089477042';
COMMIT;
-- Source XID 845, commit 0/19C6A88
BEGIN;
UPDATE "rowglass_replay"."table_0" SET "id"='42', "customer"='customer-42', "amount"='9007199254740993.123456', "status"='refunded', "metadata"='{"region": "ca"}', "updated_at"='2026-10-15 12:00:00+00' WHERE "id" IS NOT DISTINCT FROM '42';
COMMIT;
