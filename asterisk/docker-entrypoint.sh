#!/bin/sh
#
# Renders the Asterisk configuration from templates and starts Asterisk in the
# foreground. Secrets arrive through the environment and are written only into
# the container's /etc/asterisk, never into the repository.
#
set -eu

TEMPLATE_DIR=/etc/asterisk/templates
STATIC_DIR=/etc/asterisk/static
TARGET_DIR=/etc/asterisk

: "${ARI_APP:=masking}"
: "${ARI_USER:=masking}"
: "${ARI_PASSWORD:?ARI_PASSWORD must be set}"
: "${ARI_BIND_ADDR:=127.0.0.1}"
: "${SIP_A_USER:?SIP_A_USER must be set}"
: "${SIP_A_PASSWORD:?SIP_A_PASSWORD must be set}"
: "${SIP_B_USER:?SIP_B_USER must be set}"
: "${SIP_B_PASSWORD:?SIP_B_PASSWORD must be set}"
: "${SIP_C_USER:=}"
: "${SIP_C_PASSWORD:=}"
: "${ASTERISK_EXTERNAL_IP:=}"

# Only these names are substituted. Dialplan variables such as ${EXTEN} and
# ${CALLERID(num)} must survive untouched.
SUBST_VARS='${ARI_APP} ${ARI_USER} ${ARI_PASSWORD} ${ARI_BIND_ADDR} ${SIP_A_USER} ${SIP_A_PASSWORD} ${SIP_B_USER} ${SIP_B_PASSWORD} ${SIP_C_USER} ${SIP_C_PASSWORD} ${ASTERISK_EXTERNAL_IP}'

export ARI_APP ARI_USER ARI_PASSWORD ARI_BIND_ADDR \
       SIP_A_USER SIP_A_PASSWORD SIP_B_USER SIP_B_PASSWORD \
       SIP_C_USER SIP_C_PASSWORD ASTERISK_EXTERNAL_IP

cp -f "$STATIC_DIR"/*.conf "$TARGET_DIR"/

for template in "$TEMPLATE_DIR"/*.template; do
    [ -e "$template" ] || continue
    name=$(basename "$template" .template)
    envsubst "$SUBST_VARS" < "$template" > "$TARGET_DIR/$name"
    chmod 640 "$TARGET_DIR/$name"
done

# No directory may exist that a recording module could write into.
rm -rf /var/spool/asterisk/recording /var/spool/asterisk/monitor \
       /var/spool/asterisk/meetme /var/spool/asterisk/dictate

chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/log/asterisk \
                           /var/spool/asterisk /var/run/asterisk 2>/dev/null || true

echo "asterisk: configuration rendered (ari app=$ARI_APP, http bind=$ARI_BIND_ADDR)"
exec /usr/sbin/asterisk -f -U asterisk -G asterisk -vvv
