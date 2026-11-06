# Alertmanager

`alertmanager.yml` implements contract 14.4: group by `alertname, city, hier_id`,
`group_wait 10s`, `group_interval 1m`, `repeat_interval 4h`, receiver `default` without an
integration (Phase 9 adds the correlator webhook, see the comment in the file).

Inhibition: a firing `EdgeDown` mutes `ChargerOffline`, `StationDown`, `ChargerFlapping`,
`MsgRateAnomaly` and `CityLoadSpike` of the same `city`; `CSMSDown` mutes `ChargerOffline` and
`ChargerFlapping`.

Static checks (rulestest container): `amtool check-config alertmanager.yml` and
`amtool config routes test --config.file=alertmanager.yml alertname=EdgeDown city=porto`
(prints `default`). `amtool` does not evaluate inhibition offline.

Live check of the inhibition (W4-integration): stop one edge
(`incus stop porto-edge`), wait about 2 minutes, then in the Alertmanager UI or with
`amtool --alertmanager.url=http://k3s-master.incus:30093 alert query` show `EdgeDown{city=porto}`
active while the porto `StationDown`/`ChargerOffline` alerts appear as inhibited
(`amtool alert query --inhibited`), and other cities untouched. Start the edge again and check
that everything resolves. For `CSMSDown`, scale the CSMS to 0 and check `ChargerOffline` and
`ChargerFlapping` are inhibited in every city.
