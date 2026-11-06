# Wazuh agent for the K3s nodes (DaemonSet wazuh-agent, namespace wazuh).
# Built by `infra/04_build_push.sh wazuh-agent` as docker.incus:5000/evcharge-wazuh-agent:4.14.9
# from the repository root as build context.
FROM debian:trixie-slim

ARG WAZUH_AGENT_VERSION=4.14.9-1

RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      ca-certificates curl procps \
 && curl -fsS https://packages.wazuh.com/key/GPG-KEY-WAZUH -o /usr/share/keyrings/wazuh.asc \
 && echo "deb [signed-by=/usr/share/keyrings/wazuh.asc] https://packages.wazuh.com/4.x/apt/ stable main" \
      > /etc/apt/sources.list.d/wazuh.list \
 && apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      wazuh-agent=${WAZUH_AGENT_VERSION} \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && sed -i 's|<address>[^<]*</address>|<address>MANAGER_PLACEHOLDER</address>|' /var/ossec/etc/ossec.conf

COPY wazuh/agents/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
