output "components" {
  description = "All deployed applications. Each is the module's juju_application object, or { name } where the upstream module exposes only app_name. null when not deployed."
  value = {
    azure_storage_integrator = try(module.azure_storage_integrator[0].application, null)
    data_integrator          = try(module.data_integrator[0].application, null)
    gcs_integrator           = try(module.gcs_integrator[0].application, null)
    opentelemetry_collector  = try({ name = module.opentelemetry_collector[0].app_name }, null)
    s3_integrator            = try(module.s3_integrator[0].application, null)
    self_signed_certificates = try({ name = module.self_signed_certificates[0].app_name }, null)
    valkey                   = module.valkey.application
  }
}

output "credentials" {
  description = "Client connection details. Passwords should be retrieved via 'juju run <data-integrator> get-credentials'."
  value = {
    valkey = {
      app_name            = module.valkey.app_name
      client_port         = 6379
      data_integrator_app = try(module.data_integrator[0].application.name, null)
      sentinel_port       = 26379
      sentinel_tls_port   = 26380
      tls_port            = 6380
    }
  }
}

output "metadata" {
  description = "Metadata of the product deployment."
  value = {
    deployed_at = terraform_data.deployed_at.output
    version     = local.product_version
  }
}

output "models" {
  description = "Map of models and deployed components. Each component is the module's juju_application object, or { name } where the upstream module exposes only app_name."
  value = {
    (var.model.name) = {
      model_uuid = local.model_uuid
      components = merge(
        {
          valkey = module.valkey.application
        },
        var.data_integrator.deploy != null ? {
          data_integrator = module.data_integrator[0].application
        } : {},
        var.tls.deploy != null ? {
          self_signed_certificates = { name = module.self_signed_certificates[0].app_name }
        } : {},
        var.cos.deploy != null ? {
          opentelemetry_collector = { name = module.opentelemetry_collector[0].app_name }
        } : {},
        local.s3_integrator_enabled ? {
          s3_integrator = module.s3_integrator[0].application
        } : {},
        local.azure_integrator_enabled ? {
          azure_storage_integrator = module.azure_storage_integrator[0].application
        } : {},
        local.gcs_integrator_enabled ? {
          gcs_integrator = module.gcs_integrator[0].application
        } : {}
      )
    }
  }
}

output "offers" {
  description = "Map of offers exposed by this product module."
  value = {
    for endpoint, offer in juju_offer.this : replace(endpoint, "-", "_") => offer.url
  }
}

output "provides" {
  description = "Map of all 'provides' endpoints from the product."
  value = {
    cos_agent         = module.valkey.provides["cos_agent"]
    grafana_dashboard = module.valkey.provides["grafana_dashboard"]
    metrics_endpoint  = module.valkey.provides["metrics_endpoint"]
    valkey_client     = module.valkey.provides["valkey_client"]
  }
}

output "requires" {
  description = "Map of all 'requires' endpoints from the product."
  value = {
    azure_credentials    = module.valkey.requires["azure_credentials"]
    certificate_transfer = module.valkey.requires["certificate_transfer"]
    client_certificates  = module.valkey.requires["client_certificates"]
    gcs_credentials      = module.valkey.requires["gcs_credentials"]
    ldap                 = module.valkey.requires["ldap"]
    ldap_ca_cert         = module.valkey.requires["ldap_ca_cert"]
    logging              = module.valkey.requires["logging"]
    s3_credentials       = module.valkey.requires["s3_credentials"]
  }
}
