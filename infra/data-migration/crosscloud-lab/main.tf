locals {
  name = "cmp-data-xc"
  # WireGuard 터널 주소. 원본(EKS) 게이트웨이 .1, 대상(GKE) 게이트웨이 .2
  wg = { eks = "10.210.0.1", gke = "10.210.0.2", port = 51820 }
}

resource "google_compute_network" "lab" {
  name                    = local.name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "lab" {
  name          = "${local.name}-${var.region}"
  network       = google_compute_network.lab.id
  ip_cidr_range = "10.30.0.0/24"

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.31.0.0/20"
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.32.0.0/24"
  }
}

# 기본 Compute SA에는 역할이 없다(조직 정책). 노드 전용 SA에 최소 역할을 준다.
resource "google_service_account" "nodes" {
  account_id   = "${local.name}-nodes"
  display_name = "cmp data-migration crosscloud lab GKE nodes"
}

resource "google_project_iam_member" "nodes" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.nodes.email}"
}

resource "google_container_cluster" "lab" {
  name                     = local.name
  location                 = var.zone
  network                  = google_compute_network.lab.id
  subnetwork               = google_compute_subnetwork.lab.id
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  release_channel {
    channel = "REGULAR"
  }
}

resource "google_container_node_pool" "lab" {
  name       = "default"
  cluster    = google_container_cluster.lab.id
  node_count = 1

  node_config {
    machine_type    = var.machine_type
    disk_size_gb    = 30
    disk_type       = "pd-balanced"
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    shielded_instance_config {
      enable_secure_boot = true
    }
  }

  depends_on = [google_project_iam_member.nodes]
}
