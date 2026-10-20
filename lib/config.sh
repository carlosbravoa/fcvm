#!/usr/bin/env bash
# fcvm config [--json]: the settings in effect (fcvm.conf, the environment,
# the defaults in lib/common.sh) and where things are, so a program that
# drives fcvm never parses shell syntax. Credentials (REGISTRY_PASSWORD)
# aren't shown. Values are the strings the shell sees.
. "$(dirname "$0")/common.sh"

SETTINGS=(ARCH FC_VERSION KERNEL_CHANNEL UBUNTU_SUITE UBUNTU_MIRROR BASE_PACKAGES BASE_SIZE IMPORT_FREE_MB
          NET_BRIDGE NET_PREFIX NET_TAPS NET_ISOLATE NET_HOST_ACCESS NET_R_BRIDGE NET_R_PREFIX EGRESS_PORT NET_DNS
          VM_VCPUS VM_MEM_MIB VM_DISK VM_KERNEL_ARGS VOLUME_SIZE JAIL LEASE_GRACE
          NODE_ADMIT NODE_VCPUS NODE_MEM_MIB NODE_VMS NODE_DISK_MIB NODE_CPU_OVERCOMMIT NODE_MEM_OVERCOMMIT NODE_RESERVE_MIB)
: "${JAIL:=0}"

case ${1:-} in
    --json)
        args=()
        for v in "${SETTINGS[@]}"; do args+=(--arg "$v" "${!v}"); done
        jq -n "${args[@]}" --arg version "$(fcvm_version)" --argjson contract "$CONTRACT" \
            --arg conf "$FCVM_CONF" --argjson conf_exists "$([ -f "$FCVM_CONF" ] && echo true || echo false)" \
            --arg root "$FCVM_ROOT" --arg home "$FCVM_HOME" --arg images "$IMAGES_DIR" --arg vms "$VMS_DIR" \
            --arg volumes "$VOLUMES_DIR" --arg snapshots "$SNAPSHOTS_DIR" --arg kernels "$KERNELS_DIR" \
            --arg bin "$BIN_DIR" --arg cache "$CACHE_DIR" --arg build "$BUILD_DIR" --argjson names "$(printf '%s\n' "${SETTINGS[@]}" | jq -R . | jq -sc .)" \
            '{version: $version, contract: $contract, config_file: $conf, config_file_exists: $conf_exists,
              settings: ($names | map({key: ., value: $ARGS.named[.]}) | from_entries),
              paths: {code: $root, state: $home, images: $images, vms: $vms, volumes: $volumes, snapshots: $snapshots,
                      kernels: $kernels, bin: $bin, cache: $cache, build: $build}}' ;;
    "")
        printf '# fcvm %s, contract %s; settings from %s%s\n' "$(fcvm_version)" "$CONTRACT" "$FCVM_CONF" "$([ -f "$FCVM_CONF" ] || echo ' (absent: defaults)')"
        for v in "${SETTINGS[@]}"; do printf '%s=%s\n' "$v" "${!v}"; done
        printf '\n# paths\ncode=%s\nstate=%s\nimages=%s\nvms=%s\nvolumes=%s\nsnapshots=%s\nkernels=%s\nbin=%s\ncache=%s\nbuild=%s\n' \
            "$FCVM_ROOT" "$FCVM_HOME" "$IMAGES_DIR" "$VMS_DIR" "$VOLUMES_DIR" "$SNAPSHOTS_DIR" "$KERNELS_DIR" "$BIN_DIR" "$CACHE_DIR" "$BUILD_DIR" ;;
    *)  die_code invalid "usage: fcvm config [--json]" ;;
esac
