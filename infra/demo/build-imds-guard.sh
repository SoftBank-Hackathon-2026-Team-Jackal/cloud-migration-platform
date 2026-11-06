#!/bin/sh
# Ticket 54 (#189): keep user-app image builds away from the EC2 instance metadata service.
#
# The worker builds uploaded application Dockerfiles on the host Docker daemon (ticket 28). With the
# daemon's built-in BuildKit (`docker` driver) every RUN step joins the default bridge `docker0`;
# custom networks are not supported there. IMDS hop limit 2 (ticket 21) lets bridged containers reach
# 169.254.169.254, so a RUN step could read the instance-role credentials. This rule drops only
# traffic that enters from `docker0` towards IMDS. Compose services (worker, api) live on their own
# `br-*` bridge and keep the instance role.
#
# Usage (root): apply | check | remove | install
#   install  copies this script to /usr/local/sbin and enables a systemd unit that re-applies the
#            rule whenever docker.service starts (iptables rules are lost on reboot).
set -eu

chain=DOCKER-USER
target=/usr/local/sbin/cmp-build-imds-guard
unit=/etc/systemd/system/cmp-build-imds-guard.service


spec="-i docker0 -d 169.254.169.254/32 -m comment --comment cmp-build-imds-guard -j DROP"

case "${1:-}" in
  apply)
    # shellcheck disable=SC2086
    iptables -w -C "$chain" $spec 2>/dev/null || iptables -w -I "$chain" 1 $spec
    ;;
  check)
    # shellcheck disable=SC2086
    iptables -w -C "$chain" $spec
    ;;
  remove)
    # shellcheck disable=SC2086
    while iptables -w -D "$chain" $spec 2>/dev/null; do :; done
    ;;
  install)
    install -m 0755 "$0" "$target"
    cat >"$unit" <<UNIT
[Unit]
Description=CMP ticket 54: block instance metadata from Docker default-bridge build steps
After=docker.service
BindsTo=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$target apply

[Install]
WantedBy=docker.service
UNIT
    systemctl daemon-reload
    systemctl enable cmp-build-imds-guard.service >/dev/null
    systemctl restart cmp-build-imds-guard.service
    "$target" check
    ;;
  *)
    echo "usage: $0 apply|check|remove|install" >&2
    exit 2
    ;;
esac
