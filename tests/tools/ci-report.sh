#!/usr/bin/env bash
# CI diagnostics after a failure, as GitHub annotations (::error::), which
# show on the run's page and are readable through the API without logs access.
#   tests/tools/ci-report.sh FILE...   (plus status, service journals, VM consoles)
annotate() {   # title, text on stdin (the tail, ANSI stripped, newlines encoded)
    local body
    body=$(sed 's/\x1b\[[0-9;?]*[A-Za-z]//g' | tail -n "${LINES_MAX:-45}" | sed 's/%/%25/g; s/\r//g' | sed ':a;N;$!ba;s/\n/%0A/g')
    [ -n "$body" ] && echo "::error title=$1::$body"
}
for f in "$@"; do [ -s "$f" ] && annotate "$(basename "$f")" < "$f"; done
./fcvm status --offline 2>&1 | annotate "fcvm status"
sudo journalctl -u fcvm -u fcvm-jaild -u fcvm-net --no-pager -o cat 2>&1 | annotate "service journals"
for d in "${FCVM_HOME:-$HOME/.local/share/fcvm}"/vms/*/; do
    [ -f "$d/console.log" ] && LINES_MAX=15 annotate "console $(basename "$d")" < "$d/console.log"
done
exit 0
