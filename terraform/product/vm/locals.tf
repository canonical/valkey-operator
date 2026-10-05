locals {
  # Config keys the pinned integrator modules accept. Their config is a closed object that drops
  # unknown keys. Refresh these lists when the module refs are bumped. credentials is left out
  # because the module sets it from the *_secret_version secret.
  backup_config_allowed_keys = {
    azure = [
      "connection-protocol",
      "container",
      "endpoint",
      "path",
      "resource-group",
      "storage-account",
    ]
    gcs = ["bucket", "path", "storage-class"]
    s3 = [
      "attributes",
      "bucket",
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

  # The bundled backup integrator's app_name, base and channel: backup.deploy overrides the
  # per-type default. null when backup.deploy is unset.
  backup_integrator = local.backup_type == null ? null : {
    for k, v in local.backup_integrator_defaults[local.backup_type] : k => coalesce(var.backup.deploy[k], v)
  }

  backup_integrator_defaults = {
    azure = { app_name = "azure-storage-integrator", base = "ubuntu@22.04", channel = "1/${var.risk}" }
    gcs   = { app_name = "gcs-integrator", base = "ubuntu@24.04", channel = "1/${var.risk}" }
    s3    = { app_name = "s3-integrator", base = "ubuntu@24.04", channel = "2/${var.risk}" }
  }

  # backup.deploy.storage_type (s3, azure or gcs), or null when no integrator is bundled.
  backup_type = try(var.backup.deploy.storage_type, null)

  # Every component this module can deploy, as its juju_application object. null when not
  # deployed. one() keeps known fields known at plan, where try() would turn the whole object
  # unknown.
  components = {
    azure_storage_integrator = one(module.azure_storage_integrator[*].application)
    data_integrator          = one(module.data_integrator[*].application)
    gcs_integrator           = one(module.gcs_integrator[*].application)
    opentelemetry_collector  = one(juju_application.opentelemetry_collector[*])
    s3_integrator            = one(module.s3_integrator[*].application)
    valkey                   = module.valkey.application
  }

  # Valkey only uses data-integrator's prefix-name and entity-permissions
  data_integrator_config_allowed_keys = [
    "entity-permissions",
    "prefix-name",
  ]

  model_uuid      = var.model.create ? juju_model.this[0].uuid : data.juju_model.this[0].uuid
  product_version = "1.0.0"
}
