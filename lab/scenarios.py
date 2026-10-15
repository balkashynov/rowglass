"""Record real Postgres -> PeerDB -> ClickHouse scenarios with explicit fault labels.

Run after lab/bootstrap.py. Uses only the dedicated Rowglass lab. The source is
locked against writes while measuring controlled cuts. Source evidence and PeerDB
normalization progress are checked separately from row equality.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import time

import psycopg2
from rowglass import core, clickhouse, replay
from rowglass.capture import lsn_number
from bootstrap import SOURCE, PEERDB

ROOT = Path(__file__).resolve().parents[1]
CATALOG = (
    "host=127.0.0.1 port=55433 user=postgres password=rowglass-catalog dbname=postgres"
)
RUN = ROOT / "runs" / f"pipeline-{time.time_ns()}"
COMPOSE = ["docker", "compose", "-f", "compose.yaml", "-f", "lab/pipeline.yaml"]


def execute(query, params=None):
    with psycopg2.connect(SOURCE) as c, c.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


def wait_for(fn, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.25)
    raise TimeoutError(
        "Condition did not become true within " + str(timeout) + " seconds"
    )


def main(resume=False):
    RUN.mkdir(parents=True, exist_ok=True)
    os.environ["ROWGLASS_SOURCE_DSN"] = SOURCE
    os.environ["ROWGLASS_CLICKHOUSE_PASSWORD"] = "rowglass-local"
    config = json.loads(
        (RUN / "config.json" if resume else ROOT / "lab/config.json").read_text()
    )
    if not resume:
        config["slot"] = "rowglass_demo_" + str(time.time_ns())
    (RUN / "config.json").write_text(json.dumps(config, indent=2))
    journal = RUN / "evidence.rg"
    (RUN / "ready.json").unlink(missing_ok=True)
    log = (RUN / "capture.log").open("a")
    proc = subprocess.Popen(
        [
            str(ROOT / ".venv/bin/rowglass"),
            "record",
            str(RUN / "config.json"),
            str(journal),
            "--ready-file",
            str(RUN / "ready.json"),
        ],
        stdout=log,
        stderr=log,
    )
    lock = None
    client = clickhouse.Client({})
    audits = []

    def inspect():
        return core.run("inspect", journal)

    def audit(name, consistency=None):
        target = clickhouse.snapshot(config, journal, consistency=consistency)
        path = RUN / (name + "-target.json")
        path.write_text(json.dumps(target))
        result = core.run("audit", journal, str(path))
        result["scenario"] = name
        (RUN / (name + "-audit.json")).write_text(json.dumps(result, indent=2))
        audits.append(result)
        print(name, result["status"], result["totals"], flush=True)
        return result

    try:
        wait_for(
            lambda: (
                (RUN / "ready.json").exists()
                or (
                    proc.poll() is not None
                    and (_ for _ in ()).throw(RuntimeError(log.name))
                )
            )
        )
        if not resume:

            def initial_ready():
                try:
                    return (
                        client.query("SELECT count() AS n FROM orders FINAL")[0][0]["n"]
                        == "2000"
                    )
                except RuntimeError:
                    return False

            wait_for(initial_ready)
            baseline = audit("01-baseline")
            assert not baseline["mismatches"], baseline["mismatches"][:1]
        if not resume:
            run_workload(proc, config, journal, audit, inspect)
        else:
            audits.extend(
                json.loads((RUN / (n + "-audit.json")).read_text())
                for n in ["01-baseline", "02-paused-lag"]
            )
        lock = psycopg2.connect(SOURCE)
        with lock.cursor() as cur:
            cur.execute("LOCK TABLE orders,payments IN SHARE MODE")
        txs = [e["tx"] for e in replay.entries(journal) if e["kind"] == "transaction"]
        commit = lsn_number(txs[-1]["commit_lsn"])

        def drained():
            with psycopg2.connect(CATALOG) as c, c.cursor() as cur:
                cur.execute(
                    "SELECT last_offset,sync_batch_id,normalize_batch_id FROM metadata_last_sync_state WHERE job_name=%s",
                    ("rowglass_demo",),
                )
                row = cur.fetchone()
            return (
                {
                    "last_offset": str(row[0]),
                    "sync_batch_id": row[1],
                    "normalize_batch_id": row[2],
                }
                if row and row[0] >= commit and row[1] == row[2]
                else None
            )

        witness = wait_for(drained)
        consistency = {
            "mode": "controlled_quiescent",
            "source_cut": inspect()["source_cut"],
            "established_by": "Dedicated lab holds SHARE locks on both source tables. PeerDB normalized through the final captured commit.",
            "peerdb": witness,
            "source_commit_lsn": txs[-1]["commit_lsn"],
        }
        caught = audit("03-key-change", consistency)
        assert (
            caught["totals"].get("unexpected") == 1 and len(caught["mismatches"]) == 1
        ), caught["mismatches"][:2]
        known = next(
            m for m in caught["mismatches"] if m["target"]["id"]["value"] == "60"
        )
        key_bundle = replay.build(
            journal,
            known["table"],
            known["key"],
            "rowglass_replay_key_" + str(time.time_ns()),
        )
        (RUN / "key-replay.json").write_text(json.dumps(key_bundle, indent=2))
        (RUN / "key-replay.sql").write_text(key_bundle["sql"])
        reproduce_key_change(key_bundle, client)
        # Remove the documented stale-key case explicitly before the independent injected-fault control.
        client.query(
            "INSERT INTO orders SELECT * REPLACE(toUInt8(1) AS _peerdb_is_deleted,_peerdb_version+1000000000 AS _peerdb_version) FROM orders FINAL WHERE id=60",
            json_rows=False,
        )
        aligned = audit("04-aligned", consistency)
        assert aligned["status"] == "matched_at_cut"
        finish_scenarios(client, journal, audit, consistency, inspect, audits)
    finally:
        if lock:
            lock.rollback()
            lock.close()
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=20)
        log.close()


def run_workload(proc, config, journal, audit, inspect):
    paused = False
    try:
        subprocess.run(
            COMPOSE + ["pause", "flow-worker"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        paused = True
        payment_base = int(time.time()) * 1000
        # 100 real atomic two-table transactions, plus unrelated rows and a multi-step selected lineage.
        for i in range(1, 101):
            execute(
                """UPDATE orders SET status='paid',amount=amount+0.000001,updated_at='2026-10-15 12:00:00+00' WHERE id=%s;
                       INSERT INTO payments VALUES(%s,%s,12.345678,'captured')""",
                (i, payment_base + i, i),
            )
        execute(
            "UPDATE orders SET amount=9007199254740993.123456 WHERE id=42; UPDATE payments SET state='refunded' WHERE id=%s",
            (payment_base + 42,),
        )
        execute("UPDATE orders SET status='refunded' WHERE id=42")
        execute("DELETE FROM orders WHERE id=99")
        execute("UPDATE orders SET id=2060 WHERE id=60")
        with psycopg2.connect(SOURCE) as c, c.cursor() as cur:
            cur.execute("UPDATE orders SET status='rolled-back' WHERE id=42")
            c.rollback()
        expected_changes = 205
        wait_for(lambda: inspect()["changes"] >= expected_changes)
        lag = audit("02-paused-lag")
        assert lag["status"] == "live_observation" and lag["mismatches"]
        subprocess.run(
            COMPOSE + ["unpause", "flow-worker"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        paused = False
    finally:
        if paused:
            subprocess.run(COMPOSE + ["unpause", "flow-worker"], cwd=ROOT)


def finish_scenarios(client, journal, audit, consistency, inspect, audits):
    # Snapshot is a recovery oracle for our explicit destination mutation, not evidence of a PeerDB bug.
    backup = "rg_backup_" + str(time.time_ns())
    client.query(
        f"CREATE TABLE {backup} AS orders ENGINE=MergeTree ORDER BY id", json_rows=False
    )
    client.query(f"INSERT INTO {backup} SELECT * FROM orders FINAL", json_rows=False)
    client.query(
        "INSERT INTO orders SELECT * REPLACE(amount-1 AS amount,_peerdb_version+1000000000 AS _peerdb_version) FROM orders FINAL WHERE id=42",
        json_rows=False,
    )
    client.query(
        "INSERT INTO orders SELECT * REPLACE(toUInt8(1) AS _peerdb_is_deleted,_peerdb_version+1000000000 AS _peerdb_version) FROM orders FINAL WHERE id=37",
        json_rows=False,
    )
    client.query(
        "INSERT INTO orders SELECT * REPLACE(toInt64(999999) AS id,_peerdb_version+1000000000 AS _peerdb_version) FROM (SELECT * FROM orders FINAL WHERE id=1)",
        json_rows=False,
    )
    corrupt = audit("05-injected-divergence", consistency)
    assert (
        corrupt["totals"].get("changed") == 1
        and corrupt["totals"].get("missing") == 1
        and corrupt["totals"].get("unexpected") == 1
    ), corrupt["totals"]
    focus = next(m for m in corrupt["mismatches"] if m["kind"] == "changed")
    bundle = replay.build(
        journal, focus["table"], focus["key"], "rowglass_replay_" + str(time.time_ns())
    )
    (RUN / "replay.json").write_text(json.dumps(bundle, indent=2))
    (RUN / "replay.sql").write_text(bundle["sql"])
    # Execute the generated SQL in a fresh schema and compare independently through SQL text output.
    conn = psycopg2.connect(SOURCE)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(bundle["sql"])
        actual = []
        for table in sorted({r["table"] for r in bundle["expected_rows"]}):
            expected = [r for r in bundle["expected_rows"] if r["table"] == table]
            columns = list(expected[0]["values"])
            cur.execute(
                "SELECT "
                + ", ".join(replay.ident(c) + "::text" for c in columns)
                + " FROM "
                + replay.ident(bundle["schema"])
                + "."
                + replay.ident(bundle["table_map"][table])
            )
            rows = [
                dict(zip(columns, (core.cell(v) for v in row)))
                for row in cur.fetchall()
            ]
            assert sorted(rows, key=lambda x: json.dumps(x, sort_keys=True)) == sorted(
                [r["values"] for r in expected],
                key=lambda x: json.dumps(x, sort_keys=True),
            ), table
            actual += rows
    conn.close()
    client.query(
        f"INSERT INTO orders SELECT * REPLACE(_peerdb_version+2000000000 AS _peerdb_version) FROM {backup} WHERE id IN (37,42)",
        json_rows=False,
    )
    client.query(
        "INSERT INTO orders SELECT * REPLACE(toUInt8(1) AS _peerdb_is_deleted,_peerdb_version+1000000000 AS _peerdb_version) FROM orders FINAL WHERE id=999999",
        json_rows=False,
    )
    repaired = audit("06-restored", consistency)
    assert repaired["status"] == "matched_at_cut"
    outcome = {
        "status": "passed",
        "run": RUN.name,
        "source": inspect(),
        "audits": [
            {"name": a["scenario"], "status": a["status"], "totals": a["totals"]}
            for a in audits
        ],
        "replay": {
            "selected_transactions": bundle["selected_transactions"],
            "total_transactions": bundle["total_transactions"],
            "baseline_rows": bundle["baseline_rows"],
            "executed_rows": len(actual),
            "sql_sha256": bundle["sql_sha256"],
        },
        "fault": "Destination rows deliberately altered by this lab. No upstream PeerDB bug is claimed.",
        "versions": {
            "postgres": "17",
            "peerdb": "stable-v0.37.10",
            "clickhouse": "26.8.2.7",
        },
    }
    (RUN / "verification.json").write_text(json.dumps(outcome, indent=2))
    print(json.dumps(outcome, indent=2), flush=True)


def reproduce_key_change(bundle, client):
    conn = psycopg2.connect(SOURCE)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(bundle["bootstrap_sql"])
    name = "rg_replay_" + str(time.time_ns())
    mappings = {
        t: name + "_" + str(i) for i, t in enumerate(sorted(bundle["table_map"]))
    }
    peer = psycopg2.connect(PEERDB)
    peer.autocommit = True
    with peer.cursor() as cur:
        mapping = ", ".join(
            bundle["schema"] + "." + bundle["table_map"][t] + ":" + dest
            for t, dest in mappings.items()
        )
        cur.execute(
            f"CREATE MIRROR {name} FROM rowglass_source TO rowglass_target WITH TABLE MAPPING ({mapping}) WITH (do_initial_copy=true,sync_interval=1)"
        )
    peer.close()
    target = mappings["public.orders"]

    def snapshot_ready():
        try:
            return (
                client.query(f"SELECT count() AS n FROM {target} FINAL")[0][0]["n"]
                == "1"
            )
        except RuntimeError:
            return False

    wait_for(snapshot_ready)
    for tx in bundle["transactions"]:
        with conn.cursor() as cur:
            cur.execute(tx["sql"])

    def replay_done():
        rows = client.query(
            f"SELECT id FROM {target} FINAL WHERE _peerdb_is_deleted=0 ORDER BY id"
        )[0]
        return rows if {r["id"] for r in rows} == {"60", "2060"} else None

    observed = wait_for(replay_done)
    conn.close()
    result = {
        "status": "reproduced_known_limitation",
        "mirror": name,
        "source_expected_keys": ["2060"],
        "target_observed_keys": [r["id"] for r in observed],
        "original_transactions": bundle["total_transactions"],
        "replay_transactions": bundle["selected_transactions"],
        "baseline_rows": bundle["baseline_rows"],
        "reference": "https://clickhouse.com/docs/integrations/clickpipes/postgres/faq#can-i-update-primary-key-columns-in-postgresql",
    }
    (RUN / "key-replay-verification.json").write_text(json.dumps(result, indent=2))
    print("Known key-change limitation independently reproduced:", result, flush=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        RUN = Path(sys.argv[1]).resolve()
    main(resume=len(sys.argv) > 1)
