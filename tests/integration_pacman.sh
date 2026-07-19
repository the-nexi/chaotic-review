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

mkdir -p "$work"/{build-chaotic,build-higher,cache,db,hooks,repo-chaotic,repo-higher,state}
chmod 755 "$work" "$work"/{cache,db,state}
for system_hook in /usr/share/libalpm/hooks/*.hook; do
    ln -s /dev/null "$work/hooks/$(basename "$system_hook")"
done

cat >"$work/build-chaotic/PKGBUILD" <<'PKGBUILD'
pkgname=chaotic-review-fixture
pkgver=1
pkgrel=1
pkgdesc='Chaotic review integration fixture'
arch=(any)
license=(MIT)
package() {
    install -Dm644 /dev/null "$pkgdir/tmp/chaotic-review-integration-marker"
    printf 'chaotic\n' > "$pkgdir/tmp/chaotic-review-integration-marker"
}
PKGBUILD
(cd "$work/build-chaotic" && makepkg --nodeps --noconfirm >/dev/null)
package=$(find "$work/build-chaotic" -name '*.pkg.tar.*' -type f | head -1)
cp "$package" "$work/repo-chaotic/"
repo-add "$work/repo-chaotic/chaotic-aur.db.tar.gz" \
    "$work/repo-chaotic/$(basename "$package")" >/dev/null

cat >"$work/build-higher/PKGBUILD" <<'PKGBUILD'
pkgname=chaotic-review-fixture
pkgver=2
pkgrel=1
pkgdesc='Higher-priority duplicate fixture'
arch=(any)
license=(MIT)
package() {
    install -Dm644 /dev/null "$pkgdir/tmp/chaotic-review-integration-marker"
    printf 'higher\n' > "$pkgdir/tmp/chaotic-review-integration-marker"
}
PKGBUILD
(cd "$work/build-higher" && makepkg --nodeps --noconfirm >/dev/null)
higher_package=$(find "$work/build-higher" -name '*.pkg.tar.*' -type f | head -1)
cp "$higher_package" "$work/repo-higher/"
repo-add "$work/repo-higher/higher.db.tar.gz" \
    "$work/repo-higher/$(basename "$higher_package")" >/dev/null

cat >"$work/pacman.conf" <<EOF
[options]
Architecture = auto
DBPath = $work/db/
CacheDir = $work/cache/
HookDir = $work/hooks/
SigLevel = Never

[higher]
Server = file://$work/repo-higher

[chaotic-aur]
Server = file://$work/repo-chaotic
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
EOF

as_root=(unshare -Ur)
"${as_root[@]}" pacman --config "$work/pacman.conf" -Sy --noconfirm >/dev/null

# An unapproved artifact in a noninteractive transaction must abort.
if "${as_root[@]}" pacman --config "$work/pacman.conf" \
    -S chaotic-aur/chaotic-review-fixture --noconfirm </dev/null >/dev/null 2>&1; then
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

"${as_root[@]}" pacman --config "$work/pacman.conf" \
    -S chaotic-aur/chaotic-review-fixture --noconfirm >/dev/null
test -f "$marker"
grep -qx chaotic "$marker"

# The persisted approval must describe the lower-priority Chaotic artifact, not
# the same-name package from the first repository.
PYTHONPATH="$source_dir" python3 - "$package" "$higher_package" "$work/state" <<'PY'
import hashlib, json, pathlib, sys

chaotic = pathlib.Path(sys.argv[1])
higher = pathlib.Path(sys.argv[2])
state = pathlib.Path(sys.argv[3]) / "packages/chaotic-review-fixture.json"
approved = json.loads(state.read_text())["archive_sha256"]
chaotic_hash = hashlib.sha256(chaotic.read_bytes()).hexdigest()
higher_hash = hashlib.sha256(higher.read_bytes()).hexdigest()
assert chaotic_hash != higher_hash
assert approved == chaotic_hash
PY

"${as_root[@]}" pacman --config "$work/pacman.conf" -R chaotic-review-fixture --noconfirm >/dev/null
test ! -e "$marker"

# Without the exact Chaotic candidate in cache, the higher-priority duplicate
# is not a Chaotic transaction target and must install without review.
rm -f "$work/cache/$(basename "$package")"
rm -f "$work/state/packages/chaotic-review-fixture.json"
"${as_root[@]}" pacman --config "$work/pacman.conf" \
    -S higher/chaotic-review-fixture --noconfirm >/dev/null
grep -qx higher "$marker"
"${as_root[@]}" pacman --config "$work/pacman.conf" -R chaotic-review-fixture --noconfirm >/dev/null

# A previously cached exact Chaotic candidate intentionally makes a same-name
# non-Chaotic transaction conservative: without approval it must be reviewed.
cp "$package" "$work/cache/"
if "${as_root[@]}" pacman --config "$work/pacman.conf" \
    -S higher/chaotic-review-fixture --noconfirm </dev/null >/dev/null 2>&1; then
    echo "cached Chaotic duplicate unexpectedly bypassed review" >&2
    exit 1
fi

echo "sandboxed ALPM hook integration: PASS"
