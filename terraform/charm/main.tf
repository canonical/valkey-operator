resource "juju_application" "valkey" {
  name               = var.app_name
  model_uuid         = var.model_uuid
  units              = var.machines != null && length(var.machines) > 0 ? null : var.units
  config             = var.config
  constraints        = var.constraints
  endpoint_bindings  = var.endpoint_bindings
  machines           = var.machines != null && length(var.machines) > 0 ? var.machines : null
  resources          = var.resources
  storage_directives = var.storage_directives
  trust              = var.trust

  charm {
    name     = local.charm_name
    channel  = var.channel
    revision = var.revision
    base     = var.base
  }

  dynamic "expose" {
    for_each = var.expose
    content {
      cidrs     = expose.value.cidrs
      endpoints = expose.value.endpoints
      spaces    = expose.value.spaces
    }
  }
}

resource "juju_offer" "valkey" {
  for_each         = var.offered_endpoints
  model_uuid       = var.model_uuid
  application_name = juju_application.valkey.name
  endpoints        = [each.value]
}
