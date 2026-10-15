"""Real PostgreSQL validation. Uses only its dedicated test table/publication/slot."""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import psycopg2
from psycopg2 import sql
from rowglass import core
from rowglass.capture import relation

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get(
    "ROWGLASS_SOURCE_DSN",
    "host=127.0.0.1 port=55432 user=rowglass password=rowglass-local dbname=rowglass",
)
os.environ["ROWGLASS_SOURCE_DSN"] = DSN
RUN = ROOT / "runs" / f"capture-{time.time_ns()}"
RUN.mkdir(parents=True)
CONFIG = {
    "publication": "rowglass_test",
    "slot": "rowglass_test",
    "source_dsn_env": "ROWGLASS_SOURCE_DSN",
    "tables": [{"source": "public.test_orders", "target": "test_orders"}],
}
(RUN / "config.json").write_text(json.dumps(CONFIG))


def execute(statement, params=None):
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(statement, params)
        if cur.description:
            return cur.fetchall()


def start():
    ready = RUN / "ready.json"
    ready.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [
            str(ROOT / ".venv/bin/rowglass"),
            "record",
            str(RUN / "config.json"),
            str(RUN / "evidence.rg"),
            "--ready-file",
            str(ready),
        ],
        stdout=(RUN / f"recorder-{time.time_ns()}.log").open("w"),
    )
    for _ in range(200):
        if ready.exists():
            return proc
        if proc.poll() is not None:
            raise RuntimeError("recorder failed to start")
        time.sleep(0.05)
    proc.terminate()
    raise TimeoutError("recorder did not become ready")


def wait_changes(n):
    for _ in range(200):
        state = core.run("inspect", RUN / "evidence.rg")
        if state["changes"] >= n:
            return state
        time.sleep(0.05)
    raise TimeoutError(f"capture did not reach {n} changes: {state}")


def compare():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
        rel = relation(cur, "public.test_orders")
        fields = sql.SQL(", ").join(
            sql.SQL("{}::text").format(sql.Identifier(c["name"]))
            for c in rel["columns"]
        )
        cur.execute(
            sql.SQL("SELECT {} FROM public.test_orders ORDER BY id").format(fields)
        )
        rows = [
            {c["name"]: core.cell(v) for c, v in zip(rel["columns"], row)}
            for row in cur.fetchall()
        ]
    summary = core.run("inspect", RUN / "evidence.rg")
    reference = {
        "tables": [{"source_table": "public.test_orders", "rows": rows}],
        "consistency": {
            "mode": "controlled_quiescent",
            "source_cut": summary["source_cut"],
        },
        "metadata": {
            "purpose": "independent source SQL cross-check, not a ClickHouse test"
        },
    }
    path = RUN / "reference.json"
    path.write_text(json.dumps(reference))
    audit = core.run("audit", RUN / "evidence.rg", str(path))
    assert audit["status"] == "matched_at_cut", audit
    return audit


def main():
    assert execute("SELECT current_database()")[0][0] == "rowglass", (
        "requires dedicated lab database"
    )
    execute(
        "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name='rowglass_test'"
    )
    execute("DROP PUBLICATION IF EXISTS rowglass_test")
    execute("DROP TABLE IF EXISTS public.test_orders")
    execute("""CREATE TABLE public.test_orders(id bigint PRIMARY KEY, balance numeric(38,6), payload text,
                metadata jsonb, active boolean, created timestamptz, blob bytea, ratio double precision)""")
    payload = "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(1000))
    execute(
        "INSERT INTO test_orders VALUES(1,9007199254740993.123456,%s,'{\"n\":9007199254740993}',true,'2026-01-01 00:00:00+00',decode('00abff','hex'),0.1),(2,-0.000000,'initial','null',false,'2025-12-31 17:00:00-07',NULL,-0.0)",
        (payload,),
    )
    execute("CREATE PUBLICATION rowglass_test FOR TABLE public.test_orders")
    proc = start()
    try:
        compare()
        execute(
            "UPDATE test_orders SET balance=balance+1,metadata='{\"n\":1.00}' WHERE id=1"
        )
        execute("UPDATE test_orders SET id=20 WHERE id=2")
        execute(
            'INSERT INTO test_orders VALUES(3,NULL,NULL,\'{"$number":"1e0"}\',NULL,NULL,NULL,NULL)'
        )
        with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("UPDATE test_orders SET payload='must not appear' WHERE id=3")
            conn.rollback()
        wait_changes(3)
        first = compare()
        # Unpublished WAL and keepalives must never advance the slot beyond the durable cut.
        execute("CREATE TABLE IF NOT EXISTS rowglass_unpublished(n int)")
        execute("INSERT INTO rowglass_unpublished SELECT generate_series(1,1000)")
        time.sleep(2)
        flush = execute(
            "SELECT confirmed_flush_lsn::text FROM pg_replication_slots WHERE slot_name='rowglass_test'"
        )[0][0]
        assert flush == first["source"]["source_cut"], (
            flush,
            first["source"]["source_cut"],
        )
        # Hard-stop transport: the server must never be acknowledged beyond the durable native journal.
        proc.kill()
        proc.wait(timeout=5)
        execute("DELETE FROM test_orders WHERE id=20")
        execute("UPDATE test_orders SET payload='after restart' WHERE id=3")
        proc = start()
        wait_changes(5)
        second = compare()
        execute("ALTER TABLE test_orders ADD COLUMN note text")
        execute("UPDATE test_orders SET note='schema evolved'")
        wait_changes(7)
        third = compare()
        execute("TRUNCATE test_orders")
        wait_changes(8)
        fourth = compare()
        result = {
            "status": "passed",
            "checks": [
                "consistent snapshot",
                "exact numeric/JSON/null/bytea/timestamps",
                "unchanged TOAST",
                "primary-key update",
                "rollback omitted",
                "hard-stop and WAL resume",
                "unrelated WAL and keepalive do not advance ACK past durable evidence",
                "schema change followed by all-row updates",
                "truncate",
            ],
            "runs": [a["source"] for a in (first, second, third, fourth)],
            "journal": str(RUN / "evidence.rg"),
        }
        (RUN / "verification.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    main()
