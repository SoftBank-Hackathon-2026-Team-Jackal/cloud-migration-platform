# GCP GKE infrastructure Plan template

This root creates a new custom VPC, one regional subnetwork with GKE Pod and
Service secondary ranges, Cloud Router/NAT for private nodes, an internal
firewall rule, a regional GKE Standard cluster and a managed node pool.
GKE also creates its own cluster firewall rules. It does not deploy an app,
database, Ingress, TLS, storage class, or image pull credentials.

The calling Planning Activity must resolve the reviewed target profile to
`GcpPlanInput`: target project/region, explicit CIDRs, authorized API CIDRs,
release channel, node type/count/disk, existing node service account, stable
S3 State identity and credential references. The current v2 Planning Spec does
not yet contain all these concrete GCP fields; that adapter is a later
integration task. Do not infer a production profile from this test fixture.

The caller supplies a pre-existing node service account in the target project
with the required GKE node permissions, and a pre-existing central S3 State
bucket. The S3 backend uses an environment-stable
`cmp/gcp/<environment_id>/terraform.tfstate` key and native lockfile. The
bucket, service account and permissions are not created here. The S3 region is
separate from the GCP region. An explicit Google ADC principal is needed for
the provider and `cmp-aws-prod` is the workstation profile for the S3 backend.
`cmp-gcp-prod` selects the workstation GCP project but does not provide ADC.

## Local Plan→Preview check

After `terraform fmt -check`, `terraform init -backend=false` and
`terraform validate`, run the public Plan command with the safe fixture:

```sh
uv run python scripts/plan_gcp_gke.py \
  tests/fixtures/provisioning/gcp-local-plan-input.json \
  /private/tmp/cmp-gcp-plan-artifacts \
  --mode LOCAL_TEST
```

`LOCAL_TEST` requires the fake project `cmp-plan-local`, replaces the copied
backend block with local State, injects an invalid token, and plans without
refresh. It does not call gcloud or AWS. To exercise the actual project and
ADC identity before a central State bucket exists, use `LOCAL_PROJECT` with
the reviewed project input, `--gcloud-configuration cmp-gcp-prod` and
`--expected-adc-email <ADC principal>`. This mode checks project and ADC,
then creates a local, no-refresh Plan without S3 access or Apply.

Both local modes write binary Plan, Spec, Manifest, Preview and evidence under
a protected Plan ID/revision directory. Their Preview has
`LOCAL_STATE_UNAPPROVABLE` in `errors`; the shared approval check rejects
them. Never use a local binary Plan for Apply or operational approval.
The original template and provider lock digests are recorded in
`evidence.json`; the local backend override is identified by `plan_mode`.
These artifacts remain outside Git because saved Plans can contain sensitive
data. A local project Plan does not prove GKE API enablement, service-account
existence/IAM, quota, central State access or actual cluster creation.

## S3-backed Plan preconditions

For a real target, provide an input JSON with the reviewed project, existing
node service account and S3 bucket/account/region. Use
`--gcloud-configuration cmp-gcp-prod --expected-adc-email <ADC principal>
--aws-profile cmp-aws-prod` with the default `S3` mode. The runner checks the
gcloud project, AWS State account and ADC principal before backend init. ADC
must include the email scope to expose its identity in Google tokeninfo; an
absent or different identity fails closed. S3 bucket access needs the State key
and `.tflock` permissions. A real S3-backed Plan and project permission check
must be reviewed before any operational approval. Ticket 06 never runs Apply.

## Approved Plan Apply (ticket 10)

`gcp-gke-v3` (ticket 26) sets `datapath_provider = "ADVANCED_DATAPATH"` (Dataplane V2), which enforces Kubernetes NetworkPolicy without an add-on. Changing the datapath replaces a cluster, so it applies to new clusters only; 11 refuses a cluster without `anetd`.

`gcp-gke-v2` adds non-sensitive outputs (project, network, subnetwork, cluster name/location, node
pool) and turns `deletion_protection` into a variable (default `true`; it can only be lifted by a
new reviewed Plan before cleanup).

After the 07 approval claim, `execute_gcp_plan` (`cmp_providers.gcp_apply`) verifies:

- the stored `spec.json`/`saved.tfplan` bytes against the manifest ArtifactRefs
- the project/region/State identity/key `cmp/gcp/<environment_id>/terraform.tfstate`
- the Plan mode recorded in `evidence.json` is `S3` (`LOCAL_TEST`/`LOCAL_PROJECT` are refused)
- the template and lockfile fingerprint
- the Google ADC principal (`PROVISIONING_GCP_ADC_EMAIL`)
- the AWS account holding the central State

It then applies only the saved Plan with no re-plan, destroy or retry, and records non-sensitive
outputs plus `gcloud container clusters/node-pools describe` status.

One-time project prerequisites outside Terraform (ticket 10 G6): enable `container.googleapis.com`
and create the node service account `cmp-gke-nodes`. Granting it
`roles/container.defaultNodeServiceAccount` (node log/metric writes) is recommended but was
deferred for the P0 demo project because the operator account has only `roles/editor` and cannot
change project IAM; nodes run and workloads deploy without it, but Cloud Logging/Monitoring stay
empty. A project Owner can add it later without touching Terraform State.

Worker settings: `PROVISIONING_ARTIFACT_ROOT`, `PROVISIONING_AWS_PROFILE` (S3 State),
`PROVISIONING_GCLOUD_CONFIGURATION`, `PROVISIONING_GCP_ADC_EMAIL`, `TERRAFORM_BIN`.

Recovery Revision (ticket 14 F9): when this environment's State already holds resources from an
earlier Apply, a Plan that keeps them as `no-op` and creates only the missing ones is accepted with
the Preview warning `RECOVERY_CONTINUATION`. Plans with `update` or `delete` stay rejected.
