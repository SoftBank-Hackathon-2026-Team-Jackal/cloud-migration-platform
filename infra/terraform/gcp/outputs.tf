# Non-sensitive identifiers only; ticket 10 copies these into execution resource references.

output "project_id" {
  value = var.project_id
}

output "network_name" {
  value = google_compute_network.target.name
}

output "subnetwork_name" {
  value = google_compute_subnetwork.target.name
}

output "cluster_name" {
  value = google_container_cluster.target.name
}

output "cluster_location" {
  value = google_container_cluster.target.location
}

output "node_pool_name" {
  value = google_container_node_pool.target.name
}
