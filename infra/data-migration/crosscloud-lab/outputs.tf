output "cluster" {
  value = {
    project_id = var.project_id
    name       = google_container_cluster.lab.name
    location   = google_container_cluster.lab.location
  }
}

output "wireguard" {
  # 키는 Terraform이 만들지 않는다(state에 남지 않게). 실습 스크립트가 만들어 게이트웨이 Secret에만 넣는다.
  value = { eks_ip = local.wg.eks, gke_ip = local.wg.gke, port = local.wg.port, eks_nat_ips = var.eks_nat_ips }
}
