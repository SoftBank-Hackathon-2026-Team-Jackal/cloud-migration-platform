resource "google_compute_network" "target" {
  name                    = "${var.name}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "target" {
  name                     = "${var.name}-subnet"
  region                   = var.region
  network                  = google_compute_network.target.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "${var.name}-pods"
    ip_cidr_range = var.pods_cidr
  }
  secondary_ip_range {
    range_name    = "${var.name}-services"
    ip_cidr_range = var.services_cidr
  }
}

resource "google_compute_router" "target" {
  name    = "${var.name}-router"
  region  = var.region
  network = google_compute_network.target.id
}

resource "google_compute_router_nat" "target" {
  name                               = "${var.name}-nat"
  region                             = var.region
  router                             = google_compute_router.target.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.target.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
}

resource "google_compute_firewall" "internal" {
  name          = "${var.name}-internal"
  network       = google_compute_network.target.name
  direction     = "INGRESS"
  source_ranges = [var.subnet_cidr, var.pods_cidr]
  target_tags   = ["${var.name}-node"]

  allow { protocol = "tcp" }
  allow { protocol = "udp" }
  allow { protocol = "icmp" }
}

resource "google_container_cluster" "target" {
  name                     = var.name
  location                 = var.region
  network                  = google_compute_network.target.id
  subnetwork               = google_compute_subnetwork.target.id
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = var.deletion_protection
  # Ticket 26: Dataplane V2 (eBPF) enforces Kubernetes NetworkPolicy natively. New clusters only:
  # changing the datapath replaces an existing cluster.
  datapath_provider = "ADVANCED_DATAPATH"

  release_channel { channel = var.release_channel }
  ip_allocation_policy {
    cluster_secondary_range_name  = "${var.name}-pods"
    services_secondary_range_name = "${var.name}-services"
  }
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = var.master_cidr
  }
  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.api_access_cidrs
      content {
        cidr_block   = cidr_blocks.value
        display_name = "approved-${cidr_blocks.key}"
      }
    }
  }
}

resource "google_container_node_pool" "target" {
  name       = "${var.name}-primary"
  location   = var.region
  cluster    = google_container_cluster.target.name
  node_count = var.node_count

  node_config {
    machine_type    = var.node_machine_type
    disk_size_gb    = var.node_disk_gib
    service_account = var.node_service_account_email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    tags            = ["${var.name}-node"]
    metadata        = { "disable-legacy-endpoints" = "true" }
  }
  depends_on = [google_compute_router_nat.target]
}
