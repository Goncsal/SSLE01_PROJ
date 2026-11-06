#!/bin/sh
# Container entrypoint of the central Wazuh agent (one pod per K3s node).
#
# Environment: NODE_NAME (downward API), WAZUH_MANAGER (host name of the manager),
# WAZUH_GROUP (default central), WAZUH_AGENT_STATE (hostPath, default /agent-state), and
# optionally WAZUH_REGISTRATION_PASSWORD (from the Secret wazuh-enroll).
# The identity (client.keys) is kept in the state directory so a pod restart or a new image
# keeps the same agent id; the logcollector read positions live there too.
set -eu

OSSEC=/var/ossec
STATE="${WAZUH_AGENT_STATE:-/agent-state}"
NAME="pt.central.${NODE_NAME:?NODE_NAME is not set}"
GROUP="${WAZUH_GROUP:-central}"
MANAGER="${WAZUH_MANAGER:?WAZUH_MANAGER is not set}"

sed -i "s|<address>[^<]*</address>|<address>${MANAGER}</address>|" "$OSSEC/etc/ossec.conf"

mkdir -p "$STATE/logcollector"
# read positions survive restarts, so old log lines are not alerted a second time
rm -rf "$OSSEC/queue/logcollector"
ln -s "$STATE/logcollector" "$OSSEC/queue/logcollector"
chown -R wazuh:wazuh "$STATE/logcollector"

if [ -s "$STATE/client.keys" ]; then
  cp "$STATE/client.keys" "$OSSEC/etc/client.keys"
else
  if [ -n "${WAZUH_REGISTRATION_PASSWORD:-}" ]; then
    printf '%s' "$WAZUH_REGISTRATION_PASSWORD" > "$OSSEC/etc/authd.pass"
    chmod 640 "$OSSEC/etc/authd.pass"; chown root:wazuh "$OSSEC/etc/authd.pass"
  fi
  i=0
  until "$OSSEC/bin/agent-auth" -m "$MANAGER" -A "$NAME" -G "$GROUP" >/dev/null 2>&1; do
    i=$((i + 1)); [ "$i" -lt 30 ] || { echo "enrolment of $NAME failed" >&2; exit 1; }
    sleep 5
  done
  rm -f "$OSSEC/etc/authd.pass"
  cp "$OSSEC/etc/client.keys" "$STATE/client.keys"
  chmod 600 "$STATE/client.keys"
fi
chown root:wazuh "$OSSEC/etc/client.keys"; chmod 640 "$OSSEC/etc/client.keys"
unset WAZUH_REGISTRATION_PASSWORD

stop() { "$OSSEC/bin/wazuh-control" stop >/dev/null 2>&1 || true; exit 0; }
trap stop TERM INT
"$OSSEC/bin/wazuh-control" start
touch "$OSSEC/logs/ossec.log"
tail -F "$OSSEC/logs/ossec.log" &
wait $!
