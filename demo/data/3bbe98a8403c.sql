-- Rowglass isolated row-lineage replay. Original triggers, constraints and DDL are not copied.
-- CREATE SCHEMA fails if the destination already exists. Never replay into a production schema.
SET standard_conforming_strings = on;
SET TIME ZONE 'UTC';
BEGIN;
CREATE SCHEMA "rowglass_replay";
CREATE TABLE "rowglass_replay"."table_0" ("id" bigint, "customer" text, "amount" numeric, "status" text, "metadata" jsonb, "updated_at" timestamptz, PRIMARY KEY ("id"));
CREATE TABLE "rowglass_replay"."table_1" ("id" bigint, "order_id" bigint, "amount" numeric, "state" text, PRIMARY KEY ("id"));
INSERT INTO "rowglass_replay"."table_0" ("id", "customer", "amount", "status", "metadata", "updated_at") VALUES ('35', 'customer-35', '43.750035', 'pending', '{"region": "us"}', '2026-01-01 00:00:00+00');
COMMIT;
-- Source XID 778, commit 0/19BFFE8
BEGIN;
UPDATE "rowglass_replay"."table_0" SET "id"='35', "customer"='customer-35', "amount"='43.750036', "status"='paid', "metadata"='{"region": "us"}', "updated_at"='2026-10-15 12:00:00+00' WHERE "id" IS NOT DISTINCT FROM '35';
INSERT INTO "rowglass_replay"."table_1" ("id", "order_id", "amount", "state") VALUES ('1792089477035', '35', '12.345678', 'captured');
COMMIT;
