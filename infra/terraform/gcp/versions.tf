terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.0.0, < 8.0.0"
    }
  }
  backend "s3" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}
