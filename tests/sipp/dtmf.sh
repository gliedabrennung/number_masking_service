#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat >&2 <<USAGE
Types a DTMF extension code into a live call.

    dtmf.sh <proxy-number> <pin> <sip-user> <sip-password> [target]

Places a call to <proxy-number> as <sip-user>, waits for the prompt and types
<pin> as RFC 2833 events. The callee of the selected session must be
registered, otherwise the second leg cannot be dialled. Target defaults to
127.0.0.1:5060.
USAGE
    exit 2
}

[ $# -ge 4 ] || usage

PROXY=$1
PIN=$2
USER=$3
PASSWORD=$4
TARGET=${5:-127.0.0.1:5060}
MEDIA_PORT=${MEDIA_PORT:-6100}
GAP_MS=${GAP_MS:-400}
REPEAT_GAP_MS=${REPEAT_GAP_MS:-1200}
PCAP_DIR=${PCAP_DIR:-/usr/share/sip-tester}
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

blocks=""
previous=""
for (( i=0; i<${#PIN}; i++ )); do
    digit=${PIN:$i:1}
    gap=$GAP_MS
    [ "$digit" = "$previous" ] && gap=$REPEAT_GAP_MS
    previous=$digit
    blocks+="  <pause milliseconds=\"$gap\" />

  <nop>
    <action>
      <exec play_pcap_audio=\"$PCAP_DIR/dtmf_2833_$digit.pcap\" />
    </action>
  </nop>

"
done

awk -v blocks="$blocks" '{ if ($0 == "@DTMF_BLOCKS@") printf "%s", blocks; else print }' \
    "$HERE/uac_extension_code.xml.template" > "$WORK/uac_extension_code.xml"

printf 'SEQUENTIAL\n%s\n' "$PROXY" > "$WORK/subscribers.csv"

sipp -sf "$WORK/uac_extension_code.xml" -inf "$WORK/subscribers.csv" \
     -s "$USER" -au "$USER" -ap "$PASSWORD" -mp "$MEDIA_PORT" \
     -m 1 -timeout 60 -nostdin "$TARGET"
