-- Rowglass isolated row-lineage replay. Original triggers, constraints and DDL are not copied.
-- CREATE SCHEMA fails if the destination already exists. Never replay into a production schema.
SET standard_conforming_strings = on;
SET TIME ZONE 'UTC';
BEGIN;
CREATE SCHEMA "rowglass_replay_key_1792089480330238000";
CREATE TABLE "rowglass_replay_key_1792089480330238000"."table_0" ("id" bigint, "customer" text, "amount" numeric, "status" text, "metadata" jsonb, "updated_at" timestamptz, PRIMARY KEY ("id"));
CREATE TABLE "rowglass_replay_key_1792089480330238000"."table_1" ("id" bigint, "order_id" bigint, "amount" numeric, "state" text, PRIMARY KEY ("id"));
INSERT INTO "rowglass_replay_key_1792089480330238000"."table_0" ("id", "customer", "amount", "status", "metadata", "updated_at") VALUES ('60', 'customer-60', '75.000060', 'pending', '{"region": "ca"}', '2026-01-01 00:00:00+00');
COMMIT;
-- Source XID 803, commit 0/19C27F0
BEGIN;
UPDATE "rowglass_replay_key_1792089480330238000"."table_0" SET "id"='60', "customer"='customer-60', "amount"='75.000061', "status"='paid', "metadata"='{"region": "ca"}', "updated_at"='2026-10-15 12:00:00+00' WHERE "id" IS NOT DISTINCT FROM '60';
INSERT INTO "rowglass_replay_key_1792089480330238000"."table_1" ("id", "order_id", "amount", "state") VALUES ('1792089477060', '60', '12.345678', 'captured');
COMMIT;
-- Source XID 847, commit 0/19C6C80
BEGIN;
UPDATE "rowglass_replay_key_1792089480330238000"."table_0" SET "id"='2060', "customer"='customer-60', "amount"='75.000061', "status"='paid', "metadata"='{"region": "ca"}', "updated_at"='2026-10-15 12:00:00+00' WHERE "id" IS NOT DISTINCT FROM '60';
COMMIT;
