"""Reproducible local reconstruction/audit benchmark, including a restricted Python baseline.

Fixtures use the documented journal format and are explicitly synthetic. They do
not measure WAL transport, fsync ingestion, network or hosted ClickPipes latency.
Each timed command includes process startup, file reads, CRC checks, reconstruction
and (for audits) exact comparisons. Warm-cache repetitions are not cold I/O claims.
"""

from __future__ import annotations
import argparse
import decimal
import hashlib
import json
from pathlib import Path
import platform
import re
import statistics
import struct
import subprocess
import sys
import time
import zlib

from rowglass import core


def write_frame(file, entry):
    payload = json.dumps(entry, separators=(",", ":")).encode()
    file.write(struct.pack("<II", len(payload), zlib.crc32(payload)) + payload)


def fixture(directory, rows):
    relation = {
        "id": 42,
        "namespace": "public",
        "name": "bench",
        "columns": [
            {"name": "id", "oid": 20, "key": True},
            {"name": "value", "oid": 1700, "key": False},
            {"name": "note", "oid": 25, "key": False},
        ],
    }

    def values(i, changed=False):
        return [
            core.cell(str(i)),
            core.cell(
                str(decimal.Decimal(i) + decimal.Decimal("0.123456") + int(changed))
            ),
            core.cell("row-" + str(i % 100)),
        ]

    journal = directory / f"{rows}.rg"
    target = directory / f"{rows}-target.json"
    with journal.open("wb") as file:
        file.write(b"ROWGLS01")
        write_frame(file, {"kind": "start", "identity": {"synthetic": True}})
        write_frame(file, {"kind": "schema", "relation": relation})
        for start in range(0, rows, 1000):
            write_frame(
                file,
                {
                    "kind": "snapshot",
                    "relation_id": 42,
                    "rows": [values(i) for i in range(start, min(start + 1000, rows))],
                },
            )
        write_frame(file, {"kind": "seal", "lsn": "0/10"})
        for i in range(rows // 10):
            lsn = "0/" + format(32 + i * 32, "X")
            write_frame(
                file,
                {
                    "kind": "transaction",
                    "tx": {
                        "xid": i + 1,
                        "commit_lsn": lsn,
                        "end_lsn": lsn,
                        "time_micros_pg": 0,
                        "changes": [
                            {
                                "op": "update",
                                "relation": relation,
                                "old": None,
                                "new": values(i, True),
                            }
                        ],
                    },
                },
            )
    expected = [
        dict(zip(("id", "value", "note"), values(i, i < rows // 10)))
        for i in range(rows)
    ]
    cut = "0/" + format(32 + (rows // 10 - 1) * 32, "X")
    target.write_text(
        json.dumps(
            {
                "tables": [{"source_table": "public.bench", "rows": expected}],
                "consistency": {"mode": "controlled_quiescent", "source_cut": cut},
            },
            separators=(",", ":"),
        )
    )
    return journal, target


def reference(journal, target):
    """Independent dict reconstruction for this fixture's int/numeric/text schema only."""
    rows = {}
    digest = hashlib.sha256()
    with journal.open("rb") as f:
        header = f.read(8)
        assert header == b"ROWGLS01"
        digest.update(header)
        while head := f.read(8):
            length, crc = struct.unpack("<II", head)
            payload = f.read(length)
            assert zlib.crc32(payload) == crc
            digest.update(head)
            digest.update(payload)
            entry = json.loads(payload)
            if entry["kind"] == "snapshot":
                for row in entry["rows"]:
                    rows[int(row[0]["value"])] = row
            elif entry["kind"] == "transaction":
                for change in entry["tx"]["changes"]:
                    row = change["new"]
                    rows[int(row[0]["value"])] = row
    target_rows = json.loads(target.read_text())["tables"][0]["rows"]
    assert len(target_rows) == len(rows)
    for actual in target_rows:
        expected = rows[int(actual["id"]["value"])]
        assert decimal.Decimal(expected[1]["value"]) == decimal.Decimal(
            actual["value"]["value"]
        )
        assert expected[2] == actual["note"]
    print(json.dumps({"rows": len(rows), "sha256": digest.hexdigest()}))


def measured(command):
    start = time.perf_counter()
    result = subprocess.run(
        ["/usr/bin/time", "-l", *command], text=True, capture_output=True
    )
    elapsed = time.perf_counter() - start
    if result.returncode:
        raise RuntimeError(result.stderr)
    rss = re.search(r"(\d+)\s+maximum resident set size", result.stderr)
    return {
        "seconds": elapsed,
        "peak_rss_bytes": int(rss[1]) if rss else None,
        "output": json.loads(result.stdout),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", nargs=2)
    parser.add_argument("--output", type=Path, default=Path("runs/benchmark"))
    args = parser.parse_args()
    if args.reference:
        return reference(*map(Path, args.reference))
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for count in (1000, 10000, 100000):
        journal, target = fixture(args.output, count)
        for name, command in [
            ("native_inspect", [core.binary(), "inspect", str(journal)]),
            ("native_audit", [core.binary(), "audit", str(journal), str(target)]),
            (
                "python_reference",
                [sys.executable, __file__, "--reference", str(journal), str(target)],
            ),
        ]:
            repetitions = [measured(command) for _ in range(3)]
            for result in repetitions:
                output = result.pop("output")
                if name == "native_audit":
                    assert output["status"] == "matched_at_cut"
                else:
                    assert output["rows"] == count
            record = {
                "rows": count,
                "updates": count // 10,
                "engine": name,
                "journal_bytes": journal.stat().st_size,
                "target_bytes": target.stat().st_size,
                "median_seconds": statistics.median(r["seconds"] for r in repetitions),
                "peak_rss_bytes": max(r["peak_rss_bytes"] for r in repetitions),
                "repetitions": repetitions,
            }
            results.append(record)
            print(json.dumps(record), flush=True)
    report = {
        "environment": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "rust": subprocess.check_output(["rustc", "--version"], text=True).strip(),
            "python": platform.python_version(),
        },
        "method": "Synthetic three-column journal, 10% one-row updates, exact matching target. Three warm-cache runs including process startup; macOS time -l peak RSS bytes. Inspect does not keep history. Audit retains state and reconstructs histories only for discrepant keys. Python baseline checks CRC/SHA and exact scalar equality but supports only this fixture schema; it is not a full feature competitor.",
        "results": results,
    }
    (args.output / "results.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
