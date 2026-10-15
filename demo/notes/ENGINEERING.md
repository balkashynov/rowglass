# Rowglass: from a replica discrepancy to an executable case

A lag metric says how far a pipeline has progressed. A table comparison says which values differ. Neither necessarily retains the committed source changes needed to explain a particular row. Rowglass records that evidence independently and packages a small, inspectable workload.

The concrete demonstration is deliberately modest in its claim: a known primary-key update limitation reproduced with two of 104 transactions. The technical work is the path from real WAL to durable evidence, typed comparison, preserved transaction lineage and a second real replication run.

## Evidence path

```text
Postgres exported snapshot + pgoutput
              │
              ▼
Python COPY BOTH transport ──► Rust decoder ──► CRC-framed journal + sync
              ▲                                      │
              └──────── durable commit ACK ◄──────────┘
                                                     │
ClickHouse latest versions ──► typed audit ◄── state + source cut
                                     │
                             discrepant row history
                                     │
                             backward dependency slice
                                     │
                         isolated SQL + portable browser report
```

The source and destination are observed through separate connections. Their timestamps are not assumed to define a shared snapshot. In the controlled lab, source writes are locked, the recorder has reached the final commit, and PeerDB's catalog has both synchronized and normalized through that commit. Those conditions accompany the report. The generic snapshot CLI always labels its output as live.

## Where the engineering is

**Bootstrap and durability.** A logical slot exports the snapshot used to copy the baseline. A seal records the consistent starting LSN only after the baseline is durable. The native engine appends committed transactions, syncs the file, then returns the safe ACK. A new file and parent directory are synced. A torn trailing frame can be truncated by the exclusive writer; a complete CRC failure is not silently repaired. The journal has a SHA-256 identity for handoff, not an authenticity signature. See `native/journal.rs`, `native/pgoutput.rs`, `python/rowglass/capture.py`.

**An ACK trap found during implementation.** psycopg2's replication cursor can advance feedback on keepalive processing using the last received position. For a recorder, receipt is not durability. Rowglass uses psycopg2 for connection setup and explicit libpq COPY BOTH reads/writes for streaming. Feedback comes only from the core's durable boundary. A real test generates unrelated WAL and verifies the slot does not acknowledge beyond that evidence. The pinned driver internals are a maintenance risk and a reason to keep this transport narrow. See `python/rowglass/transport.py` and [the driver code](https://github.com/psycopg/psycopg2/blob/2.9.11/psycopg/pqpath.c).

**Type semantics.** Decimal values remain text through transport and become exact canonical numeric strings. JSON numbers are canonicalized structurally without passing through binary floating point. Null, absent rows, unchanged TOAST and unknown values are different states. Timestamp, byte and floating-point comparisons have explicit rules. Unsupported OIDs make equality inconclusive; malformed supported values fail instead of producing a reassuring match. See `native/canonical.rs` and `native/state.rs`.

**Destination semantics.** The adapter ranks rows by `_peerdb_version` per source key, checks visible equal-version candidates, then applies `_peerdb_is_deleted`. Conflicting ties fail the snapshot. Identical retry duplicates are tolerated. A background merge may already have removed historical versions, so this is not an immutable destination log. See `python/rowglass/clickhouse.py`.

**Replay.** Starting at a selected key, scan transactions backward. Include a transaction when it touches a needed key; include every companion write and add both old and new keys to the dependency set. Carry required baseline rows, then emit typed SQL in original commit order. Truncates conservatively retain the affected table's baseline lineage. This preserves observed row dependencies and transaction boundaries, not foreign-key dependencies, triggers, statement predicates or a minimum failure predicate. Selected schema transitions are rejected. See `python/rowglass/replay.py`.

**Memory tradeoff.** The first audit implementation parsed target JSON directly from an unbuffered file and cloned histories for every row. Buffered typed decoding and a second history pass restricted to discrepant keys reduced the measured 100k-row audit from about 4.80 seconds / 640 MiB to 0.444 seconds / 197.6 MiB. The restricted Python reference still finished faster at 0.298 seconds. Current value is the evidence workflow, not a universal speed claim. See `tests/benchmark.py` and [the full method](VALIDATION.md).

## A useful next adoption experiment

Give a replication engineer one saved case and ask whether the history, source cut and scratch SQL are sufficient to investigate without access to the original source. Measure missing information and time to reproduce. If useful, the next engineering steps are a disk-backed state index, explicit DDL contracts, report redaction, native transport replacing driver internals, and a destination barrier API with a precise implementation-specific contract.

This experiment has not happened yet. There are no external users, hiring endorsements or production reliability claims. The code and recorded evidence are ready for that technical review.

## Prior work and fit

[PeerDB](https://github.com/PeerDB-io/peerdb) is the replication engine used in the lab. [ClickHouse's PostgreSQL FAQ](https://clickhouse.com/docs/integrations/clickpipes/postgres/faq#can-i-update-primary-key-columns-in-postgresql) describes the default key-update behavior. [data-diff](https://github.com/datafold/data-diff) is prior art for cross-database comparison; its public repository is archived. Rowglass is not claiming to invent table comparison, event sourcing or workload reduction. Its specific integration is independent committed evidence, an uncertainty-aware replica audit, and an executable row-lineage handoff.
