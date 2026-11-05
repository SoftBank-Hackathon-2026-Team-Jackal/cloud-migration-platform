# On-premises Agent image (ticket 15, #69; tunnel ticket 17, #71). Build from the repository root:
#   docker build -f infra/onprem-agent/Dockerfile -t cmp-onprem-agent:dev .

# wireguard-go: userspace fallback when the node kernel lacks WireGuard (ticket 17 W1). Pinned to
# the 0.0.20250522 tag commit; module downloads are verified against the Go checksum database.
FROM golang:1.24-bookworm AS wireguard-go
ARG WIREGUARD_GO_COMMIT=f333402bd9cbe0f3eeb02507bd14e23d7d639280
RUN CGO_ENABLED=0 GOFLAGS=-trimpath GOBIN=/out \
      go install "golang.zx2c4.com/wireguard@${WIREGUARD_GO_COMMIT}" \
    && mv /out/wireguard /out/wireguard-go

FROM python:3.11-slim-bookworm

ARG TARGETARCH
ARG KUBECTL_VERSION=v1.37.1
ARG HELM_VERSION=v4.3.1
# Official release checksums, pinned per architecture.
ARG KUBECTL_SHA256_amd64=65691ff77eb6fa44c908b77a1082c9f092c3b9733b5cefabec0d1104890e21a8
ARG KUBECTL_SHA256_arm64=ff749f4b78d9c4f1ec87307df9b50119ed819e2094aa9810cb9acffc3286c8c7
ARG HELM_SHA256_amd64=0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b
ARG HELM_SHA256_arm64=8cf8f66ecbc252ac8f7f06cca68aba88508ddfde3a6a34feb1678ad1694b4f88

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) kubectl_sha="${KUBECTL_SHA256_amd64}"; helm_sha="${HELM_SHA256_amd64}" ;; \
      arm64) kubectl_sha="${KUBECTL_SHA256_arm64}"; helm_sha="${HELM_SHA256_arm64}" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}"; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl iproute2 wireguard-tools; \
    curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl"; \
    echo "${kubectl_sha}  /usr/local/bin/kubectl" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/kubectl; \
    curl -fsSLo /tmp/helm.tgz "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz"; \
    echo "${helm_sha}  /tmp/helm.tgz" | sha256sum -c -; \
    tar -xzf /tmp/helm.tgz -C /tmp "linux-${TARGETARCH}/helm"; \
    install -m 0755 "/tmp/linux-${TARGETARCH}/helm" /usr/local/bin/helm; \
    rm -rf /tmp/helm.tgz "/tmp/linux-${TARGETARCH}"; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

COPY --from=wireguard-go /out/wireguard-go /usr/local/bin/wireguard-go

COPY infra/onprem-agent/requirements.lock.txt /tmp/requirements.lock.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock.txt \
    && rm /tmp/requirements.lock.txt

COPY packages/contracts/src/cmp_contracts /app/cmp_contracts
COPY packages/providers/src/cmp_providers /app/cmp_providers

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

USER 65532:65532
ENTRYPOINT ["python", "-m", "cmp_providers.agent_runtime"]
