"""System boundaries for commands, state, terminals, packages, and sources."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import pwd
import re
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from .models import REPO, Config, ReviewError, SyncPackage
from .diff import validate_recipe_path

MAX_SOURCE_ARCHIVE = 20 * 1024 * 1024
MAX_TEXT_FILE = 2 * 1024 * 1024
MAX_SOURCE_FILE = 8 * 1024 * 1024
MAX_SOURCE_TOTAL = 64 * 1024 * 1024
MAX_SOURCE_MEMBERS = 10_000
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9@._+:-]+$")


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


def terminal_candidates(start_pid: int | None = None) -> list[str]:
    """Return /dev/tty followed by stdio descriptors from this process ancestry."""
    candidates = ["/dev/tty"]
    pid = start_pid or os.getpid()
    visited: set[int] = set()
    while pid > 1 and pid not in visited and len(visited) < 16:
        visited.add(pid)
        candidates.extend(f"/proc/{pid}/fd/{fd}" for fd in (0, 1, 2))
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
            parent_line = next(line for line in status.splitlines() if line.startswith("PPid:"))
            pid = int(parent_line.split()[1])
        except (OSError, StopIteration, ValueError):
            break
    return candidates


def resolve_review_user(
    review_user: str, candidates: Iterable[str] | None = None
) -> str:
    """Resolve ``auto`` to the non-root owner of a terminal in the process ancestry."""
    if review_user != "auto":
        try:
            pwd.getpwnam(review_user)
        except KeyError as exc:
            raise ReviewError(f"configured review user does not exist: {review_user}") from exc
        return review_user
    for candidate in candidates or terminal_candidates():
        try:
            descriptor = os.open(candidate, os.O_RDONLY | os.O_NOCTTY | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            metadata = os.fstat(descriptor)
            if os.isatty(descriptor) and metadata.st_uid != 0:
                try:
                    return pwd.getpwuid(metadata.st_uid).pw_name
                except KeyError:
                    continue
        finally:
            os.close(descriptor)
    raise ReviewError("could not determine the review user from the transaction terminal")


def open_review_terminal(review_user: str, candidates: Iterable[str] | None = None):
    """Open a terminal explicitly, even when an ALPM hook has no controlling TTY."""
    selected_candidates = list(candidates or terminal_candidates())
    review_user = resolve_review_user(review_user, selected_candidates)
    review_uid = pwd.getpwnam(review_user).pw_uid
    for candidate in selected_candidates:
        try:
            descriptor = os.open(candidate, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            metadata = os.fstat(descriptor)
            if not os.isatty(descriptor) or metadata.st_uid not in {0, review_uid}:
                os.close(descriptor)
                continue
            raw_terminal = io.FileIO(descriptor, mode="r+", closefd=True)
            return io.TextIOWrapper(
                raw_terminal, encoding="utf-8", line_buffering=True, write_through=True
            )
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
    raise ReviewError("no interactive terminal found in the hook process ancestry")


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


def required_single(values: dict[str, list[str]], key: str, source: str) -> str:
    found = values.get(key, [])
    if len(found) != 1 or not found[0]:
        raise ReviewError(f"{source} must contain exactly one {key}")
    return found[0]


def extract_member(archive: Path, member: str) -> str:
    return run(["/usr/bin/bsdtar", "-xOf", str(archive), member])


def package_record(archive: Path, sync: SyncPackage | None = None) -> dict:
    fd, temporary_name = tempfile.mkstemp(prefix="chaotic-review-package-")
    digest = hashlib.sha256()
    try:
        with archive.open("rb") as source, os.fdopen(fd, "wb") as destination:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                destination.write(block)
            destination.flush()
            os.fsync(destination.fileno())
        temporary = Path(temporary_name)
        actual_digest = digest.hexdigest()
        if sync and actual_digest != sync.sha256:
            raise ReviewError(
                f"repository hash mismatch for {sync.name}: "
                f"expected {sync.sha256}, got {actual_digest}"
            )
        pkginfo = parse_key_values(extract_member(temporary, ".PKGINFO"))
        buildinfo = parse_key_values(extract_member(temporary, ".BUILDINFO"))
        name = required_single(pkginfo, "pkgname", ".PKGINFO")
        version = required_single(pkginfo, "pkgver", ".PKGINFO")
        base = required_single(buildinfo, "pkgbase", ".BUILDINFO")
        if sync:
            mismatches = []
            if archive.name != sync.filename:
                mismatches.append(f"filename {archive.name!r} != {sync.filename!r}")
            if name != sync.name:
                mismatches.append(f"pkgname {name!r} != {sync.name!r}")
            if base != sync.base:
                mismatches.append(f"pkgbase {base!r} != {sync.base!r}")
            if version != sync.version:
                mismatches.append(f"pkgver {version!r} != {sync.version!r}")
            if mismatches:
                raise ReviewError(
                    f"repository identity mismatch for {sync.name}: " + "; ".join(mismatches)
                )
        members = run(["/usr/bin/bsdtar", "-tf", temporary_name]).splitlines()
        files = sorted(
            item
            for item in members
            if item and item not in {".PKGINFO", ".BUILDINFO", ".MTREE", ".INSTALL"}
        )
        return {
            "name": name,
            "base": base,
            "version": version,
            "filename": archive.name,
            "archive_sha256": actual_digest,
            "pkginfo": pkginfo,
            "buildinfo": buildinfo,
            "files": files,
        }
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


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
                members = tar.getmembers()
                if len(members) > MAX_SOURCE_MEMBERS:
                    raise ReviewError(
                        f"GitLab source archive exceeds {MAX_SOURCE_MEMBERS} members"
                    )
                declared_total = 0
                actual_total = 0
                for member in members:
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise ReviewError(
                            f"unsupported member in GitLab source archive: {member.name}"
                        )
                    parts = member.name.split("/", 2)
                    if len(parts) != 3 or parts[1] != base:
                        continue
                    relative = validate_recipe_path(parts[2])
                    if relative in files:
                        raise ReviewError(f"duplicate recipe path in GitLab archive: {relative}")
                    if member.size > MAX_SOURCE_FILE:
                        raise ReviewError(
                            f"recipe file exceeds {MAX_SOURCE_FILE} bytes: {relative}"
                        )
                    declared_total += member.size
                    if declared_total > MAX_SOURCE_TOTAL:
                        raise ReviewError(
                            f"GitLab source archive exceeds {MAX_SOURCE_TOTAL} expanded bytes"
                        )
                    stream = tar.extractfile(member)
                    if stream is None:
                        continue
                    content = stream.read(MAX_SOURCE_FILE + 1)
                    if len(content) != member.size:
                        raise ReviewError(f"invalid size for recipe file: {relative}")
                    actual_total += len(content)
                    if actual_total > MAX_SOURCE_TOTAL:
                        raise ReviewError(
                            f"GitLab source archive exceeds {MAX_SOURCE_TOTAL} expanded bytes"
                        )
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
        self._chaotic_index: dict[str, SyncPackage] | None = None

    def _expac(self, *arguments: str) -> str:
        return run(["/usr/bin/expac", "--config", str(self.config.pacman_config), *arguments])

    def chaotic_packages(self) -> dict[str, SyncPackage]:
        if self._chaotic_index is not None:
            return self._chaotic_index

        repositories = run(
            [
                "/usr/bin/pacman-conf",
                "--config",
                str(self.config.pacman_config),
                "--repo-list",
            ]
        ).splitlines()
        if REPO not in repositories:
            raise ReviewError(
                f"repository {REPO} is not configured in {self.config.pacman_config}"
            )

        packages: dict[str, SyncPackage] = {}
        output = self._expac("-S", "%r|%e|%n|%v|%f|%h")
        for line in output.splitlines():
            parts = line.split("|", 5)
            if len(parts) != 6:
                raise ReviewError(f"unexpected sync metadata: {line!r}")
            package = SyncPackage(*parts)
            if package.repo != REPO:
                continue
            if not PACKAGE_NAME_RE.fullmatch(package.name):
                raise ReviewError(f"invalid package name in Chaotic metadata: {package.name!r}")
            if package.name in packages:
                raise ReviewError(f"duplicate Chaotic metadata for package: {package.name}")
            packages[package.name] = package
        self._chaotic_index = packages
        return packages

    def sync_package(self, name: str) -> SyncPackage | None:
        if not PACKAGE_NAME_RE.fullmatch(name):
            raise ReviewError(f"invalid package name from transaction: {name!r}")
        return self.chaotic_packages().get(name)

    def installed_names(self) -> list[str]:
        return [line for line in self._expac("-Q", "%n").splitlines() if line]

    def installed_sync_packages(self) -> list[SyncPackage]:
        installed = set(self.installed_names())
        return [
            package
            for name, package in self.chaotic_packages().items()
            if name in installed
        ]

    def installed_version(self, name: str) -> str:
        return self._expac("-Q", "%v", name).strip()

    def cache_dirs(self) -> list[Path]:
        output = run(["/usr/bin/pacman-conf", "--config", str(self.config.pacman_config), "CacheDir"])
        return [Path(line.strip()) for line in output.splitlines() if line.strip()]

    def candidate_archive(self, sync: SyncPackage) -> Path | None:
        for directory in self.cache_dirs():
            path = directory / sync.filename
            if path.is_file() and self._archive_sha256(path) == sync.sha256:
                return path
        return None

    @staticmethod
    def _archive_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise ReviewError(f"could not hash candidate archive {path}: {exc}") from exc
        return digest.hexdigest()

    def installed_archive(self, name: str, version: str) -> Path:
        matches: list[Path] = []
        for directory in self.cache_dirs():
            matches.extend(directory.glob(f"{name}-{version}-*.pkg.tar.*"))
        matches = [path for path in matches if not path.name.endswith(".sig")]
        if not matches:
            raise ReviewError(f"installed archive not found in pacman cache: {name} {version}")
        return max(matches, key=lambda path: path.stat().st_mtime)
