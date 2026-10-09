#!/bin/bash
# Make a release from a committed, tagged tree:   packaging/release.sh 0.1.0
#
# Produces, in dist/release-VERSION/:
#   cygnus-VERSION.tar.gz            source tarball  -> upload as an asset of the GitHub release vVERSION
#   PKGBUILD, .SRCINFO               the recipe      -> push to the AUR (aur.archlinux.org/cygnus.git)
#   cygnus-VERSION-1-any.pkg.tar.zst the finished build (tests run during the build) -> Gumroad
set -euo pipefail

die() { echo "release: $*" >&2; exit 1; }
[[ $# -eq 1 ]] || die "usage: packaging/release.sh VERSION"
version=$1
root=$(git rev-parse --show-toplevel 2>/dev/null) || die "not a git repository"
cd "$root"
git rev-parse -q --verify HEAD >/dev/null || die "nothing is committed yet"
[[ -z $(git status --porcelain) ]] || die "commit or stash your changes first"
git rev-parse -q --verify "refs/tags/v$version" >/dev/null || die "tag the release first: git tag -s v$version"
code_version=$(python3 -c 'import cygnus; print(cygnus.__version__)')
[[ $code_version == "$version" ]] || die "cygnus/__init__.py says $code_version, not $version"
grep -q "<release version=\"$version\"" data/metainfo/io.github.omranabdulaziz.Cygnus.metainfo.xml \
    || die "add <release version=\"$version\" date=...> to the AppStream metainfo"

out="dist/release-$version"
rm -rf "$out"
mkdir -p "$out"
git archive --format=tar.gz --prefix="Cygnus-$version/" -o "$out/cygnus-$version.tar.gz" "v$version"
sha=$(sha256sum "$out/cygnus-$version.tar.gz" | cut -d' ' -f1)
sed -e "s/^pkgver=.*/pkgver=$version/" -e "s/^pkgrel=.*/pkgrel=1/" \
    -e "s/^sha256sums=.*/sha256sums=('$sha')/" packaging/arch/release/PKGBUILD > "$out/PKGBUILD"
(cd "$out" && makepkg --printsrcinfo > .SRCINFO)
# The finished build is made from the very same tarball AUR users will download (found locally here).
(cd "$out" && makepkg -f --noconfirm)

cat <<DONE

Release $version is ready in $out:
  1. GitHub:  create the release v$version and upload cygnus-$version.tar.gz
  2. AUR:     copy PKGBUILD and .SRCINFO into your clone of ssh://aur@aur.archlinux.org/cygnus.git, commit, push
  3. Gumroad: upload $(cd "$out" && ls cygnus-"$version"-*.pkg.tar.zst)
DONE
