variable "project_id" {
  type    = string
  default = "sbh26-prod"
}

variable "region" {
  type    = string
  default = "asia-northeast3"
}

variable "zone" {
  type    = string
  default = "asia-northeast3-a"
}

variable "machine_type" {
  type    = string
  default = "e2-standard-2"
}

variable "eks_nat_ips" {
  description = "원본 EKS 노드가 인터넷으로 나갈 때의 공인 IP(CIDR). WireGuard UDP는 여기서 오는 것만 받는다"
  type        = list(string)
}
