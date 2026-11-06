# Non-sensitive identifiers only; ticket 08 copies these into execution resource references.

output "vpc_id" {
  value = aws_vpc.target.id
}

output "public_subnet_ids" {
  value = [for key in sort(keys(aws_subnet.public)) : aws_subnet.public[key].id]
}

output "private_subnet_ids" {
  value = [for key in sort(keys(aws_subnet.private)) : aws_subnet.private[key].id]
}

output "cluster_name" {
  value = aws_eks_cluster.target.name
}

output "cluster_arn" {
  value = aws_eks_cluster.target.arn
}

output "node_group_name" {
  value = aws_eks_node_group.target.node_group_name
}

output "oidc_provider_arn" {
  value = aws_iam_openid_connect_provider.eks.arn
}

output "ebs_csi_role_arn" {
  value = aws_iam_role.ebs_csi.arn
}

output "lb_controller_role_arn" {
  value = aws_iam_role.lb_controller.arn
}

output "cluster_admin_principal_arns" {
  value = sort([for entry in aws_eks_access_entry.admin : entry.principal_arn])
}
