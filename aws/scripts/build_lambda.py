"""Build the two Lambda deployment zips without Docker or ECR.

Usage (from the aws/ directory):
    python scripts/build_lambda.py --target all
    python scripts/build_lambda.py --target api
    python scripts/build_lambda.py --target worker

The script cross-installs manylinux CPython 3.12 wheels, copies the backend
package, prunes caches, and creates S3-ready zips in aws/dist/.

It also sets the executable bit on Playwright's bundled Node driver. A zip
created on Windows otherwise marks that file 0666 and the worker fails at
runtime with "Permission denied".
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

AWS_DIR = Path(__file__).resolve().parent.parent
BACKEND = AWS_DIR / "backend"
BUILD = AWS_DIR / "build"
DIST = AWS_DIR / "dist"

TARGETS = {
    "api": {
        "requirements": AWS_DIR / "requirements-api.txt",
        "handler": "api_handler.py",
    },
    "worker": {
        "requirements": AWS_DIR / "requirements-worker.txt",
        "handler": "worker_handler.py",
    },
}


def run(cmd: list[str]) -> None:
    print("+", " ".join(str(part) for part in cmd))
    subprocess.check_call([str(part) for part in cmd])


def install(target: str, build_dir: Path) -> None:
    requirements = TARGETS[target]["requirements"]
    if not requirements.exists():
        raise SystemExit(f"Missing requirements file: {requirements}")
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--no-compile",
            "--ignore-installed",
            "--platform",
            "manylinux2014_x86_64",
            "--python-version",
            "3.12",
            "--implementation",
            "cp",
            "--abi",
            "cp312",
            "--only-binary=:all:",
            "--target",
            str(build_dir),
            "-r",
            str(requirements),
        ]
    )


def copy_backend(target: str, build_dir: Path) -> None:
    handler = BACKEND / TARGETS[target]["handler"]
    if not handler.exists():
        raise SystemExit(f"Missing handler: {handler}")
    shutil.copy2(handler, build_dir / handler.name)

    package_src = BACKEND / "truthphone"
    package_dst = build_dir / "truthphone"
    shutil.copytree(
        package_src,
        package_dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )


def prune(build_dir: Path) -> None:
    for path in list(build_dir.rglob("__pycache__")):
        shutil.rmtree(path, ignore_errors=True)
    for path in list(build_dir.rglob("*.pyc")):
        try:
            path.unlink()
        except OSError:
            pass
    # Playwright's local browser cache is never needed: Chrome runs remotely.
    for path in list(build_dir.rglob(".local-browsers")):
        shutil.rmtree(path, ignore_errors=True)


def zip_dir(build_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    files = sorted(path for path in build_dir.rglob("*") if path.is_file())
    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in files:
            arcname = path.relative_to(build_dir).as_posix()
            info = zipfile.ZipInfo.from_file(path, arcname)
            if arcname == "playwright/driver/node":
                info.external_attr = (0o100755 << 16)
            else:
                info.external_attr = (0o100644 << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as source:
                archive.writestr(info, source.read())


def measure(build_dir: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(build_dir):
        for name in files:
            total += os.path.getsize(os.path.join(root, name))
    return total


def build_target(target: str) -> None:
    build_dir = BUILD / target
    print(f"\n=== Building {target} ===")
    install(target, build_dir)
    copy_backend(target, build_dir)
    prune(build_dir)

    zip_path = DIST / f"{target}.zip"
    zip_dir(build_dir, zip_path)
    unzipped = measure(build_dir) / (1024 * 1024)
    zipped = zip_path.stat().st_size / (1024 * 1024)
    print(
        f"{target}: {unzipped:.1f} MB unzipped, {zipped:.1f} MB zipped -> {zip_path}"
    )
    if unzipped > 250:
        raise SystemExit(
            f"{target} exceeds the 250 MB Lambda unzipped limit ({unzipped:.1f} MB)"
        )
    if target == "worker":
        with zipfile.ZipFile(zip_path) as archive:
            node = archive.getinfo("playwright/driver/node")
            mode = (node.external_attr >> 16) & 0o777
            print(f"playwright/driver/node mode in zip: {oct(mode)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target", choices=["all", "api", "worker"], default="all"
    )
    args = parser.parse_args()

    targets = ["api", "worker"] if args.target == "all" else [args.target]
    for target in targets:
        build_target(target)


if __name__ == "__main__":
    main()
