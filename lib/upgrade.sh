#!/usr/bin/env bash
# fcvm upgrade [VERSION] [--source PATH]: move an installed fcvm to the newest
# release, or to VERSION (also to go back), or to a release tarball or
# directory you have (--source, e.g. offline). Uses the installer of the
# running release (the command's link goes through `current`, so it stays),
# then shows what else needs refreshing: root-owned copies (jail-setup, service
# install), and the running service. A git checkout updates with `git pull`.
. "$(dirname "$0")/common.sh"

want="" source=""
while [ $# -gt 0 ]; do
    case $1 in
        --source) source=${2:?--source wants a release tarball or directory}; shift 2 ;;
        -*)       die "usage: fcvm upgrade [VERSION] [--source PATH]" ;;
        *)        want=${1#v}; shift ;;
    esac
done
[ ! -e "$FCVM_ROOT/.git" ] || die "this fcvm is a git checkout ($FCVM_ROOT): update it with git pull, then ./fcvm status"
lib=$(dirname "$FCVM_ROOT")
[ -L "$lib/current" ] && [ "$(readlink -f "$lib/current")" = "$(readlink -f "$FCVM_ROOT")" ] ||
    die "$FCVM_ROOT isn't an installed release (installed ones live in .../fcvm/VERSION with a current link)"

args=(--no-link)
[ -w "$lib" ] || args+=(--system)
export FCVM_LIB_DIR=$lib
[ -z "$want" ] || args+=(--version "$want")
[ -z "$source" ] || args+=(--source "$source")

before=$(fcvm_version)
sh "$FCVM_ROOT/install.sh" "${args[@]}"
after=$(cat "$lib/current/VERSION")
[ "$before" != "$after" ] || { log "fcvm $after is current"; exit 0; }

log "fcvm $before -> $after"
# What the new release needs refreshed (jailer copies, boot network, service code).
"$lib/current/fcvm" status --offline | sed -n '/^.*To do:/,$p' || true
