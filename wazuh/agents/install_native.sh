#!/usr/bin/env bash
# Install and enrol the Wazuh agent in one edge or station container (contract 15.1, 15.7).
#
#   [printf '%s\n' "$PASSWORD" |] wazuh/agents/install_native.sh <instance> <hier-id> <groups>
#
#   instance  Incus instance, e.g. porto-st-001 or porto-edge
#   hier-id   agent name and station id, e.g. pt.porto.st-001 or pt.porto.edge
#   groups    comma-separated agent groups, e.g. city-porto,station (must exist on the manager)
#
# The enrolment password (if the manager requires one) is read from stdin when stdin is not a
# terminal. It reaches the package scripts through the environment of the remote shell, is
# never on a command line, and the file the agent keeps it in is removed after enrolment.
# Idempotent: nothing is reinstalled, re-enrolled or restarted when the state already matches.
# Edges also get iptables (firewall-drop) and the evcharge-edge-drop script with its role and
# city network files; stations also get netwatch and remote commands.
. "$(dirname "${BASH_SOURCE[0]}")/../../infra/lib.sh"

AGENT_DIR="$ROOT/wazuh/agents"
AGENT_VERSION="${WAZUH_AGENT_VERSION:-4.14.9-1}"
REPO_URL="https://packages.wazuh.com/4.x/apt/"
KEY_URL="https://packages.wazuh.com/key/GPG-KEY-WAZUH"
KEYRING=/usr/share/keyrings/wazuh.asc
OSSEC=/var/ossec

[ $# -eq 3 ] || { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
inst="$1"; hid="$2"; groups="$3"
exists "$inst" && running "$inst" || { bad "instance $inst is not running"; exit 1; }

pw=""
if [ ! -t 0 ]; then IFS= read -r pw || true; fi

ix() { incus exec "$inst" -- "$@"; }

# push stdin to a file when the content differs; returns 0 if it changed
put_if_changed() { # <path> <mode>
  local path="$1" mode="$2" tmp new old
  tmp="$(mktemp)"; cat > "$tmp"
  new=$(sha256sum < "$tmp" | cut -d' ' -f1)
  old=$(ix sh -c "[ -f '$path' ] && sha256sum < '$path' | cut -d' ' -f1 || true")
  if [ "$new" != "$old" ]; then
    incus file push -q --mode "$mode" "$tmp" "$inst$path"; rm -f "$tmp"; return 0
  fi
  rm -f "$tmp"; return 1
}

role=station; case "$hid" in *.edge) role=edge ;; esac
mgr="$WAZUH_HOST.incus"

# ---- package ----------------------------------------------------------------------------
have=$(ix dpkg-query -W -f '${Version}' wazuh-agent 2>/dev/null || true)
if [ "$have" != "$AGENT_VERSION" ]; then
  log "$inst: installing wazuh-agent=$AGENT_VERSION (name $hid, groups $groups)"
  ix sh -c "export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null
    apt-get install -y -qq --no-install-recommends curl ca-certificates >/dev/null
    curl -fsS '$KEY_URL' -o '$KEYRING' && chmod 644 '$KEYRING'
    echo 'deb [signed-by=$KEYRING] $REPO_URL stable main' > /etc/apt/sources.list.d/wazuh.list
    apt-get update -qq >/dev/null"
  # name, groups and manager are not secret; the password is read from stdin by the remote shell
  printf '%s\n' "$pw" | ix sh -c "export DEBIAN_FRONTEND=noninteractive
    IFS= read -r pw
    export WAZUH_MANAGER='$mgr' WAZUH_AGENT_NAME='$hid' WAZUH_AGENT_GROUP='$groups'
    [ -z \"\$pw\" ] || export WAZUH_REGISTRATION_PASSWORD=\"\$pw\"
    apt-get install -y -qq --no-install-recommends wazuh-agent='$AGENT_VERSION' >/dev/null
    apt-get clean; rm -rf /var/lib/apt/lists/*"
  ok "$inst: wazuh-agent $AGENT_VERSION installed"
else
  ok "$inst: wazuh-agent $AGENT_VERSION already installed"
fi
ix dpkg -s wazuh-agent >/dev/null
ix apt-mark hold wazuh-agent >/dev/null

# ---- enrolment (only when the agent has no key yet) ------------------------------------------
if ! ix test -s $OSSEC/etc/client.keys; then
  log "$inst: enrolling as $hid"
  printf '%s\n' "$pw" | ix sh -c "IFS= read -r pw
    rm -f $OSSEC/etc/authd.pass
    if [ -n \"\$pw\" ]; then printf '%s' \"\$pw\" > $OSSEC/etc/authd.pass; chmod 640 $OSSEC/etc/authd.pass
      chown root:wazuh $OSSEC/etc/authd.pass; fi
    rc=0; $OSSEC/bin/agent-auth -m '$mgr' -A '$hid' -G '$groups' >/dev/null 2>&1 || rc=\$?
    rm -f $OSSEC/etc/authd.pass
    exit \$rc"
fi
ix rm -f $OSSEC/etc/authd.pass

# manager address in ossec.conf (the package sets it from WAZUH_MANAGER)
ix grep -q "<address>$mgr</address>" $OSSEC/etc/ossec.conf \
  || ix sed -i "0,/<address>[^<]*<\/address>/s||<address>$mgr</address>|" $OSSEC/etc/ossec.conf

changed=0
# ---- role specific ----------------------------------------------------------------------------
if [ "$role" = edge ]; then
  if ! ix dpkg -s iptables >/dev/null 2>&1; then
    ix sh -c 'DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null &&
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends iptables >/dev/null &&
      rm -rf /var/lib/apt/lists/*'
    ok "$inst: iptables installed"
  fi
  # evcharge-edge-drop acts only where role is edge and the address lies in this city network
  ecity="${hid#pt.}"; ecity="${ecity%%.*}"
  ix install -d -m 0755 /etc/evcharge /var/log/evcharge
  put_if_changed $OSSEC/active-response/bin/evcharge-edge-drop 0750 < "$ROOT/wazuh/active-response/evcharge-edge-drop" || true
  ix chown root:wazuh $OSSEC/active-response/bin/evcharge-edge-drop
  printf 'edge\n' | put_if_changed /etc/evcharge/role 0644 || true
  printf '%s\n' "${CITY_NET[$ecity]}" | put_if_changed /etc/evcharge/city-net 0644 || true
else
  city="${hid#pt.}"; city="${city%%.*}"
  # remote commands are required for the group's full_command (station-sockets)
  if ! ix grep -qs '^logcollector.remote_commands=1' $OSSEC/etc/local_internal_options.conf; then
    ix sh -c "echo 'logcollector.remote_commands=1' >> $OSSEC/etc/local_internal_options.conf"
    changed=1
  fi
  ix install -d -m 0755 /opt/evcharge /etc/evcharge /var/log/evcharge
  # logcollector does not pick up a file that is missing when it starts (netwatch only creates
  # its log on the first finding), so the file exists, empty, before the agent runs
  if ! ix test -e /var/log/evcharge/netwatch.json; then
    ix sh -c ': > /var/log/evcharge/netwatch.json; chmod 644 /var/log/evcharge/netwatch.json'
    changed=1
  fi
  # the response is sent to every agent; without /etc/evcharge/role = edge it does nothing
  put_if_changed $OSSEC/active-response/bin/evcharge-edge-drop 0750 < "$ROOT/wazuh/active-response/evcharge-edge-drop" || true
  ix chown root:wazuh $OSSEC/active-response/bin/evcharge-edge-drop
  put_if_changed /opt/evcharge/netwatch.py 0755 < "$AGENT_DIR/netwatch.py" || true
  sed "s|@STATION@|$hid|g" "$AGENT_DIR/netwatch.service" | put_if_changed /etc/systemd/system/netwatch.service 0644 && changed=2
  put_if_changed /etc/systemd/system/netwatch.timer 0644 < "$AGENT_DIR/netwatch.timer" && changed=2
  # addresses come from the live instances, never from the repo
  gw="${CITY_NET[$city]%/*}"
  allow=$(printf '# written by install_native.sh; edge, city gateway, Wazuh manager\n%s\n%s\n%s\n' \
    "$(ipof "$city-edge")" "$gw" "$(ipof "$WAZUH_HOST")")
  printf '%s\n' "$allow" | put_if_changed /etc/evcharge/netwatch-allow.txt 0644 || true
  if [ "$changed" = 2 ]; then ix systemctl daemon-reload; fi
  ix systemctl enable --now netwatch.timer >/dev/null 2>&1
fi

# ---- service --------------------------------------------------------------------------------------
if ! ix systemctl is-active --quiet wazuh-agent; then
  ix systemctl enable --now wazuh-agent >/dev/null 2>&1
  ok "$inst: wazuh-agent started"
elif [ "$changed" != 0 ]; then
  ix systemctl restart wazuh-agent
  ok "$inst: wazuh-agent restarted (new local option)"
else
  ok "$inst: wazuh-agent active"
fi
