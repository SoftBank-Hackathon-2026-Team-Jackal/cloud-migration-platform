"""AWS API calls the target connection role must allow (ticket 47, B-04·B-14·B-18).

`infra/connections/target-role.yaml` is the user's target build role. Terraform (`infra/terraform/
aws`, Plan·Apply·destroy) and the AWS workloads (`aws eks update-kubeconfig`, describe calls) run
as that role. This module is the reviewed list of those calls and of calls the role must refuse.
`tests/test_aws_target_access.py` evaluates the template against it offline and
`infra/connections/simulate-target-role.py` asks IAM (`simulate-custom-policy`, read-only).

Each call names the resources IAM authorizes it against (Service Authorization Reference) and how
they carry the Terraform tag `cmp:managed-by=terraform`:
- `new`: the resource the call creates. Its tags come with the request (`aws:RequestTag`).
- `on`: resources Terraform created earlier. They carry the tag (`aws:ResourceTag`).
- `untagged`: resources the call touches that Terraform never tags (an SG rule, an ENI).
EC2 evaluates request tags per created resource, so `on` resources get no `aws:RequestTag` (the
strict reading). EKS lists `aws:RequestTag` at action level (`request_tags="all"`).
"""

import re
from dataclasses import dataclass, field
from typing import Literal

import yaml

TERRAFORM_TAG_KEY = "cmp:managed-by"
TERRAFORM_TAG_VALUE = "terraform"
# The tag the platform principal's AssumeRole policy requires (23b `cmp-demo-connections`).
CONNECTION_TAG_VALUE = "cloud-migration-platform"
EKS_ADMIN_POLICY = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
# AWS managed policies `infra/terraform/aws` attaches; anything else needs the platform prefix.
REVIEWED_MANAGED_POLICIES = (
    "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly",
    "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy",
)


@dataclass(frozen=True)
class Call:
    action: str
    new: str | None = None
    on: tuple[str, ...] = ()
    untagged: tuple[str, ...] = ()
    context: tuple[tuple[str, str], ...] = ()
    request_tags: Literal["new", "all"] = "new"


def _ec2_create(action: str, new: str, *parents: str) -> list[Call]:
    # ec2:CreateAction holds the API name without the service prefix.
    tag = Call("ec2:CreateTags", new=new, context=(("ec2:CreateAction", action[4:]),))
    return [Call(action, new=new, on=parents), tag]


def _eks(action: str, *on: str, create: bool = False, **extra) -> Call:
    # EKS creates are authorized against the parent cluster (CreateCluster: no resource) with the
    # request tags at action level; the new nodegroup/addon/access entry is not a resource here.
    return Call(action, on=on, request_tags="all" if create else "new", **extra)


_PASS_EKS = (("iam:PassedToService", "eks.amazonaws.com"),)
_DESCRIBE_NETWORK = [Call("ec2:DescribeNetworkInterfaces"), Call("ec2:DescribeVpcs")]

# Terraform resource type -> API calls its create/read/update/delete makes (AWS provider 6.x).
TERRAFORM_CALLS: dict[str, list[Call]] = {
    "aws_vpc": [
        *_ec2_create("ec2:CreateVpc", "vpc"),
        Call("ec2:ModifyVpcAttribute", on=("vpc",)),
        Call("ec2:DeleteVpc", on=("vpc",)),
        Call("ec2:DescribeVpcs"),
        Call("ec2:DescribeVpcAttribute", on=("vpc",)),
        Call("ec2:DescribeRouteTables"),
        Call("ec2:DescribeSecurityGroups"),
        Call("ec2:DescribeNetworkAcls"),
    ],
    "aws_internet_gateway": [
        *_ec2_create("ec2:CreateInternetGateway", "internet-gateway"),
        Call("ec2:AttachInternetGateway", on=("internet-gateway", "vpc")),
        Call("ec2:DetachInternetGateway", on=("internet-gateway", "vpc")),
        Call("ec2:DeleteInternetGateway", on=("internet-gateway",)),
        Call("ec2:DescribeInternetGateways"),
    ],
    "aws_subnet": [
        *_ec2_create("ec2:CreateSubnet", "subnet", "vpc"),
        Call("ec2:ModifySubnetAttribute", on=("subnet",)),
        Call("ec2:DeleteSubnet", on=("subnet",)),
        Call("ec2:DescribeSubnets"),
        *_DESCRIBE_NETWORK,
    ],
    "aws_route_table": [
        *_ec2_create("ec2:CreateRouteTable", "route-table", "vpc"),
        Call("ec2:DeleteRouteTable", on=("route-table",)),
        Call("ec2:DescribeRouteTables"),
    ],
    "aws_route": [
        Call("ec2:CreateRoute", on=("route-table",)),
        Call("ec2:DeleteRoute", on=("route-table",)),
        Call("ec2:DescribeRouteTables"),
    ],
    "aws_route_table_association": [
        Call("ec2:AssociateRouteTable", on=("route-table", "subnet")),
        Call("ec2:DisassociateRouteTable", on=("route-table", "subnet")),
        Call("ec2:DescribeRouteTables"),
    ],
    "aws_eip": [
        *_ec2_create("ec2:AllocateAddress", "elastic-ip"),
        Call("ec2:ReleaseAddress", on=("elastic-ip",)),
        Call("ec2:DisassociateAddress", on=("elastic-ip",), untagged=("network-interface",)),
        Call("ec2:DescribeAddresses"),
        Call("ec2:DescribeAddressesAttribute"),
    ],
    "aws_nat_gateway": [
        *_ec2_create("ec2:CreateNatGateway", "natgateway", "subnet", "elastic-ip", "vpc"),
        Call("ec2:DeleteNatGateway", on=("natgateway",)),
        Call("ec2:DescribeNatGateways"),
    ],
    "aws_security_group": [
        *_ec2_create("ec2:CreateSecurityGroup", "security-group", "vpc"),
        # The provider revokes the default allow-all egress rule right after creating the SG.
        Call("ec2:RevokeSecurityGroupEgress", on=("security-group",)),
        Call("ec2:DeleteSecurityGroup", on=("security-group",)),
        Call("ec2:DescribeSecurityGroups"),
        Call("ec2:DescribeSecurityGroupRules"),
        *_DESCRIBE_NETWORK,
    ],
    "aws_vpc_security_group_egress_rule": [
        # B-14: the new rule is its own resource; it is authorized next to its (tagged) SG.
        Call(
            "ec2:AuthorizeSecurityGroupEgress",
            on=("security-group",),
            untagged=("security-group-rule",),
        ),
        Call(
            "ec2:CreateTags",
            new="security-group-rule",
            context=(("ec2:CreateAction", "AuthorizeSecurityGroupEgress"),),
        ),
        Call("ec2:RevokeSecurityGroupEgress", on=("security-group",)),
        Call("ec2:DescribeSecurityGroupRules"),
    ],
    "aws_iam_role": [
        Call("iam:CreateRole", new="role"),
        Call("iam:TagRole", new="role"),
        Call("iam:GetRole", on=("role",)),
        Call("iam:ListRolePolicies", on=("role",)),
        Call("iam:ListAttachedRolePolicies", on=("role",)),
        Call("iam:ListInstanceProfilesForRole", on=("role",)),
        Call("iam:UpdateAssumeRolePolicy", on=("role",)),
        Call("iam:DeleteRole", on=("role",)),
    ],
    "aws_iam_role_policy_attachment": [
        *(
            Call("iam:AttachRolePolicy", on=("role",), context=(("iam:PolicyARN", arn),))
            for arn in REVIEWED_MANAGED_POLICIES
        ),
        Call("iam:AttachRolePolicy", on=("role",), context=(("iam:PolicyARN", "{policy}"),)),
        Call("iam:DetachRolePolicy", on=("role",)),
        Call("iam:ListAttachedRolePolicies", on=("role",)),
    ],
    "aws_iam_policy": [
        Call("iam:CreatePolicy", new="policy"),
        Call("iam:TagPolicy", new="policy"),
        Call("iam:GetPolicy", on=("policy",)),
        Call("iam:GetPolicyVersion", on=("policy",)),
        Call("iam:ListPolicyVersions", on=("policy",)),
        Call("iam:ListPolicyTags", on=("policy",)),
        Call("iam:CreatePolicyVersion", on=("policy",)),
        Call("iam:DeletePolicyVersion", on=("policy",)),
        Call("iam:DeletePolicy", on=("policy",)),
    ],
    "aws_iam_openid_connect_provider": [
        Call("iam:CreateOpenIDConnectProvider", new="oidc-provider"),
        Call("iam:TagOpenIDConnectProvider", new="oidc-provider"),
        Call("iam:GetOpenIDConnectProvider", on=("oidc-provider",)),
        Call("iam:ListOpenIDConnectProviderTags", on=("oidc-provider",)),
        Call("iam:UpdateOpenIDConnectProviderThumbprint", on=("oidc-provider",)),
        Call("iam:DeleteOpenIDConnectProvider", on=("oidc-provider",)),
    ],
    "aws_eks_cluster": [
        Call("iam:PassRole", on=("role",), context=_PASS_EKS),
        Call(
            "iam:CreateServiceLinkedRole",
            context=(("iam:AWSServiceName", "eks.amazonaws.com"),),
        ),
        _eks("eks:CreateCluster", create=True),
        _eks("eks:TagResource", "cluster"),
        _eks("eks:DescribeCluster", "cluster"),
        _eks("eks:UpdateClusterConfig", "cluster"),
        _eks("eks:UpdateClusterVersion", "cluster"),
        _eks("eks:DeleteCluster", "cluster"),
        Call("ec2:DescribeSubnets"),
        Call("ec2:DescribeSecurityGroups"),
    ],
    "aws_eks_node_group": [
        Call("iam:PassRole", on=("role",), context=_PASS_EKS),
        Call(
            "iam:CreateServiceLinkedRole",
            context=(("iam:AWSServiceName", "eks-nodegroup.amazonaws.com"),),
        ),
        _eks("eks:CreateNodegroup", "cluster", create=True),
        _eks("eks:TagResource", "nodegroup"),
        _eks("eks:DescribeNodegroup", "nodegroup"),
        _eks("eks:UpdateNodegroupConfig", "nodegroup"),
        _eks("eks:UpdateNodegroupVersion", "nodegroup"),
        _eks("eks:DeleteNodegroup", "nodegroup"),
        Call("ec2:DescribeSubnets"),
    ],
    "aws_eks_addon": [
        Call("iam:PassRole", on=("role",), context=_PASS_EKS),
        _eks("eks:CreateAddon", "cluster", create=True),
        _eks("eks:TagResource", "addon"),
        _eks("eks:DescribeAddon", "addon"),
        _eks("eks:DescribeAddonVersions"),
        _eks("eks:UpdateAddon", "addon"),
        _eks("eks:DeleteAddon", "addon"),
    ],
    # Ticket 47 (B-10): the user's kubectl access to the new cluster.
    "aws_eks_access_entry": [
        _eks(
            "eks:CreateAccessEntry",
            "cluster",
            create=True,
            context=(("eks:accessEntryType", "STANDARD"),),
        ),
        _eks("eks:TagResource", "access-entry"),
        _eks("eks:DescribeAccessEntry", "access-entry"),
        _eks("eks:UpdateAccessEntry", "access-entry"),
        _eks("eks:DeleteAccessEntry", "access-entry"),
        _eks("eks:ListAccessEntries", "cluster"),
    ],
    "aws_eks_access_policy_association": [
        _eks(
            "eks:AssociateAccessPolicy",
            "access-entry",
            context=(("eks:policyArn", EKS_ADMIN_POLICY),),
        ),
        _eks("eks:ListAssociatedAccessPolicies", "access-entry"),
        _eks("eks:DisassociateAccessPolicy", "access-entry"),
    ],
}

# AWS workloads (09): `aws sts get-caller-identity`, `aws eks update-kubeconfig`/`describe-*`.
# kubectl authenticates with `aws eks get-token` (a presigned STS call, no IAM action) and gets
# Kubernetes admin from the cluster creator access entry (`bootstrap_cluster_creator_admin_…`).
WORKLOAD_CALLS: list[Call] = [
    Call("sts:GetCallerIdentity"),
    _eks("eks:DescribeCluster", "cluster"),
    _eks("eks:DescribeNodegroup", "nodegroup"),
    _eks("eks:ListClusters"),
]


@dataclass(frozen=True)
class Refusal:
    """A call the role must not be able to make (privilege guard)."""

    why: str
    action: str
    resource: str
    context: dict[str, str] = field(default_factory=dict)


def refusals(account: str, region: str, name: str) -> list[Refusal]:
    vpc = f"arn:aws:ec2:{region}:{account}:vpc/vpc-0user"
    cluster = f"arn:aws:eks:{region}:{account}:cluster/user-prod"
    request_tf = {f"aws:RequestTag/{TERRAFORM_TAG_KEY}": TERRAFORM_TAG_VALUE}
    return [
        Refusal("tag a user's existing VPC so it becomes deletable", "ec2:CreateTags", vpc,
                request_tf),
        Refusal("delete a user's untagged VPC", "ec2:DeleteVpc", vpc),
        Refusal("create a subnet in a user's VPC", "ec2:CreateSubnet", vpc, request_tf),
        Refusal("delete a user's untagged security group", "ec2:DeleteSecurityGroup",
                f"arn:aws:ec2:{region}:{account}:security-group/sg-0user"),
        Refusal("read instance user data", "ec2:DescribeInstanceAttribute",
                f"arn:aws:ec2:{region}:{account}:instance/i-0user"),
        Refusal("attach AdministratorAccess to a platform role", "iam:AttachRolePolicy",
                f"arn:aws:iam::{account}:role/{name}-eks-node",
                {"iam:PolicyARN": "arn:aws:iam::aws:policy/AdministratorAccess"}),
        Refusal("give a platform role inline permissions", "iam:PutRolePolicy",
                f"arn:aws:iam::{account}:role/{name}-eks-node"),
        Refusal("make a role the platform principal may assume", "iam:CreateRole",
                f"arn:aws:iam::{account}:role/{name}-escalate",
                {f"aws:RequestTag/{TERRAFORM_TAG_KEY}": CONNECTION_TAG_VALUE}),
        Refusal("tag a role so the platform principal may assume it", "iam:TagRole",
                f"arn:aws:iam::{account}:role/{name}-escalate",
                {f"aws:RequestTag/{TERRAFORM_TAG_KEY}": CONNECTION_TAG_VALUE}),
        Refusal("create a role outside the platform prefix", "iam:CreateRole",
                f"arn:aws:iam::{account}:role/user-admin", request_tf),
        Refusal("read a user role's policies", "iam:GetRolePolicy",
                f"arn:aws:iam::{account}:role/user-admin"),
        Refusal("pass a platform role to Lambda", "iam:PassRole",
                f"arn:aws:iam::{account}:role/{name}-eks-node",
                {"iam:PassedToService": "lambda.amazonaws.com"}),
        Refusal("delete a user's EKS cluster", "eks:DeleteCluster", cluster,
                {f"aws:ResourceTag/{TERRAFORM_TAG_KEY}": TERRAFORM_TAG_VALUE}),
        Refusal("describe a user's EKS cluster", "eks:DescribeCluster", cluster),
        Refusal("grant a non-admin access policy", "eks:AssociateAccessPolicy",
                f"arn:aws:eks:{region}:{account}:access-entry/{name}/role/{account}/x/1",
                {f"aws:ResourceTag/{TERRAFORM_TAG_KEY}": TERRAFORM_TAG_VALUE,
                 "eks:policyArn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"}),
        Refusal("create a node-type access entry", "eks:CreateAccessEntry",
                f"arn:aws:eks:{region}:{account}:cluster/{name}",
                {f"aws:ResourceTag/{TERRAFORM_TAG_KEY}": TERRAFORM_TAG_VALUE,
                 "eks:accessEntryType": "HYBRID_LINUX"}),
        Refusal("create an IAM user", "iam:CreateUser", f"arn:aws:iam::{account}:user/{name}"),
    ]  # fmt: skip


def resource_arn(kind: str, account: str, region: str, name: str) -> str:
    """A sample ARN of `kind` for cluster `name` (EC2 ids are placeholders)."""
    ec2 = {
        "vpc": "vpc-0cmp",
        "subnet": "subnet-0cmp",
        "internet-gateway": "igw-0cmp",
        "route-table": "rtb-0cmp",
        "elastic-ip": "eipalloc-0cmp",
        "natgateway": "nat-0cmp",
        "security-group": "sg-0cmp",
        "security-group-rule": "sgr-0cmp",
        "network-interface": "eni-0cmp",
    }
    if kind in ec2:
        return f"arn:aws:ec2:{region}:{account}:{kind}/{ec2[kind]}"
    eks = f"arn:aws:eks:{region}:{account}"
    return {
        "cluster": f"{eks}:cluster/{name}",
        "nodegroup": f"{eks}:nodegroup/{name}/{name}-primary/0cmp",
        "addon": f"{eks}:addon/{name}/vpc-cni/0cmp",
        "access-entry": f"{eks}:access-entry/{name}/role/{account}/admin/0cmp",
        "role": f"arn:aws:iam::{account}:role/{name}-eks-node",
        "policy": f"arn:aws:iam::{account}:policy/{name}-lb-controller",
        "oidc-provider": f"arn:aws:iam::{account}:oidc-provider/oidc.eks.{region}.amazonaws.com/id/0CMP",
    }[kind]


@dataclass(frozen=True)
class Case:
    """One authorization question: this action on this resource with this request context."""

    label: str
    action: str
    resource: str
    context: dict[str, str]


def allowed_cases(account: str, region: str, name: str) -> list[Case]:
    """Every (call, resource) Terraform and the workloads need, in the strict tag reading."""
    request = {f"aws:RequestTag/{TERRAFORM_TAG_KEY}": TERRAFORM_TAG_VALUE}
    existing = {f"aws:ResourceTag/{TERRAFORM_TAG_KEY}": TERRAFORM_TAG_VALUE}
    cases: list[Case] = []
    groups = {**TERRAFORM_CALLS, "workloads": WORKLOAD_CALLS}
    for group, calls in groups.items():
        for call in calls:
            extra = {
                key: value.format(policy=resource_arn("policy", account, region, name))
                for key, value in call.context
            }
            everywhere = dict(extra, **request) if call.request_tags == "all" else extra
            targets: list[tuple[str, dict[str, str]]] = []
            if call.new:
                targets.append((call.new, dict(extra, **request)))
            targets += [(kind, dict(everywhere, **existing)) for kind in call.on]
            targets += [(kind, dict(everywhere)) for kind in call.untagged]
            if not targets:
                targets.append(("*", dict(everywhere)))
            for kind, context in targets:
                arn = "*" if kind == "*" else resource_arn(kind, account, region, name)
                cases.append(Case(f"{group}:{call.action}:{kind}", call.action, arn, context))
    return cases


def _render(node, params: dict[str, str]):
    if isinstance(node, list):
        return [_render(item, params) for item in node]
    if not isinstance(node, dict):
        return node
    if set(node) == {"Ref"}:
        return params[node["Ref"]]
    if set(node) == {"Fn::Sub"}:
        return re.sub(r"\$\{([^}]+)\}", lambda m: params[m.group(1)], node["Fn::Sub"])
    if set(node) == {"Fn::GetAtt"}:
        return params[".".join(node["Fn::GetAtt"])]
    return {key: _render(value, params) for key, value in node.items()}


def render_target_policies(template_text: str, *, account: str, role_arn: str) -> list[dict]:
    """The role's managed policy documents as CloudFormation would create them (defaults).

    The template uses long-form intrinsics only, so it is plain YAML.
    """
    template = yaml.safe_load(template_text)
    params = {
        "AWS::Partition": "aws",
        "AWS::AccountId": account,
        "ResourcePrefix": template["Parameters"]["ResourcePrefix"]["Default"],
        "ManagedByTag": template["Parameters"]["ManagedByTag"]["Default"],
        "ConnectionRole.Arn": role_arn,
    }
    return [
        _render(resource["Properties"]["PolicyDocument"], params)
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::IAM::ManagedPolicy"
    ]


def render_target_policy(template_text: str, *, account: str, role_arn: str) -> dict:
    """All statements of the role's managed policies as one document (for evaluation)."""
    documents = render_target_policies(template_text, account=account, role_arn=role_arn)
    return {
        "Version": "2012-10-17",
        "Statement": [statement for document in documents for statement in document["Statement"]],
    }
