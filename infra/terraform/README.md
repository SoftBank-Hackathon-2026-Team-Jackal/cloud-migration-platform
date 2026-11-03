# Terraform templates

AWS EKS와 GCP GKE 인프라 Plan 템플릿은 각각 [aws/README.md](aws/README.md), [gcp/README.md](gcp/README.md)를 따른다. 중앙 S3 State와 lockfile은 실제 운영 계획에 사용하며, 검증 전용 로컬 State Plan은 승인 불가다. Terraform Plan/State/변수 JSON은 Git에 저장하지 않는다.
