resource "juju_model" "this" {
  count       = var.model.create ? 1 : 0
  name        = var.model.name
  constraints = var.model.constraints

  config = merge(
    var.logging_config != null ? { "logging-config" = var.logging_config } : {},
    var.proxy != null ? {
      for k, v in {
        "http-proxy"  = var.proxy.http
        "https-proxy" = var.proxy.https
        "no-proxy"    = var.proxy["no-proxy"]
      } : k => v if v != null
    } : {}
  )
}

data "juju_model" "this" {
  count = var.model.create ? 0 : 1
  name  = var.model.name
  owner = var.model.owner
}

resource "terraform_data" "deployed_at" {
  input = timestamp()

  lifecycle {
    ignore_changes = [input]
  }
}

# Secrets using write-only attributes: the version variable controls creation
# (0 -> no secret), rotation (increment), and removal (back to 0). Grants
# follow the secret's length.
resource "juju_secret" "admin_password" {
  count            = var.admin_password_version > 0 ? 1 : 0
  model_uuid       = local.model_uuid
  name             = "${var.valkey.app_name}-admin-password"
  value_wo         = { "charmed-operator" = var.admin_password }
  value_wo_version = var.admin_password_version
  info             = "Admin password for ${var.valkey.app_name}"
}

resource "juju_access_secret" "admin_password" {
  count        = length(juju_secret.admin_password)
  model_uuid   = local.model_uuid
  applications = [module.valkey.application.name]
  secret_id    = juju_secret.admin_password[0].secret_id
  depends_on   = [module.valkey]
}

resource "juju_secret" "tls_client_private_key" {
  count            = var.tls_client_private_key_version > 0 ? 1 : 0
  model_uuid       = local.model_uuid
  name             = "${var.valkey.app_name}-tls-client-private-key"
  value_wo         = { "private-key" = var.tls_client_private_key }
  value_wo_version = var.tls_client_private_key_version
  info             = "TLS client private key for ${var.valkey.app_name}"
}

resource "juju_access_secret" "tls_client_private_key" {
  count        = length(juju_secret.tls_client_private_key)
  model_uuid   = local.model_uuid
  applications = [module.valkey.application.name]
  secret_id    = juju_secret.tls_client_private_key[0].secret_id
  depends_on   = [module.valkey]
}

resource "juju_secret" "s3_secret" {
  count            = local.backup_type == "s3" && var.s3_secret_version > 0 ? 1 : 0
  model_uuid       = local.model_uuid
  name             = "${local.backup_integrator.app_name}-credentials"
  value_wo         = { "access-key" = var.s3_access_key, "secret-key" = var.s3_secret_key }
  value_wo_version = var.s3_secret_version
  info             = "S3 credentials for ${local.backup_integrator.app_name}"
}

resource "juju_access_secret" "s3_secret_access" {
  count        = length(juju_secret.s3_secret)
  model_uuid   = local.model_uuid
  applications = [module.s3_integrator[0].application.name]
  secret_id    = juju_secret.s3_secret[0].secret_id
  depends_on   = [module.s3_integrator]
}

resource "juju_secret" "azure_secret" {
  count            = local.backup_type == "azure" && var.azure_secret_version > 0 ? 1 : 0
  model_uuid       = local.model_uuid
  name             = "${local.backup_integrator.app_name}-credentials"
  value_wo         = { "secret-key" = var.azure_secret_key }
  value_wo_version = var.azure_secret_version
  info             = "Azure credentials for ${local.backup_integrator.app_name}"
}

resource "juju_access_secret" "azure_secret_access" {
  count        = length(juju_secret.azure_secret)
  model_uuid   = local.model_uuid
  applications = [module.azure_storage_integrator[0].application.name]
  secret_id    = juju_secret.azure_secret[0].secret_id
  depends_on   = [module.azure_storage_integrator]
}

resource "juju_secret" "gcs_secret" {
  count            = local.backup_type == "gcs" && var.gcs_secret_version > 0 ? 1 : 0
  model_uuid       = local.model_uuid
  name             = "${local.backup_integrator.app_name}-credentials"
  value_wo         = { "secret-key" = var.gcs_secret_key }
  value_wo_version = var.gcs_secret_version
  info             = "GCS credentials for ${local.backup_integrator.app_name}"
}

resource "juju_access_secret" "gcs_secret_access" {
  count        = length(juju_secret.gcs_secret)
  model_uuid   = local.model_uuid
  applications = [module.gcs_integrator[0].application.name]
  secret_id    = juju_secret.gcs_secret[0].secret_id
  depends_on   = [module.gcs_integrator]
}

