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

  validation {
    condition     = var.azure_secret_version == 0 || local.backup_type == "azure"
    error_message = "azure_secret_version is set, but backup.deploy does not deploy azure-storage-integrator (storage_type = \"azure\")."
  }

  validation {
    condition     = var.azure_secret_version >= 0 && floor(var.azure_secret_version) == var.azure_secret_version
    error_message = "azure_secret_version must be a whole number of at least 0."
  }
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
    condition = length([
      for x in [var.backup.deploy, var.backup.azure_credentials, var.backup.gcs_credentials, var.backup.s3_credentials] : x if x != null
    ]) <= 1
    error_message = "Valkey supports one backup storage integrator at a time. Set at most one of backup.deploy, backup.s3_credentials, backup.azure_credentials and backup.gcs_credentials."
  }

  validation {
    condition = var.backup.deploy == null || length(setsubtract(
      keys(var.backup.deploy.config),
      lookup(local.backup_config_allowed_keys, var.backup.deploy.storage_type, keys(var.backup.deploy.config))
    )) == 0
    error_message = "backup.deploy.config for the ${try(var.backup.deploy.storage_type, "")} integrator accepts only: ${try(join(", ", local.backup_config_allowed_keys[var.backup.deploy.storage_type]), "")}. The module sets credentials itself from the matching *_secret_version secret."
  }
}

variable "cos" {
  description = "COS configuration. deploy deploys the opentelemetry-collector subordinate, and prometheus, loki and grafana connect it to COS. cos_agent instead connects Valkey's cos-agent to an existing subordinate collector in the model."
  type = object({
    # Bundled integrator: the opentelemetry-collector subordinate on Valkey's cos-agent. It runs on
    # the Valkey machines with Valkey's base. null = do not deploy.
    deploy = optional(object({
      app_name = optional(string, "opentelemetry-collector")
      channel  = optional(string)
      config   = optional(map(string), {})
      revision = optional(number)
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

    # Existing subordinate collector in the same model. Mutually exclusive with deploy.
    cos_agent = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
  })
  default = {}

  validation {
    condition     = var.cos.deploy == null || var.cos.cos_agent == null
    error_message = "cos.deploy and cos.cos_agent are mutually exclusive. Set cos.deploy = null to connect Valkey to an existing collector."
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
    condition     = var.cos.cos_agent == null || try(var.cos.cos_agent.kind, "") == "endpoint"
    error_message = "cos.cos_agent accepts kind = \"endpoint\" only, because a subordinate collector must run in the same model as Valkey. To reach COS in another model, set cos.deploy and use cos.prometheus, cos.loki and cos.grafana."
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
    condition     = var.data_integrator.deploy == null || length(setsubtract(keys(var.data_integrator.deploy.config), local.data_integrator_config_allowed_keys)) == 0
    error_message = "data_integrator.deploy.config accepts only: ${join(", ", local.data_integrator_config_allowed_keys)}."
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

  validation {
    condition     = var.gcs_secret_version == 0 || local.backup_type == "gcs"
    error_message = "gcs_secret_version is set, but backup.deploy does not deploy gcs-integrator (storage_type = \"gcs\")."
  }

  validation {
    condition     = var.gcs_secret_version >= 0 && floor(var.gcs_secret_version) == var.gcs_secret_version
    error_message = "gcs_secret_version must be a whole number of at least 0."
  }
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
    condition     = (var.ldap.ldap != null) == (var.ldap.ldap_ca_cert != null)
    error_message = "Set ldap.ldap and ldap.ldap_ca_cert together, or neither."
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
  description = "Juju model configuration. With create = true, the module creates a model called name. With create = false, it looks up an existing model by name and owner. cloud, constraints and credential apply only to a created model. cloud and credential default to the controller's default cloud and credential, so set them when that cloud is not a machine cloud. Applications that set no arch inherit constraints, so set constraints = \"arch=arm64\" on arm64."
  type = object({
    cloud = optional(object({
      name   = string
      region = optional(string)
    }))
    constraints = optional(string)
    create      = optional(bool, true)
    credential  = optional(string)
    name        = string
    owner       = optional(string, "admin")
  })
  default = {
    name = "valkey"
  }

  validation {
    condition     = var.model.create || var.model.constraints == null
    error_message = "model.constraints only applies when model.create is true. For an existing model, run juju set-model-constraints instead."
  }

  validation {
    condition     = var.model.create || (var.model.cloud == null && var.model.credential == null)
    error_message = "model.cloud and model.credential only apply when model.create is true. An existing model keeps the cloud it was created on."
  }
}

variable "offered_endpoints" {
  description = "Valkey provides endpoints to expose as Juju offers. Each offer is named <valkey app_name>-<endpoint>."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for e in var.offered_endpoints : e == "valkey-client"])
    error_message = "offered_endpoints accepts valkey-client only. On machines, Valkey serves COS through cos-agent, which needs a subordinate in the same model and cannot be offered. grafana-dashboard and metrics-endpoint are K8s-only."
  }
}

variable "proxy" {
  description = "Proxy settings to apply to the Juju model. Applies to a created model only (model.create = true)."
  type = object({
    http     = optional(string)
    https    = optional(string)
    no-proxy = optional(string)
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

  validation {
    condition     = var.s3_secret_version == 0 || local.backup_type == "s3"
    error_message = "s3_secret_version is set, but backup.deploy does not deploy s3-integrator (storage_type = \"s3\")."
  }

  validation {
    condition     = var.s3_secret_version >= 0 && floor(var.s3_secret_version) == var.s3_secret_version
    error_message = "s3_secret_version must be a whole number of at least 0."
  }
}

variable "system_users" {
  description = "Passwords for the charm's internal system users, keyed by username. Users left out keep their generated passwords. For internal use only: applications must not authenticate as these users. Supply through TF_VAR_system_users or -var, at plan and at apply."
  type        = map(string)
  sensitive   = true
  ephemeral   = true
  default     = null

  validation {
    condition = alltrue([for k in try(keys(var.system_users), []) : contains([
      "charmed-operator",
      "charmed-replication",
      "charmed-sentinel-operator",
      "charmed-sentinel-peers",
      "charmed-sentinel-valkey",
      "charmed-stats",
    ], k)])
    error_message = "system_users keys must be charm system usernames: charmed-operator, charmed-replication, charmed-sentinel-operator, charmed-sentinel-peers, charmed-sentinel-valkey or charmed-stats."
  }

  validation {
    condition     = var.system_users == null || (length(var.system_users) > 0 && alltrue([for v in values(var.system_users) : length(v) > 0]))
    error_message = "system_users must set at least one user, and every password must be non-empty. Leave it null to keep the generated passwords."
  }
}

variable "system_users_version" {
  description = "0 creates no secret. 1 creates it. Increment to rotate the system users' passwords."
  type        = number
  default     = 0

  validation {
    condition     = var.system_users_version >= 0 && floor(var.system_users_version) == var.system_users_version
    error_message = "system_users_version must be a whole number of at least 0."
  }
}

variable "tls" {
  description = "Client TLS configuration. Set client_certificates to integrate an external tls-certificates provider, and certificate_transfer to trust client CAs for mTLS. Omitted: client TLS off."
  type = object({
    # External tls-certificates provider. null = not integrated.
    client_certificates = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))

    # CA certificate transfer provider for client mTLS. null = not integrated.
    certificate_transfer = optional(object({
      kind       = string
      name       = optional(string)
      endpoint   = optional(string)
      url        = optional(string)
      controller = optional(string)
    }))
  })
  default = {}
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

  validation {
    condition     = var.tls_client_private_key_version >= 0 && floor(var.tls_client_private_key_version) == var.tls_client_private_key_version
    error_message = "tls_client_private_key_version must be a whole number of at least 0."
  }
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
    })), [])
    expose = optional(list(object({
      cidrs     = optional(string)
      endpoints = optional(string)
      spaces    = optional(string)
    })), [])
    revision           = optional(number)
    storage_directives = optional(map(string), {})
    units              = optional(number, 3)
  })
  default = {}

  validation {
    condition     = var.valkey.units >= 1 && floor(var.valkey.units) == var.valkey.units
    error_message = "valkey.units must be a whole number of at least 1."
  }

  validation {
    condition     = !anytrue([for k in ["system-users", "tls-client-private-key"] : contains(keys(var.valkey.config), k)])
    error_message = "valkey.config must not set system-users or tls-client-private-key. The module creates and grants those secrets from system_users and tls_client_private_key (with their *_version variables)."
  }
}
