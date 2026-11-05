# 데이터 이전 크로스 클라우드 실습(specs/data-migration ticket 06): 원본 EKS ↔ 대상 GKE를 WireGuard로 잇는다.
# GKE 클러스터(서울, 노드 1대)만 만든다. 원본 EKS는 기존 클러스터를 쓴다. README.md 참고.
#
#   export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token --account=<계정>)
#   terraform init -backend-config=backend.hcl && terraform apply -var eks_nat_ips='["x.x.x.x/32"]'
#   끝나면 terraform destroy (노드·LB가 켜져 있는 동안 과금)

terraform {
  required_version = ">= 1.10"

  required_providers {
    google = { source = "hashicorp/google", version = "~> 7.0" }
  }

  # bucket·key·region·profile은 backend.hcl로 넘긴다(저장소에 두지 않음)
  backend "s3" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone

  default_labels = { managed-by = "terraform", stack = "cmp-data-crosscloud-lab" }
}
