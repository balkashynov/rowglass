"""Small transport adapter for the native evidence engine."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def binary() -> str:
    if configured := os.environ.get("ROWGLASS_CORE"):
        return configured
    for flavor in ("release", "debug"):
        path = ROOT / "target" / flavor / "rowglass-core"
        if path.exists():
            return str(path)
    raise RuntimeError("Build the native engine with cargo build --release first")


def run(command: str, journal: Path, *args: str) -> dict:
    result = subprocess.run(
        [binary(), command, str(journal), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


class Ingest:
    def __init__(self, journal: Path):
        journal.parent.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [binary(), "ingest", str(journal)],
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        self.ready = self._read()

    def _read(self) -> dict:
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(
                f"Native recorder stopped (exit {self.process.poll()}); see its error above"
            )
        return json.loads(line)

    def send(self, command: dict) -> dict:
        self.process.stdin.write(
            json.dumps(command, separators=(",", ":"), ensure_ascii=False) + "\n"
        )
        self.process.stdin.flush()
        return self._read()

    def entry(self, kind: str, **values) -> dict:
        return self.send({"op": "entry", "entry": {"kind": kind, **values}})

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(timeout=20)
        self.process.stdout.close()


def cell(value: str | None) -> dict:
    return {"kind": "null"} if value is None else {"kind": "text", "value": value}
