#!/usr/bin/env bash
# Acceptance test of the edge gateway in a throwaway instance "edgetest" (default bridge).
# A test CA and all keys are generated inside the instance. The instance is deleted at the end
# (set KEEP=1 to keep it). Usage: gateway/tests/run_edgetest.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
T=edgetest
IMAGE="${IMAGE:-images:debian/trixie}"
fails=0
x() { incus exec "$T" -- "$@"; }
sh_() { incus exec "$T" -- sh -c "$1"; }
check() { # check <label> <condition-exit-code> [detail]
  if [ "$2" -eq 0 ]; then printf '  PASS %s\n' "$1"; else printf '  FAIL %s %s\n' "$1" "${3:-}"; fails=$((fails+1)); fi
}
cleanup() { [ "${KEEP:-0}" = 1 ] || incus delete -f "$T" >/dev/null 2>&1; }
trap cleanup EXIT

incus delete -f "$T" >/dev/null 2>&1
echo "== setup"
incus launch "$IMAGE" "$T" -n incus-default >/dev/null || { echo "cannot launch $T"; exit 1; }
for _ in $(seq 1 60); do sh_ 'getent hosts deb.debian.org' >/dev/null 2>&1 && break; sleep 2; done
sh_ 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssl curl python3 >/dev/null' || exit 1

# test PKI, generated inside the instance
incus exec "$T" -- bash -s <<'PKI'
set -e
mkdir -p /etc/evcharge/pki /root/t && cd /root/t
mkca() { # dir cn
  mkdir -p "$1/certs"; : > "$1/index.txt"; echo 1000 > "$1/crlnumber"
  cat > "$1/ca.cnf" <<CNF
[ca]
default_ca=CA_default
[CA_default]
dir=$1
database=\$dir/index.txt
new_certs_dir=\$dir/certs
serial=\$dir/serial
crlnumber=\$dir/crlnumber
default_md=sha256
default_days=30
default_crl_days=7
policy=pol
unique_subject=no
copy_extensions=copy
[pol]
commonName=supplied
[req]
distinguished_name=dn
prompt=no
[dn]
CN=x
CNF
  echo 1000 > "$1/serial"
  openssl ecparam -name prime256v1 -genkey -noout -out "$1/ca.key"
  openssl req -x509 -new -key "$1/ca.key" -subj "/CN=$2/O=EVCharge Lab" -days 30 -out "$1/ca.crt" \
    -addext basicConstraints=critical,CA:true,pathlen:0 -addext keyUsage=keyCertSign,cRLSign
}
issue() { # cadir name eku san
  openssl ecparam -name prime256v1 -genkey -noout -out "$2.key"
  openssl req -new -key "$2.key" -subj "/CN=$2/OU=testcity/O=EVCharge Lab" -out "$2.csr"
  printf 'basicConstraints=CA:false\nkeyUsage=digitalSignature\nextendedKeyUsage=%s\n%s\n' "$3" "${4:-}" > "$2.ext"
  openssl ca -batch -config "$1/ca.cnf" -cert "$1/ca.crt" -keyfile "$1/ca.key" -in "$2.csr" -out "$2.crt" -extfile "$2.ext" -notext 2>/dev/null
}
mkca ca "EVCharge Test CA"
mkca foreign "Foreign CA"
issue ca pt.testcity.edge serverAuth "subjectAltName=DNS:edge.testcity.pt.lab"
issue ca pt.testcity.st-001.cp-1 clientAuth
issue ca pt.testcity.st-001.cp-2 clientAuth
issue foreign pt.testcity.st-001.cp-3 clientAuth
openssl ca -config ca/ca.cnf -cert ca/ca.crt -keyfile ca/ca.key -revoke pt.testcity.st-001.cp-2.crt 2>/dev/null
openssl ca -config ca/ca.cnf -cert ca/ca.crt -keyfile ca/ca.key -gencrl -out ca/crl.pem 2>/dev/null
cp ca/ca.crt /etc/evcharge/pki/ca.crt; cp ca/crl.pem /etc/evcharge/pki/crl.pem
cp pt.testcity.edge.crt pt.testcity.edge.key /etc/evcharge/pki/
chmod 600 /etc/evcharge/pki/*.key
PKI
check "test PKI" $?

# upstreams: minimal WebSocket handshake servers that log the X-* headers, then close
incus exec "$T" -- sh -c 'cat > /root/up.py' <<'PY'
import base64, hashlib, socketserver, sys
port = int(sys.argv[1])
class H(socketserver.StreamRequestHandler):
    def handle(self):
        hdr = {}
        self.rfile.readline()
        while True:
            l = self.rfile.readline().decode().strip()
            if not l: break
            k, _, v = l.partition(":"); hdr[k.strip().lower()] = v.strip()
        xs = " ".join(f"{k}={v!r}" for k, v in hdr.items() if k.startswith("x-"))
        with open(f"/tmp/up-{port}.log", "a") as f: f.write(f"{port} {xs}\n")
        acc = base64.b64encode(hashlib.sha1((hdr.get("sec-websocket-key", "") + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.wfile.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n".encode())
class S(socketserver.ThreadingTCPServer): allow_reuse_address = True
S(("127.0.0.1", port), H).serve_forever()
PY
sh_ 'nohup python3 /root/up.py 19901 >/dev/null 2>&1 & nohup python3 /root/up.py 19902 >/dev/null 2>&1 &'

echo "== deploy_edge.sh testcity --tls --pki-skip"
"$ROOT/gateway/deploy_edge.sh" testcity --tls --pki-skip --instance "$T" --upstreams "127.0.0.1:19901 127.0.0.1:19902" 2>&1 | tail -3
check "deploy (idempotent rerun)" "$(
  "$ROOT/gateway/deploy_edge.sh" testcity --tls --pki-skip --instance "$T" --upstreams "127.0.0.1:19901 127.0.0.1:19902" >/dev/null 2>&1; echo $?)"

# client helper: prints the HTTP status line of the first response
cli() { # cli <path> <extra curl args...>
  local p="$1"; shift
  incus exec "$T" -- curl -sk -m 3 -D - -o /dev/null --resolve edge.testcity.pt.lab:443:127.0.0.1 \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
    -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' "$@" "https://edge.testcity.pt.lab$p" | head -1 | tr -d '\r'
}
P=/ocpp/pt.testcity.st-001.cp-1
CD="--cacert /root/t/ca/ca.crt"
good="--cert /root/t/pt.testcity.st-001.cp-1.crt --key /root/t/pt.testcity.st-001.cp-1.key"
lastup() { sh_ 'cat /tmp/up-*.log 2>/dev/null | tail -n '"${1:-1}"; }

echo "== 1 valid certificate"
sh_ 'rm -f /tmp/up-*.log'
s=$(cli $P $CD $good); echo "  $s"; sleep 0.5
echo "  upstream: $(lastup 1)"
echo "$s" | grep -q 101; r1=$?
lastup 1 | grep -q "x-client-cert-verify='SUCCESS'" && lastup 1 | grep -q "CN=pt.testcity.st-001.cp-1" &&
  lastup 1 | grep -q "x-client-cert-serial=" && lastup 1 | grep -q "x-real-ip='127.0.0.1'" && lastup 1 | grep -q "x-edge-id='pt.testcity.edge'"; r1b=$?
check "1 valid cert: 101 and identity headers" $((r1+r1b))

echo "== 2 no certificate"
sh_ 'rm -f /tmp/up-*.log'
s=$(cli $P $CD); echo "  $s"; sleep 0.5; echo "  upstream: $(lastup 1)"
echo "$s" | grep -q 101 && lastup 1 | grep -q "x-client-cert-verify='NONE'"; check "2 no cert: 101, Verify NONE" $?

echo "== 3 revoked and foreign-CA certificates"
s=$(cli /ocpp/pt.testcity.st-001.cp-2 $CD --cert /root/t/pt.testcity.st-001.cp-2.crt --key /root/t/pt.testcity.st-001.cp-2.key); echo "  revoked: $s"
echo "$s" | grep -q 400; a=$?
s=$(cli /ocpp/pt.testcity.st-001.cp-3 $CD --cert /root/t/pt.testcity.st-001.cp-3.crt --key /root/t/pt.testcity.st-001.cp-3.key); echo "  foreign: $s"
echo "$s" | grep -q 400; b=$?
sleep 0.5
sh_ 'grep -h "FAILED" /var/log/evcharge/edge-pt.testcity.edge.json | cut -c1-330'
sh_ 'grep -c "FAILED:certificate revoked" /var/log/evcharge/edge-pt.testcity.edge.json' >/dev/null; c=$?
check "3 revoked/foreign -> 400, logged FAILED" $((a+b+c))

echo "== 4 spoofed headers"
sh_ 'rm -f /tmp/up-*.log'
cli $P $CD $good -H 'X-Client-Cert-DN: CN=pt.testcity.st-009.cp-9' -H 'X-Real-IP: 6.6.6.6' -H 'X-Edge-Id: pt.evil.edge' -H 'X-Client-Cert-Verify: SUCCESS' >/dev/null; sleep 0.5
echo "  upstream: $(lastup 1)"
! lastup 1 | grep -q -e '6.6.6.6' -e 'st-009' -e 'evil' && lastup 1 | grep -q "x-real-ip='127.0.0.1'"; check "4 client-sent headers replaced" $?

echo "== 5 bad path / no Upgrade / port 80"
s404=$(cli /nope $CD $good); s426=$(incus exec "$T" -- curl -sk -m 3 -o /dev/null -w '%{http_code}' --resolve edge.testcity.pt.lab:443:127.0.0.1 $CD $good "https://edge.testcity.pt.lab$P")
s80=$(incus exec "$T" -- curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1$P")
echo "  /nope: $s404 | no Upgrade: $s426 | port 80: $s80"
echo "$s404" | grep -q 404; a=$?; [ "$s426" = 426 ]; b=$?; [ "$s80" = 426 ]; c=$?
check "5 404 / 426 / 426" $((a+b+c))

echo "== 6 failover (19901 stopped)"
sh_ 'pkill -f "up.py 19901"; sleep 0.5; rm -f /tmp/up-*.log'
ok6=0; for _ in 1 2 3; do r=$(cli $P $CD $good); case "$r" in *101*) ;; *) ok6=1 ;; esac; sleep 0.3; done
sleep 0.5
sh_ 'grep -h "19901, 127.0.0.1:19902" /var/log/evcharge/edge-pt.testcity.edge.json | tail -1 | cut -c 150-420'
sh_ 'grep -q "127.0.0.1:19901, 127.0.0.1:19902" /var/log/evcharge/edge-pt.testcity.edge.json'; l6=$?
check "6 failover keeps 101, upstream chain logged" $((ok6+l6))

echo "== 7 burst of 40 handshakes"
sleep 12
codes=$(incus exec "$T" -- bash -c 'seq 40 | xargs -P40 -I{} curl -sk -m 5 -o /dev/null -w "%{http_code}\n" --resolve edge.testcity.pt.lab:443:127.0.0.1 --cacert /root/t/ca/ca.crt --cert /root/t/pt.testcity.st-001.cp-1.crt --key /root/t/pt.testcity.st-001.cp-1.key -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" https://edge.testcity.pt.lab'"$P" | sort | uniq -c | tr '\n' ' ')
echo "  $codes"
echo "$codes" | grep -q ' 429'; check "7 some 429" $?

echo "== 8 log lines parse"
sleep 1
sh_ 'cat /var/log/evcharge/edge-*.json' | python3 -I -c '
import sys, json
base = ["ts","level","service","instance","event","msg"]
n = bad = 0
for l in sys.stdin:
    n += 1
    try:
        d = json.loads(l)
        assert list(d)[:6] == base and d["service"] == "edge" and d["event"] == "edge.access"
        assert isinstance(d["http_status"], int) and isinstance(d["duration_s"], float) and isinstance(d["bytes_out"], int)
    except Exception as e:
        bad += 1; print("  bad:", l[:120], e)
print(f"  {n} lines, {bad} bad"); sys.exit(1 if bad or not n else 0)'
check "8 JSON log" $?

echo "== 9 exporter"
sh_ 'curl -s 127.0.0.1:9113/metrics | grep -m2 nginx_connections_active'
sh_ 'curl -s 127.0.0.1:9113/metrics | grep -q "^nginx_connections_active"'; check "9 nginx_connections_active" $?

echo "== push_crl.sh (the test instance plays the CA too)"
sh_ 'mkdir -p /root/ca && cp /root/t/ca/crl.pem /root/ca/crl.pem'
CA_INSTANCE="$T" "$ROOT/gateway/push_crl.sh" --instance "$T" testcity; check "push_crl.sh" $?
echo "== memory"
sh_ 'ps -eo rss,comm | awk "/nginx|prometheus/ {s[\$2]+=\$1} END {for (k in s) printf \"  %s %d kB\n\", k, s[k]}"'
incus info "$T" | grep -iE 'memory' | head -3

echo; [ "$fails" -eq 0 ] && echo "ALL PASS" || echo "$fails FAILED"
exit "$fails"
