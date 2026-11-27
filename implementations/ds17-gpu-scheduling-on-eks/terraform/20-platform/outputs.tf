output "code_configmap" {
  value = kubernetes_config_map_v1.code.metadata[0].name
}
