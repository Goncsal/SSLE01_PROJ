# City sites

`infra/03_cities.sh` builds the three city sites (contract section 13). Steps:
`image networks instances certs edges stations register csms status` (`all` runs them in
this order, every step can be repeated). `infra/03_cities.sh <step> porto` limits a step to
one city. Only `incus` and bash are used on the host.

## Layout

All instances are unprivileged Debian trixie containers from the image `evcharge-city`
(`REBUILD=1 infra/03_cities.sh image` rebuilds it). Files in this directory:

| File | Purpose |
|---|---|
| `evsim.service` | systemd unit template (`@STATION@` is replaced by the station id) |
| `sim.env.tmpl` | `/etc/evcharge/sim.env` template, written once per station |
| `register_city.py` | runs inside the edge: roster query and registry registration |

### Station `<city>-st-NNN` (160 MiB, 1 CPU, DHCP)

- `/opt/evcharge/venv` with `evcommon` and `evsim`; user `evcharge` (uid 10001).
- `/etc/evcharge/roster.yaml`, `/etc/evcharge/sim.env`, `/etc/evcharge/pki/` (CA certificate and
  only this station's charger certificates and keys, group `evcharge`, keys 0640).
- `evsim.service` runs as `evcharge`; metrics on port 9300; logs in `/var/log/evcharge/`
  (also `journalctl -u evsim`).

### Edge `<city>-edge` (256 MiB, 1 CPU, static host address `.2` of the city network)

nginx and the exporters configured by `gateway/deploy_edge.sh` (see `gateway/README.md`);
log `/var/log/evcharge/edge-pt.<city>.edge.json`. The edge also holds the roster and
`register_city.py`, because the city subtree is registered from the edge with the city token.

Names: `edge.<city>.pt.lab` and `st-NNN.<city>.pt.lab` are DNS aliases created by
`infra/02_networks.sh` for `STATIONS_PER_CITY` stations.

## Operate

```bash
infra/03_cities.sh status                    # instances, memory, evsim, nginx -t, connected chargers
incus exec porto-st-003 -- systemctl stop evsim      # (start / restart / status likewise)
incus exec porto-st-003 -- journalctl -u evsim -n 20
infra/03_cities.sh update-sim porto          # push new sim/ and common/ code, restart evsim
```

`sim.env` is created once and never overwritten, so manual changes survive repeated runs.

## Change a station's flags

Edit `/etc/evcharge/sim.env` in the station and restart:

```bash
incus exec porto-st-003 -- sed -i 's|^SIM_EXTRA_ARGS=.*|SIM_EXTRA_ARGS=--tamper 0.5|' /etc/evcharge/sim.env
incus exec porto-st-003 -- systemctl restart evsim
```

## Switch Aveiro to TLS

```bash
gateway/deploy_edge.sh aveiro --tls
for i in 1 2 3 4 5; do
  incus exec aveiro-st-00$i -- sed -i 's|^SIM_CSMS_URL=ws://|SIM_CSMS_URL=wss://|' /etc/evcharge/sim.env
  incus exec aveiro-st-00$i -- systemctl restart evsim
done
```

The charger certificates are already in the stations. To make the change permanent for
later `all` runs, remove `aveiro` from `PLAIN_WS_CITIES` (`infra/local.env`).

## Add a station

1. Add it to the roster (`dev/roster.yaml`) and raise `STATIONS_PER_CITY` in `infra/env.sh`
   (or `infra/local.env`); station ids must be consecutive (`st-001` ...).
2. `infra/03_cities.sh all` (creates the DNS alias, the container, the certificates, the
   service and the registry entries; existing parts do not change).

## Secrets

Tokens come from `secrets/platform.env` and travel on stdin of `incus exec` only. Private keys
go from `lab-ca` straight into the target instance (`pki/export.sh`).
