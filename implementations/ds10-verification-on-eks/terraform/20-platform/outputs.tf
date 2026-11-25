output "bucket" {
  value = aws_s3_bucket.data.id
}
output "code_configmap" {
  value = kubernetes_config_map_v1.code["rewards"].metadata[0].name
}
output "verifier_services" {
  value = { for k, v in local.vname : k => "http://${v}.rewards.svc:8080" }
}
