"""Interactive review gate for packages selected from Chaotic-AUR."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import fcntl
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterable

from .models import (
    DEFAULT_CONFIG,
    REPO,
    VERSION,
    Config,
    ReviewError,
    SyncPackage,
    load_config,
)
from .diff import sanitize_untrusted_line, source_diff
from .runtime import (
    PACKAGE_NAME_RE,
    GitLabSource,
    Pacman,
    atomic_json,
    first,
    open_review_terminal,
    package_record,
    read_json,
    resolve_review_user,
)


class Reviewer:
    def __init__(
        self,
        config: Config,
        pacman: Pacman | None = None,
        source: GitLabSource | None = None,
        pager: Callable[[str], None] | None = None,
        prompt: Callable[[bool], bool] | None = None,
    ):
        self.config = config
        self.pacman = pacman or Pacman(config)
        self.source = source or GitLabSource(config)
        self.pager = pager or self._pager
        self.prompt = prompt or self._prompt

    @property
    def packages_dir(self) -> Path:
        return self.config.state_dir / "packages"

    @property
    def sources_dir(self) -> Path:
        return self.config.state_dir / "sources"

    def _package_path(self, name: str) -> Path:
        return self.packages_dir / f"{name}.json"

    def _source_path(self, base: str) -> Path:
        return self.sources_dir / f"{base}.json"

    def initialize_state(self) -> None:
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config.state_dir, 0o755)
        os.chmod(self.packages_dir, 0o755)
        os.chmod(self.sources_dir, 0o755)

    def _pager(self, report: str) -> None:
        review_user = resolve_review_user(self.config.review_user)
        fd, path = tempfile.mkstemp(prefix="chaotic-review-", suffix=".diff")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(report)
            os.chmod(path, 0o644)
            command = [
                "/usr/bin/runuser",
                "-u",
                review_user,
                "--",
                "/usr/bin/env",
                "LESSSECURE=1",
                "LESSHISTFILE=-",
                "/usr/bin/less",
                "-RFX",
                "--",
                path,
            ]
            with open_review_terminal(review_user) as terminal:
                result = subprocess.run(
                    command, stdin=terminal, stdout=terminal, stderr=terminal
                )
            if result.returncode:
                raise ReviewError(f"pager exited with status {result.returncode}")
        finally:
            os.unlink(path)

    def _prompt(self, override: bool) -> bool:
        review_user = resolve_review_user(self.config.review_user)
        expected = "OVERRIDE" if override else "YES"
        explanation = (
            "AUR source history is unavailable. Type OVERRIDE to approve these exact artifacts: "
            if override
            else "Type YES to approve the displayed AUR recipe diffs and exact artifacts: "
        )
        with open_review_terminal(review_user) as tty:
            tty.write(explanation)
            answer = tty.readline().strip()
        return answer == expected

    def bootstrap(self, force: bool = False) -> tuple[int, list[str]]:
        self.initialize_state()
        completed = 0
        warnings: list[str] = []
        prepared: list[tuple[SyncPackage, Path, dict, int]] = []
        for sync in self.pacman.installed_sync_packages():
            name = sync.name
            if sync.repo != REPO:
                continue
            destination = self._package_path(name)
            if destination.exists() and not force:
                continue
            try:
                version = self.pacman.installed_version(name)
                archive = self.pacman.installed_archive(name, version)
                record = package_record(archive)
                builddate = int(first(record["buildinfo"], "builddate", "0"))
                prepared.append((sync, destination, record, builddate))
            except (ReviewError, ValueError) as exc:
                warnings.append(f"{name}: {exc}")

        keys = {(sync.base, builddate) for sync, _, _, builddate in prepared}
        source_cache: dict[tuple[str, int], dict | Exception] = {}

        def bootstrap_snapshot(base: str, builddate: int) -> dict:
            try:
                return self.source.snapshot(base, builddate)
            except ReviewError as exc:
                if "no Chaotic PKGBUILD history" not in str(exc):
                    raise
                current = self.source.snapshot(base, int(dt.datetime.now(dt.UTC).timestamp()))
                current["baseline_note"] = (
                    "installed artifact predates available Chaotic GitLab history; "
                    "current recipe trusted as initial baseline"
                )
                return current

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(keys) or 1)) as pool:
            futures = {
                pool.submit(bootstrap_snapshot, base, builddate): (base, builddate)
                for base, builddate in keys
            }
            for future, key in futures.items():
                try:
                    source_cache[key] = future.result()
                except Exception as exc:  # Preserve per-package bootstrap progress.
                    source_cache[key] = exc

        for sync, destination, record, builddate in prepared:
            name = sync.name
            try:
                result = source_cache[(sync.base, builddate)]
                if isinstance(result, Exception):
                    raise result
                snapshot = result
                record.update(
                    {
                        "repo": REPO,
                        "source_revision": snapshot["revision"],
                        "source_snapshot_sha256": snapshot["snapshot_sha256"],
                        "approval": "trusted-bootstrap",
                        "approved_at": dt.datetime.now(dt.UTC).isoformat(),
                    }
                )
                atomic_json(destination, record)
                atomic_json(self._source_path(sync.base), snapshot)
                completed += 1
            except (ReviewError, ValueError, OSError) as exc:
                record.update(
                    {
                        "repo": REPO,
                        "source_revision": None,
                        "source_snapshot_sha256": None,
                        "source_error": str(exc),
                        "approval": "trusted-bootstrap",
                        "approved_at": dt.datetime.now(dt.UTC).isoformat(),
                    }
                )
                atomic_json(destination, record)
                completed += 1
                warnings.append(f"{name}: {exc}")
        return completed, warnings

    def review(self, names: Iterable[str]) -> tuple[bool, str]:
        self.initialize_state()
        sync_packages: list[SyncPackage] = []
        seen: set[str] = set()
        for name in names:
            name = name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            sync = self.pacman.sync_package(name)
            if sync and sync.repo == REPO:
                sync_packages.append(sync)
        if not sync_packages:
            return True, "no Chaotic-AUR transaction targets"

        pending: list[tuple[SyncPackage, dict]] = []
        for sync in sync_packages:
            archive = self.pacman.candidate_archive(sync)
            record = package_record(archive, sync)
            approved = read_json(self._package_path(sync.name))
            if approved and approved.get("archive_sha256") == record["archive_sha256"]:
                continue
            pending.append((sync, record))
        if not pending:
            return True, "all Chaotic-AUR artifacts already approved"

        by_base: dict[str, list[tuple[SyncPackage, dict]]] = {}
        for item in pending:
            by_base.setdefault(item[0].base, []).append(item)
        reports: list[str] = ["AUR PACKAGE RECIPE DIFFS\n" + "=" * 80 + "\n"]
        snapshots: dict[str, dict] = {}
        source_errors: dict[str, str] = {}
        for base in sorted(by_base):
            items = by_base[base]
            builddates = [int(first(record["buildinfo"], "builddate", "0")) for _, record in items]
            reports.append(
                f"\nPACKAGE BASE: {sanitize_untrusted_line(base)}\n" + "-" * 80 + "\n"
            )
            try:
                snapshot = self.source.snapshot(base, max(builddates))
                snapshots[base] = snapshot
                reports.append(source_diff(read_json(self._source_path(base)), snapshot))
            except ReviewError as exc:
                source_errors[base] = str(exc)
                reports.append(
                    f"AUR SOURCE DIFF UNAVAILABLE: {sanitize_untrusted_line(str(exc))}\n"
                )

        report = "".join(reports)
        self.pager(report)
        override = bool(source_errors)
        if not self.prompt(override):
            return False, "review rejected"

        now = dt.datetime.now(dt.UTC).isoformat()
        for sync, record in pending:
            snapshot = snapshots.get(sync.base)
            record.update(
                {
                    "repo": REPO,
                    "source_revision": snapshot.get("revision") if snapshot else None,
                    "source_snapshot_sha256": snapshot.get("snapshot_sha256") if snapshot else None,
                    "approval": "source-override" if sync.base in source_errors else "reviewed",
                    "source_error": source_errors.get(sync.base),
                    "approved_at": now,
                }
            )
            atomic_json(self._package_path(sync.name), record)
        for base, snapshot in snapshots.items():
            atomic_json(self._source_path(base), snapshot)
        return True, f"approved {len(pending)} exact artifact(s)"

    def status(self) -> str:
        rows: list[str] = []
        if not self.packages_dir.exists():
            return "No Chaotic-AUR approvals or bootstrap state found."
        for path in sorted(self.packages_dir.glob("*.json")):
            record = read_json(path) or {}
            name = sanitize_untrusted_line(str(record.get("name", path.stem)))
            version = sanitize_untrusted_line(str(record.get("version", "?")))
            approval = sanitize_untrusted_line(str(record.get("approval", "?")))
            digest = sanitize_untrusted_line(str(record.get("archive_sha256", ""))[:12])
            rows.append(
                f"{name:35} {version:25} {approval:17} {digest}"
            )
        header = f"{'PACKAGE':35} {'VERSION':25} {'STATE':17} SHA256"
        return "\n".join([header, *rows]) if rows else "No package state found."

    def reset(self, names: Iterable[str]) -> int:
        removed = 0
        for name in names:
            if not PACKAGE_NAME_RE.fullmatch(name):
                raise ReviewError(f"invalid package name: {name!r}")
            path = self._package_path(name)
            if path.exists():
                path.unlink()
                removed += 1
        return removed


def require_root() -> None:
    if os.geteuid() != 0:
        raise ReviewError("this operation changes system review state; rerun it with sudo")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="ALPM pre-transaction entry point; targets are read from stdin")
    bootstrap = sub.add_parser("bootstrap", help="trust currently installed Chaotic packages as baseline")
    bootstrap.add_argument("--force", action="store_true")
    sub.add_parser("status", help="show approved artifact state")
    reset = sub.add_parser("reset", help="remove approval for packages")
    reset.add_argument("packages", nargs="+")
    cached = sub.add_parser("review-cached", help="review cached sync candidates")
    cached.add_argument("packages", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    reviewer = Reviewer(config)
    try:
        if args.command == "status":
            print(reviewer.status())
            return 0
        require_root()
        config.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = config.state_dir / ".lock"
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.command == "bootstrap":
                completed, warnings = reviewer.bootstrap(args.force)
                print(f"Bootstrapped {completed} package artifact(s).")
                for warning in warnings:
                    print(
                        f"warning: {sanitize_untrusted_line(warning)}",
                        file=sys.stderr,
                    )
                return 0
            if args.command == "reset":
                print(f"Removed {reviewer.reset(args.packages)} approval(s).")
                return 0
            if args.command == "review-cached":
                accepted, message = reviewer.review(args.packages)
            else:
                accepted, message = reviewer.review(sys.stdin.read().splitlines())
            print(message)
            return 0 if accepted else 1
    except ReviewError as exc:
        print(f"chaotic-review: {sanitize_untrusted_line(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
