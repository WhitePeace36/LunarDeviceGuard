# Maintainer: Timon <timon2201@gmail.com>
pkgname=devguard
pkgver=0.1.0
pkgrel=1
pkgdesc="Contain misbehaving hardware: IRQ storms, PCIe AER floods, USB reconnect loops, kernel log floods"
arch=('any')
url="https://github.com/WhitePeace36/linux-lunar"
license=('GPL-2.0-only')
depends=('python>=3.11')
backup=('etc/devguard.toml')
source=('devguard' 'devguard.toml' 'devguard.service')
sha256sums=('SKIP' 'SKIP' 'SKIP')

check() {
	python3 -m py_compile "$srcdir/devguard" 2>/dev/null || python3 -c "import ast,sys; ast.parse(open('$srcdir/devguard').read())"
}

package() {
	install -Dm755 devguard "$pkgdir/usr/bin/devguard"
	install -Dm644 devguard.toml "$pkgdir/etc/devguard.toml"
	install -Dm644 devguard.service "$pkgdir/usr/lib/systemd/system/devguard.service"
}
