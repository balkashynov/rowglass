"""Regenerate the recorded demo exclusively from the shipped synthetic evidence."""

import argparse
import hashlib
import json
from pathlib import Path
from rowglass.report import build

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "demo")
    args = parser.parse_args()
    evidence = ROOT / "evidence"
    pipeline = evidence / "pipeline"
    extra = {
        "verification": json.loads((pipeline / "verification.json").read_text()),
        "key_replay": json.loads(
            (pipeline / "key-replay-verification.json").read_text()
        ),
        "benchmarks": json.loads((evidence / "benchmark.json").read_text()),
        "benchmark_summary": {
            "title": "100,000 rows in 0.44 seconds",
            "description": "Native audit: 0.44 s median, 198 MiB peak memory. The restricted Python reference was faster at 0.30 s, using 256 MiB. Three warm-cache runs on macOS arm64; synthetic three-column rows and 10% updates. These measure reconstruction and comparison, not WAL transport or fsync throughput.",
        },
    }
    print(
        build(
            pipeline / "evidence.rg",
            sorted(pipeline.glob("*-audit.json")),
            args.output,
            extra=extra,
        )
    )
    # Engineering page and plain source notes stay inspectable in the offline package.
    import shutil

    shutil.copyfile(ROOT / "web/engineering.html", args.output / "engineering.html")
    (args.output / "notes").mkdir(exist_ok=True)
    for name in ("ENGINEERING.md", "DESIGN.md", "VALIDATION.md", "OPERATIONS.md"):
        shutil.copyfile(ROOT / "docs" / name, args.output / "notes" / name)
    # Refresh cached styles when a published demo changes, including existing tabs.
    style_version = hashlib.sha256((args.output / "style.css").read_bytes()).hexdigest()[:12]
    for page in (args.output / "index.html", args.output / "engineering.html"):
        page.write_text(
            page.read_text().replace(
                'href="style.css"', f'href="style.css?v={style_version}"'
            )
        )
    # Keep the engineering page's source download functional even without package.py.
    from package import archive, source_files

    archive(args.output / "rowglass-source.zip", source_files(), ROOT, "rowglass")


if __name__ == "__main__":
    main()
