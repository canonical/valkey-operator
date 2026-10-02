output "components" {
  description = "All deployed applications, as juju_application objects. null when not deployed."
  value       = local.components
}

output "credentials" {
  description = "Connection details for Valkey clients. It holds no password. Run the data-integrator get-credentials action for one."
  value = {
    valkey = {
      app_name            = module.valkey.app_name
      client_port         = 6379
      data_integrator_app = one(module.data_integrator[*].application.name)
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
  description = "Map of models and deployed components. Each component is its juju_application object."
  value = {
    (var.model.name) = {
      model_uuid = local.model_uuid
      components = { for k, v in local.components : k => v if v != null }
    }
  }
}

output "offers" {
  description = "Map of offers exposed by this product module, keyed like provides. Each value is { kind = \"offer\", url }."
  value       = module.valkey.offers
}

output "provides" {
  description = "Valkey provides endpoints."
  value       = module.valkey.provides
}

output "requires" {
  description = "Valkey requires endpoints."
  value       = module.valkey.requires
}
