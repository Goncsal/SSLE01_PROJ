# Lab PKI

Single-level lab CA for the charger mTLS link. It runs in its own unprivileged Incus
container `lab-ca` (Debian trixie, `openssl` and `python3-yaml`, no listening service,
64 MiB). The CA key never leaves it. Leaf keys are generated inside `lab-ca` and streamed
into the instance that uses them (`incus exec ... | incus exec ...`); nothing private
touches the host disk. `pki/out/` (gitignored) only receives public material.

Layout inside `lab-ca`: `/root/ca/{openssl.cnf, ca.key, ca.crt, index.txt, crlnumber,
crl.pem, certs/<hier_id>.crt, keys/<hier_id>.key}`.

## Profiles

| | CA | Charger | Edge |
|---|---|---|---|
| Subject | `O=EVCharge Lab, CN=EVCharge Lab Root CA` | `O=EVCharge Lab, OU=<city>, CN=<hier_id>` | `O=EVCharge Lab, OU=<city>, CN=pt.<city>.edge` |
| Key / validity | EC P-256, 10 years | EC P-256, 397 days | EC P-256, 397 days |
| Extensions | CA:true pathlen:0; keyCertSign, cRLSign | CA:false; digitalSignature; clientAuth; SAN `cp-N.st-NNN.<city>.pt.lab` | CA:false; digitalSignature; serverAuth; SAN `edge.<city>.pt.lab`, `<city>-edge.<city>.pt.lab` |

SHA-256, random serials. Serials are printed as upper-case hex without colons or leading
zeros (the form kept in the registry's `cert_serial`).

## Scripts

| Script | Purpose |
|---|---|
| `ca.sh` | create `lab-ca` and the CA; idempotent, never replaces an existing key |
| `issue.sh <hier_id> [--force]` / `issue.sh --roster PATH [--city X] [--force]` | issue; prints `<hier_id> <SERIAL>`; skips valid certificates; `--force` revokes the old one first; refuses ids that are not charger or edge ids |
| `serial.sh <hier_id>` | current serial |
| `export.sh <hier_id> <instance> [--dir D] [--group GID] [--crl]` | write `ca.crt`, `<id>.crt`, `<id>.key` (0640 root:GID with `--group`, else 0600) and optionally `crl.pem`; `export.sh --public` copies `ca.crt` and `crl.pem` to `pki/out/` |
| `crl.sh` | regenerate the CRL (`default_crl_days 7`) |
| `revoke.sh <hier_id> [reason]` | revoke (default `keyCompromise`), regenerate the CRL, run `gateway/push_crl.sh` if executable, print the serial |

Exit codes: 0 success, 1 failure (unknown id or certificate, missing CA), 2 usage error.
Progress messages go to stderr; stdout carries only the documented output.

## Revoke flow

Hijacked identity: registry `POST /quarantine/{id}`, CSMS `disconnect`, `pki/revoke.sh <id>`.
The edge then refuses the old certificate (`FAILED:certificate revoked`). Re-enrolment:
`pki/issue.sh <id> --force`, registry `PATCH cert_serial`, `pki/export.sh` to the station,
restart `evsim`, release the quarantine.

## Limitations

- One CA level only; an intermediate CA is future work.
- The CRL expires after 7 days and nginx then refuses every client certificate, so
  `crl.sh` and the CRL push must run at least weekly.
- No OCSP; revocation is only as fresh as the last CRL push.
- Keys are generated centrally and copied to the device, not generated on the device
  with a CSR.
- `apt` needs more than 64 MiB, so `ca.sh` raises the memory limit to 256 MiB during the
  install and sets it back.
