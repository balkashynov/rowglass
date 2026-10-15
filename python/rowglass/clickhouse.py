"""Read-only ClickHouse adapter. Exposes ambiguous visible version ties, never hides them."""

from __future__ import annotations
import base64
import datetime as dt
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import core


def ident(name: str) -> str:
    if not name or "\x00" in name:
        raise ValueError("Invalid ClickHouse identifier")
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


class Client:
    def __init__(self, config: dict):
        self.url = os.environ.get(
            config.get("url_env", "ROWGLASS_CLICKHOUSE_URL"), "http://127.0.0.1:58123"
        )
        self.user = os.environ.get(
            config.get("user_env", "ROWGLASS_CLICKHOUSE_USER"), "rowglass"
        )
        self.password = os.environ.get(
            config.get("password_env", "ROWGLASS_CLICKHOUSE_PASSWORD"), ""
        )
        self.database = config.get("database", "rowglass")

    def query(self, query: str, *, json_rows=True):
        query_id = str(uuid.uuid4())
        params = {
            "database": self.database,
            "query_id": query_id,
            "max_execution_time": 60,
            "output_format_json_quote_64bit_integers": 1,
            "date_time_output_format": "iso",
            "session_timezone": "UTC",
        }
        payload = query + (" FORMAT JSONEachRow" if json_rows else "")
        auth = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        request = urllib.request.Request(
            self.url.rstrip("/") + "/?" + urllib.parse.urlencode(params),
            data=payload.encode(),
            headers={"Authorization": "Basic " + auth},
        )
        try:
            with urllib.request.urlopen(request, timeout=70) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as error:
            raise RuntimeError("ClickHouse: " + error.read().decode()[:4000]) from None
        return (
            [json.loads(line) for line in raw.splitlines() if line]
            if json_rows
            else raw
        ), query_id


def expression(column: dict) -> str:
    name = ident(column["name"])
    if column["oid"] == 17:
        value = f"concat('\\\\x', lower(hex({name})))"
    elif column["oid"] == 1184:
        value = f"toString(toTimeZone({name}, 'UTC'))"
    else:
        value = f"toString({name})"
    return f"{value} AS {name}"


def snapshot(config: dict, journal: Path, *, consistency: dict | None = None) -> dict:
    source = core.run("export", journal)
    client = Client(config.get("clickhouse", {}))
    started = time.monotonic()
    tables, queries = [], []
    for table in source["tables"]:
        relation = table["relation"]
        mapping = next(t for t in config["tables"] if t["source"] == table["name"])
        destination = ".".join(ident(part) for part in mapping["target"].split("."))
        columns = relation["columns"]
        key_columns = [c["name"] for c in columns if c["key"]]
        version = ident(mapping.get("version", "_peerdb_version"))
        deleted = ident(mapping.get("deleted", "_peerdb_is_deleted"))
        fields = ", ".join(expression(c) for c in columns)
        partition = ", ".join(ident(c) for c in key_columns)
        query = f"""SELECT {fields}, toString({version}) AS __rg_version,
                    toString({deleted}) AS __rg_deleted FROM (
                    SELECT *, dense_rank() OVER (PARTITION BY {partition} ORDER BY {version} DESC) AS __rg_rank
                    FROM {destination}) WHERE __rg_rank = 1"""
        observed, query_id = client.query(query)
        # Identical retries are harmless. Conflicting rows at a maximum version are not.
        resolved = {}
        duplicates = 0
        for row in observed:
            key = tuple(row[c] for c in key_columns)
            if key in resolved:
                if resolved[key] != row:
                    raise ValueError(
                        f"Ambiguous maximum version for {table['name']} key {key}; cannot choose a winner"
                    )
                duplicates += 1
            resolved[key] = row
        rows = []
        for raw in resolved.values():
            if raw["__rg_deleted"] not in ("0", "false"):
                continue
            values = {}
            for c in columns:
                value = raw[c["name"]]
                if (
                    c["oid"] == 1184
                    and value is not None
                    and not value.endswith(("Z", "+00:00"))
                ):
                    value += "+00:00"
                values[c["name"]] = core.cell(value)
            rows.append(values)
        tables.append(
            {
                "source_table": table["name"],
                "target_table": mapping["target"],
                "rows": rows,
                "visible_latest_keys": len(resolved),
                "identical_latest_duplicates": duplicates,
            }
        )
        queries.append({"query_id": query_id, "sql": query})
    return {
        "format": "rowglass-target-v1",
        "tables": tables,
        "consistency": consistency
        or {"mode": "live", "source_cut_observed": source["summary"]["source_cut"]},
        "metadata": {
            "engine": "ClickHouse",
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "elapsed_ms": (time.monotonic() - started) * 1000,
            "queries": queries,
            "limitation": "Per-table reads, no shared source/target snapshot. Version conflicts already merged away cannot be recovered from this read.",
        },
    }
