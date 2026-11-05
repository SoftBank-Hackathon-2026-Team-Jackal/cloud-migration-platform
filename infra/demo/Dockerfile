# Demo control-plane image. `api` stays slim; `worker` adds the pinned Provisioning CLIs (ticket 21, #100).
#   docker build -f infra/demo/Dockerfile --target worker -t cmp-demo-worker:dev .
FROM python:3.11-slim AS api

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY infra/demo/requirements.lock.txt ./requirements.lock.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt

COPY pyproject.toml README.md ./
COPY apps ./apps
COPY packages ./packages
COPY infra/terraform ./infra/terraform
COPY scripts ./scripts
COPY alembic.ini ./
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8000

FROM api AS worker

ARG TARGETARCH
# Terraform matches the version used for the saved-Plan/Apply checks (tickets 05, 08, 10).
ARG TERRAFORM_VERSION=1.17.0
# kubectl/Helm match infra/onprem-agent/Dockerfile (ticket 15).
ARG KUBECTL_VERSION=v1.37.1
ARG HELM_VERSION=v4.3.1
ARG AWSCLI_VERSION=2.35.21
ARG GCLOUD_VERSION=588.0.0
# Terraform/kubectl/Helm: official published SHA-256. AWS CLI and gcloud publish no SHA-256 file;
# these were computed from the official download URLs on 2026-11-05 and are pinned here.
ARG TERRAFORM_SHA256_amd64=c168cab7c637a8769fdb90089ddd19de701bc6ff8aca64dc2e2185fe46f59f67
ARG TERRAFORM_SHA256_arm64=f32478134ffc6d710eec474bbd316652cdddc0f4e336973a3745e8d3a3309cdf
ARG KUBECTL_SHA256_amd64=65691ff77eb6fa44c908b77a1082c9f092c3b9733b5cefabec0d1104890e21a8
ARG KUBECTL_SHA256_arm64=ff749f4b78d9c4f1ec87307df9b50119ed819e2094aa9810cb9acffc3286c8c7
ARG HELM_SHA256_amd64=0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b
ARG HELM_SHA256_arm64=8cf8f66ecbc252ac8f7f06cca68aba88508ddfde3a6a34feb1678ad1694b4f88
ARG AWSCLI_SHA256_amd64=1fe665267a6149dfb8551cec52b419fa6e82533fab6dd7678939209246e792ee
ARG AWSCLI_SHA256_arm64=1d7a1f26a1bd9f0610663b7a4b571def6990692c8c66223ebe09ac2445787bcc
ARG GCLOUD_SHA256_amd64=e38ceac43022bb5a4d94d5a4a9c9c51f90f28c910b59a018ae07011e220c4412
ARG GCLOUD_SHA256_arm64=15d0365242d72caebda77647642ae73f68bde10b1f71338c8a704aea200d8ab1

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) aws_arch=x86_64; gcloud_arch=x86_64; \
             tf_sha="${TERRAFORM_SHA256_amd64}"; kubectl_sha="${KUBECTL_SHA256_amd64}"; \
             helm_sha="${HELM_SHA256_amd64}"; aws_sha="${AWSCLI_SHA256_amd64}"; gcloud_sha="${GCLOUD_SHA256_amd64}" ;; \
      arm64) aws_arch=aarch64; gcloud_arch=arm; \
             tf_sha="${TERRAFORM_SHA256_arm64}"; kubectl_sha="${KUBECTL_SHA256_arm64}"; \
             helm_sha="${HELM_SHA256_arm64}"; aws_sha="${AWSCLI_SHA256_arm64}"; gcloud_sha="${GCLOUD_SHA256_arm64}" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}"; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip; \
    cd /tmp; \
    curl -fsSLo terraform.zip \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip"; \
    echo "${tf_sha}  terraform.zip" | sha256sum -c -; \
    unzip -q terraform.zip terraform -d /usr/local/bin; \
    curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl"; \
    echo "${kubectl_sha}  /usr/local/bin/kubectl" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/kubectl; \
    curl -fsSLo helm.tgz "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz"; \
    echo "${helm_sha}  helm.tgz" | sha256sum -c -; \
    tar -xzf helm.tgz "linux-${TARGETARCH}/helm"; \
    install -m 0755 "linux-${TARGETARCH}/helm" /usr/local/bin/helm; \
    curl -fsSLo awscli.zip "https://awscli.amazonaws.com/awscli-exe-linux-${aws_arch}-${AWSCLI_VERSION}.zip"; \
    echo "${aws_sha}  awscli.zip" | sha256sum -c -; \
    unzip -q awscli.zip; \
    ./aws/install --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli; \
    curl -fsSLo gcloud.tgz \
      "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-${GCLOUD_VERSION}-linux-${gcloud_arch}.tar.gz"; \
    echo "${gcloud_sha}  gcloud.tgz" | sha256sum -c -; \
    tar -xzf gcloud.tgz -C /opt; \
    # The versioned snapshot pins gke-gcloud-auth-plugin to the same SDK release (hash-checked by gcloud).
    CLOUDSDK_COMPONENT_MANAGER_SNAPSHOT_URL="https://dl.google.com/dl/cloudsdk/channels/rapid/components-v${GCLOUD_VERSION}.json" \
      /opt/google-cloud-sdk/bin/gcloud components install gke-gcloud-auth-plugin --quiet; \
    rm -rf /opt/google-cloud-sdk/.install/.backup /root/.config/gcloud; \
    rm -rf /tmp/*; \
    apt-get purge -y curl unzip; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

ENV PATH=/opt/google-cloud-sdk/bin:${PATH} \
    CLOUDSDK_CORE_DISABLE_PROMPTS=1 \
    CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK=1 \
    CHECKPOINT_DISABLE=1

COPY infra/demo/worker-entrypoint.sh /usr/local/bin/cmp-worker-entrypoint
RUN chmod 0755 /usr/local/bin/cmp-worker-entrypoint
ENTRYPOINT ["cmp-worker-entrypoint"]
CMD ["python", "-m", "cmp_worker.workers.run_all"]
