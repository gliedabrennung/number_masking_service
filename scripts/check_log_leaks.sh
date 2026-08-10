#!/usr/bin/env bash
#
# A full real phone number must not appear anywhere in the logs. Greps the
# container logs of the control plane and of Asterisk for the numbers that are
# currently configured for the demo subscribers.
#
# Usage: scripts/check_log_leaks.sh [extra-number ...]
#
set -uo pipefail

COMPOSE="docker compose"

numbers=()
[ -f .env ] && set -a && . ./.env && set +a
for var in SIP_A_USER SIP_B_USER SIP_C_USER; do
    value="${!var:-}"
    [ -n "$value" ] && numbers+=("$value")
done
numbers+=("$@")

if [ ${#numbers[@]} -eq 0 ]; then
    echo "no numbers to check — pass them as arguments or set SIP_*_USER in .env"
    exit 2
fi

echo "checking application logs for: ${numbers[*]}"

fail=0

app_logs=$($COMPOSE logs --no-color masking-app 2>/dev/null)
for number in "${numbers[@]}"; do
    digits="${number#+}"
    if grep -q -- "$digits" <<<"$app_logs"; then
        echo "LEAK: full number $digits found in masking-app logs"
        grep -n -- "$digits" <<<"$app_logs" | head -5
        fail=1
    fi
done

if [ $fail -eq 0 ]; then
    echo "OK: application logs contain no full subscriber number"
fi

# Asterisk logs SIP signalling and therefore *does* contain real numbers: that
# is unavoidable at the protocol level and is why the container log is treated
# as sensitive and is not shipped anywhere. Reported separately, not as a
# failure of the application-level requirement.
ast_logs=$($COMPOSE logs --no-color asterisk 2>/dev/null | head -2000)
for number in "${numbers[@]}"; do
    digits="${number#+}"
    if grep -q -- "$digits" <<<"$ast_logs"; then
        echo "note: $digits appears in Asterisk signalling logs (expected, PII-sensitive)"
        break
    fi
done

exit $fail
