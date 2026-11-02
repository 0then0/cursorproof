#!/usr/bin/env python3
"""Fetch the two pinned EmDash snapshots and install the harness dependencies.

This is the only step of the lab that uses the network. It downloads GitHub source archives for the
pinned commits, extracts only packages/core/src plus a few metadata files (nothing is executed),
verifies them against provenance.json and runs `npm ci --ignore-scripts` for the harness's own
lockfile. Upstream's install scripts and dependency tree are never used.

    python3 prepare.py --workdir /tmp/cursorproof-emdash-2691
    python3 prepare.py --workdir ... --record     # (re)write digests into provenance.json
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

LAB = Path(__file__).resolve().parent
PROVENANCE = LAB / "provenance.json"
ARCHIVE_URL = "https://codeload.github.com/emdash-cms/emdash/tar.gz/{commit}"
KEEP_FILES = {"LICENSE", "pnpm-lock.yaml", "tsconfig.base.json", "packages/core/package.json"}
KEEP_PREFIX = "packages/core/src/"
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT = 120


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(commit: str, target: Path) -> str:
    request = urllib.request.Request(
        ARCHIVE_URL.format(commit=commit), headers={"User-Agent": "cursorproof-lab"}
    )
    digest = hashlib.sha256()
    size = 0
    with (
        urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response,
        target.open("wb") as out,
    ):
        while chunk := response.read(1 << 20):
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise SystemExit("Archive exceeds the size limit")
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def extract(archive: Path, destination: Path) -> None:
    """Extract regular files from the allowlist; refuse anything unexpected."""
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            parts = Path(member.name).parts
            if ".." in parts or member.name.startswith("/"):
                raise SystemExit(f"Unsafe archive member: {member.name}")
            if len(parts) < 2:
                continue  # the archive's top-level directory
            relative = "/".join(parts[1:])
            if not (relative in KEEP_FILES or relative.startswith(KEEP_PREFIX)):
                continue
            if not member.isfile():
                continue  # directories, and symlinks, are never materialised
            if member.size > MAX_MEMBER_BYTES:
                raise SystemExit(f"Oversized archive member: {relative}")
            source = tar.extractfile(member)
            assert source is not None
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read())


def tree_digest(root: Path) -> tuple[str, int]:
    lines = []
    source = root / "packages/core/src"
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        lines.append(f"{path.relative_to(root).as_posix()}\0{sha256_bytes(path.read_bytes())}\n")
    return sha256_bytes("".join(lines).encode()), len(lines)


def check_kysely(root: Path, lock_integrity: str) -> None:
    lock = (root / "pnpm-lock.yaml").read_text()
    match = re.search(r"^  kysely@0\.29\.2:\n    resolution: \{integrity: ([^,}]+)", lock, re.M)
    if not match or match.group(1) != lock_integrity:
        raise SystemExit("Snapshot does not lock kysely 0.29.2 with the harness's integrity")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "cursorproof-emdash-2691",
        help="Disposable directory for snapshots and harness dependencies.",
    )
    parser.add_argument(
        "--record", action="store_true", help="Write observed digests to provenance.json."
    )
    arguments = parser.parse_args()
    workdir = arguments.workdir.resolve()
    provenance = json.loads(PROVENANCE.read_text())
    harness_lock = json.loads((LAB / "harness/package-lock.json").read_text())
    kysely_integrity = harness_lock["packages"]["node_modules/kysely"]["integrity"]

    workdir.mkdir(parents=True, exist_ok=True)
    for label, version in provenance["versions"].items():
        commit = version["commit"]
        root = workdir / "snapshots" / commit
        if root.exists():
            shutil.rmtree(root)
        with tempfile.TemporaryDirectory(dir=workdir, prefix="download-") as scratch:
            archive = Path(scratch) / "source.tar.gz"
            archive_digest = download(commit, archive)
            extract(archive, root)
        digest, count = tree_digest(root)
        check_kysely(root, kysely_integrity)
        recorded = provenance["snapshots"].setdefault(commit, {})
        if arguments.record:
            recorded.update(
                label=label,
                archive_sha256_observed=archive_digest,
                source_files=count,
                source_tree_sha256=digest,
            )
        elif recorded.get("source_tree_sha256") != digest:
            raise SystemExit(
                f"{label} ({commit}): packages/core/src digest differs from provenance.json"
            )
        print(f"{label}: {commit} {count} source files, tree sha256 {digest}")

    dependencies = workdir / "harness-deps"
    dependencies.mkdir(exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(LAB / "harness" / name, dependencies / name)
    subprocess.run(
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=dependencies,
        check=True,
        timeout=300,
    )
    if arguments.record:
        PROVENANCE.write_text(json.dumps(provenance, indent=2) + "\n")
        print(f"Recorded digests in {PROVENANCE.name}")
    print(f"Prepared {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
