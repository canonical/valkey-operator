module "valkey" {
  source = "../../charm"

  model_uuid = local.model_uuid

  app_name = var.valkey.app_name
  base     = var.valkey.base
  channel  = var.valkey.channel != null ? var.valkey.channel : "9/${var.risk}"
  config = merge(
    var.valkey.config,
    length(juju_secret.admin_password) > 0 ? {
      "system-users" = juju_secret.admin_password[0].secret_uri
    } : {},
    length(juju_secret.tls_client_private_key) > 0 ? {
      "tls-client-private-key" = juju_secret.tls_client_private_key[0].secret_uri
    } : {}
  )
  constraints        = var.valkey.constraints
  endpoint_bindings  = var.valkey.endpoint_bindings
  expose             = var.valkey.expose
  resources          = var.valkey.resources
  revision           = var.valkey.revision
  storage_directives = var.valkey.storage_directives
  trust              = true
  units              = var.valkey.units
}

module "self_signed_certificates" {
  count  = var.tls.deploy != null ? 1 : 0
  source = "git::https://github.com/canonical/self-signed-certificates-operator//terraform?ref=baf7355a536d454871b96e7afafcd166bbc029f2"

  app_name    = var.tls.deploy.app_name
  base        = var.tls.deploy.base
  channel     = var.tls.deploy.channel != null ? var.tls.deploy.channel : "1/${var.risk}"
  config      = var.tls.deploy.config
  constraints = var.tls.deploy.constraints
  model_uuid  = local.model_uuid
  revision    = var.tls.deploy.revision
}

module "data_integrator" {
  count  = var.data_integrator.deploy != null ? 1 : 0
  source = "git::https://github.com/canonical/data-integrator//terraform/charm/data_integrator?ref=c6582254d6585e119657c66a3797621ec7567019"

  app_name    = var.data_integrator.deploy.app_name
  base        = var.data_integrator.deploy.base
  channel     = var.data_integrator.deploy.channel != null ? var.data_integrator.deploy.channel : "latest/${var.risk}"
  constraints = var.data_integrator.deploy.constraints
  model_uuid  = local.model_uuid
  revision    = var.data_integrator.deploy.revision

  config = merge(
    var.data_integrator.deploy.config,
    {
      prefix-name = var.data_integrator.deploy.prefix_name
    }
  )
}

module "opentelemetry_collector" {
  count  = var.cos.deploy != null ? 1 : 0
  source = "git::https://github.com/canonical/opentelemetry-collector-k8s-operator//terraform?ref=tf-0.130.4"

  app_name    = var.cos.deploy.app_name
  base        = var.cos.deploy.base
  channel     = var.cos.deploy.channel != null ? var.cos.deploy.channel : "0.130/${var.risk}"
  config      = var.cos.deploy.config
  constraints = var.cos.deploy.constraints
  model_uuid  = local.model_uuid
  resources   = var.cos.deploy.resources != null ? var.cos.deploy.resources : {}
  revision    = var.cos.deploy.revision

  storage_directives = var.cos.deploy.storage_directives
}

module "s3_integrator" {
  count  = local.s3_integrator_enabled ? 1 : 0
  source = "git::https://github.com/canonical/object-storage-integrator//s3/terraform/charm/s3_integrator?ref=85fef8977e0a6d146af5c418ec463aefb6bdbddb"

  app_name    = local.backups_integrator_app_name
  base        = local.backups_integrator_base
  channel     = local.backups_integrator_channel
  constraints = var.backup.deploy.constraints
  model_uuid  = local.model_uuid
  revision    = var.backup.deploy.revision

  config = merge(
    var.backup.deploy.config,
    length(juju_secret.s3_secret) > 0 ? {
      credentials = juju_secret.s3_secret[0].secret_uri
    } : {}
  )
}

module "azure_storage_integrator" {
  count  = local.azure_integrator_enabled ? 1 : 0
  source = "git::https://github.com/canonical/object-storage-integrator//azure_storage/terraform/charm/azure_storage_integrator?ref=85fef8977e0a6d146af5c418ec463aefb6bdbddb"

  app_name    = local.backups_integrator_app_name
  base        = local.backups_integrator_base
  channel     = local.backups_integrator_channel
  constraints = var.backup.deploy.constraints
  model_uuid  = local.model_uuid
  revision    = var.backup.deploy.revision

  config = merge(
    var.backup.deploy.config,
    length(juju_secret.azure_secret) > 0 ? {
      credentials = juju_secret.azure_secret[0].secret_uri
    } : {}
  )
}

module "gcs_integrator" {
  count  = local.gcs_integrator_enabled ? 1 : 0
  source = "git::https://github.com/canonical/object-storage-integrator//gcs/terraform/charm/gcs_integrator?ref=85fef8977e0a6d146af5c418ec463aefb6bdbddb"

  app_name    = local.backups_integrator_app_name
  base        = local.backups_integrator_base
  channel     = local.backups_integrator_channel
  constraints = var.backup.deploy.constraints
  model_uuid  = local.model_uuid
  revision    = var.backup.deploy.revision

  config = merge(
    var.backup.deploy.config,
    length(juju_secret.gcs_secret) > 0 ? {
      credentials = juju_secret.gcs_secret[0].secret_uri
    } : {}
  )
}
