"""Extract transaction-preserving backward lineage and compile an isolated SQL replay.

This is a row-dependency slice, not an execution of a failure predicate or an
inferred foreign-key graph. Whole selected transactions are kept intact.
"""

from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess

from . import core

TYPES = {
    16: "boolean",
    17: "bytea",
    18: '"char"',
    19: "name",
    20: "bigint",
    21: "smallint",
    23: "integer",
    25: "text",
    26: "oid",
    114: "json",
    700: "real",
    701: "double precision",
    1042: "text",
    1043: "text",
    1082: "date",
    1114: "timestamp",
    1184: "timestamptz",
    1700: "numeric",
    2950: "uuid",
    3802: "jsonb",
}


def ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def literal(value: dict) -> str:
    if value["kind"] == "null":
        return "NULL"
    if value["kind"] != "text":
        raise ValueError("Replay requires fully observed row values")
    return "'" + value["value"].replace("'", "''") + "'"


def label(relation: dict) -> str:
    return relation["namespace"] + "." + relation["name"]


def touched(change: dict) -> set[tuple[str, str]]:
    table = label(change["relation"])
    return {(table, change[k]) for k in ("old_key", "new_key") if change[k] is not None}


def entries(journal: Path):
    process = subprocess.Popen(
        [core.binary(), "stream", str(journal)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for line in process.stdout:
            yield json.loads(line)
        error = process.stderr.read()
        if process.wait() != 0:
            raise RuntimeError(error)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()


def build(journal: Path, table: str, key: str, schema: str = "rowglass_replay") -> dict:
    if not re.fullmatch(r"rowglass_replay[a-z0-9_]*", schema):
        raise ValueError(
            "Replay schema must begin rowglass_replay and contain only lower-case letters, digits and underscores"
        )
    if journal.stat().st_size > 256 * 1024 * 1024:
        raise ValueError(
            "Replay slice currently supports journals up to 256 MiB; use a bounded capture window"
        )
    verified = core.run("inspect", journal)
    if not verified["sealed"]:
        raise ValueError("Cannot replay an unfinished bootstrap")
    relations, baseline, transactions = {}, {}, []
    for event in entries(journal):
        if event["kind"] == "schema":
            r = event["relation"]
            relations[r["id"]] = r
        elif event["kind"] == "snapshot":
            r = relations[event["relation_id"]]
            for k, values in zip(event["keys"], event["rows"]):
                baseline[(label(r), k)] = dict(
                    zip((c["name"] for c in r["columns"]), values)
                )
        elif event["kind"] == "transaction":
            transactions.append(event["tx"])
    if table not in {label(r) for r in relations.values()}:
        raise ValueError("Selected table is not in the journal")
    needed = {(table, key)}
    selected = []
    for tx in reversed(transactions):
        keys = set().union(*(touched(c) for c in tx["changes"]))
        truncates = {
            label(c["relation"]) for c in tx["changes"] if c["op"] == "truncate"
        }
        if keys & needed or truncates & {t for t, k in needed}:
            selected.append(tx)
            needed |= keys
            # A truncate acts on the entire table: preserve its entire observed baseline/lineage.
            for truncated in truncates:
                needed |= {(t, k) for t, k in baseline if t == truncated}
    selected.reverse()
    selected_tables = {t for t, k in needed}
    rels = {label(r): r for r in relations.values() if label(r) in selected_tables}
    table_map = {t: "table_" + str(i) for i, t in enumerate(sorted(selected_tables))}
    names = {t: ident(schema) + "." + ident(table_map[t]) for t in selected_tables}
    sql = [
        "-- Rowglass isolated row-lineage replay. Original triggers, constraints and DDL are not copied.",
        "-- CREATE SCHEMA fails if the destination already exists. Never replay into a production schema.",
        "SET standard_conforming_strings = on;",
        "SET TIME ZONE 'UTC';",
        "BEGIN;",
        f"CREATE SCHEMA {ident(schema)};",
    ]
    for t, r in sorted(rels.items()):
        cols = []
        for c in r["columns"]:
            if c["oid"] not in TYPES:
                raise ValueError(f"Unsupported replay type OID {c['oid']}")
            cols.append(ident(c["name"]) + " " + TYPES[c["oid"]])
        cols.append(
            "PRIMARY KEY ("
            + ", ".join(ident(c["name"]) for c in r["columns"] if c["key"])
            + ")"
        )
        sql.append(f"CREATE TABLE {names[t]} (" + ", ".join(cols) + ");")
    state = {k: copy.deepcopy(v) for k, v in baseline.items() if k in needed}
    for (t, k), row in sorted(state.items()):
        sql.append(
            f"INSERT INTO {names[t]} ("
            + ", ".join(map(ident, row))
            + ") VALUES ("
            + ", ".join(map(literal, row.values()))
            + ");"
        )
    sql.append("COMMIT;")
    bootstrap_sql = "\n".join(sql) + "\n"
    events = []
    for tx in selected:
        sql_start = len(sql)
        sql += [f"-- Source XID {tx['xid']}, commit {tx['end_lsn']}", "BEGIN;"]
        steps = []
        for change in tx["changes"]:
            r = change["relation"]
            t = label(r)
            if r != rels[t]:
                raise ValueError(
                    "Selected lineage crosses a schema change; DDL must be supplied explicitly before replay"
                )
            op = change["op"]
            old = change["old_key"]
            new = change["new_key"]
            if op == "truncate":
                sql.append(f"TRUNCATE {names[t]};")
                state = {k: v for k, v in state.items() if k[0] != t}
                steps.append({"table": t, "op": op})
                continue
            prior = state.get((t, old or new))
            if op in ("update", "delete") and prior is None:
                raise ValueError(
                    "Selected lineage lacks a preimage; cannot compile a faithful row replay"
                )
            row = None
            if change["new"] is not None:
                row = {
                    c["name"]: (
                        copy.deepcopy(prior[c["name"]])
                        if v["kind"] == "unchanged" and prior and c["name"] in prior
                        else copy.deepcopy(v)
                    )
                    for c, v in zip(r["columns"], change["new"])
                }
                if any(v["kind"] not in ("null", "text") for v in row.values()):
                    raise ValueError(
                        "Unknown or unresolved TOAST value in replay lineage"
                    )
            predicate = (
                " AND ".join(
                    ident(c["name"])
                    + " IS NOT DISTINCT FROM "
                    + literal(prior[c["name"]])
                    for c in r["columns"]
                    if c["key"]
                )
                if prior
                else ""
            )
            if op == "insert":
                sql.append(
                    f"INSERT INTO {names[t]} ("
                    + ", ".join(map(ident, row))
                    + ") VALUES ("
                    + ", ".join(map(literal, row.values()))
                    + ");"
                )
            elif op == "update":
                sql.append(
                    f"UPDATE {names[t]} SET "
                    + ", ".join(ident(n) + "=" + literal(v) for n, v in row.items())
                    + " WHERE "
                    + predicate
                    + ";"
                )
            elif op == "delete":
                sql.append(f"DELETE FROM {names[t]} WHERE {predicate};")
            else:
                raise ValueError("Unsupported replay operation: " + op)
            steps.append(
                {
                    "table": t,
                    "op": op,
                    "old_key": old,
                    "key": new or old,
                    "before": copy.deepcopy(prior),
                    "after": copy.deepcopy(row),
                }
            )
            if old and old != new:
                state.pop((t, old), None)
            if row is not None:
                state[(t, new)] = row
        sql.append("COMMIT;")
        events.append(
            {
                "xid": tx["xid"],
                "lsn": tx["end_lsn"],
                "changes": steps,
                "sql": "\n".join(sql[sql_start:]) + "\n",
            }
        )
    if not selected and (table, key) not in baseline:
        raise ValueError("Selected row has no captured history")
    text = "\n".join(sql) + "\n"
    return {
        "format": "rowglass-replay-v1",
        "selected": {"table": table, "key": key},
        "schema": schema,
        "table_map": table_map,
        "journal": verified["journal"],
        "source_cut": verified["source_cut"],
        "total_transactions": len(transactions),
        "selected_transactions": len(selected),
        "baseline_rows": sum(k in needed for k in baseline),
        "dependency_keys": len(needed),
        "transactions": events,
        "bootstrap_sql": bootstrap_sql,
        "sql": text,
        "sql_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "expected_rows": [
            {"table": t, "key": k, "values": v} for (t, k), v in sorted(state.items())
        ],
        "limits": "Backward row lineage with whole transactions. Not a minimal failing reproducer. Foreign keys, triggers, constraints, typmods and original statement predicates are not reconstructed.",
    }
