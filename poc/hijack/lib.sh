#!/usr/bin/env bash
# Shared helpers for the hijacking PoC scripts (Phase 6, signal/identity hijacking).
# Design background: the Phase 6 hijacking design note. Interfaces: docs/interfaces.md v6
# 5.2, 9.2, 11, 12, 16.
#
# Every script here defaults to --dry-run: it prints the commands and target names it
# would use and changes nothing. A live run needs --run *and* the environment variable
# EVCHARGE_BASELINE_DONE=1 (set by the matching RUN card, never by this one): see
# require_baseline() below. Only `incus` is needed on the host.
set -euo pipefail

HIJACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HIJACK_DIR/../.." && pwd)"
# shellcheck source=../../infra/lib.sh
. "$ROOT/infra/lib.sh"

CITY_IMG="evcharge-city"
ROSTER="${ROSTER:-$ROOT/dev/roster.yaml}"

# wait_exec_ready <instance>: waits only until `incus exec` on <instance> succeeds (its
# agent channel is up) -- WITHOUT infra/lib.sh's wait_ready, whose second phase is an
# external DNS lookup (`getent hosts deb.debian.org`). That phase would spin for its full
# timeout and then fail on an instance that has been moved onto a deliberately isolated,
# no-internet-by-design network (mitm_aveiro.sh's aveiro-mitm) until something else (the
# interceptor, forwarding) gives it a path out. Use this right after such a move; use the
# full wait_ready again once the instance is back on a network with real internet access.
wait_exec_ready() {
  local i
  for i in $(seq 1 90); do incus exec "$1" -- true >/dev/null 2>&1 && return 0; sleep 2; done
  echo "instance $1 not exec-ready" >&2; return 1
}

# --- shared flag state; each script's own getopts loop sets these ---
DRY_RUN=1
RUN_REQUESTED=0
DURATION="${DURATION:-120}"
CITY="${CITY:-porto}"

# --- cleanup: scripts push one undo command per resource they create; run in reverse
# order on exit, success or failure, and on INT/TERM ---
CLEANUP_CMDS=()
add_cleanup() { CLEANUP_CMDS+=("$1"); }
# run_cleanup: in a dry run this must only PRINT what it would undo, never eval it -- a
# dry run must never execute `incus delete`, reset a real station's network, etc. Callers
# are responsible for calling add_cleanup only once the matching live resource actually
# exists (right after its creation succeeds), so a failed create is never "undone".
run_cleanup() {
  local i cmd
  for ((i = ${#CLEANUP_CMDS[@]} - 1; i >= 0; i--)); do
    cmd="${CLEANUP_CMDS[$i]}"
    if [ "$DRY_RUN" = 1 ]; then
      would "cleanup: $cmd"
    else
      log "cleanup: $cmd"
      eval "$cmd" || true
    fi
  done
  CLEANUP_CMDS=()
}
trap run_cleanup EXIT
trap 'run_cleanup; exit 130' INT TERM

# print a planned action without doing it
would() { printf '  would: %s\n' "$*"; }

# run_or_plan <argv...>: dry-run prints "would: argv"; a live run executes it as given
# (no shell re-parsing, so quoting the caller used is preserved exactly).
run_or_plan() {
  if [ "$DRY_RUN" = 1 ]; then
    would "$*"
  else
    log "$*"
    "$@"
  fi
}

# run_evsim <argv...>: runs a rogue/attacker evsim invocation. IMPORTANT: evsim's own
# retry loop (ChargerSim.run in sim/src/evsim/charger.py) swallows connection refusals
# internally and still exits 0 once its --duration elapses -- a refusal/detection is NOT
# what makes evsim return non-zero. So a non-zero exit here is read as a setup/infra
# problem, not a detection outcome, and is classified by exit code:
#   0        normal: evsim ran its full time-box. Whether it actually connected or was
#            detected is decided by the evidence check (verify.sh), never by this code.
#   124      the outer `timeout` fired: evsim did not exit on its own within duration+10s
#            (it may be hung). Surfaced as a visible WARNING, not silently treated as ok.
#   126,127  shell-level exec failure (not executable / command not found) or 2 (evsim's
#   or 2     own argparse rejected the CLI) -- always a setup bug. FATAL: this is
#            re-raised so `set -e` fails the script, same as any other real script error.
#   other    anything else is equally unexpected given evsim's internal exception
#            swallowing, so it is also treated as FATAL.
#
# stdin is always redirected from /dev/null (belt-and-suspenders on top of the `-n -T`
# the caller already passes to `incus exec`): an attacker-side evsim call must never be
# able to inherit a terminal stdin, whatever shape it's invoked in (e.g. piped through
# `tee` inside an interactive shell) -- that is exactly what set up an exec control
# channel that incusd then dropped ("close 1006 (abnormal closure)"), killing evsim
# outright, not a detection and not evsim's own fault.
run_evsim() {
  local rc=0
  log "$*"
  "$@" < /dev/null || rc=$?
  case "$rc" in
    0)
      echo "  evsim exited 0 (ran its full time-box; connection/detection outcome is" \
           "decided by the evidence check, never by this exit code)"
      ;;
    124)
      echo "  WARNING: evsim exited 124 -- the outer timeout fired, evsim did not exit on" \
           "its own within duration+10s (it may be hung). Treat this run as inconclusive," \
           "not as a clean pass." >&2
      ;;
    126|127|2)
      echo "  FATAL: evsim exited $rc (126=not executable, 127=command not found," \
           "2=evsim rejected its own CLI arguments) -- a setup/infra failure, not a" \
           "detection outcome; failing the script." >&2
      return "$rc"
      ;;
    *)
      echo "  FATAL: evsim exited $rc -- evsim's own retry loop normally returns 0 even" \
           "when every connection attempt was refused, so any other non-zero almost" \
           "always means a setup/infra failure; failing the script." >&2
      return "$rc"
      ;;
  esac
  return 0
}

# probe_connectivity <instance> <host> <port> <label> [tls 0|1]: read-only, side-effect-
# free diagnostic run FROM <instance> -- DNS, a raw TCP connect, and (tls=1) a real TLS
# handshake verified against the lab CA with hostname/SNI checking. No file is written
# and no instance is created or changed; it only execs into instances that already
# exist. Meant to be run once for the rogue and once for a real station so the operator
# sees them side by side, instead of evsim's connection loop silently swallowing the same
# failure (charger.py's per-attempt exception handling).
#
# Hang-safety (a probe must NEVER be able to stall or abort the scenario it runs inside):
#   - the kill-switch lives INSIDE the container (`incus exec -- timeout ... cmd`), so a
#     stuck remote process is actually killed and `incus exec` gets a clean EOF; an OUTER
#     `timeout --kill-after=...` wraps the whole `incus exec` too, purely as a backstop
#     in case the local client itself wedges. A single outer timeout around just the
#     client is not enough: killing the local `incus exec` process does not reliably
#     close the remote side's write end, so `$(...)` can block on EOF well past the
#     outer deadline -- this is exactly what hung a live run for 52 minutes before this
#     fix. Both layers get --kill-after so a SIGTERM that's ignored is still followed by
#     SIGKILL.
#   - `-n`/`-T` (or stdin piped from a fully-buffered local string, never from another
#     live `incus exec`) keep every call non-interactive with no pty and no FD that can
#     be left open waiting for a peer.
#   - every verdict is read from the PAYLOAD the in-container command printed (python's
#     own "ok"/"fail: ..."; getent's own stdout), never from incus's transport exit code:
#     incus can log benign transport noise on close (e.g. a broken-pipe warning) that
#     makes its own exit code non-zero even though the payload printed "ok" -- that
#     exact mismatch previously showed up as a bogus "FAIL: ok" line.
#   - probe_step (below) always returns 0 itself, so no probe failure can ever abort the
#     calling script under `set -e`; an unreadable/empty payload prints a clear
#     "PROBE ERROR/TIMEOUT" line and the script continues to the real evsim steps.
probe_step() {
  local inst="$1" inner_t="$2"; shift 2
  # `cmd && rc=0 || rc=$?`, not `cmd; rc=$?`: under `set -e` a plain failing assignment
  # statement aborts the function (and the whole calling script) on the spot, before
  # `rc=$?` is ever reached -- and a timeout/transport failure is exactly the case this
  # probe exists to observe, not crash on.
  PROBE_OUT="$(timeout --kill-after=3 "$((inner_t + 6))" \
    incus exec -n -T "$inst" -- timeout --kill-after=2 "$inner_t" "$@" 2>/dev/null)" \
    && PROBE_RC=0 || PROBE_RC=$?
}

probe_connectivity() {
  local inst="$1" host="$2" port="$3" label="$4" tls="${5:-0}"
  local ca_pem ca_rc

  probe_step "$inst" 4 getent hosts "$host"
  if [ -n "$PROBE_OUT" ]; then
    echo "  [$label] DNS $host -> $PROBE_OUT"
  else
    echo "  [$label] DNS $host: FAIL/PROBE TIMEOUT (rc=$PROBE_RC)"
  fi

  probe_step "$inst" 5 /opt/evcharge/venv/bin/python -c '
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
    print("ok")
except Exception as e:
    print("fail: " + str(e))
finally:
    s.close()
' "$host" "$port"
  case "$PROBE_OUT" in
    ok) echo "  [$label] TCP $host:$port OK" ;;
    fail:*) echo "  [$label] TCP $host:$port FAIL: ${PROBE_OUT#fail: }" ;;
    "") echo "  [$label] TCP $host:$port PROBE ERROR/TIMEOUT (rc=$PROBE_RC)" ;;
    *) echo "  [$label] TCP $host:$port PROBE ERROR: unexpected output [$PROBE_OUT] (rc=$PROBE_RC)" ;;
  esac

  if [ "$tls" = 1 ]; then
    # Read the CA cert as its own bounded, self-contained step first (rather than piping
    # two live `incus exec` processes together, which doubles the hang surface); it ends
    # up as a plain local string, fed to the second step from memory, never from a
    # second live remote process.
    ca_pem="$(timeout --kill-after=2 7 incus exec -n -T lab-ca -- timeout --kill-after=1 4 \
      cat /root/ca/ca.crt 2>/dev/null)" && ca_rc=0 || ca_rc=$?
    if [ -z "$ca_pem" ]; then
      echo "  [$label] TLS $host:$port PROBE ERROR: could not read the lab CA cert (rc=$ca_rc)"
    else
      # -n is deliberately NOT passed here: this is the one call that needs real stdin
      # (the CA cert, piped in from the local $ca_pem string above, not from a live
      # process); -T still disables the pty. The in-container `timeout` is still the
      # real kill-switch for the python process itself.
      PROBE_OUT="$(printf '%s' "$ca_pem" | timeout --kill-after=3 11 \
        incus exec -T "$inst" -- timeout --kill-after=2 5 /opt/evcharge/venv/bin/python -c '
import socket, ssl, sys
host, port = sys.argv[1], int(sys.argv[2])
ca_pem = sys.stdin.read()
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.load_verify_locations(cadata=ca_pem)
ctx.check_hostname = True
ctx.verify_mode = ssl.CERT_REQUIRED
try:
    with socket.create_connection((host, port), timeout=3) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as wrapped:
            print("ok " + wrapped.version())
except Exception as e:
    print("fail: " + str(e))
' "$host" "$port" 2>/dev/null)" && PROBE_RC=0 || PROBE_RC=$?
      case "$PROBE_OUT" in
        ok\ *) echo "  [$label] TLS $host:$port OK (${PROBE_OUT#ok })" ;;
        fail:*) echo "  [$label] TLS $host:$port FAIL: ${PROBE_OUT#fail: }" ;;
        "") echo "  [$label] TLS $host:$port PROBE ERROR/TIMEOUT (rc=$PROBE_RC)" ;;
        *) echo "  [$label] TLS $host:$port PROBE ERROR: unexpected output [$PROBE_OUT] (rc=$PROBE_RC)" ;;
      esac
    fi
  fi
}

# report_target_state <hier_id> <city>: read-only report of whether an earlier scenario
# left this charger's registry entry quarantined and/or its real station's current
# address firewall-dropped at the edge (the composite 110130 response, docs/interfaces.md
# v6 15.2). Prints what it finds; changes nothing. Scoped to detect+report only -- an
# automatic reset is left for a later change. Dry-run prints the plan and does nothing.
report_target_state() {
  local hier="$1" city="$2" prefix station_ip fw_out fw_rc
  prefix="$(echo "$hier" | cut -d. -f1-3)"
  if [ "$DRY_RUN" = 1 ]; then
    would "report_target_state $hier $city (registry quarantine + edge firewall, read-only)"
    return 0
  fi
  echo "-- inter-scenario state check: $hier --"
  if [ -n "${REGISTRY_ADMIN_TOKEN:-}" ]; then
    curl -s -H "Authorization: Bearer $REGISTRY_ADMIN_TOKEN" \
      "http://10.255.255.80:30801/quarantine?prefix=$prefix" | sed 's/^/  registry: /'
  else
    echo "  registry: not checked (REGISTRY_ADMIN_TOKEN not in this shell; see secrets/platform.env)"
  fi
  station_ip="$(ipof "$(station_instance "$prefix")" 2>/dev/null)" && : || station_ip=""
  if [ -n "$station_ip" ]; then
    fw_out="$(incus exec "$city-edge" -- sh -c "iptables -L -n 2>/dev/null | grep -F '$station_ip'")" && fw_rc=0 || fw_rc=$?
    if [ "$fw_rc" -eq 0 ] && [ -n "$fw_out" ]; then
      echo "  edge firewall: DROP rule present for $station_ip:"
      echo "$fw_out" | sed 's/^/    /'
    else
      echo "  edge firewall: no rule found for $station_ip"
    fi
  else
    echo "  edge firewall: not checked (could not resolve the station's current address)"
  fi
  echo
}

# Refuses a live run unless the RUN card's flag is set. Call once --run has been parsed.
require_baseline() {
  if [ "${EVCHARGE_BASELINE_DONE:-}" != "1" ]; then
    echo "refusing --run: EVCHARGE_BASELINE_DONE is not set to 1." >&2
    echo "A live baseline recording must not be disturbed; the W6-run card sets this" \
         "once the baseline is captured. Use --dry-run to check the script without it." >&2
    exit 1
  fi
}

# --- naming helpers (names only, never addresses: section 13.1 / 11.1) ---
edge_host() { echo "edge.$1.pt.lab"; }

is_plain_city() {
  local c="$1" p
  for p in ${PLAIN_WS_CITIES//,/ }; do
    if [ "$p" = "$c" ]; then return 0; fi
  done
  return 1
}

edge_url() {
  local c="$1"
  if is_plain_city "$c"; then echo "ws://$(edge_host "$c")/ocpp"; else echo "wss://$(edge_host "$c")/ocpp"; fi
}

# pt.porto.st-001 -> porto-st-001 (the Incus instance name, section 13.2)
station_instance() {
  local hier="$1"
  [[ "$hier" =~ ^pt\.([a-z][a-z0-9]*)\.(st-[0-9]{3})$ ]] || {
    echo "not a station hier-id: $hier" >&2; return 1
  }
  echo "${BASH_REMATCH[1]}-${BASH_REMATCH[2]}"
}

rogue_name() { echo "${1}-rogue"; }

usage_common() {
  cat <<'EOF'

Common flags:
  --city NAME        city to target (default: porto)
  --station HIER_ID  station hier-id, e.g. pt.porto.st-001 (default: pt.<city>.st-001)
  --duration SECONDS hard time-box for the live scenario (default: 120)
  --dry-run          print what would happen, change nothing (default)
  --run              actually perform the scenario; refused unless EVCHARGE_BASELINE_DONE=1
  -h, --help         this help
EOF
}

# Parses the flags common to the scenario scripts. Unrecognised flags are left in
# REMAINING_ARGS for the caller to parse its own extras (currently none needed).
parse_common_args() {
  STATION=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --city) CITY="$2"; shift 2 ;;
      --station) STATION="$2"; shift 2 ;;
      --duration) DURATION="$2"; shift 2 ;;
      --run) RUN_REQUESTED=1; DRY_RUN=0; shift ;;
      --dry-run) RUN_REQUESTED=0; DRY_RUN=1; shift ;;
      -h|--help) print_usage; exit 0 ;;
      *) echo "unknown argument: $1" >&2; print_usage; exit 2 ;;
    esac
  done
  [ -n "$STATION" ] || STATION="pt.$CITY.st-001"
  if [ "$RUN_REQUESTED" = 1 ]; then require_baseline; fi
  return 0
}
