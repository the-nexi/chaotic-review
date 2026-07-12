#!/usr/bin/bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$project_dir/src"
work=$(mktemp -d)
marker=/tmp/chaotic-review-integration-marker
cleanup() {
    rm -rf "$work" "$marker"
}
trap cleanup EXIT

mkdir -p "$work"/{build,cache,db,hooks,repo,state}
chmod 755 "$work" "$work"/{cache,db,state}
for system_hook in /usr/share/libalpm/hooks/*.hook; do
    ln -s /dev/null "$work/hooks/$(basename "$system_hook")"
done

cat >"$work/build/PKGBUILD" <<'PKGBUILD'
pkgname=chaotic-review-fixture
pkgver=1
pkgrel=1
pkgdesc='Chaotic review integration fixture'
arch=(any)
license=(MIT)
package() {
    install -Dm644 /dev/null "$pkgdir/tmp/chaotic-review-integration-marker"
}
PKGBUILD
(cd "$work/build" && makepkg --nodeps --noconfirm >/dev/null)
package=$(find "$work/build" -name '*.pkg.tar.*' -type f | head -1)
cp "$package" "$work/repo/"
repo-add "$work/repo/chaotic-aur.db.tar.gz" "$work/repo/$(basename "$package")" >/dev/null

cat >"$work/pacman.conf" <<EOF
[options]
Architecture = auto
DBPath = $work/db/
CacheDir = $work/cache/
HookDir = $work/hooks/
SigLevel = Never

[chaotic-aur]
Server = file://$work/repo
EOF

cat >"$work/review.conf" <<EOF
[chaotic-review]
review_user = alpha
state_dir = $work/state
gitlab_project = 54867625
gitlab_api = http://127.0.0.1:9
pacman_config = $work/pacman.conf
EOF

cat >"$work/hooks/06-chaotic-review-integration.hook" <<EOF
[Trigger]
Operation = Install
Operation = Upgrade
Type = Package
Target = *

[Action]
Description = Integration-test Chaotic review gate
When = PreTransaction
Exec = /usr/bin/env PYTHONPATH=$source_dir /usr/bin/python3 -m chaotic_review --config $work/review.conf hook
NeedsTargets
AbortOnFail
NetworkAccess = allowed
EOF

as_root=(unshare -Ur)
"${as_root[@]}" pacman --config "$work/pacman.conf" -Sy --noconfirm >/dev/null

# An unapproved artifact in a noninteractive transaction must abort.
if "${as_root[@]}" pacman --config "$work/pacman.conf" -S chaotic-review-fixture --noconfirm </dev/null >/dev/null 2>&1; then
    echo "unapproved transaction unexpectedly succeeded" >&2
    exit 1
fi

# Seed approval for the exact repository artifact, then the real ALPM hook must pass.
PYTHONPATH="$source_dir" python3 - "$package" "$work/state" <<PY
import json, pathlib, sys
sys.path.insert(0, "$source_dir")
from chaotic_review import package_record
record = package_record(pathlib.Path(sys.argv[1]))
record.update({
    "repo": "chaotic-aur",
    "approval": "integration-fixture",
})
path = pathlib.Path(sys.argv[2]) / "packages" / "chaotic-review-fixture.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(record))
PY

"${as_root[@]}" pacman --config "$work/pacman.conf" -S chaotic-review-fixture --noconfirm >/dev/null
test -f "$marker"
"${as_root[@]}" pacman --config "$work/pacman.conf" -R chaotic-review-fixture --noconfirm >/dev/null
test ! -e "$marker"
echo "sandboxed ALPM hook integration: PASS"
