# Valkey charm Terraform module

Terraform module that deploys the Valkey charm on Kubernetes or machine clouds with the Juju
Terraform provider.

The module follows the CC008 Charm Terraform Standards.

## Requirements

| Name      | Version  |
| --------- | -------- |
| terraform | >= 1.11  |
| juju      | >= 2.2.1 |

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

For Kubernetes deployments, set `trust = true`:

```hcl
module "valkey" {
  source     = "git::https://github.com/canonical/valkey-operator//terraform/charm?ref=tf-1.0.0"
  model_uuid = juju_model.my_model.uuid

  app_name = "valkey"
  channel  = "9/edge"
  units    = 3
  trust    = true
}
```

## Inputs

| Name               | Description                                                                                                                                                                                                                                                                                                                         | Type                                                                                                  | Default          | Required |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ---------------- | :------: |
| app_name           | Name to give the deployed application.                                                                                                                                                                                                                                                                                              | `string`                                                                                              | `"valkey"`       |    no    |
| base               | Operating system base on which to deploy.                                                                                                                                                                                                                                                                                           | `string`                                                                                              | `"ubuntu@26.04"` |    no    |
| channel            | Channel of the charm.                                                                                                                                                                                                                                                                                                               | `string`                                                                                              | `"9/edge"`       |    no    |
| config             | Map of charm configuration options.                                                                                                                                                                                                                                                                                                 | `map(string)`                                                                                         | `{}`             |    no    |
| constraints        | Juju constraints for this application.                                                                                                                                                                                                                                                                                              | `string`                                                                                              | `null`           |    no    |
| endpoint_bindings  | Set of endpoint-to-space bindings. An entry without `endpoint` sets the default space.                                                                                                                                                                                                                                              | `set(object({ endpoint = optional(string), space = string }))`                                        | `[]`             |    no    |
| expose             | Expose settings. Takes at most one entry. `[]` leaves the application unexposed and `[{}]` exposes it to all CIDRs.                                                                                                                                                                                                                 | `list(object({ cidrs = optional(string), endpoints = optional(string), spaces = optional(string) }))` | `[]`             |    no    |
| machines           | List of Juju machine IDs to place units on.                                                                                                                                                                                                                                                                                         | `set(string)`                                                                                         | `[]`             |    no    |
| model_uuid         | Reference to an existing Juju model UUID.                                                                                                                                                                                                                                                                                           | `string`                                                                                              | n/a              |   yes    |
| offered_endpoints  | List of endpoints to expose as Juju offers. Each offer is named `<app_name>-<endpoint>`.                                                                                                                                                                                                                                            | `list(string)`                                                                                        | `[]`             |    no    |
| resources          | Map of charm resource name to a Charmhub revision number or an OCI image URL. `{}` uses the resources published with the charm revision. Do not override `valkey-image`. Charm refresh only accepts the OCI image published with the charm revision, so a unit running another image is refused as incompatible and does not start. | `map(string)`                                                                                         | `{}`             |    no    |
| revision           | Revision number of the charm. Set to null for latest.                                                                                                                                                                                                                                                                               | `number`                                                                                              | `null`           |    no    |
| storage_directives | Map of storage directives for application storage.                                                                                                                                                                                                                                                                                  | `map(string)`                                                                                         | `{}`             |    no    |
| trust              | Grant charm trusted status / cloud credentials. Required on K8s.                                                                                                                                                                                                                                                                    | `bool`                                                                                                | `false`          |    no    |
| units              | Unit count for the application scale.                                                                                                                                                                                                                                                                                               | `number`                                                                                              | `1`              |    no    |

## Outputs

| Name        | Description                                                                                                                          | Type          |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------ | ------------- |
| app_name    | Name of the deployed application.                                                                                                    | `string`      |
| application | Object representing the deployed application.                                                                                        | `object`      |
| offers      | Map of all offers exposed by the charm, keyed like `provides`, for example `valkey_client`. Each value is `{ kind = "offer", url }`. | `map(object)` |
| provides    | Provides endpoints.                                                                                                                  | `map(object)` |
| requires    | Requires endpoints.                                                                                                                  | `map(object)` |
