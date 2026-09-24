variable "admin_password" {
  description = "Admin password for the internal charmed-operator user. Supply through TF_VAR_admin_password or -var, at plan and at apply."
  type        = string
  sensitive   = true
  ephemeral   = true
  default     = null
}

variable "admin_password_version" {
  description = "0 creates no secret. 1 creates it. Increment to rotate the admin password."
  type        = number
  default     = 0
}

variable "azure_secret_key" {
  description = "Azure Storage Account key or connection string for azure-storage-integrator. Supply through TF_VAR_azure_secret_key or -var, at plan and at apply."
  type        = string
  sensitive   = true
  ephemeral   = true
  default     = null
}

variable "azure_secret_version" {
  description = "0 creates no secret. 1 creates it. Increment to rotate the Azure credentials."
  type        = number
  default     = 0
}

variable "backup" {
  description = "Remote storage backup configuration. Deploys a bundled integrator under 'deploy' (s3, azure, or gcs) or consumes an existing integrator via s3_credentials, azure_credentials, or gcs_credentials."
  type = object({
    # Bundled integrator: s3-integrator, azure-storage-integrator, or gcs-integrator. null = do not deploy.
    deploy = optional(object({
      app_name     = optional(string)
      base         = optional(string)
      channel      = optional(string)
      constraints  = optional(string) # null: follow the model constraints (set model.constraints = "arch=arm64" on arm64)
      config       = optional(map(string), {})
      revision     = optional(number)
      storage_type = optional(string, "s3")
    }))

    # Consumed existing integrators. null = not integrated.
    azure_credentials = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
    gcs_credentials = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
    s3_credentials = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
  })
  default = {}

  validation {
    condition     = var.backup.deploy == null || contains(["s3", "azure", "gcs"], try(var.backup.deploy.storage_type, ""))
    error_message = "backup.deploy.storage_type must be one of: 's3', 'azure', 'gcs'."
  }

  validation {
    condition = !(
      var.backup.deploy != null && (
        var.backup.s3_credentials != null ||
        var.backup.azure_credentials != null ||
        var.backup.gcs_credentials != null
      )
    )
    error_message = "backup.deploy and consumed credentials relations (s3_credentials, azure_credentials, gcs_credentials) are mutually exclusive. Set backup.deploy = null to consume an existing integrator."
  }

  validation {
    condition = alltrue([
      for target in [var.backup.azure_credentials, var.backup.gcs_credentials, var.backup.s3_credentials] :
      target == null || contains(["endpoint", "offer"], target.kind)
    ])
    error_message = "backup integration target kind must be either 'endpoint' or 'offer'."
  }

  validation {
    condition = alltrue([
      for target in [var.backup.azure_credentials, var.backup.gcs_credentials, var.backup.s3_credentials] :
      target == null ? true :
      target.kind == "endpoint" ? (target.name != null && target.name != "" && target.endpoint != null && target.endpoint != "") : true
    ])
    error_message = "Both 'name' and 'endpoint' attributes must be provided for in-model backup integrations."
  }

  validation {
    condition = alltrue([
      for target in [var.backup.azure_credentials, var.backup.gcs_credentials, var.backup.s3_credentials] :
      target == null ? true :
      target.kind == "offer" ? (target.url != null && target.url != "") : true
    ])
    error_message = "The 'url' attribute must be provided for cross-model backup integrations."
  }

  validation {
    condition = var.backup.deploy == null || length(setsubtract(
      keys(var.backup.deploy.config),
      lookup(local.backup_config_allowed_keys, var.backup.deploy.storage_type, keys(var.backup.deploy.config))
    )) == 0
    error_message = "backup.deploy.config contains keys not supported by the pinned ${try(var.backup.deploy.storage_type, "")} integrator module: ${try(join(", ", sort(setsubtract(keys(var.backup.deploy.config), lookup(local.backup_config_allowed_keys, var.backup.deploy.storage_type, [])))), "")}."
  }

  validation {
    condition     = var.backup.deploy == null || !contains(keys(try(var.backup.deploy.config, {})), "credentials")
    error_message = "backup.deploy.config must not set 'credentials'. The module sets it to the Juju secret it creates from the s3, azure or gcs secret variables."
  }
}

variable "certificate_transfer" {
  description = "CA certificate transfer integration (consumed relation)."
  type = object({
    certificate_transfer = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
  })
  default = {}

  validation {
    condition     = var.certificate_transfer.certificate_transfer == null || contains(["endpoint", "offer"], try(var.certificate_transfer.certificate_transfer.kind, ""))
    error_message = "certificate_transfer.certificate_transfer.kind must be either 'endpoint' or 'offer'."
  }

  validation {
    condition = (
      var.certificate_transfer.certificate_transfer == null ? true :
      var.certificate_transfer.certificate_transfer.kind == "endpoint" ? (
        var.certificate_transfer.certificate_transfer.name != null && var.certificate_transfer.certificate_transfer.name != "" &&
        var.certificate_transfer.certificate_transfer.endpoint != null && var.certificate_transfer.certificate_transfer.endpoint != ""
      ) : true
    )
    error_message = "Both 'name' and 'endpoint' attributes must be provided for in-model certificate_transfer integration."
  }

  validation {
    condition = (
      var.certificate_transfer.certificate_transfer == null ? true :
      var.certificate_transfer.certificate_transfer.kind == "offer" ? (
        var.certificate_transfer.certificate_transfer.url != null && var.certificate_transfer.certificate_transfer.url != ""
      ) : true
    )
    error_message = "The 'url' attribute must be provided for cross-model certificate_transfer integration."
  }
}

variable "cos" {
  description = "COS configuration. Deploys an in-model integrator under 'deploy' (with outbound relations prometheus, loki, grafana), or connects directly to same-model escape hatches (metrics_endpoint, logging, grafana_dashboard)."
  type = object({
    # Bundled integrator: opentelemetry-collector-k8s on K8s. null = do not deploy.
    deploy = optional(object({
      app_name           = optional(string, "opentelemetry-collector-k8s")
      base               = optional(string, "ubuntu@26.04")
      channel            = optional(string)
      constraints        = optional(string) # null: follow the model constraints (set model.constraints = "arch=arm64" on arm64)
      config             = optional(map(string), {})
      resources          = optional(map(string))
      revision           = optional(number)
      storage_directives = optional(map(string), {})
    }))

    # Outbound relations for the bundled integrator (Prometheus remote-write, Loki push, Grafana dashboards)
    prometheus = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
    loki = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
    grafana = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))

    # Direct Valkey-to-COS escape hatches (mutually exclusive with deploy)
    grafana_dashboard = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
    logging = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
    metrics_endpoint = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
  })
  default = {}

  validation {
    condition = !(
      var.cos.deploy != null && (
        var.cos.metrics_endpoint != null ||
        var.cos.logging != null ||
        var.cos.grafana_dashboard != null
      )
    )
    error_message = "cos.deploy and direct escape hatches (metrics_endpoint, logging, grafana_dashboard) are mutually exclusive. Set cos.deploy = null to connect directly to external targets."
  }

  validation {
    condition = (
      var.cos.deploy != null || (
        var.cos.prometheus == null &&
        var.cos.loki == null &&
        var.cos.grafana == null
      )
    )
    error_message = "cos.prometheus, cos.loki, and cos.grafana are outbound relations for the bundled integrator and require cos.deploy != null."
  }

  validation {
    condition = alltrue([
      for target in [var.cos.metrics_endpoint, var.cos.logging, var.cos.grafana_dashboard] :
      target == null || try(target.kind, "") == "endpoint"
    ])
    error_message = "cos.metrics_endpoint, cos.logging and cos.grafana_dashboard are a same-model escape hatch and accept kind = \"endpoint\" only. To reach COS in another model, set cos.deploy and use cos.prometheus, cos.loki and cos.grafana."
  }

  validation {
    condition = alltrue([
      for target in [var.cos.grafana_dashboard, var.cos.logging, var.cos.metrics_endpoint, var.cos.prometheus, var.cos.loki, var.cos.grafana] :
      target == null || contains(["endpoint", "offer"], target.kind)
    ])
    error_message = "cos integration target kind must be either 'endpoint' or 'offer'."
  }

  validation {
    condition = alltrue([
      for target in [var.cos.grafana_dashboard, var.cos.logging, var.cos.metrics_endpoint, var.cos.prometheus, var.cos.loki, var.cos.grafana] :
      target == null ? true :
      target.kind == "endpoint" ? (target.name != null && target.name != "" && target.endpoint != null && target.endpoint != "") : true
    ])
    error_message = "Both 'name' and 'endpoint' attributes must be provided for in-model cos integrations."
  }

  validation {
    condition = alltrue([
      for target in [var.cos.grafana_dashboard, var.cos.logging, var.cos.metrics_endpoint, var.cos.prometheus, var.cos.loki, var.cos.grafana] :
      target == null ? true :
      target.kind == "offer" ? (target.url != null && target.url != "") : true
    ])
    error_message = "The 'url' attribute must be provided for cross-model cos integrations."
  }
}

variable "data_integrator" {
  description = "Data Integrator charm configuration for managing client credentials."
  type = object({
    # Bundled integrator: data-integrator. null = do not deploy.
    deploy = optional(object({
      app_name    = optional(string, "data-integrator")
      base        = optional(string, "ubuntu@24.04")
      channel     = optional(string)
      constraints = optional(string) # null: follow the model constraints (set model.constraints = "arch=arm64" on arm64)
      config      = optional(map(string), {})
      prefix_name = optional(string, "*")
      revision    = optional(number)
    }))
  })
  default = {
    deploy = {}
  }

  validation {
    condition = var.data_integrator.deploy == null || alltrue([
      for k in keys(var.data_integrator.deploy.config) : contains([
        "consumer-group-prefix",
        "database-name",
        "entity-permissions",
        "entity-type",
        "extra-group-roles",
        "extra-user-roles",
        "index-name",
        "keyspace-name",
        "mtls-cert",
        "prefix-name",
        "requested-entities-secret",
        "topic-name",
      ], k)
    ])
    error_message = "data_integrator.deploy.config contains keys not supported by the pinned data-integrator module."
  }
}

variable "gcs_secret_key" {
  description = "GCP service-account JSON key for gcs-integrator. Supply through TF_VAR_gcs_secret_key or -var, at plan and at apply."
  type        = string
  sensitive   = true
  ephemeral   = true
  default     = null
}

variable "gcs_secret_version" {
  description = "0 creates no secret. 1 creates it. Increment to rotate the GCS credentials."
  type        = number
  default     = 0
}

variable "juju_controller" {
  description = "Juju controller connection details. Ephemeral: supply at plan and at apply, for example through TF_VAR_juju_controller."
  type = object({
    ca       = optional(string)
    endpoint = string
    password = string
    username = string
  })
  default   = null
  sensitive = true
  ephemeral = true
}

variable "ldap" {
  description = "LDAP authentication and certificate integrations."
  type = object({
    ldap = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
    ldap_ca_cert = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
  })
  default = {}

  validation {
    condition = alltrue([
      for target in [var.ldap.ldap, var.ldap.ldap_ca_cert] :
      target == null || contains(["endpoint", "offer"], target.kind)
    ])
    error_message = "ldap integration target kind must be either 'endpoint' or 'offer'."
  }

  validation {
    condition = alltrue([
      for target in [var.ldap.ldap, var.ldap.ldap_ca_cert] :
      target == null ? true :
      target.kind == "endpoint" ? (target.name != null && target.name != "" && target.endpoint != null && target.endpoint != "") : true
    ])
    error_message = "Both 'name' and 'endpoint' attributes must be provided for in-model ldap integrations."
  }

  validation {
    condition = alltrue([
      for target in [var.ldap.ldap, var.ldap.ldap_ca_cert] :
      target == null ? true :
      target.kind == "offer" ? (target.url != null && target.url != "") : true
    ])
    error_message = "The 'url' attribute must be provided for cross-model ldap integrations."
  }
}

variable "logging_config" {
  description = "Logging configuration to apply to the model. Applies to a created model only (model.create = true)."
  type        = string
  default     = null

  validation {
    condition     = var.logging_config == null || var.model.create
    error_message = "logging_config only applies when model.create is true. For an existing model, run juju model-config logging-config=... instead."
  }
}

variable "model" {
  description = "Juju model configuration. If create is true, a new model with 'name' is created. If false, an existing model is looked up by name and owner. constraints applies to a created model only; applications that set no arch inherit it, so set constraints = \"arch=arm64\" on arm64."
  type = object({
    constraints = optional(string)
    create      = optional(bool, true)
    name        = string
    owner       = optional(string, "admin")
  })
  default = {
    create = true
    name   = "valkey"
    owner  = "admin"
  }

  validation {
    condition     = var.model.create || var.model.constraints == null
    error_message = "model.constraints only applies when model.create is true. For an existing model, run juju set-model-constraints instead."
  }
}

variable "offered_endpoints" {
  description = "Valkey provides endpoints to expose as Juju offers. Each offer is named <valkey app_name>-<endpoint>."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for e in var.offered_endpoints : contains(["cos-agent", "grafana-dashboard", "metrics-endpoint", "valkey-client"], e)])
    error_message = "offered_endpoints accepts only Valkey provides endpoints: cos-agent, grafana-dashboard, metrics-endpoint, valkey-client."
  }
}

variable "proxy" {
  description = "Proxy settings to apply to the Juju model. Applies to a created model only (model.create = true)."
  type = object({
    http     = optional(string)
    https    = optional(string)
    no_proxy = optional(string)
  })
  default = null

  validation {
    condition     = var.proxy == null || var.model.create
    error_message = "proxy only applies when model.create is true. For an existing model, run juju model-config http-proxy=... https-proxy=... no-proxy=... instead."
  }
}

variable "risk" {
  description = "Risk level for the solution (e.g. edge, beta, candidate, stable)."
  type        = string
  default     = "edge"

  validation {
    condition     = contains(["edge", "beta", "candidate", "stable"], var.risk)
    error_message = "risk must be one of: 'edge', 'beta', 'candidate', 'stable'."
  }
}

variable "s3_access_key" {
  description = "AWS S3 Access key for s3-integrator. Supply through TF_VAR_s3_access_key or -var, at plan and at apply."
  type        = string
  sensitive   = true
  ephemeral   = true
  default     = null
}

variable "s3_secret_key" {
  description = "AWS S3 Secret key for s3-integrator. Supply through TF_VAR_s3_secret_key or -var, at plan and at apply."
  type        = string
  sensitive   = true
  ephemeral   = true
  default     = null
}

variable "s3_secret_version" {
  description = "0 creates no secret. 1 creates it. Increment to rotate the S3 credentials."
  type        = number
  default     = 0
}

variable "tls" {
  description = "Client TLS configuration. Omitted: bundled self-signed-certificates. Set client_certificates (leaving deploy unset) to consume an external provider. tls = {} disables client TLS."
  type = object({
    # Bundled default implementation: self-signed-certificates. null = do not deploy.
    deploy = optional(object({
      app_name    = optional(string, "self-signed-certificates")
      base        = optional(string, "ubuntu@24.04")
      channel     = optional(string)
      constraints = optional(string) # null: follow the model constraints (set model.constraints = "arch=arm64" on arm64)
      config      = optional(map(string), {})
      revision    = optional(number)
    }))

    # Consumed external tls-certificates provider. null = not integrated.
    client_certificates = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
  })
  default = {
    deploy = {}
  }

  validation {
    condition     = !(var.tls.deploy != null && var.tls.client_certificates != null)
    error_message = "tls.deploy and tls.client_certificates are mutually exclusive. To consume an external tls-certificates provider, leave tls.deploy unset or set it to null."
  }

  validation {
    condition     = var.tls.client_certificates == null || contains(["endpoint", "offer"], try(var.tls.client_certificates.kind, ""))
    error_message = "tls.client_certificates.kind must be either 'endpoint' or 'offer'."
  }

  validation {
    condition = (
      var.tls.client_certificates == null ? true :
      var.tls.client_certificates.kind == "endpoint" ? (
        var.tls.client_certificates.name != null && var.tls.client_certificates.name != "" &&
        var.tls.client_certificates.endpoint != null && var.tls.client_certificates.endpoint != ""
      ) : true
    )
    error_message = "Both 'name' and 'endpoint' attributes must be provided for in-model client_certificates integration."
  }

  validation {
    condition = (
      var.tls.client_certificates == null ? true :
      var.tls.client_certificates.kind == "offer" ? (
        var.tls.client_certificates.url != null && var.tls.client_certificates.url != ""
      ) : true
    )
    error_message = "The 'url' attribute must be provided for cross-model client_certificates integration."
  }
}

variable "tls_client_private_key" {
  description = "Private key for client TLS certificates. Supply through TF_VAR_tls_client_private_key or -var, at plan and at apply."
  type        = string
  sensitive   = true
  ephemeral   = true
  default     = null
}

variable "tls_client_private_key_version" {
  description = "0 creates no secret. 1 creates it. Increment to rotate the client TLS private key."
  type        = number
  default     = 0
}

variable "valkey" {
  description = "Valkey charm configuration options."
  type = object({
    app_name    = optional(string, "valkey")
    base        = optional(string, "ubuntu@26.04")
    channel     = optional(string)
    config      = optional(map(string), {})
    constraints = optional(string)
    endpoint_bindings = optional(set(object({
      endpoint = optional(string)
      space    = string
    })))
    expose = optional(map(object({
      cidrs     = optional(string)
      endpoints = optional(string)
      spaces    = optional(string)
    })), {})
    resources          = optional(map(string), {})
    revision           = optional(number)
    storage_directives = optional(map(string), {})
    units              = optional(number, 3)
  })
  default = {}

  validation {
    condition     = try(var.valkey.units, 3) >= 1
    error_message = "valkey.units must be at least 1."
  }

  validation {
    condition     = !anytrue([for k in ["system-users", "tls-client-private-key"] : contains(keys(var.valkey.config), k)])
    error_message = "valkey.config must not set system-users or tls-client-private-key. The module creates and grants those secrets from admin_password and tls_client_private_key (with their *_version variables)."
  }
}
