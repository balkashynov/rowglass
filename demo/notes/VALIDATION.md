# Validation record

Checked on 2026-10-15. All supplied datasets are synthetic. Integration evidence came from actual local database execution, not mocked output. No hosted ClickPipes or production workload was tested.

## Real pipeline

Frozen run: `pipeline-1792089476859139000`. Postgres 17, PeerDB stable-v0.37.10, ClickHouse 26.8.2.7; Docker Linux aarch64 with 12 vCPU / 8 GB allocated. This is the explicitly tested PeerDB version, not a claim to use the latest release. The journal contains 104 committed transactions, 205 changes and 592,579 bytes.

SHA-256: `6257a189fa76cf0d1bd80a99cf510489486f506d18243b911d2b0c2ca4b83e8b`.

| Observation | Matched | Changed | Missing | Extra | Interpretation |
| --- | ---: | ---: | ---: | ---: | --- |
| Baseline | 2,000 | 0 | 0 | 0 | Live observation |
| Worker paused | 1,900 | 98 | 101 | 2 | Induced lag; live observation |
| Key changed, pipeline drained | 2,099 | 0 | 0 | 1 | Known default key-update limitation |
| Explicit stale-key tombstone | 2,099 | 0 | 0 | 0 | Match at controlled cut |
| Three injected destination faults | 2,097 | 1 | 1 | 1 | Diagnostic test, deliberate corruption |
| Explicit restoration | 2,099 | 0 | 0 | 0 | Match at controlled cut |

The lab holds SHARE locks on both source tables while establishing its final cut. It verifies its own recorder and requires the PeerDB catalog's `last_offset` to cover the final commit LSN, with sync and normalize batch IDs equal. It then performs sequential target observations while source writes stay locked. These implementation-specific checks support a bounded local controlled-cut claim; they are not a universal barrier API.

The key-change slice contains two transactions and one baseline row. A fresh PeerDB mirror reproduces source key `2060` and target keys `60` plus `2060`. A separate row-42 slice preserves three transactions, including a companion payment, and its SQL result was cross-checked in a scratch Postgres schema. The SQL and manifests are in `evidence/pipeline/`.

`scripts/verify_evidence.py` independently reruns every saved target audit at its original source cut and verifies both replay SQL hashes and resulting expected rows. It does not stand in for rerunning the real databases.

## Source and negative tests

Six native tests cover decoder boundaries, committed transactions, exact numeric/JSON semantics, bytes/times/floats, key/TOAST/delete state, uncertain comparisons, durable journals, torn tails and complete corruption rejection. Six Python product tests cover companion transaction closure, key predecessors, unrelated transactions, schema naming, historical cuts, missing history, corrupt replay rejection, keepalive parsing and conflicting/identical target version ties.

The final real capture run is `capture-1792090234585034000`. It cross-checks exported-snapshot state against independent SQL; exercises large exact decimals/JSON integers, null, bytes, time zones, floats and unchanged 64k TOAST; excludes rolled-back writes; kills the capture process, commits offline writes and resumes; updates all rows after adding a column; and truncates. Unpublished WAL must not advance `confirmed_flush_lsn` past the recorded durable cut. Final verification and journal are in `evidence/capture/`.

These are targeted tests, not exhaustive protocol fuzzing or a power-loss proof. Native Clippy with warnings denied and Python Ruff checks passed. The browser was exercised at desktop and 390-pixel mobile width: six scenarios, filters/search, history slider, replay links, deep links and empty states. Screenshots are in `docs/screenshots/`.

## Performance and a fair reference

`tests/benchmark.py` generates 1k, 10k and 100k rows, three columns, exact decimals/text and 10% one-row update transactions. The target exactly matches. Each of three runs includes process startup, file reads, CRC32/SHA-256 validation, reconstruction and (for audit) comparison. Generation and repetitions warm caches. macOS `/usr/bin/time -l` provides per-process peak RSS. These are not cold-storage, ingestion, fsync, network or end-to-end replication throughput measurements.

Environment: macOS 27.0.1 arm64, Rust 1.93.0 release, Python 3.13.5. Container resource allocation above applies to integration tests, not this host-process benchmark.

| Rows | Native inspect | Native audit | Audit peak RSS | Restricted Python audit | Python peak RSS |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.0114 s | 0.0105 s | 5.6 MiB | 0.0414 s | 23.6 MiB |
| 10,000 | 0.0234 s | 0.0454 s | 23.0 MiB | 0.0735 s | 44.3 MiB |
| 100,000 | 0.2233 s | 0.4441 s | 197.6 MiB | 0.2978 s | 256.0 MiB |

Times are medians; memory is the highest of three runs. The independent Python reference supports this fixture's integer/numeric/text schema and verifies CRC/SHA plus exact equality. It does not implement Rowglass's full type, history or replay model. It is faster at 100k; native uses less peak memory. Full state and target rows remain memory-resident. Histories for mismatches require a second journal scan. Many mismatches and wide values can be much more expensive than this all-matching narrow-row fixture.

Reproduce on macOS:

```sh
.venv/bin/python tests/benchmark.py --output runs/benchmark-new
```

Raw repetitions and environment are in `evidence/benchmark.json`. This measurement runner uses macOS time syntax and needs adaptation for Linux RSS units/options.
