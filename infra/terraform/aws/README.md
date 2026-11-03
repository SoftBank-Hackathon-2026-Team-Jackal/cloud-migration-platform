# AWS EKS infrastructure Plan (Ticket 05, Issue #2)

This root plans a **new** VPC, two or more public/private AZ pairs, IGW, one NAT gateway, routing, control-plane security group, IAM roles, EKS cluster and managed node group. Public subnets are tagged for a future internet-facing ALB; the ALB controller, Ingress, TLS, EBS CSI, StorageClass, application and database are not planned here. The EKS API has a private endpoint and a public endpoint limited to explicit CIDRs. Private nodes use the NAT path for ECR access. A single NAT gateway is an AZ dependency and incurs cost **only if a later approved Apply runs**.

The input is `cmp_providers.aws_preview.AwsPlanInput`. The current `ProvisioningSpecV2` has only target/profile/capability references, so a caller must resolve and review concrete Network/Node inputs; no automatic v2-to-AWS conversion is claimed. It requires an existing, access-controlled S3 state bucket and the stable key `cmp/<environment_id>/terraform.tfstate`. Terraform 1.10+ `use_lockfile=true` and encryption are set at initialization. The bucket and permissions are external prerequisites; this ticket never creates them. The AWS provider is constrained to the asserted account ID and region. The checked-in `.terraform.lock.hcl` pins the provider selected during static validation. Actual IAM, AZ availability, EKS version, instance type, quotas, state access and absence of conflicting resources require a separate isolated-account Plan check.

Example invocation after reviewing a complete JSON input, its AWS account, S3 backend and IAM permissions:

```sh
aws sts get-caller-identity --profile cmp-aws-prod
uv run python scripts/plan_aws_eks.py /protected/input.json /protected/plan-artifacts \
  --aws-profile cmp-aws-prod --terraform-bin terraform
```

The runner rechecks the AWS account before Terraform. It writes `spec.json`, `saved.tfplan`, `manifest.json`, `preview.json` and `evidence.json` under a new Plan ID/revision directory with mode 0700/0600. It refuses an existing revision and never calls `apply`. The binary Plan and input/spec are internal artifacts: keep them encrypted and access controlled, do not commit or print them. `evidence.json` records template, provider lockfile and manifest digests; the manifest binds the exact Plan and Spec bytes to the target account, State identity, Plan ID, Revision and template version. Production publication to a durable immutable artifact store and approved Apply are later integration work.

The Preview uses Terraform's action/type list but ignores every `before`, `after`, `*_sensitive` and output value. It only displays fixed resource labels and validated network/Node/API access inputs. Unknown resource types or unexpected actions reject the Plan. `INFRASTRUCTURE` scope cannot stand for application or database deployment. ALB deployment and the single-NAT limitation are explicit warnings.

`tests/fixtures/terraform/aws-create-plan.json` is a sanitized action-only shape from a real AWS Provider Plan, with a fake canary to verify redaction. It contains no actual Plan bytes, State or Provider output values. Ticket 06 needs its own GCP resource-action fixture; the shared Plan/Preview contract fixtures do not replace Provider-specific Plan checks.

Static checks (no AWS resource changes):

```sh
terraform fmt -check -recursive infra/terraform/aws
cd infra/terraform/aws
terraform init -backend=false -input=false -lockfile=readonly
terraform validate -no-color
```
