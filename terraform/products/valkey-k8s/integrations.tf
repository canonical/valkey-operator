# Data Integrator integration
resource "juju_integration" "data_integrator" {
  count      = var.data_integrator.deploy != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.provides["valkey_client"].name
    endpoint = module.valkey.provides["valkey_client"].endpoint
  }

  application {
    name     = module.data_integrator[0].requires["valkey"].name
    endpoint = module.data_integrator[0].requires["valkey"].endpoint
  }
}

# Client TLS integrations
resource "juju_integration" "bundled_client_tls" {
  count      = var.tls.deploy != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["client_certificates"].name
    endpoint = module.valkey.requires["client_certificates"].endpoint
  }

  application {
    name     = module.self_signed_certificates[0].app_name
    endpoint = module.self_signed_certificates[0].provides["certificates"]
  }
}

resource "juju_integration" "external_client_tls" {
  count      = var.tls.client_certificates != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["client_certificates"].name
    endpoint = module.valkey.requires["client_certificates"].endpoint
  }

  application {
    name                = var.tls.client_certificates.kind == "endpoint" ? var.tls.client_certificates.name : null
    endpoint            = var.tls.client_certificates.kind == "endpoint" ? var.tls.client_certificates.endpoint : null
    offer_url           = var.tls.client_certificates.kind == "offer" ? var.tls.client_certificates.url : null
    offering_controller = var.tls.client_certificates.kind == "offer" ? var.tls.client_certificates.controller : null
  }
}

# Bundled COS integrations (opentelemetry-collector-k8s)
resource "juju_integration" "bundled_cos_metrics" {
  count      = var.cos.deploy != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.provides["metrics_endpoint"].name
    endpoint = module.valkey.provides["metrics_endpoint"].endpoint
  }

  application {
    name     = module.opentelemetry_collector[0].app_name
    endpoint = module.opentelemetry_collector[0].requires["metrics_endpoint"]
  }
}

resource "juju_integration" "bundled_cos_logging" {
  count      = var.cos.deploy != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["logging"].name
    endpoint = module.valkey.requires["logging"].endpoint
  }

  application {
    name     = module.opentelemetry_collector[0].app_name
    endpoint = module.opentelemetry_collector[0].provides["receive_loki_logs"]
  }
}

resource "juju_integration" "bundled_cos_dashboard" {
  count      = var.cos.deploy != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.provides["grafana_dashboard"].name
    endpoint = module.valkey.provides["grafana_dashboard"].endpoint
  }

  application {
    name     = module.opentelemetry_collector[0].app_name
    endpoint = module.opentelemetry_collector[0].requires["grafana_dashboards_consumer"]
  }
}

# Outbound COS integrations from the bundled collector
resource "juju_integration" "collector_prometheus" {
  count      = var.cos.deploy != null && var.cos.prometheus != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.opentelemetry_collector[0].app_name
    endpoint = module.opentelemetry_collector[0].requires["send_remote_write"]
  }

  application {
    name                = var.cos.prometheus.kind == "endpoint" ? var.cos.prometheus.name : null
    endpoint            = var.cos.prometheus.kind == "endpoint" ? var.cos.prometheus.endpoint : null
    offer_url           = var.cos.prometheus.kind == "offer" ? var.cos.prometheus.url : null
    offering_controller = var.cos.prometheus.kind == "offer" ? var.cos.prometheus.controller : null
  }
}

resource "juju_integration" "collector_loki" {
  count      = var.cos.deploy != null && var.cos.loki != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.opentelemetry_collector[0].app_name
    endpoint = module.opentelemetry_collector[0].requires["send_loki_logs"]
  }

  application {
    name                = var.cos.loki.kind == "endpoint" ? var.cos.loki.name : null
    endpoint            = var.cos.loki.kind == "endpoint" ? var.cos.loki.endpoint : null
    offer_url           = var.cos.loki.kind == "offer" ? var.cos.loki.url : null
    offering_controller = var.cos.loki.kind == "offer" ? var.cos.loki.controller : null
  }
}

resource "juju_integration" "collector_grafana" {
  count      = var.cos.deploy != null && var.cos.grafana != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.opentelemetry_collector[0].app_name
    endpoint = module.opentelemetry_collector[0].provides["grafana_dashboards_provider"]
  }

  application {
    name                = var.cos.grafana.kind == "endpoint" ? var.cos.grafana.name : null
    endpoint            = var.cos.grafana.kind == "endpoint" ? var.cos.grafana.endpoint : null
    offer_url           = var.cos.grafana.kind == "offer" ? var.cos.grafana.url : null
    offering_controller = var.cos.grafana.kind == "offer" ? var.cos.grafana.controller : null
  }
}

# External COS integrations
resource "juju_integration" "external_cos_metrics" {
  count      = var.cos.metrics_endpoint != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.provides["metrics_endpoint"].name
    endpoint = module.valkey.provides["metrics_endpoint"].endpoint
  }

  application {
    name                = var.cos.metrics_endpoint.kind == "endpoint" ? var.cos.metrics_endpoint.name : null
    endpoint            = var.cos.metrics_endpoint.kind == "endpoint" ? var.cos.metrics_endpoint.endpoint : null
    offer_url           = var.cos.metrics_endpoint.kind == "offer" ? var.cos.metrics_endpoint.url : null
    offering_controller = var.cos.metrics_endpoint.kind == "offer" ? var.cos.metrics_endpoint.controller : null
  }
}

resource "juju_integration" "external_cos_logging" {
  count      = var.cos.logging != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["logging"].name
    endpoint = module.valkey.requires["logging"].endpoint
  }

  application {
    name                = var.cos.logging.kind == "endpoint" ? var.cos.logging.name : null
    endpoint            = var.cos.logging.kind == "endpoint" ? var.cos.logging.endpoint : null
    offer_url           = var.cos.logging.kind == "offer" ? var.cos.logging.url : null
    offering_controller = var.cos.logging.kind == "offer" ? var.cos.logging.controller : null
  }
}

resource "juju_integration" "external_cos_dashboard" {
  count      = var.cos.grafana_dashboard != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.provides["grafana_dashboard"].name
    endpoint = module.valkey.provides["grafana_dashboard"].endpoint
  }

  application {
    name                = var.cos.grafana_dashboard.kind == "endpoint" ? var.cos.grafana_dashboard.name : null
    endpoint            = var.cos.grafana_dashboard.kind == "endpoint" ? var.cos.grafana_dashboard.endpoint : null
    offer_url           = var.cos.grafana_dashboard.kind == "offer" ? var.cos.grafana_dashboard.url : null
    offering_controller = var.cos.grafana_dashboard.kind == "offer" ? var.cos.grafana_dashboard.controller : null
  }
}

# Bundled backup integrations
resource "juju_integration" "bundled_azure_credentials" {
  count      = local.azure_integrator_enabled ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["azure_credentials"].name
    endpoint = module.valkey.requires["azure_credentials"].endpoint
  }

  application {
    name     = module.azure_storage_integrator[0].provides["azure_storage_credentials"].name
    endpoint = module.azure_storage_integrator[0].provides["azure_storage_credentials"].endpoint
  }
}

resource "juju_integration" "bundled_gcs_credentials" {
  count      = local.gcs_integrator_enabled ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["gcs_credentials"].name
    endpoint = module.valkey.requires["gcs_credentials"].endpoint
  }

  application {
    name     = module.gcs_integrator[0].provides["gcs_credentials"].name
    endpoint = module.gcs_integrator[0].provides["gcs_credentials"].endpoint
  }
}

resource "juju_integration" "bundled_s3_credentials" {
  count      = local.s3_integrator_enabled ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["s3_credentials"].name
    endpoint = module.valkey.requires["s3_credentials"].endpoint
  }

  application {
    name     = module.s3_integrator[0].provides["s3_credentials"].name
    endpoint = module.s3_integrator[0].provides["s3_credentials"].endpoint
  }
}

# External backup integrations
resource "juju_integration" "azure_credentials" {
  count      = var.backup.azure_credentials != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["azure_credentials"].name
    endpoint = module.valkey.requires["azure_credentials"].endpoint
  }

  application {
    name                = var.backup.azure_credentials.kind == "endpoint" ? var.backup.azure_credentials.name : null
    endpoint            = var.backup.azure_credentials.kind == "endpoint" ? var.backup.azure_credentials.endpoint : null
    offer_url           = var.backup.azure_credentials.kind == "offer" ? var.backup.azure_credentials.url : null
    offering_controller = var.backup.azure_credentials.kind == "offer" ? var.backup.azure_credentials.controller : null
  }
}

resource "juju_integration" "gcs_credentials" {
  count      = var.backup.gcs_credentials != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["gcs_credentials"].name
    endpoint = module.valkey.requires["gcs_credentials"].endpoint
  }

  application {
    name                = var.backup.gcs_credentials.kind == "endpoint" ? var.backup.gcs_credentials.name : null
    endpoint            = var.backup.gcs_credentials.kind == "endpoint" ? var.backup.gcs_credentials.endpoint : null
    offer_url           = var.backup.gcs_credentials.kind == "offer" ? var.backup.gcs_credentials.url : null
    offering_controller = var.backup.gcs_credentials.kind == "offer" ? var.backup.gcs_credentials.controller : null
  }
}

resource "juju_integration" "s3_credentials" {
  count      = var.backup.s3_credentials != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["s3_credentials"].name
    endpoint = module.valkey.requires["s3_credentials"].endpoint
  }

  application {
    name                = var.backup.s3_credentials.kind == "endpoint" ? var.backup.s3_credentials.name : null
    endpoint            = var.backup.s3_credentials.kind == "endpoint" ? var.backup.s3_credentials.endpoint : null
    offer_url           = var.backup.s3_credentials.kind == "offer" ? var.backup.s3_credentials.url : null
    offering_controller = var.backup.s3_credentials.kind == "offer" ? var.backup.s3_credentials.controller : null
  }
}

# LDAP integrations
resource "juju_integration" "ldap" {
  count      = var.ldap.ldap != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["ldap"].name
    endpoint = module.valkey.requires["ldap"].endpoint
  }

  application {
    name                = var.ldap.ldap.kind == "endpoint" ? var.ldap.ldap.name : null
    endpoint            = var.ldap.ldap.kind == "endpoint" ? var.ldap.ldap.endpoint : null
    offer_url           = var.ldap.ldap.kind == "offer" ? var.ldap.ldap.url : null
    offering_controller = var.ldap.ldap.kind == "offer" ? var.ldap.ldap.controller : null
  }
}

resource "juju_integration" "ldap_ca_cert" {
  count      = var.ldap.ldap_ca_cert != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["ldap_ca_cert"].name
    endpoint = module.valkey.requires["ldap_ca_cert"].endpoint
  }

  application {
    name                = var.ldap.ldap_ca_cert.kind == "endpoint" ? var.ldap.ldap_ca_cert.name : null
    endpoint            = var.ldap.ldap_ca_cert.kind == "endpoint" ? var.ldap.ldap_ca_cert.endpoint : null
    offer_url           = var.ldap.ldap_ca_cert.kind == "offer" ? var.ldap.ldap_ca_cert.url : null
    offering_controller = var.ldap.ldap_ca_cert.kind == "offer" ? var.ldap.ldap_ca_cert.controller : null
  }
}

# Certificate Transfer integration
resource "juju_integration" "certificate_transfer" {
  count      = var.certificate_transfer.certificate_transfer != null ? 1 : 0
  model_uuid = local.model_uuid

  application {
    name     = module.valkey.requires["certificate_transfer"].name
    endpoint = module.valkey.requires["certificate_transfer"].endpoint
  }

  application {
    name                = var.certificate_transfer.certificate_transfer.kind == "endpoint" ? var.certificate_transfer.certificate_transfer.name : null
    endpoint            = var.certificate_transfer.certificate_transfer.kind == "endpoint" ? var.certificate_transfer.certificate_transfer.endpoint : null
    offer_url           = var.certificate_transfer.certificate_transfer.kind == "offer" ? var.certificate_transfer.certificate_transfer.url : null
    offering_controller = var.certificate_transfer.certificate_transfer.kind == "offer" ? var.certificate_transfer.certificate_transfer.controller : null
  }
}
