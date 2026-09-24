locals {
  valkey_endpoints = merge(module.valkey.provides, module.valkey.requires)

  # Collector endpoint key => { name, endpoint }, or null when the collector is not deployed.
  collector = one([for m in module.opentelemetry_collector : {
    for k, v in merge(m.provides, m.requires) : k => { name = m.app_name, endpoint = v }
  }])

  # Valkey endpoint key => the bundled application's { name, endpoint }. one() over a module's
  # instances yields null when the module is not deployed, and the filter drops it. A renamed
  # upstream output key fails the plan instead of dropping the integration.
  bundled_integrations = { for k, v in {
    azure_credentials   = one([for m in module.azure_storage_integrator : m.provides["azure_storage_credentials"]])
    client_certificates = one([for m in module.self_signed_certificates : { name = m.app_name, endpoint = m.provides["certificates"] }])
    gcs_credentials     = one([for m in module.gcs_integrator : m.provides["gcs_credentials"]])
    grafana_dashboard   = local.collector == null ? null : local.collector["grafana_dashboards_consumer"]
    logging             = local.collector == null ? null : local.collector["receive_loki_logs"]
    metrics_endpoint    = local.collector == null ? null : local.collector["metrics_endpoint"]
    s3_credentials      = one([for m in module.s3_integrator : m.provides["s3_credentials"]])
    valkey_client       = one([for m in module.data_integrator : m.requires["valkey"]])
  } : k => { name = v.name, endpoint = v.endpoint } if v != null }

  # Integrations with applications this module does not deploy. app is the side this module owns.
  # target is the { kind, name, endpoint, url, controller } object from the input variables.
  external_integrations = { for k, v in {
    azure_credentials    = { app = local.valkey_endpoints["azure_credentials"], target = var.backup.azure_credentials }
    certificate_transfer = { app = local.valkey_endpoints["certificate_transfer"], target = var.certificate_transfer }
    client_certificates  = { app = local.valkey_endpoints["client_certificates"], target = var.tls.client_certificates }
    collector_grafana    = { app = local.collector == null ? null : local.collector["grafana_dashboards_provider"], target = var.cos.grafana }
    collector_loki       = { app = local.collector == null ? null : local.collector["send_loki_logs"], target = var.cos.loki }
    collector_prometheus = { app = local.collector == null ? null : local.collector["send_remote_write"], target = var.cos.prometheus }
    gcs_credentials      = { app = local.valkey_endpoints["gcs_credentials"], target = var.backup.gcs_credentials }
    grafana_dashboard    = { app = local.valkey_endpoints["grafana_dashboard"], target = var.cos.grafana_dashboard }
    ldap                 = { app = local.valkey_endpoints["ldap"], target = var.ldap.ldap }
    ldap_ca_cert         = { app = local.valkey_endpoints["ldap_ca_cert"], target = var.ldap.ldap_ca_cert }
    logging              = { app = local.valkey_endpoints["logging"], target = var.cos.logging }
    metrics_endpoint     = { app = local.valkey_endpoints["metrics_endpoint"], target = var.cos.metrics_endpoint }
    s3_credentials       = { app = local.valkey_endpoints["s3_credentials"], target = var.backup.s3_credentials }
  } : k => { app = { name = v.app.name, endpoint = v.app.endpoint }, target = v.target } if v.target != null }
}

resource "juju_integration" "bundled" {
  for_each   = local.bundled_integrations
  model_uuid = local.model_uuid

  application {
    name     = local.valkey_endpoints[each.key].name
    endpoint = local.valkey_endpoints[each.key].endpoint
  }

  application {
    name     = each.value.name
    endpoint = each.value.endpoint
  }
}

resource "juju_integration" "external" {
  for_each   = local.external_integrations
  model_uuid = local.model_uuid

  application {
    name     = each.value.app.name
    endpoint = each.value.app.endpoint
  }

  application {
    name                = each.value.target.kind == "endpoint" ? each.value.target.name : null
    endpoint            = each.value.target.kind == "endpoint" ? each.value.target.endpoint : null
    offer_url           = each.value.target.kind == "offer" ? each.value.target.url : null
    offering_controller = each.value.target.kind == "offer" ? each.value.target.controller : null
  }

  lifecycle {
    # length() fails on null, and try() turns that into false.
    precondition {
      condition = (
        each.value.target.kind == "endpoint" ? try(length(each.value.target.name) > 0 && length(each.value.target.endpoint) > 0, false) :
        each.value.target.kind == "offer" ? try(length(each.value.target.url) > 0, false) : false
      )
      error_message = "${each.key}: set kind = \"endpoint\" with name and endpoint, or kind = \"offer\" with url."
    }
  }
}
