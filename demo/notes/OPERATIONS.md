# Running Rowglass

## Local lab

Use the README setup from a source checkout. The Compose project is named `rowglass-lab`; all published ports bind to loopback. PostgreSQL source is 55432, catalog 55433, ClickHouse HTTP 58123/native 59000, PeerDB SQL 59900 and flow API 58113. Ports must be free. Docker needs roughly 8 GB RAM; initial image pulls require network access.

The lab uses public, deliberately weak development credentials in its Compose files. Do not expose it to a network or reuse those credentials elsewhere. It includes Temporal, a MinIO-compatible store, PeerDB services and databases. Images and service topology are recorded in the checked-in Compose files. Some version tags remain mutable; `evidence/container-images.json` records the actual images used for verification when available.

`lab/bootstrap.py` creates source tables, a publication, PeerDB peers and a mirror. `lab/scenarios.py` pauses only the lab worker, mutates synthetic source/destination data, obtains source locks, performs audits, restores injected target faults and verifies a separate replay. It is a fresh-lab experiment, not an idempotent migration.

Stop while preserving volumes:

```sh
docker compose -f compose.yaml -f lab/pipeline.yaml stop
```

To deliberately discard this lab and start over:

```sh
docker compose -f compose.yaml -f lab/pipeline.yaml down -v
```

That command deletes the Rowglass lab volumes. It is not part of the default verification commands. Capture files under `runs/` remain on the host. Avoid running another copy with the same Compose project name.

## Record an existing development pipeline

Use a dedicated PostgreSQL role with logical replication access, SELECT on recorded tables and the permissions needed to create its own slot. Creating a publication additionally requires the relevant database/table ownership privileges. Source `wal_level` must be logical, with enough slots/senders. Do not borrow the replication engine's slot.

Copy `lab/config.json` and change the source and target table mapping, publication, slot and ClickHouse database. A source table name must be an explicit `schema.table`; identifiers containing dots are unsupported. Place connection strings in environment variables, not the JSON or report:

```sh
export ROWGLASS_SOURCE_DSN='host=your-host dbname=your-db user=your-user sslmode=verify-full'
export ROWGLASS_CLICKHOUSE_PASSWORD='your-password'
export ROWGLASS_CLICKHOUSE_URL='https://your-clickhouse-host:8443'
export ROWGLASS_CLICKHOUSE_USER='your-user'
# Create only the dedicated publication if it does not already exist:
.venv/bin/rowglass setup config.json
.venv/bin/rowglass record config.json evidence.rg --seconds 120
.venv/bin/rowglass snapshot config.json evidence.rg target.json
.venv/bin/rowglass audit evidence.rg target.json > audit.json
.venv/bin/rowglass report evidence.rg audit.json report
```

The environment variable names can be overridden with `clickhouse.url_env`, `clickhouse.user_env` and `clickhouse.password_env`. The adapter defaults to the local lab URL/user; configure both for another server and use HTTPS. The snapshot command reads target rows but does not write them. `record` creates a logical slot and may retain source WAL when stopped. PostgreSQL connection credentials use normal libpq facilities; never paste real secrets into shareable artifacts.

## Recovery and cleanup

Restart `record` with the same config and journal to resume. Preserve evidence on failure. An incomplete baseline, source timeline change, lost WAL or server slot ahead of the journal requires investigation and a new explicitly chosen recording. Do not delete the old journal and silently reuse its slot.

A stopped recorder leaves its slot in place for restart. Inspect `pg_replication_slots`, monitor retained WAL and set a source retention budget. When a recording is permanently finished and its slot is inactive, an authorized operator can drop that specific slot using `pg_drop_replication_slot('your_rowglass_slot')` and drop the dedicated publication. Never drop the pipeline's slot. Rowglass does not automate these destructive operations.

`inspect` validates the journal and summarizes its source cut. `export --at LSN` reconstructs an earlier committed cut (row output is capped at 2,000 per table). `history` accepts a table and the exact canonical key string from an audit. `replay` writes a new directory with SQL and a manifest:

```sh
.venv/bin/rowglass replay evidence/pipeline/evidence.rg public.orders '["number:6e1"]' runs/key-replay
```

Review generated SQL, then execute it only in a scratch PostgreSQL database. Creating the schema fails if it already exists. A separate fresh replica is needed to reproduce destination behavior; the lab script contains that complete experiment.

## Shareable artifacts

`python scripts/package.py` builds source and offline-demo ZIPs under `dist/` with SHA-256 checksums. The package uses an explicit source allowlist and excludes `.venv`, build outputs, runs, local contact notes and Git history. Reports contain row values, source identifiers and transaction metadata. The provided demo is synthetic and reviewable; your own recordings need a deliberate privacy review and redaction before sharing. Nothing in these commands publishes a website or contacts anyone.
