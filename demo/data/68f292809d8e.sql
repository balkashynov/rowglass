-- Rowglass isolated row-lineage replay. Original triggers, constraints and DDL are not copied.
-- CREATE SCHEMA fails if the destination already exists. Never replay into a production schema.
SET standard_conforming_strings = on;
SET TIME ZONE 'UTC';
BEGIN;
CREATE SCHEMA "rowglass_replay";
CREATE TABLE "rowglass_replay"."table_0" ("id" bigint, "customer" text, "amount" numeric, "status" text, "metadata" jsonb, "updated_at" timestamptz, PRIMARY KEY ("id"));
CREATE TABLE "rowglass_replay"."table_1" ("id" bigint, "order_id" bigint, "amount" numeric, "state" text, PRIMARY KEY ("id"));
INSERT INTO "rowglass_replay"."table_0" ("id", "customer", "amount", "status", "metadata", "updated_at") VALUES ('32', 'customer-32', '40.000032', 'pending', '{"region": "ca"}', '2026-01-01 00:00:00+00');
COMMIT;
-- Source XID 775, commit 0/19BFB20
BEGIN;
UPDATE "rowglass_replay"."table_0" SET "id"='32', "customer"='customer-32', "amount"='40.000033', "status"='paid', "metadata"='{"region": "ca"}', "updated_at"='2026-10-15 12:00:00+00' WHERE "id" IS NOT DISTINCT FROM '32';
INSERT INTO "rowglass_replay"."table_1" ("id", "order_id", "amount", "state") VALUES ('1792089477032', '32', '12.345678', 'captured');
COMMIT;
