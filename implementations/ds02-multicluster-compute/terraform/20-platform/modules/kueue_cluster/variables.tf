variable "cluster_name" { type = string }
variable "role" {
  description = "manager (the hub) or worker. Decides which extras the cluster gets."
  type        = string
}
variable "kueue_version" { type = string }
variable "teams" {
  description = "Team namespaces the platform creates in this cluster."
  type        = map(object({ cpu_pods = number }))
}
variable "flavors" {
  description = "ResourceFlavors: name => spec"
  type        = any
  default     = {}
}
variable "cluster_queues" {
  description = "ClusterQueues: name => spec"
  type        = any
  default     = {}
}
variable "local_queues" {
  description = "LocalQueues: 'team/name' => clusterQueue"
  type        = map(string)
  default     = {}
}
variable "node_pools" {
  description = "Auto Mode NodePools: name => spec"
  type        = any
  default     = {}
}
variable "priority_classes" {
  type = map(number)
}
variable "create_multikueue_sa" {
  description = "Create the narrowly scoped identity the hub uses to dispatch jobs here."
  type        = bool
  default     = false
}
