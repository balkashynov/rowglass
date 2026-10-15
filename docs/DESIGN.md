# Design contracts and limitations

Rowglass 0.1 consists of a native Rust evidence process, Python source/destination adapters and replay/report commands, and a static browser investigation. [The engineering brief](ENGINEERING.md) explains the mechanisms; this document specifies the boundaries.

## Journal and source

`ROWGLS01` is followed by frames containing little-endian u32 length, CRC32 and UTF-8 JSON. Entries are start identity, schema, baseline rows, baseline seal and committed transaction. The format is versioned but no migration compatibility is promised. Maximum frame size is 128 MiB; a pending decoded transaction is limited to 64 MiB. Current state is reconstructed in memory. Replay loads journals up to 256 MiB.

The source contract is PostgreSQL 17 as tested, pgoutput protocol v1, UTF-8 text tuples, ordinary non-partitioned tables with explicit stable primary keys, and a fixed publication containing all configured columns and rows for insert/update/delete/truncate. Relation/type metadata is preserved. The recorder requests UTC, ISO dates, hex bytea and sufficient floating-point precision.

An exported snapshot supplies the baseline. ACK never intentionally exceeds the last synchronized journal commit. Process termination and restart are tested; physical power loss, storage firmware failure and network partitions at every protocol boundary are not exhaustively tested. An exclusive writer lock prevents two local writers. Slots must belong exclusively to this recorder.

Resume checks system ID, database, timeline, publication, table configuration, slot plugin and retained WAL. Missing/lost WAL, a server slot ahead of durable evidence or an unfinished bootstrap fail explicitly. There is no silent rebootstrap, failover repair, automatic slot deletion or indefinite reconnect supervisor.

DDL is not an event stream in this implementation. Changed relation messages update observed row schemas. Added columns can leave older rows unknown until observed again. Replay rejects selected schema transitions. Changes to primary-key definitions, replica identity, publication membership or filters while recording are outside the contract; start a separate recording. A schema change followed by all-row updates is tested, not general schema migration correctness.

## Typed comparison

Common integers/OID, exact numerics, JSON/JSONB, booleans, text, UUID, bytes, dates, timestamps and floating-point values have explicit canonical rules. JSON duplicate-key semantics follow parsed objects; this is semantic comparison, not source-byte identity. Unsupported types remain unverifiable. Finite supported timestamp representations are parsed; unusual ranges or infinity may fail explicitly. Text comparison is byte-oriented after UTF-8 decoding, not a general collation equivalence engine.

A target observation must contain exactly one live semantic row per key after version resolution. The ClickHouse adapter examines visible maximum versions and tombstones. It cannot recover conflicts already removed by merges, infer arbitrary transforms, or establish a distributed snapshot across shards. Target reads are sequential across tables.

A controlled comparison requires externally established quiescence and drain conditions. JSON declaring such conditions is a caller assertion, not a proof Rowglass can cryptographically verify. The lab establishes and records these conditions; the public snapshot command always emits a live observation. Unsupported fields or incomplete schema evidence prevent full equality claims. A discrepancy at a controlled cut is still not proof of permanent loss.

## Replay and presentation

Replay preserves whole selected transactions and observed predecessor rows. It maps tables into a fresh `rowglass_replay...` schema, retains primary keys, and fails if the schema exists. It does not drop or overwrite tables. It omits foreign keys, non-primary unique constraints, triggers, indexes, typmods, original statement predicates and application side effects. Execute only in a scratch database.

A replay covers the complete supplied captured journal, which can extend beyond an earlier audit observation. Its manifest exposes its source cut. It is a backward lineage slice; no automated failure-predicate search establishes mathematical minimality. The separate key-change experiment explicitly executes the extracted SQL through a fresh PeerDB mirror.

Reports prebuild replays for at most 30 discrepancies per audit; remaining keys can be compiled using the CLI. The UI explores recorded JSON and SQL offline, with exact decimal values kept as strings. Reports may contain raw source values and are not automatically anonymized. The shipped evidence uses only generated lab data. Private contact research is excluded from distributions.
