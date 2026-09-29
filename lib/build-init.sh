#!/usr/bin/env bash
# Build the static PID 1 injected into container-derived images.
. "$(dirname "$0")/common.sh"
need gcc strip

mkdir -p "$BUILD_DIR"
gcc -static -Os -Wall -Wextra -Wno-unused-result -o "$BUILD_DIR/fc-init" "$FCVM_ROOT/init/fc-init.c"
strip "$BUILD_DIR/fc-init"
log "built $BUILD_DIR/fc-init ($(du -h "$BUILD_DIR/fc-init" | cut -f1))"
