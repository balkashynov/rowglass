from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from . import capture, core, clickhouse, replay


def run_cli():
    parser = argparse.ArgumentParser(description="Rowglass replication flight recorder")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Explicitly create a source publication")
    setup.add_argument("config", type=Path)
    record = commands.add_parser(
        "record", help="Record a consistent baseline and committed WAL"
    )
    record.add_argument("config", type=Path)
    record.add_argument("journal", type=Path)
    record.add_argument("--seconds", type=float)
    record.add_argument("--ready-file", type=Path)
    for command in ("inspect", "export"):
        cmd = commands.add_parser(command)
        cmd.add_argument("journal", type=Path)
        cmd.add_argument("--at", help="Reconstruct at a recorded commit LSN")
    audit = commands.add_parser("audit")
    audit.add_argument("journal", type=Path)
    audit.add_argument("target", type=Path)
    snap = commands.add_parser(
        "snapshot",
        help="Read latest visible ClickHouse versions; conservatively label as live",
    )
    snap.add_argument("config", type=Path)
    snap.add_argument("journal", type=Path)
    snap.add_argument("output", type=Path)
    hist = commands.add_parser(
        "history", help="Inspect a row using the canonical key from an audit"
    )
    hist.add_argument("journal", type=Path)
    hist.add_argument("table")
    hist.add_argument("key")
    rep = commands.add_parser(
        "replay", help="Compile a transaction-preserving backward row-lineage slice"
    )
    rep.add_argument("journal", type=Path)
    rep.add_argument("table")
    rep.add_argument("key")
    rep.add_argument("output", type=Path)
    rep.add_argument("--schema", default="rowglass_replay")
    report = commands.add_parser(
        "report", help="Build a portable browser investigation report"
    )
    report.add_argument("journal", type=Path)
    report.add_argument("audit", type=Path)
    report.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "setup":
        capture.setup(capture.load_config(args.config))
        print("Publication created")
    elif args.command == "record":
        print(
            json.dumps(
                capture.capture(
                    capture.load_config(args.config),
                    args.journal,
                    args.seconds,
                    args.ready_file,
                ),
                indent=2,
            )
        )
    elif args.command == "snapshot":
        result = clickhouse.snapshot(capture.load_config(args.config), args.journal)
        args.output.write_text(json.dumps(result, indent=2))
        print(args.output)
    elif args.command == "history":
        print(
            json.dumps(
                core.run("history", args.journal, args.table, args.key), indent=2
            )
        )
    elif args.command == "replay":
        result = replay.build(args.journal, args.table, args.key, args.schema)
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "replay.json").write_text(json.dumps(result, indent=2))
        (args.output / "replay.sql").write_text(result["sql"])
        print(
            f"{result['selected_transactions']} of {result['total_transactions']} transactions; {args.output / 'replay.sql'}"
        )
    elif args.command == "report":
        from .report import build

        print(build(args.journal, [args.audit], args.output))
    else:
        extra = [str(args.target)] if args.command == "audit" else []
        if getattr(args, "at", None):
            extra += ["--at", args.at]
        print(json.dumps(core.run(args.command, args.journal, *extra), indent=2))


def main():
    try:
        run_cli()
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        print("rowglass: " + str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
