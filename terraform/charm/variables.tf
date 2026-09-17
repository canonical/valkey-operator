variable "app_name" {
  description = "Name to give the deployed application."
  type        = string
  default     = "valkey"
}

variable "base" {
  description = "The operating system base on which to deploy (e.g. ubuntu@26.04)."
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
  description = "Map of endpoint bindings for spaces."
  type = set(object({
    endpoint = optional(string)
    space    = string
  }))
  default = null
}

variable "expose" {
  description = "Map of expose definitions to make application ports publicly accessible."
  type = map(object({
    cidrs     = optional(string)
    endpoints = optional(string)
    spaces    = optional(string)
  }))
  default = {}
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
  description = "List of endpoints to expose as Juju offers for cross-model consumption."
  type        = set(string)
  default     = []
}

variable "resources" {
  description = "Map of charm resource names to OCI image hashes or revisions."
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
