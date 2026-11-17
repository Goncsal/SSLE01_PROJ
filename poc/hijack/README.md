# Hijacking PoC (threat 1: signal/identity hijacking)

Design background: the hijacking design note. Interfaces: `docs/interfaces.md` v6
sections 5.2 (identity checks), 9.2 (simulator), 11 (edge), 12 (PKI), 16 (city ACLs).

Every script here defaults to `--dry-run`: it prints the exact commands and target
names it would use and changes nothing. A live run additionally needs `--run` *and*
the environment variable `EVCHARGE_BASELINE_DONE=1` (set only by the matching
the environment, once the baseline recording is captured). The scenario is time-boxed
(`--duration`, default 120 s) and deletes the temporary rogue container it creates on
exit and on `INT`/`TERM`.

## Scenario

### `rogue_dupe.sh` -- rogue with a stolen certificate from a foreign address (Porto)

A throwaway `porto-rogue` container (image `evcharge-city`) on the Porto city network
presents a certificate **copied from a real station** (`pki/export.sh`, straight from
`lab-ca`) and connects to the city edge, while the genuine `pt.porto.st-001` stays up.

What the run shows, end to end:

- **mTLS proves key possession, not location.** The stolen certificate is validly
  signed and not yet revoked, so the edge accepts the TLS handshake and the rogue
  reaches the CSMS -- a copied key passes the CN and CA checks.
- **The CSMS refuses on the source address.** With `CSMS_ENFORCE_STATION_IP=true`
  (the lab default) the CSMS compares the connection's address against the registry
  record for that charger id and returns **403, `ident.ip_mismatch`** -> Wazuh
  **110104** per attempt. The genuine station is never disturbed: check 9 (address)
  refuses the rogue before the duplicate-id guard even runs.
- **Three refusals raise the response.** `evsim` retries, so three refused
  connections from the same address land inside 60 s -> Wazuh **110132** (stolen
  credential in use) -> the **`evcharge-edge-drop`** active response installs a
  self-expiring DROP rule for the rogue's address at the Porto edge. No session is
  touched; `pt.porto.st-001.cp-1` keeps charging.

```
rogue_dupe.sh [--city porto] [--station pt.porto.st-001] [--run] [--duration N]
```

### `run.sh` -- the scenario with a banner

Prints a banner (what, expected detection, expected response) and runs `rogue_dupe.sh`.
Relies on the sub-script's own cleanup.

```
run.sh [--run] [--duration N]
```

### `verify.sh` -- read-only evidence check

Takes the UTC window captured around a live run and checks the real signals: did the
rogue reach the edge (distinct srcips in the edge access log), did the CSMS log
`ident.ip_mismatch` refusals, is there an `evcharge-edge-drop` DROP rule for the
rogue's address at the edge, and the Wazuh alerts for 110104/110132. Prints
`expected detection observed`, `unexpected result`, or `setup failed`. Changes nothing.

```
verify.sh <start_iso> <end_iso> [--charger HIER_ID] [--city CITY]
```

### `selfcheck.sh`

Runs `rogue_dupe.sh` and `run.sh` on their `--dry-run` paths, checks each exits 0 and
prints a plan, and checks that `--run` is refused without `EVCHARGE_BASELINE_DONE=1`.
Creates and changes nothing.

## Preconditions the live run needs (set outside this PoC)

- **`CSMS_ENFORCE_STATION_IP=true`** in the `csms-edge` ConfigMap (the lab default). The
  script does not change it: that would lock out a real station, so it is a lab setting,
  not an attack step. With enforcement off the rogue is accepted and only logged, which
  is the weaker "before" state the design note discusses.
- **City ACLs off for the rogue to reach the edge.** The city networks have default-drop
  egress and an allow list built from the stations' addresses; the throwaway rogue gets a
  fresh address that is not on the list, so with ACLs applied it is dropped at the network
  before it reaches the edge -- an earlier but weaker signal. This is the same stated
  concession the botnet PoC makes; the live run removes the ACLs for the rogue, captures
  the identity-layer refusal, and re-applies them. The "ACL on" case (rogue stopped at the
  network) is evidenced separately in `evidence/hijack/`.

## Lab-safety notes

- **One throwaway container on the city network**: the rogue acts only on `city-porto`;
  nothing outside the lab is targeted, and no secrets or lab addresses are written into
  this repo (names only, resolved at run time, same convention as `infra/08_acls.sh`).
- **Genuine station untouched**: the real `pt.porto.st-001` is left running on purpose --
  the whole point is that the response blocks the rogue's address while the genuine
  charger keeps charging. The script never stops or quarantines it.
- **Time-box**: the scenario takes `--duration` (default 120 s) and the `evsim` run inside
  it is bounded the same way; nothing runs unattended.
- **Cleanup**: the script registers the rogue's delete as soon as its launch succeeds and
  runs it in reverse order on exit, success, failure, `INT` or `TERM`.
