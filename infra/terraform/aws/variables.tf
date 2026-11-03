variable "region" { type = string }
variable "account_id" { type = string }
variable "name" { type = string }
variable "kubernetes_version" { type = string }
variable "vpc_cidr" { type = string }
variable "public_subnets" {
  type = map(object({ cidr = string, zone = string }))
}
variable "private_subnets" {
  type = map(object({ cidr = string, zone = string }))
}
variable "api_access_cidrs" { type = list(string) }
variable "node_instance_type" { type = string }
variable "node_count" { type = number }
variable "node_disk_gib" { type = number }
