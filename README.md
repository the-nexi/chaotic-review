# chaotic-review

`chaotic-review` is an MVP review gate for Arch Linux systems using the
[Chaotic-AUR](https://aur.chaotic.cx/) binary repository. It runs as an ALPM
pre-transaction hook underneath both `pacman` and `paru`.

When an unapproved Chaotic-AUR artifact is about to be installed, the tool:

1. Retrieves the corresponding recipe snapshot from Chaotic's PKGBUILD GitLab repository.
2. Shows recipe, `.CI`, package metadata, build metadata, and payload-path diffs.
3. Binds approval to the exact package archive SHA-256.
4. Aborts the transaction if review is rejected or no interactive terminal is available.

This is an experimental local project. It audits available provenance; it does
not prove reproducibility or cryptographically establish that a published
binary was produced from a particular source revision.

## Requirements

- Arch Linux with `pacman` 7 hook network controls
- `expac`, `python`, `libarchive`, `util-linux`, and `less`
- A configured `[chaotic-aur]` repository

## Install

Run as your normal desktop user:

```sh
./scripts/install.sh
```

The installer detects the invoking user, installs the executable and hook,
creates `/etc/chaotic-review.conf`, and bootstraps currently installed
Chaotic-AUR artifacts. Existing configuration is preserved; use
`--force-config` to regenerate it.

Continue updating normally:

```sh
paru -Syu
```

Quit the report pager with `q`, then type `YES` to approve the displayed exact
artifacts. If source provenance is unavailable, the explicit response is
`OVERRIDE`.

The ALPM hook explicitly recovers the invoking terminal from its process
ancestry because `NeedsTargets` replaces hook stdin and some pacman frontends
run hooks without a conventional controlling `/dev/tty`.

Unified diffs are rendered with colored headers, hunks, additions, and removals
through `less -R`.

## Commands

```sh
chaotic-review status
sudo chaotic-review review-cached PACKAGE...
sudo chaotic-review reset PACKAGE...
sudo chaotic-review bootstrap
```

## Uninstall

```sh
./scripts/uninstall.sh
```

The script interactively asks whether review state should be removed. For
automation, use `--purge-state` or `--keep-state`; noninteractive operation
preserves state by default.

## Development

```sh
make test
make integration-test
make check
```

The integration test creates a disposable package repository and pacman
database, then uses an unprivileged user namespace to exercise the real ALPM
hook behavior without modifying the host package database.

## Layout

- `src/`: Python CLI and hook implementation
- `config/`: generated system configuration template
- `packaging/`: ALPM hook
- `scripts/`: installation lifecycle
- `tests/`: unit tests and sandboxed pacman integration test

No license has been selected yet.
