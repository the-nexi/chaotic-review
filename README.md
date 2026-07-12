# chaotic-review

`chaotic-review` is an interactive review gate for Arch Linux systems using
the [Chaotic-AUR](https://aur.chaotic.cx/) binary repository. Before pacman
installs or upgrades an unapproved Chaotic-AUR artifact, an ALPM
pre-transaction hook shows the corresponding upstream AUR recipe changes and
asks for an explicit decision.

## What it reviews

For every pending Chaotic-AUR package, `chaotic-review`:

1. Checks the downloaded archive against the SHA-256 recorded in pacman's sync
   database and cross-checks its name, package base, version, and filename.
2. Retrieves the recipe snapshot that preceded the artifact's build time from
   Chaotic-AUR's PKGBUILDs repository.
3. Removes Chaotic-specific `.CI/` controls from both snapshots.
4. Shows only the upstream AUR recipe diff relative to the last approved
   baseline and binds the
   decision to the exact package archive SHA-256.

Chaotic-specific PKGBUILD interference is deliberately outside the review. The
tool does not display it, reconstruct it, verify it, or require the user to
understand it. The review answers the same question as a local AUR upgrade:
what changed in the package's upstream AUR recipe?

This does **not** prove that the binary was produced from the displayed recipe,
that it is reproducible, or that upstream source URLs are immutable. It also
does not audit modifications performed later by Chaotic-AUR's infrastructure.
If the upstream source snapshot cannot be retrieved, the exact artifact can
only be accepted by typing `OVERRIDE`; that degraded approval is recorded.

## Requirements

- Arch Linux with pacman 7 or newer
- A configured `[chaotic-aur]` repository
- An interactive terminal reachable from the pacman or AUR-helper process

Runtime dependencies are declared by the package: `expac`, `less`,
`libarchive`, `python`, and `util-linux`.

## Installation

Once published in the AUR:

```sh
paru -S chaotic-review
```

To build the package from a release checkout:

```sh
makepkg -si
```

The package installs:

- `/usr/bin/chaotic-review`
- `/usr/lib/chaotic-review/`
- `/usr/share/libalpm/hooks/05-chaotic-review.hook`
- `/etc/chaotic-review.conf`
- `/usr/lib/tmpfiles.d/chaotic-review.conf`

Pacman preserves local changes to `/etc/chaotic-review.conf` during upgrades.
The default `review_user = auto` selects the non-root owner of the recovered
transaction terminal. Set an explicit local username if automatic detection is
not appropriate for the machine.

### Establish the initial baseline

After installation, trust the exact Chaotic-AUR artifacts already installed on
the system as the starting baseline:

```sh
sudo chaotic-review bootstrap
```

Bootstrap does not retrospectively audit those packages. It records their AUR
source snapshots as the initial diff baseline and ensures subsequent artifacts
require review.

## Using the review gate

Update normally with pacman or an AUR helper:

```sh
paru -Syu
```

When a new Chaotic-AUR artifact is encountered:

1. Inspect the colored recipe diff in `less`.
2. Quit the pager with `q`.
3. Type `YES` to approve the displayed AUR recipe changes and exact artifacts.
4. Type `OVERRIDE` only when the report says the AUR source diff is unavailable.

Any other response rejects the review and aborts the package transaction.
Previously approved state is reused only when the candidate archive SHA-256 is
identical, so same-version rebuilds are reviewed again.

Recipe text and filenames are treated as hostile terminal input. Control and
bidirectional-formatting characters are displayed visibly before project-owned
color is added, `less` runs in secure mode, snapshot expansion is bounded, and
recipe paths are normalized before temporary materialization.

## Commands

```text
chaotic-review status
    Show exact artifact approvals.

sudo chaotic-review review-cached PACKAGE...
    Review named candidates that have already been downloaded into pacman's cache.

sudo chaotic-review reset PACKAGE...
    Remove exact-artifact approvals for the named packages.

sudo chaotic-review bootstrap [--force]
    Trust installed Chaotic-AUR artifacts as the baseline. --force refreshes
    existing bootstrap records.
```

State is stored under `/var/lib/chaotic-review` and serialized under an
exclusive process lock. Approval and AUR source-snapshot records remain
available for inspection as JSON.

## Recovery and troubleshooting

If a package transaction cannot find the interactive terminal, run pacman
directly from a local terminal rather than through a detached service or GUI.
For a multi-user system, set `review_user` explicitly in
`/etc/chaotic-review.conf`.

If the upstream snapshot is unavailable, the report offers the explicit
`OVERRIDE` path. Reject the transaction if the missing diff is unexpected;
retry after connectivity or source history is restored.

To temporarily disable the gate while repairing a system, override the packaged
hook with the same filename in pacman's administrator hook directory:

```sh
sudo ln -s /dev/null /etc/pacman.d/hooks/05-chaotic-review.hook
```

Remove that override to re-enable review:

```sh
sudo rm /etc/pacman.d/hooks/05-chaotic-review.hook
```

To uninstall the program while retaining its review history:

```sh
sudo pacman -Rns chaotic-review
```

Pacman does not remove `/var/lib/chaotic-review`; delete it explicitly only if
the stored approvals and baselines are no longer wanted.

## Development

Run the complete local verification suite on Arch Linux:

```sh
make check
```

`make test` runs compilation and unit tests. `make integration-test` creates a
disposable package repository and pacman database, then exercises the actual
ALPM hook inside an unprivileged user namespace without modifying the host
package database.

The implementation is separated by responsibility:

- `cli.py` contains review orchestration and commands.
- `runtime.py` contains package, pacman, GitLab, terminal, and state boundaries.
- `diff.py` filters Chaotic controls and safely renders AUR recipe diffs.
- `models.py` contains shared configuration and value objects.

Release tags are numeric versions matching `pkgver` in `PKGBUILD` and `VERSION`
in `src/chaotic_review/models.py`.

## License

Copyright © 2026 Oleg Chagaev.

`chaotic-review` is free software licensed under the
[GNU General Public License version 3 or later](LICENSE).
