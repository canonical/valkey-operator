output "app_name" {
  description = "Name of the deployed application."
  value       = juju_application.valkey.name
}

output "application" {
  description = "Object representing the deployed application."
  value       = juju_application.valkey
}

output "offers" {
  description = "Map of all offers exposed by the charm."
  value = {
    for endpoint, offer in juju_offer.valkey : replace(endpoint, "-", "_") => {
      kind = "offer"
      url  = offer.url
    }
  }
}

output "provides" {
  description = "Provides endpoints."
  value = {
    cos_agent = {
      endpoint = "cos-agent"
      kind     = "endpoint"
      name     = juju_application.valkey.name
    }
    grafana_dashboard = {
      endpoint = "grafana-dashboard"
      kind     = "endpoint"
      name     = juju_application.valkey.name
    }
    metrics_endpoint = {
      endpoint = "metrics-endpoint"
      kind     = "endpoint"
      name     = juju_application.valkey.name
    }
    valkey_client = {
      endpoint = "valkey-client"
      kind     = "endpoint"
      name     = juju_application.valkey.name
    }
  }
}

output "requires" {
  description = "Requires endpoints."
  value = {
    azure_credentials = {
      endpoint = "azure-credentials"
      kind     = "endpoint"
      name     = juju_application.valkey.name
    }
    certificate_transfer = {
      endpoint = "certificate-transfer"
      kind     = "endpoint"
      name     = juju_application.valkey.name
    }
    client_certificates = {
      endpoint = "client-certificates"
      kind     = "endpoint"
      name     = juju_application.valkey.name
    }
    gcs_credentials = {
      endpoint = "gcs-credentials"
      kind     = "endpoint"
      name     = juju_application.valkey.name
    }
    ldap = {
      endpoint = "ldap"
      kind     = "endpoint"
      name     = juju_application.valkey.name
    }
    ldap_ca_cert = {
      endpoint = "ldap-ca-cert"
      kind     = "endpoint"
      name     = juju_application.valkey.name
    }
    logging = {
      endpoint = "logging"
      kind     = "endpoint"
      name     = juju_application.valkey.name
    }
    s3_credentials = {
      endpoint = "s3-credentials"
      kind     = "endpoint"
      name     = juju_application.valkey.name
    }
  }
}
