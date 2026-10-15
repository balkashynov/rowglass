-- Rowglass isolated row-lineage replay. Original triggers, constraints and DDL are not copied.
-- CREATE SCHEMA fails if the destination already exists. Never replay into a production schema.
SET standard_conforming_strings = on;
SET TIME ZONE 'UTC';
BEGIN;
CREATE SCHEMA "rowglass_replay";
CREATE TABLE "rowglass_replay"."table_0" ("id" bigint, "customer" text, "amount" numeric, "status" text, "metadata" jsonb, "updated_at" timestamptz, PRIMARY KEY ("id"));
CREATE TABLE "rowglass_replay"."table_1" ("id" bigint, "order_id" bigint, "amount" numeric, "state" text, PRIMARY KEY ("id"));
INSERT INTO "rowglass_replay"."table_0" ("id", "customer", "amount", "status", "metadata", "updated_at") VALUES ('21', 'customer-21', '26.250021', 'pending', '{"region": "us"}', '2026-01-01 00:00:00+00');
COMMIT;
-- Source XID 764, commit 0/19BE998
BEGIN;
UPDATE "rowglass_replay"."table_0" SET "id"='21', "customer"='customer-21', "amount"='26.250022', "status"='paid', "metadata"='{"region": "us"}', "updated_at"='2026-10-15 12:00:00+00' WHERE "id" IS NOT DISTINCT FROM '21';
INSERT INTO "rowglass_replay"."table_1" ("id", "order_id", "amount", "state") VALUES ('1792089477021', '21', '12.345678', 'captured');
COMMIT;
