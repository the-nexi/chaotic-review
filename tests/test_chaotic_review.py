from __future__ import annotations

import hashlib
import io
import json
import os
import pwd
import pty
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from chaotic_review import (  # noqa: E402
    Config,
    ReviewError,
    Reviewer,
    SyncPackage,
    open_review_terminal,
    package_record,
    source_diff,
)


def make_package(path: Path, name: str, base: str, version: str, files: list[str]) -> str:
    pkginfo = f"pkgname = {name}\npkgbase = {base}\npkgver = {version}\npkgdesc = fixture\n"
    buildinfo = (
        f"pkgname = {name}\npkgbase = {base}\npkgver = {version}\n"
        "pkgbuild_sha256sum = deadbeef\npackager = Unit Test\nbuilddate = 1700000000\n"
    )
    with tarfile.open(path, "w") as archive:
        for member, content in ((".PKGINFO", pkginfo), (".BUILDINFO", buildinfo)):
            data = content.encode()
            info = tarfile.TarInfo(member)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for member in files:
            data = member.encode()
            info = tarfile.TarInfo(member)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(base: str, text: str, revision: str = "new") -> dict:
    entry = {"text": text, "size": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()}
    files = {"PKGBUILD": entry}
    return {
        "base": base,
        "revision": revision,
        "committed_date": "2026-01-01T00:00:00Z",
        "title": "fixture",
        "snapshot_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        "files": files,
    }


class FakePacman:
    def __init__(self, packages: dict[str, tuple[SyncPackage, Path]], installed: dict[str, Path] | None = None):
        self.packages = packages
        self.installed = installed or {}

    def sync_package(self, name):
        value = self.packages.get(name)
        return value[0] if value else None

    def candidate_archive(self, sync):
        return self.packages[sync.name][1]

    def installed_names(self):
        return list(self.installed)

    def installed_sync_packages(self):
        return [self.packages[name][0] for name in self.installed]

    def installed_version(self, name):
        return package_record(self.installed[name])["version"]

    def installed_archive(self, name, version):
        return self.installed[name]


class FakeSource:
    def __init__(self, snapshots=None, error=None):
        self.snapshots = snapshots or {}
        self.error = error

    def snapshot(self, base, builddate):
        if self.error:
            raise ReviewError(self.error)
        return self.snapshots[base]


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(state_dir=self.root / "state")

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, name="demo", base="demo", version="2-1", repo="chaotic-aur"):
        archive = self.root / f"{name}-{version}-x86_64.pkg.tar"
        digest = make_package(archive, name, base, version, ["usr/bin/demo", "usr/share/demo"])
        sync = SyncPackage(repo, base, name, version, archive.name, digest)
        return sync, archive

    def test_package_record_and_repository_hash(self):
        sync, archive = self.fixture()
        record = package_record(archive, sync)
        self.assertEqual(record["version"], "2-1")
        self.assertEqual(record["files"], ["usr/bin/demo", "usr/share/demo"])
        bad = SyncPackage(sync.repo, sync.base, sync.name, sync.version, sync.filename, "0" * 64)
        with self.assertRaisesRegex(ReviewError, "repository hash mismatch"):
            package_record(archive, bad)

    def test_source_unified_diff(self):
        old = snapshot("demo", "pkgver=1\n", "old")
        new = snapshot("demo", "pkgver=2\n", "new")
        difference = source_diff(old, new)
        self.assertIn("-pkgver=1", difference)
        self.assertIn("+pkgver=2", difference)

    def test_terminal_can_be_reopened_from_an_inherited_pty_descriptor(self):
        master, slave = pty.openpty()
        try:
            user = pwd.getpwuid(os.getuid()).pw_name
            candidate = f"/proc/{os.getpid()}/fd/{slave}"
            with open_review_terminal(user, [candidate]) as terminal:
                terminal.write("terminal-probe\n")
            self.assertIn(b"terminal-probe", os.read(master, 1024))
        finally:
            os.close(master)
            os.close(slave)

    def test_terminal_lookup_fails_without_a_tty(self):
        user = pwd.getpwuid(os.getuid()).pw_name
        with self.assertRaisesRegex(ReviewError, "process ancestry"):
            open_review_terminal(user, ["/dev/null"])

    def test_terminal_is_recovered_after_controlling_tty_is_detached(self):
        master, slave = pty.openpty()
        child = os.fork()
        if child == 0:
            try:
                os.setsid()
                null = os.open("/dev/null", os.O_RDONLY)
                os.dup2(null, 0)
                os.dup2(slave, 1)
                os.dup2(slave, 2)
                os.close(null)
                os.close(master)
                os.close(slave)
                user = pwd.getpwuid(os.getuid()).pw_name
                with open_review_terminal(user) as terminal:
                    terminal.write("detached-terminal-recovered\n")
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(slave)
        output = bytearray()
        try:
            while True:
                try:
                    chunk = os.read(master, 1024)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
        finally:
            os.close(master)
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertIn(b"detached-terminal-recovered", output)

    def test_review_batch_approval_and_exact_hash_cache(self):
        one = self.fixture("one", "shared", "2-1")
        two = self.fixture("two", "shared", "2-1")
        reports = []
        prompts = []
        reviewer = Reviewer(
            self.config,
            pacman=FakePacman({"one": one, "two": two}),
            source=FakeSource({"shared": snapshot("shared", "pkgver=2\n")}),
            pager=reports.append,
            prompt=lambda override: prompts.append(override) or True,
        )
        accepted, message = reviewer.review(["one", "two", "unrelated"])
        self.assertTrue(accepted)
        self.assertIn("approved 2", message)
        self.assertEqual(prompts, [False])
        self.assertEqual(len(reports), 1)
        self.assertIn("ARTIFACT: one", reports[0])
        self.assertIn("ARTIFACT: two", reports[0])
        self.assertEqual(
            stat.S_IMODE((self.config.state_dir / "packages/one.json").stat().st_mode), 0o644
        )

        accepted, message = reviewer.review(["one", "two"])
        self.assertTrue(accepted)
        self.assertIn("already approved", message)
        self.assertEqual(len(reports), 1)

        # A same-version rebuild is a new review because approval is hash-bound.
        rebuilt_hash = make_package(
            one[1], "one", "shared", "2-1", ["usr/bin/demo", "usr/share/rebuilt"]
        )
        rebuilt = SyncPackage("chaotic-aur", "shared", "one", "2-1", one[1].name, rebuilt_hash)
        reviewer.pacman.packages["one"] = (rebuilt, one[1])
        accepted, message = reviewer.review(["one"])
        self.assertTrue(accepted)
        self.assertIn("approved 1", message)
        self.assertEqual(prompts, [False, False])

    def test_rejection_does_not_write_approval(self):
        item = self.fixture()
        reviewer = Reviewer(
            self.config,
            pacman=FakePacman({"demo": item}),
            source=FakeSource({"demo": snapshot("demo", "pkgver=2\n")}),
            pager=lambda report: None,
            prompt=lambda override: False,
        )
        accepted, _ = reviewer.review(["demo"])
        self.assertFalse(accepted)
        self.assertFalse((self.config.state_dir / "packages/demo.json").exists())

    def test_source_failure_requires_override_and_binds_artifact(self):
        item = self.fixture()
        override_values = []
        reviewer = Reviewer(
            self.config,
            pacman=FakePacman({"demo": item}),
            source=FakeSource(error="offline"),
            pager=lambda report: self.assertIn("SOURCE PROVENANCE UNAVAILABLE", report),
            prompt=lambda override: override_values.append(override) or True,
        )
        accepted, _ = reviewer.review(["demo"])
        self.assertTrue(accepted)
        self.assertEqual(override_values, [True])
        state = json.loads((self.config.state_dir / "packages/demo.json").read_text())
        self.assertEqual(state["approval"], "source-override")
        self.assertEqual(state["archive_sha256"], item[0].sha256)

    def test_bootstrap_only_selected_chaotic_packages(self):
        chaotic = self.fixture("chaotic", "chaotic", "1-1")
        official = self.fixture("official", "official", "1-1", "extra")
        pacman = FakePacman(
            {"chaotic": chaotic, "official": official},
            installed={"chaotic": chaotic[1], "official": official[1]},
        )
        reviewer = Reviewer(
            self.config,
            pacman=pacman,
            source=FakeSource({"chaotic": snapshot("chaotic", "pkgver=1\n")}),
        )
        count, warnings = reviewer.bootstrap()
        self.assertEqual((count, warnings), (1, []))
        self.assertTrue((self.config.state_dir / "packages/chaotic.json").exists())
        self.assertFalse((self.config.state_dir / "packages/official.json").exists())

    def test_no_chaotic_targets_is_silent(self):
        official = self.fixture("official", "official", "1-1", "extra")
        reviewer = Reviewer(
            self.config,
            pacman=FakePacman({"official": official}),
            source=FakeSource(),
            pager=lambda report: self.fail("pager should not run"),
            prompt=lambda override: self.fail("prompt should not run"),
        )
        accepted, message = reviewer.review(["official"])
        self.assertTrue(accepted)
        self.assertIn("no Chaotic", message)


if __name__ == "__main__":
    unittest.main()
