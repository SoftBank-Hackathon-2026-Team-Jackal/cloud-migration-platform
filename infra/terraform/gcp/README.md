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
refresh. It does not call gcloud or AWS. The binary Plan, Spec, Manifest,
Preview and evidence are written under a protected Plan ID/revision directory.
The Preview has `LOCAL_STATE_UNAPPROVABLE` in `errors`; the shared approval
check rejects it. Never use this binary Plan for Apply or operational approval.
The original template and provider lock digests are recorded in
`evidence.json`; the local backend override is identified by `plan_mode`.
These artifacts remain outside Git because saved Plans can contain sensitive
data.

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
