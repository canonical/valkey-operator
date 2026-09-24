# Valkey Charm Terraform Module

Terraform module to deploy the Charmed Valkey application on Kubernetes or VM clouds using the Juju Terraform provider.

This module conforms to CC008 Charm Terraform Standards.

## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.6 |
| juju | >= 1.0.0 |

## Usage

```hcl
module "valkey" {
  source     = "git::https://github.com/canonical/valkey-operator//terraform/charm?ref=tf-1.0.0"
  model_uuid = juju_model.my_model.uuid

  app_name = "valkey"
  channel  = "9/edge"
  units    = 3
}
```

For Kubernetes deployments, set `trust = true` and provide the `valkey-image` resource:

```hcl
module "valkey" {
  source     = "git::https://github.com/canonical/valkey-operator//terraform/charm?ref=tf-1.0.0"
  model_uuid = juju_model.my_model.uuid

  app_name = "valkey"
  channel  = "9/edge"
  units    = 3
  trust    = true

  resources = {
    valkey-image = "ghcr.io/canonical/valkey-charmed:9.0.4-26.04_edge"
  }
}
```

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| app_name | Name to give the deployed application. | `string` | `"valkey"` | no |
| base | Operating system base on which to deploy. | `string` | `"ubuntu@26.04"` | no |
| channel | Channel of the charm. | `string` | `"9/edge"` | no |
| config | Map of charm configuration options. | `map(string)` | `{}` | no |
| constraints | Juju constraints for this application. | `string` | `null` | no |
| endpoint_bindings | Map of endpoint bindings for spaces. | `set(object({ endpoint = optional(string), space = string }))` | `null` | no |
| expose | Map of expose definitions to make ports publicly accessible. | `map(object({ cidrs = optional(string), endpoints = optional(string), spaces = optional(string) }))` | `{}` | no |
| machines | List of Juju machine IDs to place units on. | `set(string)` | `[]` | no |
| model_uuid | Reference to an existing Juju model UUID. | `string` | n/a | yes |
| offered_endpoints | List of endpoints to expose as Juju offers. Each offer is named `<app_name>-<endpoint>`. | `set(string)` | `[]` | no |
| resources | Map of charm resource names to OCI images or revisions. | `map(string)` | `{}` | no |
| revision | Revision number of the charm. Set to null for latest. | `number` | `null` | no |
| storage_directives | Map of storage directives for application storage. | `map(string)` | `{}` | no |
| trust | Grant charm trusted status / cloud credentials. Required on K8s. | `bool` | `false` | no |
| units | Unit count for the application scale. | `number` | `1` | no |

## Outputs

| Name | Description | Type |
|------|-------------|------|
| app_name | Name of the deployed application. | `string` |
| application | Object representing the deployed application. | `object` |
| offers | Map of all offers exposed by the charm, keyed like `provides` (e.g. `valkey_client`). | `map(object)` |
| provides | Provides endpoints. | `map(object)` |
| requires | Requires endpoints. | `map(object)` |
