#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat >&2 <<USAGE
Keeps several bridged calls up at once and reports what the service did.

    load.sh <proxy-number> <sip-user> <sip-password> [calls] [seconds] [target]

Defaults: 20 calls, 20 seconds of talk time, 127.0.0.1:5060. The callee of the
session must be answering, otherwise nothing gets bridged.
USAGE
    exit 2
}

[ $# -ge 3 ] || usage

PROXY=$1
USER=$2
PASSWORD=$3
CALLS=${4:-20}
SECONDS_UP=${5:-20}
TARGET=${6:-127.0.0.1:5060}
RATE=${RATE:-10}
MEDIA_PORT=${MEDIA_PORT:-6100}
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

printf 'SEQUENTIAL\n' > "$WORK/subscribers.csv"
for (( i=0; i<CALLS; i++ )); do printf '%s\n' "$PROXY" >> "$WORK/subscribers.csv"; done

sipp -sf "$HERE/uac_load.xml" -inf "$WORK/subscribers.csv" \
     -s "$USER" -au "$USER" -ap "$PASSWORD" -mp "$MEDIA_PORT" \
     -m "$CALLS" -r "$RATE" -l "$CALLS" -d "${SECONDS_UP}s" \
     -timeout 300 -nostdin "$TARGET"
