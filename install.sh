#!/bin/sh
# fcvm installer.
#
#   curl -fsSL https://raw.githubusercontent.com/carlosbravoa/fcvm/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --version 0.5.0 --system
#
# Installs a tagged release of fcvm's code:
#   for you (default)   ~/.local/lib/fcvm/VERSION, command ~/.local/bin/fcvm, no root
#   --system            /opt/fcvm/VERSION, command /usr/local/bin/fcvm, with sudo
# Each release gets its own directory and `current` points at the active one,
# so upgrades switch atomically and the previous release stays for rollback.
# Your images, VMs and settings aren't part of the install: they live in
# ~/.local/share/fcvm and ~/.config/fcvm. Then run `fcvm setup`.
#
# Options: --version X.Y.Z (default: the newest release), --system,
#          --source PATH (a local release tarball or directory, e.g. offline).
# FCVM_LIB_DIR and FCVM_BIN_DIR override the install locations.
set -eu

REPO=carlosbravoa/fcvm
version="" system=0 source="" link=1

say() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case $1 in
        --version) [ $# -ge 2 ] || die "--version wants X.Y.Z"; version=${2#v}; shift 2 ;;
        --system)  system=1; shift ;;
        --source)  [ $# -ge 2 ] || die "--source wants a path"; source=$2; shift 2 ;;
        --no-link) link=0; shift ;;   # (fcvm upgrade: the command's link already goes through current)
        -h|--help) [ -f "$0" ] && sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' ||
                       echo "options: --version X.Y.Z, --system, --source PATH (see install.sh)"; exit 0 ;;
        *)         die "unknown option $1 (see --help)" ;;
    esac
done

[ "$(uname -s)" = Linux ] || die "fcvm runs on Linux (with KVM)"
[ "$(uname -m)" = x86_64 ] || die "fcvm supports x86_64 hosts (this is $(uname -m))"
for c in curl tar; do command -v "$c" >/dev/null || die "missing command: $c"; done

if [ $system = 1 ]; then
    lib=${FCVM_LIB_DIR:-/opt/fcvm} bin=${FCVM_BIN_DIR:-/usr/local/bin} SUDO=sudo
    [ "$(id -u)" = 0 ] && SUDO=""
else
    lib=${FCVM_LIB_DIR:-$HOME/.local/lib/fcvm} bin=${FCVM_BIN_DIR:-$HOME/.local/bin} SUDO=""
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/src"

if [ -n "$source" ]; then
    if [ -d "$source" ]; then cp -a "$source/." "$tmp/src/"
    else tar -xzf "$source" -C "$tmp/src" --strip-components=1; fi
    version=$(cat "$tmp/src/VERSION" 2>/dev/null) || die "$source isn't an fcvm release (no VERSION)"
else
    if [ -z "$version" ]; then
        # The newest vX.Y.Z tag (tags, not GitHub "releases": no release object needed).
        version=$(curl -fsSL "https://api.github.com/repos/$REPO/tags?per_page=100" |
            grep -o '"name": *"v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*"' | sed 's/.*"v//; s/"$//' |
            sort -V | tail -n 1) || true
        [ -n "$version" ] || die "couldn't find a release of $REPO (network?)"
    fi
    say "downloading fcvm $version"
    curl -fsSL "https://github.com/$REPO/archive/refs/tags/v$version.tar.gz" |
        tar -xz -C "$tmp/src" --strip-components=1 || die "couldn't download fcvm $version"
fi
[ -x "$tmp/src/fcvm" ] && [ "$(cat "$tmp/src/VERSION")" = "$version" ] || die "the download isn't fcvm $version"

dest=$lib/$version
if [ -d "$dest" ]; then
    say "fcvm $version is already in $dest"
else
    $SUDO mkdir -p "$lib"
    $SUDO rm -rf "$dest.new"
    $SUDO cp -a "$tmp/src" "$dest.new"
    $SUDO mv "$dest.new" "$dest"
fi
$SUDO ln -sfn "$version" "$lib/current.new" && $SUDO mv -T "$lib/current.new" "$lib/current"
if [ $link = 1 ]; then
    $SUDO mkdir -p "$bin"
    $SUDO ln -sfn "$lib/current/fcvm" "$bin/fcvm"
fi

# Keep the active release and the one before it (for rollback).
for old in $(ls "$lib" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | head -n -2); do
    [ "$old" = "$version" ] || $SUDO rm -rf "${lib:?}/$old"
done

say "installed fcvm $version in $dest"
[ $link = 1 ] || exit 0
case ":$PATH:" in
    *":$bin:"*) next="fcvm setup" ;;
    *)          next="$bin/fcvm setup"
                printf '   (%s isn'"'"'t on your PATH yet; new login shells usually add ~/.local/bin)\n' "$bin" >&2 ;;
esac
printf '\nNext: %s    (guided setup: packages, network, kernel, a first VM)\n' "$next" >&2
