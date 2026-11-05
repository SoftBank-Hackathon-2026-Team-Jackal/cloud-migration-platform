#!/usr/bin/env bash
# Demo worker entrypoint (ticket 21, #100).
# Optional GCP identity: when the operator placed a service-account key at $CMP_GCP_KEY_FILE (a
# read-only host mount, never the image or repository), activate it into a tmpfs gcloud config
# directory as the named configuration $PROVISIONING_GCLOUD_CONFIGURATION, write the ADC well-known
# file there and export the expected ADC principal. Nothing secret is printed.
set -euo pipefail

key_file="${CMP_GCP_KEY_FILE:-/run/cmp/gcp/key.json}"
configuration="${PROVISIONING_GCLOUD_CONFIGURATION:-}"
project="${CMP_GCP_PROJECT:-}"

if [[ -f "$key_file" && -n "$configuration" && -n "$project" ]]; then
  config_dir="${CLOUDSDK_CONFIG:-$HOME/.config/gcloud}"
  install -d -m 700 "$config_dir"
  email="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["client_email"])' "$key_file")"
  gcloud config configurations describe "$configuration" >/dev/null 2>&1 \
    || gcloud config configurations create "$configuration" --no-activate >/dev/null 2>&1
  gcloud auth activate-service-account "$email" --key-file "$key_file" --quiet >/dev/null 2>&1
  gcloud config set account "$email" --configuration "$configuration" >/dev/null 2>&1
  gcloud config set project "$project" --configuration "$configuration" >/dev/null 2>&1
  install -m 600 "$key_file" "$config_dir/application_default_credentials.json"
  export PROVISIONING_GCP_ADC_EMAIL="${PROVISIONING_GCP_ADC_EMAIL:-$email}"
  echo "GCP configuration '$configuration' ready for project '$project'"
elif [[ -n "$configuration" ]]; then
  echo "GCP identity not configured; GCP Plan/Apply will fail closed" >&2
fi

exec "$@"
