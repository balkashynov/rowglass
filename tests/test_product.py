import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from rowglass import core, replay, clickhouse
from rowglass.transport import CopyBoth


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "evidence.rg"
        self.rel = {
            "id": 42,
            "namespace": "public",
            "name": "orders",
            "columns": [
                {"name": "id", "oid": 20, "key": True},
                {"name": "payload", "oid": 25, "key": False},
            ],
        }
        self.events = [
            {"kind": "start", "identity": {"test": True}},
            {"kind": "schema", "relation": self.rel},
            {
                "kind": "snapshot",
                "relation_id": 42,
                "rows": [
                    [core.cell("1"), core.cell("quoted ' value; $$")],
                    [core.cell("9"), core.cell("unrelated")],
                ],
            },
            {"kind": "seal", "lsn": "0/10"},
        ]

    def tearDown(self):
        self.directory.cleanup()

    def write(self):
        with self.path.open("wb") as f:
            f.write(b"ROWGLS01")
            for e in self.events:
                payload = json.dumps(e).encode()
                f.write(struct.pack("<II", len(payload), zlib.crc32(payload)) + payload)

    def tx(self, xid, changes):
        self.events.append(
            {
                "kind": "transaction",
                "tx": {
                    "xid": xid,
                    "commit_lsn": f"0/{xid * 32:X}",
                    "end_lsn": f"0/{xid * 32:X}",
                    "time_micros_pg": 0,
                    "changes": changes,
                },
            }
        )

    def change(self, old, new, op="update"):
        return {"op": op, "relation": self.rel, "old": old, "new": new}

    def test_lineage_keeps_atomic_companions_and_key_predecessors(self):
        self.tx(1, [self.change(None, [core.cell("9"), core.cell("independent")])])
        self.tx(
            2,
            [
                self.change(None, [core.cell("1"), {"kind": "unchanged"}]),
                self.change(None, [core.cell("9"), core.cell("companion")]),
            ],
        )
        self.tx(
            3,
            [
                self.change(
                    [core.cell("1"), core.cell(None)],
                    [core.cell("2"), {"kind": "unchanged"}],
                )
            ],
        )
        self.write()
        result = replay.build(self.path, "public.orders", '["number:2e0"]')
        self.assertEqual(
            result["selected_transactions"], 3
        )  # tx 1 is a necessary companion predecessor
        self.assertEqual(result["baseline_rows"], 2)
        self.assertEqual(len(result["transactions"][1]["changes"]), 2)
        row = next(
            r for r in result["expected_rows"] if r["values"]["id"]["value"] == "2"
        )
        self.assertEqual(row["values"]["payload"]["value"], "quoted ' value; $$")
        self.assertIn("'quoted '' value; $$'", result["sql"])

    def test_unrelated_transactions_excluded_and_schema_guard(self):
        self.tx(1, [self.change(None, [core.cell("9"), core.cell("independent")])])
        self.tx(2, [self.change(None, [core.cell("1"), core.cell("selected")])])
        self.write()
        result = replay.build(self.path, "public.orders", '["number:1e0"]')
        self.assertEqual(result["selected_transactions"], 1)
        with self.assertRaises(ValueError):
            replay.build(self.path, "public.orders", '["number:1e0"]', "public")
        with self.assertRaises(RuntimeError):
            core.run("export", self.path, "--at", "0/1")

    def test_time_travel_cut_and_missing_keys(self):
        self.tx(1, [self.change(None, [core.cell("1"), core.cell("new")])])
        self.write()
        before = core.run("export", self.path, "--at", "0/10")
        after = core.run("export", self.path, "--at", "0/20")
        self.assertNotEqual(before["tables"][0]["rows"], after["tables"][0]["rows"])
        with self.assertRaises(ValueError):
            replay.build(self.path, "public.orders", '["number:99e0"]')

    def test_corrupt_journal_never_produces_replay(self):
        self.write()
        data = bytearray(self.path.read_bytes())
        data[-1] ^= 1
        self.path.write_bytes(data)
        with self.assertRaises(RuntimeError):
            replay.build(self.path, "public.orders", '["number:1e0"]')

    def test_keepalive_only_describes_end_not_ack(self):
        result = CopyBoth.decode(b"k" + struct.pack("!QqB", 999999, 1, 1))
        self.assertEqual(
            result, {"kind": "keepalive", "end": 999999, "sent": 1, "reply": True}
        )
        for bad in (b"", b"k", b"w" + bytes(23), b"x" + bytes(25)):
            with self.assertRaises(ValueError):
                CopyBoth.decode(bad)

    def test_target_version_ties_rejected_but_identical_retries_allowed(self):
        self.write()
        config = {"tables": [{"source": "public.orders", "target": "orders"}]}
        row = {"id": "1", "payload": "x", "__rg_version": "10", "__rg_deleted": "0"}
        with patch.object(
            clickhouse.Client,
            "query",
            return_value=([row, {**row, "payload": "different"}], "q"),
        ):
            with self.assertRaises(ValueError):
                clickhouse.snapshot(config, self.path)
        with patch.object(clickhouse.Client, "query", return_value=([row, row], "q")):
            result = clickhouse.snapshot(config, self.path)
            self.assertEqual(len(result["tables"][0]["rows"]), 1)
            self.assertEqual(result["tables"][0]["identical_latest_duplicates"], 1)


if __name__ == "__main__":
    unittest.main()
