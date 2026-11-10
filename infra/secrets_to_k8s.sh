#!/usr/bin/env bash
# Build the Kubernetes Secrets of namespace evcharge from secrets/platform.env.
#   infra/secrets_to_k8s.sh [--generate]
# --generate creates secrets/platform.env (random values, mode 600) if it is missing, and
# appends the keys of the template that an existing file lacks (existing lines are untouched).
# Values are never printed and never appear on a command line: each Secret is built
# from a temporary env file pushed into k3s-master with mode 600 and removed afterwards.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

GENERATE=0
[ "${1:-}" = "--generate" ] && GENERATE=1

SECRETS_FILE="$ROOT/secrets/platform.env"
TEMPLATE="$ROOT/secrets.example/platform.env.example"
umask 077

rand() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40; }

if [ ! -f "$SECRETS_FILE" ]; then
  if [ "$GENERATE" = 1 ]; then
    log "generating $SECRETS_FILE"
    mkdir -p "$ROOT/secrets"
    : > "$SECRETS_FILE"
    chmod 600 "$SECRETS_FILE"
    while IFS= read -r line; do
      case "$line" in
        ''|'#'*) ;;
        *=*) printf '%s=%s\n' "${line%%=*}" "$(rand)" >> "$SECRETS_FILE" ;;
      esac
    done < "$TEMPLATE"
    ok "created (mode 600)"
  else
    echo "missing $SECRETS_FILE (run with --generate or copy $TEMPLATE)" >&2
    exit 1
  fi
fi

if [ "$GENERATE" = 1 ]; then
  while IFS= read -r line; do
    case "$line" in
      ''|'#'*) ;;
      *=*) k="${line%%=*}"
           if ! grep -q "^$k=" "$SECRETS_FILE"; then
             [ -n "$(tail -c1 "$SECRETS_FILE")" ] && echo >> "$SECRETS_FILE"
             printf '%s=%s\n' "$k" "$(rand)" >> "$SECRETS_FILE"
             chmod 600 "$SECRETS_FILE"
             ok "appended missing key $k"
           fi ;;
    esac
  done < "$TEMPLATE"
fi

# value of one key, without sourcing the file
val() { sed -n "s/^$1=//p" "$SECRETS_FILE" | head -1; }
urlenc() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

need=(POSTGRES_PASSWORD PG_PASSWORD_REGISTRY PG_PASSWORD_CSMS PG_PASSWORD_BILLING PG_PASSWORD_FRAUD
      REGISTRY_ADMIN_TOKEN CSMS_API_TOKEN BILLING_API_TOKEN)
for k in "${need[@]}"; do
  v="$(val "$k")"
  if [ -z "$v" ] || [ "$v" = "change-me" ]; then
    echo "$k is missing or still 'change-me' in $SECRETS_FILE" >&2
    exit 1
  fi
done

WORK="$(mktemp -d)"
REMOTE=/root/.evcharge-secrets
cleanup() { rm -rf "$WORK"; incus exec k3s-master -- rm -rf "$REMOTE" >/dev/null 2>&1 || true; }
trap cleanup EXIT

dburl() { # service, password-key
  printf 'DATABASE_URL=postgresql+psycopg://%s:%s@postgres:5432/%s\n' "$1" "$(urlenc "$(val "$2")")" "$1"
}
pair() { printf '%s=%s\n' "$1" "$(val "$1")"; }

{ pair POSTGRES_PASSWORD; pair PG_PASSWORD_REGISTRY; pair PG_PASSWORD_CSMS; pair PG_PASSWORD_BILLING; pair PG_PASSWORD_FRAUD; } > "$WORK/postgres-env"
# the responder tokens are optional: written only when present in platform.env
optional() { [ -n "$(val "$1")" ] && [ "$(val "$1")" != "change-me" ] && pair "$1"; true; }
{ pair REGISTRY_ADMIN_TOKEN; optional REGISTRY_RESPONDER_TOKEN
  sed -n 's/^\(REGISTRY_TOKEN_[A-Z0-9_]*\)=.*/\1/p' "$SECRETS_FILE" | while read -r k; do pair "$k"; done
  dburl registry PG_PASSWORD_REGISTRY; } > "$WORK/registry-env"
{ pair CSMS_API_TOKEN; optional CSMS_RESPONDER_TOKEN; pair BILLING_API_TOKEN; dburl csms PG_PASSWORD_CSMS
  printf 'FRAUD_URL=http://fraud:8003\n'; pair FRAUD_API_TOKEN; } > "$WORK/csms-env"
{ pair BILLING_API_TOKEN; optional BILLING_RESPONDER_TOKEN; dburl billing PG_PASSWORD_BILLING; } > "$WORK/billing-env"
{ pair FRAUD_API_TOKEN; dburl fraud PG_PASSWORD_FRAUD; optional REGISTRY_RESPONDER_TOKEN; optional CSMS_RESPONDER_TOKEN
  optional BILLING_RESPONDER_TOKEN; optional CORRELATOR_API_TOKEN; } > "$WORK/fraud-env"
# the correlator is optional until the Phase 9 run: its Secret is built only when the token is set
HAVE_CORRELATOR=0
if [ -n "$(val CORRELATOR_API_TOKEN)" ] && [ "$(val CORRELATOR_API_TOKEN)" != "change-me" ]; then
  HAVE_CORRELATOR=1
  { pair CORRELATOR_API_TOKEN; printf 'REGISTRY_URL=http://registry:8001\n'
    printf 'CSMS_API_URL=http://csms:8000\n'; pair CSMS_API_TOKEN; } > "$WORK/correlator-env"
fi

log "applying Secrets in namespace evcharge"
incus exec k3s-master -- sh -c 'rm -rf '"$REMOTE"' && mkdir -m 700 '"$REMOTE"
incus exec k3s-master -- sh -c 'kubectl create namespace evcharge --dry-run=client -o yaml | kubectl apply -f -' >/dev/null

SECRET_SERVICES="postgres registry csms billing fraud"
[ "$HAVE_CORRELATOR" = 1 ] && SECRET_SERVICES="$SECRET_SERVICES correlator"
for s in $SECRET_SERVICES; do
  incus file push --mode 600 "$WORK/$s-env" "k3s-master$REMOTE/$s-env" >/dev/null
  incus exec k3s-master -- sh -c "kubectl -n evcharge create secret generic $s-env --from-env-file=$REMOTE/$s-env --dry-run=client -o yaml \
    | kubectl label --local -f - app.kubernetes.io/name=$s app.kubernetes.io/part-of=evcharge -o yaml \
    | kubectl apply -f -" >/dev/null
  ok "secret $s-env"
done

# Grafana admin password -> Secret grafana-env in namespace monitoring (key GF_SECURITY_ADMIN_PASSWORD).
# A missing or placeholder GRAFANA_ADMIN_PASSWORD is generated into secrets/platform.env (mode 600).
gp="$(val GRAFANA_ADMIN_PASSWORD)"
if [ -z "$gp" ] || [ "$gp" = "change-me" ]; then
  gp="$(rand)"
  if grep -q '^GRAFANA_ADMIN_PASSWORD=' "$SECRETS_FILE"; then
    sed -i "s/^GRAFANA_ADMIN_PASSWORD=.*/GRAFANA_ADMIN_PASSWORD=$gp/" "$SECRETS_FILE"
  else
    printf 'GRAFANA_ADMIN_PASSWORD=%s\n' "$gp" >> "$SECRETS_FILE"
  fi
  ok "GRAFANA_ADMIN_PASSWORD generated in $SECRETS_FILE"
fi
printf 'GF_SECURITY_ADMIN_PASSWORD=%s\n' "$gp" > "$WORK/grafana-env"
incus exec k3s-master -- sh -c 'kubectl get namespace monitoring >/dev/null 2>&1 || kubectl create namespace monitoring' >/dev/null
incus file push --mode 600 "$WORK/grafana-env" "k3s-master$REMOTE/grafana-env" >/dev/null
incus exec k3s-master -- sh -c "kubectl -n monitoring create secret generic grafana-env --from-env-file=$REMOTE/grafana-env --dry-run=client -o yaml \
  | kubectl label --local -f - app.kubernetes.io/name=grafana app.kubernetes.io/part-of=evcharge -o yaml \
  | kubectl apply -f -" >/dev/null
ok "secret grafana-env (namespace monitoring)"

# Alertmanager bearer for the correlator webhook (file mounted by k8s/monitoring/alertmanager.yaml)
if [ "$HAVE_CORRELATOR" = 1 ]; then
  printf '%s' "$(val CORRELATOR_API_TOKEN)" > "$WORK/correlator-token"
  incus file push --mode 600 "$WORK/correlator-token" "k3s-master$REMOTE/correlator-token" >/dev/null
  incus exec k3s-master -- sh -c "kubectl -n monitoring create secret generic correlator-token --from-file=token=$REMOTE/correlator-token --dry-run=client -o yaml \
    | kubectl label --local -f - app.kubernetes.io/name=alertmanager app.kubernetes.io/part-of=evcharge -o yaml \
    | kubectl apply -f -" >/dev/null
  ok "secret correlator-token (namespace monitoring)"
fi
