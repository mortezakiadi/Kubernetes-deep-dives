# Secrets and certificates. Each Secret lives only in the namespace that needs it, and each pod
# mounts only the keys it needs. The sandboxes namespace gets no Secret at all.
# Values are generated here and held in Terraform state (local, git-ignored).

resource "random_password" "orchestrator_token" {
  length  = 48
  special = false
}

resource "random_password" "session_key" {
  length  = 48
  special = false
}

resource "random_password" "broker_key" {
  length  = 48
  special = false
}

resource "random_password" "egress_gateway_token" {
  length  = 48
  special = false
}

resource "random_password" "tool_gateway_token" {
  length  = 48
  special = false
}

resource "random_password" "sandboxd_token" {
  length  = 48
  special = false
}

# ---- certificate authorities -------------------------------------------------
# 1. The internal CA signs the metrics API's server certificate. The egress gateway and the
#    tool gateway trust it.
resource "tls_private_key" "internal_ca" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "internal_ca" {
  private_key_pem       = tls_private_key.internal_ca.private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 168
  allowed_uses          = ["cert_signing", "crl_signing"]
  subject {
    common_name  = "ds01 internal CA"
    organization = "design-01-sandboxing"
  }
}

resource "tls_private_key" "metrics_api" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "metrics_api" {
  private_key_pem = tls_private_key.metrics_api.private_key_pem
  dns_names       = ["metrics-api.sandbox-platform.svc", "metrics-api.sandbox-platform.svc.cluster.local"]
  subject {
    common_name = "metrics-api.sandbox-platform.svc"
  }
}

resource "tls_locally_signed_cert" "metrics_api" {
  cert_request_pem      = tls_cert_request.metrics_api.cert_request_pem
  ca_private_key_pem    = tls_private_key.internal_ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.internal_ca.cert_pem
  validity_period_hours = 168
  allowed_uses          = ["digital_signature", "key_encipherment", "server_auth"]
}

# 2. The gateway CA lets the egress gateway terminate TLS for the one host where it injects a
#    credential. Sandboxes trust its certificate (public) and never see its key.
resource "tls_private_key" "gateway_ca" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "gateway_ca" {
  private_key_pem       = tls_private_key.gateway_ca.private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 168
  allowed_uses          = ["cert_signing", "crl_signing"]
  subject {
    common_name  = "ds01 egress gateway CA"
    organization = "design-01-sandboxing"
  }
}

# ---- Kubernetes Secrets and ConfigMaps ------------------------------------------------
resource "kubernetes_secret_v1" "egress_secrets" {
  metadata {
    name      = "egress-secrets"
    namespace = "sandbox-egress"
  }
  data = {
    "egress-gateway-token" = random_password.egress_gateway_token.result
    "tool-gateway-token"   = random_password.tool_gateway_token.result
    "broker-key"           = random_password.broker_key.result
  }
  depends_on = [time_sleep.namespaces]
}

resource "kubernetes_secret_v1" "platform_secrets" {
  metadata {
    name      = "platform-secrets"
    namespace = "sandbox-platform"
  }
  data = {
    "orchestrator-token"   = random_password.orchestrator_token.result
    "session-key"          = random_password.session_key.result
    "broker-key"           = random_password.broker_key.result
    "egress-gateway-token" = random_password.egress_gateway_token.result
    "tool-gateway-token"   = random_password.tool_gateway_token.result
  }
  depends_on = [time_sleep.namespaces]
}

resource "kubernetes_secret_v1" "router_secrets" {
  metadata {
    name      = "router-secrets"
    namespace = "sandbox-router"
  }
  data = {
    "orchestrator-token" = random_password.orchestrator_token.result
    "sandboxd-token"     = random_password.sandboxd_token.result
  }
  depends_on = [time_sleep.namespaces]
}

resource "kubernetes_secret_v1" "orchestrator_secrets" {
  metadata {
    name      = "orchestrator-secrets"
    namespace = "agent-orchestrator"
  }
  data = {
    "orchestrator-token" = random_password.orchestrator_token.result
  }
  depends_on = [time_sleep.namespaces]
}

resource "kubernetes_secret_v1" "gateway_ca" {
  metadata {
    name      = "gateway-ca"
    namespace = "sandbox-egress"
  }
  data = {
    "mitmproxy-ca.pem"      = "${tls_private_key.gateway_ca.private_key_pem}${tls_self_signed_cert.gateway_ca.cert_pem}"
    "mitmproxy-ca-cert.pem" = tls_self_signed_cert.gateway_ca.cert_pem
  }
  depends_on = [time_sleep.namespaces]
}

resource "kubernetes_secret_v1" "metrics_api_tls" {
  metadata {
    name      = "metrics-api-tls"
    namespace = "sandbox-platform"
  }
  type = "kubernetes.io/tls"
  data = {
    "tls.crt" = tls_locally_signed_cert.metrics_api.cert_pem
    "tls.key" = tls_private_key.metrics_api.private_key_pem
  }
  depends_on = [time_sleep.namespaces]
}

resource "kubernetes_config_map_v1" "internal_ca" {
  for_each = toset(["sandbox-egress", "sandbox-platform"])
  metadata {
    name      = "internal-ca"
    namespace = each.key
  }
  data       = { "ca.crt" = tls_self_signed_cert.internal_ca.cert_pem }
  depends_on = [time_sleep.namespaces]
}

# Public certificate only. A certificate is not a secret; this is what sandboxes trust.
resource "kubernetes_config_map_v1" "gateway_ca_cert" {
  metadata {
    name      = "gateway-ca-cert"
    namespace = "sandboxes"
  }
  data       = { "gateway-ca.crt" = tls_self_signed_cert.gateway_ca.cert_pem }
  depends_on = [time_sleep.namespaces]
}
