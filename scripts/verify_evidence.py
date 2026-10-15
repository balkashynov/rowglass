"""Recompute all saved audit verdicts at their recorded source cuts. No databases needed."""

import hashlib
import json
from pathlib import Path
from rowglass import core, replay

ROOT = Path(__file__).resolve().parents[1]
pipeline = ROOT / "evidence/pipeline"
journal = pipeline / "evidence.rg"
verification = json.loads((pipeline / "verification.json").read_text())
assert (
    hashlib.sha256(journal.read_bytes()).hexdigest()
    == verification["source"]["journal"]["sha256"]
)
for path in sorted(pipeline.glob("*-audit.json")):
    saved = json.loads(path.read_text())
    target = path.with_name(path.name.replace("-audit.json", "-target.json"))
    actual = core.run(
        "audit", journal, str(target), "--at", saved["source"]["source_cut"]
    )
    for field in ("status", "totals", "mismatches"):
        assert actual[field] == saved[field], (path.name, field)
    print(path.stem, actual["status"], actual["totals"])
for name in ("key-replay", "replay"):
    saved = json.loads((pipeline / (name + ".json")).read_text())
    actual = replay.build(
        journal, saved["selected"]["table"], saved["selected"]["key"], saved["schema"]
    )
    for field in (
        "sql_sha256",
        "selected_transactions",
        "baseline_rows",
        "expected_rows",
    ):
        assert saved[field] == actual[field], (name, field)
    assert (
        hashlib.sha256((pipeline / (name + ".sql")).read_bytes()).hexdigest()
        == saved["sql_sha256"]
    )
    print(
        name,
        actual["selected_transactions"],
        "transactions; SQL and expected state verified",
    )
print(
    "Frozen evidence verified. This rechecks saved input; it does not rerun the databases."
)
