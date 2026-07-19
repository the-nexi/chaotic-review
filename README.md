# chaotic-review

`chaotic-review` is a pacman pre-transaction hook for reviewing Chaotic-AUR
updates. It shows upstream AUR recipe changes and requires explicit approval
before pacman installs the package.

The package archive is checked against pacman's repository metadata, and each
approval is bound to that exact archive. This does not prove reproducibility or
audit changes made by Chaotic-AUR's build infrastructure.

## Requirements

- pacman 7 or newer
- The `[chaotic-aur]` repository
- An interactive terminal available to pacman or the AUR helper

## Install

Install the VCS package from the AUR:

```sh
paru -S chaotic-review-git
```

Or build the latest `main` branch from this checkout:

```sh
makepkg -si
```

Create the initial baseline from installed Chaotic-AUR packages:

```sh
sudo chaotic-review bootstrap
```

Bootstrap trusts the currently installed artifacts without auditing them.

## Use

Update normally with `pacman` or an AUR helper. Review the displayed diff, quit
the pager with `q`, and enter `YES` to approve. Enter `OVERRIDE` only when the
tool reports that upstream source history is unavailable; any other response
aborts the transaction.

```text
chaotic-review status
sudo chaotic-review review-cached PACKAGE...
sudo chaotic-review reset PACKAGE...
sudo chaotic-review bootstrap [--force]
```

Configuration is stored in `/etc/chaotic-review.conf`; approvals and baselines
are stored under `/var/lib/chaotic-review`.

## Uninstall

```sh
sudo pacman -Rns chaotic-review-git
```

## Development

```sh
make check
```

## License

Copyright © 2026 Oleg Chagaev. Licensed under the
[GNU General Public License version 3 or later](LICENSE).
