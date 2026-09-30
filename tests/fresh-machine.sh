#!/usr/bin/env bash
# tests/fresh-machine.sh [--keep] [--image 26.04]: the whole thing on a fresh
# Ubuntu, in a Multipass VM (QEMU, with nested KVM so fcvm can run VMs in it).
#
#   1. install this tree with install.sh --source, as a release would be
#   2. fcvm setup -y, then again after re-login (the new kvm group), which
#      boots a first VM
#   3. tests/run lint unit integration, from the installed copy
#   4. a real reboot: a VM with --restart unless-stopped must come back, and
#      must have been stopped cleanly at shutdown
#   5. delete the Multipass VM (unless --keep)
#
# Needs multipass, and ~4 CPUs, 8 GB RAM, 25 GB disk while it runs (about 15
# minutes, most of it the kernel build). This covers what CI can't: a truly
# fresh machine, and rebooting.
set -euo pipefail
cd "$(dirname "$0")/.."

keep=0 image=26.04
while [ $# -gt 0 ]; do
    case $1 in
        --keep)  keep=1; shift ;;
        --image) image=$2; shift 2 ;;
        *)       echo "usage: tests/fresh-machine.sh [--keep] [--image 26.04]" >&2; exit 2 ;;
    esac
done
command -v multipass >/dev/null || { echo "needs multipass (snap install multipass)" >&2; exit 1; }

vm=fcvm-fresh-$$
step() { printf '\n\e[1;34m== %s\e[0m\n' "$*"; }
in_vm() { multipass exec "$vm" -- bash -lc "$1"; }   # a login shell: PATH, groups as a user gets them
cleanup() { [ $keep = 1 ] && echo "kept: multipass shell $vm" || multipass delete --purge "$vm" 2>/dev/null || true; }
trap cleanup EXIT

step "launching $vm (Ubuntu $image)"
multipass launch "$image" --name "$vm" --cpus 4 --memory 8G --disk 25G >/dev/null

step "installing this tree ($(git describe --always --dirty 2>/dev/null || echo 'no git'))"
# (under $HOME: multipass is a snap, and snaps can't read the host's /tmp)
tmp=$(mktemp -d "$HOME/fcvm-fresh.XXXXXX"); trap 'rm -rf "$tmp"; cleanup' EXIT
mkdir "$tmp/fcvm"
git ls-files -co --exclude-standard | tar -cf - -T - | tar -xf - -C "$tmp/fcvm"
tar -czf "$tmp/fcvm.tar.gz" -C "$tmp" fcvm
multipass transfer "$tmp/fcvm.tar.gz" "$vm:/tmp/fcvm.tar.gz"
in_vm 'tar -xzf /tmp/fcvm.tar.gz -C /tmp && sh /tmp/fcvm/install.sh --source /tmp/fcvm'

step "fcvm setup -y (first run: packages, jailer, service, kernel)"
in_vm 'fcvm setup -y'
step "fcvm setup -y (after re-login: boots a first VM)"
in_vm 'fcvm setup -y' | tee "$tmp/setup2.log"
grep -q 'a VM booted Linux' "$tmp/setup2.log" || { echo "the test VM didn't boot" >&2; exit 1; }

step "tests/run lint unit integration (from the installed copy)"
in_vm 'cd ~/.local/lib/fcvm/current && tests/run lint unit integration'

step "reboot: a VM with --restart unless-stopped comes back"
in_vm 'fcvm create fresh-web alpine-latest --idle --restart unless-stopped && fcvm start fresh-web'
multipass restart "$vm"
for _ in $(seq 1 60); do in_vm 'fcvm exec fresh-web -- true' 2>/dev/null && break; sleep 2; done
in_vm 'fcvm exec fresh-web -- echo "fresh-web is back"'
in_vm 'journalctl -b -1 -u fcvm -o cat | grep -q "stopped 1 VM(s) for shutdown"' ||
    { echo "the VM wasn't stopped cleanly at shutdown" >&2; exit 1; }

step "all good on a fresh Ubuntu $image"
