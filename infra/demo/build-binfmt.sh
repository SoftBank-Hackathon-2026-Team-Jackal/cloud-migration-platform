#!/bin/sh
# Ticket 58 (#278): let the host Docker daemon build images for the other CPU architecture.
#
# `BUILD_PLATFORM=linux/amd64,linux/arm64` makes the Build module build each platform on the
# daemon's own builder (ticket 54: RUN stays on `docker0`) and join them into one manifest list.
# The non-native platform runs under QEMU user emulation, registered in the kernel's binfmt_misc
# by the pinned `tonistiigi/binfmt` image. Emulated RUN steps stay in the same build container and
# network, so the ticket-54 IMDS guard still applies. Registrations are lost on reboot.
#
# Usage (root): apply | check | install
#   install  copies this script to /usr/local/sbin and enables a systemd unit that re-registers
#            the emulator whenever docker.service starts.
set -eu

target=/usr/local/sbin/cmp-build-binfmt
unit=/etc/systemd/system/cmp-build-binfmt.service
# tonistiigi/binfmt:qemu-v9.2.2, pinned by its manifest-list digest.
image=docker.io/tonistiigi/binfmt@sha256:1b804311fe87047a4c96d38b4b3ef6f62fca8cd125265917a9e3dc3c996c39e6

case "$(uname -m)" in
  x86_64 | amd64) arch=arm64 entry=qemu-aarch64 ;;
  aarch64 | arm64) arch=amd64 entry=qemu-x86_64 ;;
  *) echo "unsupported host architecture: $(uname -m)" >&2; exit 2 ;;
esac

case "${1:-}" in
  apply)
    docker image inspect "$image" >/dev/null 2>&1 || docker pull --quiet "$image" >/dev/null
    # No network: the installer only writes binfmt_misc registrations.
    docker run --rm --privileged --network none "$image" --install "$arch" >/dev/null
    ;;
  check)
    grep -qx enabled "/proc/sys/fs/binfmt_misc/$entry" 2>/dev/null
    ;;
  install)
    install -m 0755 "$0" "$target"
    cat >"$unit" <<UNIT
[Unit]
Description=CMP ticket 58: QEMU binfmt emulator for multi-architecture image builds
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
    systemctl enable cmp-build-binfmt.service >/dev/null
    systemctl restart cmp-build-binfmt.service
    "$target" check
    ;;
  *)
    echo "usage: $0 apply|check|install" >&2
    exit 2
    ;;
esac
