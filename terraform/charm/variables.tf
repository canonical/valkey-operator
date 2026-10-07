variable "app_name" {
  description = "Name to give the deployed application."
  type        = string
  default     = "valkey"
}

variable "base" {
  description = "The operating system base on which to deploy, for example ubuntu@26.04."
  type        = string
  default     = "ubuntu@26.04"
}

variable "channel" {
  description = "Channel of the charm."
  type        = string
  default     = "9/edge"
}

variable "config" {
  description = "Map of charm configuration options."
  type        = map(string)
  default     = {}
}

variable "constraints" {
  description = "Juju constraints for this application."
  type        = string
  default     = null
}

variable "endpoint_bindings" {
  description = "Set of endpoint-to-space bindings. An entry without endpoint sets the default space."
  type = set(object({
    endpoint = optional(string)
    space    = string
  }))
  default = []
}

variable "expose" {
  description = "Expose settings for the application. Takes at most one entry. [] leaves the application unexposed and [{}] exposes it to all CIDRs."
  type = list(object({
    cidrs     = optional(string)
    endpoints = optional(string)
    spaces    = optional(string)
  }))
  default = []

  validation {
    condition     = length(var.expose) <= 1
    error_message = "expose takes at most one entry because Juju keeps one expose setting per application. To expose several endpoints, list them comma-separated in the endpoints attribute of that entry."
  }
}

variable "machines" {
  description = "List of Juju machine IDs to place units on."
  type        = set(string)
  default     = []
}

variable "model_uuid" {
  description = "Reference to an existing Juju model UUID. This field is not nullable."
  type        = string
  nullable    = false
}

variable "offered_endpoints" {
  description = "List of endpoints to expose as Juju offers for cross-model consumption. Each offer is named <app_name>-<endpoint>."
  type        = list(string)
  default     = []
}

variable "resources" {
  description = "Map of charm resource name to a Charmhub revision number or an OCI image URL. {} uses the resources published with the charm revision. Warning: charm refresh only accepts the OCI image published with the charm revision, so a unit running another image is refused as incompatible and does not start."
  type        = map(string)
  default     = {}
}

variable "revision" {
  description = "Revision number of the charm. Set to null to deploy the latest revision on the given channel."
  type        = number
  default     = null
}

variable "storage_directives" {
  description = "Map of storage directives for application storage."
  type        = map(string)
  default     = {}
}

variable "trust" {
  description = "Whether to grant the charm trusted status / cloud credentials. Required on Kubernetes."
  type        = bool
  default     = false
}

variable "units" {
  description = "Unit count for the application scale."
  type        = number
  default     = 1
}
