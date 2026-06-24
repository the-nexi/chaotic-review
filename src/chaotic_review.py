#!/usr/bin/python3
"""Interactive review gate for packages selected from Chaotic-AUR."""

from __future__ import annotations

import argparse
import configparser
import concurrent.futures
import datetime as dt
import difflib
import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


DEFAULT_CONFIG = Path("/etc/chaotic-review.conf")
DEFAULT_STATE = Path("/var/lib/chaotic-review")
DEFAULT_PROJECT = "54867625"
DEFAULT_API = "https://gitlab.com/api/v4"
REPO = "chaotic-aur"
VERSION = "0.1.0"
MAX_SOURCE_ARCHIVE = 20 * 1024 * 1024
MAX_TEXT_FILE = 2 * 1024 * 1024
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9@._+:-]+$")


class ReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    review_user: str = "root"
    state_dir: Path = DEFAULT_STATE
    gitlab_project: str = DEFAULT_PROJECT
    gitlab_api: str = DEFAULT_API
    pacman_config: Path = Path("/etc/pacman.conf")


@dataclass(frozen=True)
class SyncPackage:
    repo: str
    base: str
    name: str
    version: str
    filename: str
    sha256: str


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    parser = configparser.ConfigParser()
    if path.exists():
        parser.read(path)
    section = parser["chaotic-review"] if parser.has_section("chaotic-review") else {}
    return Config(
        review_user=section.get("review_user", "root"),
        state_dir=Path(section.get("state_dir", str(DEFAULT_STATE))),
        gitlab_project=section.get("gitlab_project", DEFAULT_PROJECT),
        gitlab_api=section.get("gitlab_api", DEFAULT_API).rstrip("/"),
        pacman_config=Path(section.get("pacman_config", "/etc/pacman.conf")),
    )


def run(command: list[str], *, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise ReviewError(f"command failed: {' '.join(command)}: {detail.strip()}") from exc
    return result.stdout


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_key_values(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for line in text.splitlines():
        if " = " not in line or line.startswith("#"):
            continue
        key, value = line.split(" = ", 1)
        result.setdefault(key.strip(), []).append(value.strip())
    return result


def first(values: dict[str, list[str]], key: str, default: str = "") -> str:
    found = values.get(key, [])
    return found[0] if found else default


def extract_member(archive: Path, member: str) -> str:
    return run(["/usr/bin/bsdtar", "-xOf", str(archive), member])


def package_record(archive: Path, sync: SyncPackage | None = None) -> dict:
    digest = sha256_file(archive)
    if sync and digest != sync.sha256:
        raise ReviewError(
            f"repository hash mismatch for {sync.name}: expected {sync.sha256}, got {digest}"
        )
    pkginfo_text = extract_member(archive, ".PKGINFO")
    buildinfo_text = extract_member(archive, ".BUILDINFO")
    pkginfo = parse_key_values(pkginfo_text)
    buildinfo = parse_key_values(buildinfo_text)
    members = run(["/usr/bin/bsdtar", "-tf", str(archive)]).splitlines()
    files = sorted(
        item
        for item in members
        if item and item not in {".PKGINFO", ".BUILDINFO", ".MTREE", ".INSTALL"}
    )
    return {
        "name": first(pkginfo, "pkgname"),
        "base": first(buildinfo, "pkgbase", first(pkginfo, "pkgbase")),
        "version": first(pkginfo, "pkgver"),
        "filename": archive.name,
        "archive_sha256": digest,
        "pkginfo": pkginfo,
        "buildinfo": buildinfo,
        "files": files,
    }


class GitLabSource:
    def __init__(self, config: Config, opener: Callable = urllib.request.urlopen):
        self.config = config
        self.opener = opener

    def _get(self, endpoint: str, params: dict[str, str]) -> tuple[bytes, dict[str, str]]:
        query = urllib.parse.urlencode(params)
        url = f"{self.config.gitlab_api}/projects/{self.config.gitlab_project}/{endpoint}?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "chaotic-review/1"})
        try:
            with self.opener(request, timeout=30) as response:
                data = response.read(MAX_SOURCE_ARCHIVE + 1)
                headers = dict(response.headers.items())
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise ReviewError(f"GitLab request failed for {endpoint}: {exc}") from exc
        if len(data) > MAX_SOURCE_ARCHIVE:
            raise ReviewError(f"GitLab response exceeds {MAX_SOURCE_ARCHIVE} bytes")
        return data, headers

    def snapshot(self, base: str, builddate: int) -> dict:
        until = dt.datetime.fromtimestamp(builddate, dt.UTC).isoformat().replace("+00:00", "Z")
        raw_commits, _ = self._get(
            "repository/commits",
            {"ref_name": "main", "path": base, "until": until, "per_page": "1"},
        )
        try:
            commits = json.loads(raw_commits)
        except json.JSONDecodeError as exc:
            raise ReviewError(f"invalid GitLab commit response for {base}") from exc
        if not commits:
            raise ReviewError(f"no Chaotic PKGBUILD history found for {base} before build date")
        commit = commits[0]
        revision = commit["id"]
        archive, _ = self._get(
            "repository/archive.tar.gz", {"sha": revision, "path": base}
        )
        files: dict[str, dict] = {}
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    parts = member.name.split("/", 2)
                    if len(parts) != 3 or parts[1] != base:
                        continue
                    relative = parts[2]
                    stream = tar.extractfile(member)
                    if stream is None:
                        continue
                    content = stream.read(MAX_TEXT_FILE + 1)
                    digest = hashlib.sha256(content).hexdigest()
                    entry: dict[str, object] = {"sha256": digest, "size": member.size}
                    if len(content) <= MAX_TEXT_FILE and b"\0" not in content:
                        try:
                            entry["text"] = content.decode("utf-8")
                        except UnicodeDecodeError:
                            pass
                    files[relative] = entry
        except (tarfile.TarError, OSError) as exc:
            raise ReviewError(f"invalid GitLab source archive for {base}: {exc}") from exc
        if "PKGBUILD" not in files:
            raise ReviewError(f"Chaotic source snapshot for {base} has no PKGBUILD")
        snapshot_hash = hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "base": base,
            "revision": revision,
            "committed_date": commit.get("committed_date", ""),
            "title": commit.get("title", ""),
            "snapshot_sha256": snapshot_hash,
            "files": files,
        }


class Pacman:
    def __init__(self, config: Config | None = None):
        self.config = config or Config()

    def _expac(self, *arguments: str) -> str:
        return run(["/usr/bin/expac", "--config", str(self.config.pacman_config), *arguments])

    def sync_package(self, name: str) -> SyncPackage | None:
        if not PACKAGE_NAME_RE.fullmatch(name):
            raise ReviewError(f"invalid package name from transaction: {name!r}")
        try:
            value = self._expac("-S", "-1", "%r|%e|%n|%v|%f|%h", name).strip()
        except ReviewError:
            return None
        parts = value.split("|", 5)
        if len(parts) != 6:
            raise ReviewError(f"unexpected sync metadata for {name}: {value!r}")
        return SyncPackage(*parts)

    def installed_names(self) -> list[str]:
        return [line for line in self._expac("-Q", "%n").splitlines() if line]

    def installed_sync_packages(self) -> list[SyncPackage]:
        installed = set(self.installed_names())
        selected: dict[str, SyncPackage] = {}
        output = self._expac("-S", "%r|%e|%n|%v|%f|%h")
        for line in output.splitlines():
            parts = line.split("|", 5)
            if len(parts) != 6:
                continue
            package = SyncPackage(*parts)
            if package.name in installed and package.name not in selected:
                selected[package.name] = package
        return list(selected.values())

    def installed_version(self, name: str) -> str:
        return self._expac("-Q", "%v", name).strip()

    def cache_dirs(self) -> list[Path]:
        output = run(["/usr/bin/pacman-conf", "--config", str(self.config.pacman_config), "CacheDir"])
        return [Path(line.strip()) for line in output.splitlines() if line.strip()]

    def candidate_archive(self, sync: SyncPackage) -> Path:
        for directory in self.cache_dirs():
            path = directory / sync.filename
            if path.is_file():
                return path
        raise ReviewError(f"downloaded candidate archive not found: {sync.filename}")

    def installed_archive(self, name: str, version: str) -> Path:
        matches: list[Path] = []
        for directory in self.cache_dirs():
            matches.extend(directory.glob(f"{name}-{version}-*.pkg.tar.*"))
        matches = [path for path in matches if not path.name.endswith(".sig")]
        if not matches:
            raise ReviewError(f"installed archive not found in pacman cache: {name} {version}")
        return max(matches, key=lambda path: path.stat().st_mtime)


def unified(old: Iterable[str], new: Iterable[str], old_name: str, new_name: str) -> str:
    return "".join(
        difflib.unified_diff(list(old), list(new), fromfile=old_name, tofile=new_name, lineterm="\n")
    )


def source_diff(old: dict | None, new: dict) -> str:
    old_files = old.get("files", {}) if old else {}
    new_files = new.get("files", {})
    output: list[str] = []
    for name in sorted(set(old_files) | set(new_files)):
        before = old_files.get(name)
        after = new_files.get(name)
        if before == after:
            continue
        before_text = before.get("text") if before else None
        after_text = after.get("text") if after else None
        if before_text is not None or after_text is not None:
            output.append(
                unified(
                    (before_text or "").splitlines(keepends=True),
                    (after_text or "").splitlines(keepends=True),
                    f"a/{name}",
                    f"b/{name}",
                )
            )
        else:
            output.append(
                f"binary {name}: {before.get('sha256') if before else '<absent>'} -> "
                f"{after.get('sha256') if after else '<absent>'}\n"
            )
    return "".join(output) or "(no recipe file changes since the reviewed baseline)\n"


PKGINFO_KEYS = (
    "pkgdesc", "url", "license", "depend", "optdepend", "makedepend", "checkdepend",
    "provides", "conflict", "replaces", "backup",
)
BUILDINFO_KEYS = (
    "pkgbuild_sha256sum", "packager", "builddate", "buildtool", "buildtoolver",
    "buildenv", "options", "installed",
)


def metadata_lines(record: dict, group: str, keys: tuple[str, ...]) -> list[str]:
    values = record.get(group, {})
    return [f"{key} = {item}\n" for key in keys for item in values.get(key, [])]


def package_diff(old: dict | None, new: dict) -> str:
    output: list[str] = []
    old_version = old.get("version", "<not installed/reviewed>") if old else "<none>"
    output.append(f"Version: {old_version} -> {new['version']}\n")
    output.append(f"Candidate: {new['filename']}\nSHA-256: {new['archive_sha256']}\n")
    for group, keys in (("pkginfo", PKGINFO_KEYS), ("buildinfo", BUILDINFO_KEYS)):
        before = metadata_lines(old or {}, group, keys)
        after = metadata_lines(new, group, keys)
        difference = unified(before, after, f"old-{group}", f"new-{group}")
        if difference:
            output.append(f"\n{difference}")
    files = unified(
        [f"{item}\n" for item in (old or {}).get("files", [])],
        [f"{item}\n" for item in new.get("files", [])],
        "old-file-list",
        "new-file-list",
    )
    output.append(f"\n{files or '(no payload path changes)\n'}")
    return "".join(output)


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
        if not Path("/dev/tty").exists():
            raise ReviewError("no controlling terminal available for review")
        fd, path = tempfile.mkstemp(prefix="chaotic-review-", suffix=".diff")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(report)
            os.chmod(path, 0o644)
            command = ["/usr/bin/runuser", "-u", self.config.review_user, "--", "/usr/bin/less", "-R", path]
            result = subprocess.run(command)
            if result.returncode:
                raise ReviewError(f"pager exited with status {result.returncode}")
        finally:
            os.unlink(path)

    def _prompt(self, override: bool) -> bool:
        expected = "OVERRIDE" if override else "YES"
        explanation = (
            "Source provenance is incomplete. Type OVERRIDE to approve these exact artifact hashes: "
            if override
            else "Type YES to approve all exact candidate artifact hashes: "
        )
        try:
            with open("/dev/tty", "r+", encoding="utf-8", buffering=1) as tty:
                tty.write(explanation)
                answer = tty.readline().strip()
        except OSError as exc:
            raise ReviewError("no controlling terminal available for approval") from exc
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
        reports: list[str] = ["CHAOTIC-AUR TRANSACTION REVIEW\n" + "=" * 80 + "\n"]
        snapshots: dict[str, dict] = {}
        source_errors: dict[str, str] = {}
        for base in sorted(by_base):
            items = by_base[base]
            builddates = [int(first(record["buildinfo"], "builddate", "0")) for _, record in items]
            reports.append(f"\nPACKAGE BASE: {base}\n" + "-" * 80 + "\n")
            try:
                snapshot = self.source.snapshot(base, max(builddates))
                snapshots[base] = snapshot
                old_snapshot = read_json(self._source_path(base))
                reports.append(
                    f"Chaotic source revision: {snapshot['revision']} ({snapshot['committed_date']})\n"
                )
                reports.append(source_diff(old_snapshot, snapshot))
                for _, record in items:
                    declared = first(record["buildinfo"], "pkgbuild_sha256sum")
                    fetched = snapshot["files"].get("PKGBUILD", {}).get("sha256", "")
                    if declared and fetched and declared != fetched:
                        reports.append(
                            "NOTE: .BUILDINFO PKGBUILD hash differs from the repository snapshot; "
                            "Chaotic build-time bump/interference may account for this.\n"
                        )
            except ReviewError as exc:
                source_errors[base] = str(exc)
                reports.append(f"SOURCE PROVENANCE UNAVAILABLE: {exc}\n")
            for sync, record in items:
                reports.append(f"\nARTIFACT: {sync.name}\n" + "~" * 80 + "\n")
                reports.append(package_diff(read_json(self._package_path(sync.name)), record))

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
            rows.append(
                f"{record.get('name', path.stem):35} {record.get('version', '?'):25} "
                f"{record.get('approval', '?'):17} {str(record.get('archive_sha256', ''))[:12]}"
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
                    print(f"warning: {warning}", file=sys.stderr)
                return 0 if not warnings else 1
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
        print(f"chaotic-review: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
