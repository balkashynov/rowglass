"""Capture protocol-v1 pgoutput with exported-snapshot bootstrap and durable ACKs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import time

import psycopg2
from psycopg2 import sql
from psycopg2.extras import LogicalReplicationConnection

from .core import Ingest, cell
from .transport import CopyBoth


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text())
    for name in (config["publication"], config["slot"]):
        if (
            not name
            or len(name) > 63
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in name)
        ):
            raise ValueError(
                "Publication and slot names must use lower-case ASCII, digits or underscores"
            )
    return config


def dsn(config: dict) -> str:
    return os.environ[config.get("source_dsn_env", "ROWGLASS_SOURCE_DSN")]


def table_parts(table: str) -> tuple[str, str]:
    parts = table.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            "Use explicit schema.table names; names containing dots are not supported"
        )
    return tuple(parts)


def setup(config: dict):
    """Explicitly create the publication; never modify an existing publication."""
    with psycopg2.connect(dsn(config)) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pubname FROM pg_publication WHERE pubname=%s",
            (config["publication"],),
        )
        if cur.fetchone():
            raise ValueError(
                "Publication already exists; inspect it or choose a new name"
            )
        tables = sql.SQL(", ").join(
            sql.Identifier(*table_parts(t["source"])) for t in config["tables"]
        )
        cur.execute(
            sql.SQL("CREATE PUBLICATION {} FOR TABLE {}").format(
                sql.Identifier(config["publication"]), tables
            )
        )


def relation(cur, table: str) -> dict:
    schema, name = table_parts(table)
    cur.execute(
        """SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname=%s AND c.relname=%s AND c.relkind='r'""",
        (schema, name),
    )
    found = cur.fetchone()
    if not found:
        raise ValueError(f"{table} must be a regular, non-partitioned table")
    oid = found[0]
    cur.execute(
        """SELECT a.attname, a.atttypid,
                   EXISTS(SELECT 1 FROM pg_index i WHERE i.indrelid=a.attrelid AND i.indisprimary
                          AND a.attnum = ANY(i.indkey))
                   FROM pg_attribute a WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped
                   ORDER BY a.attnum""",
        (oid,),
    )
    columns = [{"name": n, "oid": o, "key": k} for n, o, k in cur.fetchall()]
    if not any(c["key"] for c in columns):
        raise ValueError(f"{table} needs an explicit primary key")
    return {"id": oid, "namespace": schema, "name": name, "columns": columns}


def lsn_number(s: str) -> int:
    a, b = s.split("/")
    return (int(a, 16) << 32) | int(b, 16)


def validate_publication(config: dict):
    with psycopg2.connect(dsn(config)) as check, check.cursor() as cur:
        cur.execute(
            "SELECT pubinsert,pubupdate,pubdelete,pubtruncate FROM pg_publication WHERE pubname=%s",
            (config["publication"],),
        )
        flags = cur.fetchone()
        if flags != (True, True, True, True):
            raise ValueError(
                "Publication must emit inserts, updates, deletes and truncates"
            )
        cur.execute(
            "SELECT schemaname,tablename,attnames,rowfilter FROM pg_publication_tables WHERE pubname=%s",
            (config["publication"],),
        )
        published = cur.fetchall()
        requested = {t["source"] for t in config["tables"]}
        if {f"{a}.{b}" for a, b, _, _ in published} != requested:
            raise ValueError("Publication tables must exactly match the capture config")
        for schema, name, columns, row_filter in published:
            rel = relation(cur, f"{schema}.{name}")
            if row_filter or set(columns) != {c["name"] for c in rel["columns"]}:
                raise ValueError(
                    "Filtered publications or column lists are not supported; use all rows and columns"
                )


def capture(
    config: dict,
    journal: Path,
    seconds: float | None = None,
    ready_file: Path | None = None,
):
    recorder = Ingest(journal)
    repl = None
    stop = False

    def stop_handler(_signum, _frame):
        nonlocal stop
        stop = True

    previous = {
        sig: signal.signal(sig, stop_handler) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        repl = psycopg2.connect(
            dsn(config),
            connection_factory=LogicalReplicationConnection,
            client_encoding="UTF8",
            options="-c timezone=UTC -c datestyle=ISO,YMD -c bytea_output=hex -c extra_float_digits=3",
        )
        rep = repl.cursor()
        rep.execute("IDENTIFY_SYSTEM")
        system_id, timeline, _, database = rep.fetchone()
        validate_publication(config)
        summary = recorder.ready["summary"]
        if summary["identity"]:
            if not summary["sealed"]:
                raise ValueError(
                    "Bootstrap did not finish. Preserve this evidence, drop its slot explicitly, and start a new journal"
                )
            identity = summary["identity"]
            if (
                identity["system_id"] != system_id
                or identity["database"] != database
                or identity["slot"] != config["slot"]
            ):
                raise ValueError("Journal belongs to a different source or slot")
            if identity["timeline"] != timeline:
                raise ValueError(
                    "Source timeline changed; failover continuity needs a new independently verified recording"
                )
            if (
                identity["publication"] != config["publication"]
                or identity["tables"] != config["tables"]
            ):
                raise ValueError(
                    "Capture configuration changed; start a separate recording"
                )
            with psycopg2.connect(dsn(config)) as check, check.cursor() as cur:
                cur.execute(
                    "SELECT confirmed_flush_lsn::text, wal_status, restart_lsn::text, plugin, database FROM pg_replication_slots WHERE slot_name=%s",
                    (config["slot"],),
                )
                slot = cur.fetchone()
                if not slot or slot[1] == "lost":
                    raise ValueError(
                        "Replication slot is missing or its WAL was lost; cannot resume this recording"
                    )
                if slot[0] and lsn_number(slot[0]) > lsn_number(summary["source_cut"]):
                    raise ValueError(
                        "Server slot is ahead of durable journal; refusing a capture gap"
                    )
                if (
                    slot[3] != "pgoutput"
                    or slot[4] != database
                    or not slot[2]
                    or lsn_number(slot[2]) > lsn_number(summary["source_cut"])
                ):
                    raise ValueError(
                        "Slot identity or retained WAL no longer covers the durable journal"
                    )
            ack = lsn_number(summary["source_cut"])
        else:
            rep.execute(
                sql.SQL(
                    "CREATE_REPLICATION_SLOT {} LOGICAL pgoutput EXPORT_SNAPSHOT"
                ).format(sql.Identifier(config["slot"]))
            )
            _, consistent_point, snapshot, _ = rep.fetchone()
            identity = {
                "system_id": system_id,
                "timeline": timeline,
                "database": database,
                "slot": config["slot"],
                "publication": config["publication"],
                "tables": config["tables"],
                "snapshot_lsn": consistent_point,
                "protocol": "pgoutput-v1",
            }
            recorder.entry("start", identity=identity)
            snapshot_conn = psycopg2.connect(dsn(config))
            try:
                with snapshot_conn.cursor() as cur:
                    cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    cur.execute("SET TRANSACTION SNAPSHOT %s", (snapshot,))
                    cur.execute("SET LOCAL TIME ZONE 'UTC'")
                    cur.execute("SET LOCAL DateStyle = 'ISO, YMD'")
                    cur.execute("SET LOCAL bytea_output = 'hex'")
                    cur.execute("SET LOCAL extra_float_digits = 3")
                    for mapping in config["tables"]:
                        rel = relation(cur, mapping["source"])
                        recorder.entry("schema", relation=rel)
                        columns = sql.SQL(", ").join(
                            sql.SQL("{}::text").format(sql.Identifier(c["name"]))
                            for c in rel["columns"]
                        )
                        query = sql.SQL("SELECT {} FROM {}").format(
                            columns, sql.Identifier(*table_parts(mapping["source"]))
                        )
                        with snapshot_conn.cursor(
                            name=f"rowglass_snapshot_{rel['id']}"
                        ) as reader:
                            reader.itersize = 1000
                            reader.execute(query)
                            while rows := reader.fetchmany(1000):
                                recorder.entry(
                                    "snapshot",
                                    relation_id=rel["id"],
                                    rows=[[cell(v) for v in row] for row in rows],
                                )
                snapshot_conn.commit()
            finally:
                snapshot_conn.close()
            recorder.entry("seal", lsn=consistent_point)
            ack = lsn_number(consistent_point)
        rep.start_replication(
            slot_name=config["slot"],
            start_lsn=ack,
            decode=False,
            options={
                "proto_version": "1",
                "publication_names": config["publication"],
                "messages": "true",
            },
            status_interval=1,
        )
        transport = CopyBoth(repl)
        if ready_file:
            ready_file.write_text(
                json.dumps({"ready": True, "source_cut": ack, "journal": str(journal)})
            )
        start = time.monotonic()
        last_feedback = 0.0
        while not stop and (seconds is None or time.monotonic() - start < seconds):
            message = transport.read()
            if message and message["kind"] == "data":
                result = recorder.send({"op": "wal", "hex": message["payload"].hex()})
                if result["ack"]:
                    ack = lsn_number(result["ack"])
                    transport.feedback(ack)
            elif message and message.get("reply"):
                transport.feedback(ack)
            elif message is None:
                select.select([rep], [], [], 0.1)
            if time.monotonic() - last_feedback >= 0.5:
                transport.feedback(ack)
                last_feedback = time.monotonic()
        return recorder.send({"op": "status"})["summary"]
    finally:
        if repl:
            repl.close()
        recorder.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
