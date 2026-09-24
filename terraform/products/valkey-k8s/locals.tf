locals {
  azure_integrator_enabled = var.backup.deploy != null && try(var.backup.deploy.storage_type, "") == "azure"

  # Config keys the pinned integrator modules accept. Their config is a closed object that drops
  # unknown keys. Refresh these lists when the module refs are bumped.
  backup_config_allowed_keys = {
    azure = [
      "connection-protocol",
      "container",
      "credentials",
      "endpoint",
      "path",
      "resource-group",
      "storage-account",
    ]
    gcs = ["bucket", "credentials", "path", "storage-class"]
    s3 = [
      "attributes",
      "bucket",
      "credentials",
      "endpoint",
      "experimental-delete-older-than-days",
      "path",
      "region",
      "s3-api-version",
      "s3-uri-style",
      "storage-class",
      "tls-ca-chain",
    ]
  }

  backups_integrator_app_name = var.backup.deploy != null ? coalesce(
    var.backup.deploy.app_name,
    local.s3_integrator_enabled ? "s3-integrator" :
    local.azure_integrator_enabled ? "azure-storage-integrator" : "gcs-integrator"
  ) : null

  backups_integrator_base = var.backup.deploy != null ? coalesce(
    var.backup.deploy.base,
    local.azure_integrator_enabled ? "ubuntu@22.04" : "ubuntu@24.04"
  ) : null

  backups_integrator_channel = var.backup.deploy != null ? coalesce(
    var.backup.deploy.channel,
    local.s3_integrator_enabled ? "2/${var.risk}" : "1/${var.risk}"
  ) : null

  gcs_integrator_enabled = var.backup.deploy != null && try(var.backup.deploy.storage_type, "") == "gcs"
  model_uuid             = var.model.create ? juju_model.this[0].uuid : data.juju_model.this[0].uuid
  product_version        = "1.0.0"
  s3_integrator_enabled  = var.backup.deploy != null && try(var.backup.deploy.storage_type, "s3") == "s3"
}
