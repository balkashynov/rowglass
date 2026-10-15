"""Build explicit public source/demo archives. Private notes and real captures are excluded."""

from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP = [
    "README.md",
    "LICENSE",
    "THIRD_PARTY.md",
    "Cargo.toml",
    "Cargo.lock",
    "pyproject.toml",
    ".gitignore",
    "compose.yaml",
]
DIRS = [
    "native",
    "python/rowglass",
    "web",
    "lab",
    "scripts",
    "tests",
    "docs",
    "evidence",
    ".github",
]


def source_files():
    files = [ROOT / name for name in TOP]
    for directory in DIRS:
        files.extend(
            p
            for p in (ROOT / directory).rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and p.suffix != ".pyc"
            and p.name != ".DS_Store"
        )
    return sorted(files)


def archive(destination, files, base, prefix):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in files:
            z.write(path, prefix + "/" + path.relative_to(base).as_posix())


def main():
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/build_demo.py")], check=True)
    source = dist / "rowglass-source.zip"
    archive(source, source_files(), ROOT, "rowglass")
    shutil.copyfile(source, ROOT / "demo/rowglass-source.zip")
    demo = dist / "rowglass-demo.zip"
    archive(
        demo,
        sorted(
            p
            for p in (ROOT / "demo").rglob("*")
            if p.is_file() and p.name != ".DS_Store"
        ),
        ROOT / "demo",
        "rowglass-demo",
    )
    result = {
        p.name: {
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in (source, demo)
    }
    (dist / "checksums.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
