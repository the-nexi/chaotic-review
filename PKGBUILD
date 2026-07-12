pkgname=chaotic-review
pkgver=0.2.0
pkgrel=1
pkgdesc="Interactive review gate for Chaotic-AUR package build-recipe changes"
arch=('any')
url="https://github.com/the-nexi/chaotic-review"
license=('GPL-3.0-or-later')
depends=('expac' 'less' 'libarchive' 'pacman>=7' 'python' 'util-linux')
makedepends=('git')
backup=('etc/chaotic-review.conf')
source=("git+$url.git#tag=$pkgver")
b2sums=('SKIP')

package() {
    cd "$pkgname"

    install -d "$pkgdir/usr/lib/chaotic-review/chaotic_review"
    install -m644 src/chaotic_review/*.py \
        "$pkgdir/usr/lib/chaotic-review/chaotic_review/"
    install -Dm755 scripts/chaotic-review "$pkgdir/usr/bin/chaotic-review"
    install -Dm644 config/chaotic-review.conf \
        "$pkgdir/etc/chaotic-review.conf"
    install -Dm644 packaging/05-chaotic-review.hook \
        "$pkgdir/usr/share/libalpm/hooks/05-chaotic-review.hook"
    install -Dm644 packaging/chaotic-review.tmpfiles \
        "$pkgdir/usr/lib/tmpfiles.d/chaotic-review.conf"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
