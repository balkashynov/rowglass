"""Package self-contained recorded investigations. No server or database credentials required."""

from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
from . import core, replay

ASSETS = Path(__file__).resolve().parents[2] / "web"


def build(
    journal: Path, audits: list[Path], output: Path, *, extra: dict | None = None
):
    output.mkdir(parents=True, exist_ok=True)
    data_dir = output / "data"
    data_dir.mkdir(exist_ok=True)
    reports = []
    bundles = {}
    for i, path in enumerate(audits):
        report = json.loads(path.read_text())
        report.setdefault("scenario", path.stem)
        file = f"audit-{i + 1}.json"
        shutil.copyfile(path, data_dir / file)
        report["download"] = "data/" + file
        reports.append(report)
        for mismatch in report["mismatches"][:30]:
            key = mismatch["table"] + "\0" + mismatch["key"]
            if key in bundles:
                continue
            try:
                bundle = replay.build(journal, mismatch["table"], mismatch["key"])
                token = hashlib.sha256(key.encode()).hexdigest()[:12]
                (data_dir / (token + ".sql")).write_text(bundle["sql"])
                (data_dir / (token + ".json")).write_text(json.dumps(bundle, indent=2))
                bundle["download"] = "data/" + token + ".sql"
                bundle["manifest"] = "data/" + token + ".json"
                bundles[key] = bundle
            except (ValueError, RuntimeError) as error:
                bundles[key] = {"unavailable": str(error)}
    timeline = []
    for entry in replay.entries(journal):
        if entry["kind"] == "transaction":
            tx = entry["tx"]
            timeline.append(
                {
                    "xid": tx["xid"],
                    "lsn": tx["end_lsn"],
                    "changes": len(tx["changes"]),
                    "tables": sorted(
                        {replay.label(c["relation"]) for c in tx["changes"]}
                    ),
                }
            )
    result = {
        "format": "rowglass-report-v1",
        "source": core.run("inspect", journal),
        "audits": reports,
        "replays": bundles,
        "timeline": timeline,
        "extra": extra or {},
    }
    (data_dir / "report.json").write_text(json.dumps(result, indent=2))
    # JS data allows a downloaded report to open directly using file://, with no fetch permission or server.
    (output / "data.js").write_text(
        "window.ROWGLASS_DATA="
        + json.dumps(result, ensure_ascii=True).replace("</", "<\\/")
        + ";\n"
    )
    for file in ["index.html", "style.css", "app.js", "favicon.svg"]:
        shutil.copyfile(ASSETS / file, output / file)
    return output / "index.html"
