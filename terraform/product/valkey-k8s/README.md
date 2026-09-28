# Valkey Kubernetes product module

Terraform product module that deploys Valkey on Kubernetes with client credentials, plus optional
client TLS, COS, backup and LDAP integrations.

The module follows the CC008 Charm Terraform Standards.

## Deployed stack

- A Juju model, created or looked up by name.
- Valkey, 3 units by default, deployed with `trust = true`.
- `data-integrator` on the Valkey `valkey-client` endpoint. It hands out client credentials.
- Optional: `opentelemetry-collector-k8s` under `cos.deploy`, connected to COS in the same model or through offers.
- Optional: a backup integrator (`s3-integrator`, `azure-storage-integrator` or `gcs-integrator`) under `backup.deploy`, or an existing integrator through `backup.*_credentials`.
- Optional: client TLS from an external `tls-certificates` provider through `tls.client_certificates`, and client mTLS CAs through `tls.certificate_transfer`.
- Optional: LDAP and its CA certificate through `ldap`.

## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.11 |
| juju | >= 2.2.1 |

## Usage

### Basic deployment

```hcl
module "valkey" {
  source = "git::https://github.com/canonical/valkey-operator//terraform/product/valkey-k8s?ref=tf-1.0.0"

  model = {
    name = "valkey-k8s"
  }
}
```

### Deploying on a specific Kubernetes cloud

With `model.create = true`, Juju creates the model on the controller's default cloud. When that cloud is not Kubernetes, for example on a controller bootstrapped on LXD, set `model.cloud` and `model.credential`. To register the cluster from the same configuration, use the provider's `juju_kubernetes_cloud` resource in your root module. The product module does not create clouds, because a cloud belongs to the controller and outlives any one deployment.

```hcl
resource "juju_kubernetes_cloud" "k8s" {
  name               = "my-k8s"
  kubernetes_config  = file("~/.kube/config")
  storage_class_name = "microk8s-hostpath"
}

module "valkey" {
  source = "git::https://github.com/canonical/valkey-operator//terraform/product/valkey-k8s?ref=tf-1.0.0"

  model = {
    name       = "valkey-k8s"
    cloud      = { name = juju_kubernetes_cloud.k8s.name }
    credential = juju_kubernetes_cloud.k8s.credential
  }
}
```

`juju_kubernetes_cloud` sets no storage class unless you pass `storage_class_name`. List the classes with `kubectl get storageclass`.

### Retrieving client credentials

The relation between Valkey and data-integrator generates the client credentials, and Terraform never writes them to state. Retrieve them after apply with the Juju CLI:

```bash
juju run data-integrator/leader get-credentials -m valkey-k8s
```

### Integrating with COS Lite

The module deploys `opentelemetry-collector-k8s` next to Valkey and connects the collector to COS. With the [canonical/observability-stack](https://github.com/canonical/observability-stack/tree/main/terraform/cos-lite) `cos-lite` module, pass its offer URLs:

```hcl
module "cos" {
  source = "git::https://github.com/canonical/observability-stack//terraform/cos-lite?ref=tf-cos-lite-3.0.2"
  # ...
}

module "valkey" {
  source = "git::https://github.com/canonical/valkey-operator//terraform/product/valkey-k8s?ref=tf-1.0.0"

  model = {
    name = "valkey-k8s"
  }

  cos = {
    deploy = {
      storage_directives = { persisted = "10G" }
    }
    prometheus = {
      kind = "offer"
      url  = module.cos.offers.prometheus_receive_remote_write.url
    }
    loki = {
      kind = "offer"
      url  = module.cos.offers.loki_logging.url
    }
    grafana = {
      kind = "offer"
      url  = module.cos.offers.grafana_dashboards.url
    }
  }
}
```

Passing explicit `{ kind = "offer", url = ... }` blocks lets Terraform count resources at plan time, so COS and Valkey can be applied together the first time without `-target`.

`cos.metrics_endpoint`, `cos.logging` and `cos.grafana_dashboard` connect Valkey straight to COS applications in the same model, without the collector. They accept `kind = "endpoint"` only and cannot be combined with `cos.deploy`.

When you set `cos.deploy`, also set `cos.deploy.storage_directives`. The upstream `opentelemetry-collector-k8s` module warns on every plan when it is empty, because the default volume is 1G and resizing it after deployment takes manual steps. See [customize storage options](https://documentation.ubuntu.com/observability/latest/how-to/configure-and-tune/customize-storage-options/).

### Enabling client TLS

The module does not deploy a certificates provider. To turn on client TLS, set `tls.client_certificates` to an existing `tls-certificates` provider, such as self-signed-certificates, Vault or manual-tls-certificates. Client TLS stays off when `tls.client_certificates` is unset.

```hcl
module "valkey" {
  source = "git::https://github.com/canonical/valkey-operator//terraform/product/valkey-k8s?ref=tf-1.0.0"

  model = {
    name = "valkey-k8s"
  }

  tls = {
    client_certificates = {
      kind = "offer"
      url  = "admin/pki.vault"
    }
  }
}
```

For mutual TLS, also set `tls.certificate_transfer` to a provider of the CAs that sign your client certificates. Valkey then trusts those CAs when it verifies clients.

```hcl
  tls = {
    client_certificates = {
      kind = "offer"
      url  = "admin/pki.vault"
    }
    certificate_transfer = {
      kind = "offer"
      url  = "admin/pki.client-cas"
    }
  }
```

### Sensitive configurations and secrets

The module takes passwords, private keys and object-store credentials as ephemeral variables and stores them in Juju secrets through the provider's write-only `value_wo` attribute. Terraform sends them to the Juju controller during apply and writes `null` to `terraform.tfstate`.

Each sensitive input is paired with a version number. The version controls creation, rotation, and removal. `count` cannot read the ephemeral credential, so the version is the switch:

| Version | Credential | Result |
|----------|------------|--------|
| `0` (default) | unset | no secret |
| `1` | set | secret created, revision 1 |
| `2` | new value | in-place update, revision 2 |
| `2` (unchanged) | unset | no changes |
| `0` again | unset | secret and grant destroyed, nothing else touched |
| `1` again | set | new secret, revision 1 |

```hcl
# 1. Root module declares ephemeral variables to receive environment variables
variable "system_users" {
  type      = map(string)
  sensitive = true
  ephemeral = true
  default   = null
}

# 2. Forward the variable into the product module
module "valkey" {
  source = "./terraform/product/valkey-k8s"

  system_users         = var.system_users
  system_users_version = 1
  # ...
}
```

Supply credentials through environment variables or command-line flags, at plan and at apply:

```bash
export TF_VAR_system_users='{ "charmed-operator" = "StrongPassword123!" }'
export TF_VAR_tls_client_private_key="$(cat tls.key)"
export TF_VAR_s3_access_key="..."
export TF_VAR_s3_secret_key="..."
```

To rotate a secret, update the credential in your environment and increment the matching version variable, for example `system_users_version = 2`. Setting the version back to `0` removes the secret and its grant. Terraform only needs the credential on runs that create or rotate the secret. Any other plan or apply works with it unset.

The charm merges `system_users` over its current passwords. Removing a user from the map on rotation keeps that user's last password and does not generate a new one.

### Provider and lifecycle notes

- **Risk.** Valkey is published on `9/edge` and `9/beta` only for now. `risk = "candidate"` or `"stable"` fails until Valkey reaches those channels.
- **arm64.** Juju deploys an application as amd64 unless the application or the model sets an arch. Set `model = { name = "...", constraints = "arch=arm64" }`, or run `juju set-model-constraints arch=arm64` on an existing model. Valkey and every bundled charm then follow the model. The upstream opentelemetry-collector-k8s module defaults to `arch=amd64`. This module passes `deploy.constraints`, which defaults to `null`, to remove that pin.
- **Provider configuration.** Per CC008, the module defines `provider "juju" {}` in `providers.tf`. Because the module owns its provider configuration, Terraform does not permit `count`, `for_each`, or `depends_on` on the `module "valkey"` block. When destroying or removing the module, run `terraform destroy` (or `terraform destroy -target=module.valkey`) before removing the block from your configuration.
- **Grant timing.** Juju can only grant a secret after the application exists, so a hook can run before the grant. If Valkey reads the secret in that window, the hook errors and Juju retries it. This needs `automatically-retry-hooks` left at its default, `true`. Tests with provider 2.3.1 never hit the window.
- **Offers from upstream modules.** Some bundled charm modules create offers that this module does not control. The `s3-integrator`, `azure-storage-integrator` and `gcs-integrator` modules always create one offer named after the integrator application. Offer names are unique per model, so two instances of this product module in one model collide. Deploy each instance into its own model.
- **Valkey offers.** Each entry in `offered_endpoints` creates an offer named `<valkey app_name>-<endpoint>`.

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| azure_secret_key | Azure Storage Account key or connection string. Supply through TF_VAR_azure_secret_key or -var. | `string` | `null` | no |
| azure_secret_version | 0 creates no secret. 1 creates it. Increment to rotate. | `number` | `0` | no |
| backup | Remote storage backup configuration. Deploys a bundled integrator under 'deploy' or consumes an existing integrator. | `object` | `{}` | no |
| cos | COS configuration. Deploys the collector under `deploy` and connects it to COS, or connects Valkey to same-model COS apps. | `object` | `{}` | no |
| data_integrator | Data Integrator charm. Omitted: deployed. `{}` or `{ deploy = null }` skips it. | `object` | `{ deploy = {} }` | no |
| gcs_secret_key | GCP service-account JSON key for gcs-integrator. Supply through TF_VAR_gcs_secret_key or -var. | `string` | `null` | no |
| gcs_secret_version | 0 creates no secret. 1 creates it. Increment to rotate. | `number` | `0` | no |
| juju_controller | Juju controller connection details. Ephemeral: supply at plan and at apply. | `object` | `null` | no |
| ldap | LDAP authentication and certificate integrations. | `object` | `{}` | no |
| logging_config | Logging configuration to apply to the model. Needs `model.create = true`. | `string` | `null` | no |
| model | Juju model configuration (cloud, constraints, create, credential, name, owner). `cloud`, `constraints`, `credential`, `logging_config` and `proxy` need `create = true`. | `object` | `{ name = "valkey" }` | no |
| offered_endpoints | Valkey provides endpoints to offer: `grafana-dashboard`, `metrics-endpoint` or `valkey-client`. Each offer is named `<app_name>-<endpoint>`. | `list(string)` | `[]` | no |
| proxy | Proxy settings for the Juju model, with the keys `http`, `https` and `no-proxy`. Needs `model.create = true`. | `object` | `null` | no |
| risk | Risk level for the solution (edge, beta, candidate, stable). | `string` | `"edge"` | no |
| s3_access_key | AWS S3 Access key for s3-integrator. Supply through TF_VAR_s3_access_key or -var. | `string` | `null` | no |
| s3_secret_key | AWS S3 Secret key for s3-integrator. Supply through TF_VAR_s3_secret_key or -var. | `string` | `null` | no |
| s3_secret_version | 0 creates no secret. 1 creates it. Increment to rotate. | `number` | `0` | no |
| system_users | Passwords for the charm's internal system users, keyed by username: `charmed-operator`, `charmed-replication`, `charmed-sentinel-operator`, `charmed-sentinel-peers`, `charmed-sentinel-valkey` or `charmed-stats`. Users left out keep their generated passwords. Supply through TF_VAR_system_users or -var. | `map(string)` | `null` | no |
| system_users_version | 0 creates no secret. 1 creates it. Increment to rotate. | `number` | `0` | no |
| tls | Client TLS. Set `client_certificates` to an external `tls-certificates` provider, and `certificate_transfer` to a CA provider for client mTLS. Each takes `{ kind, name, endpoint, url, controller }`. Omitted: client TLS off. | `object` | `{}` | no |
| tls_client_private_key | Private key for client TLS certificates. Supply through TF_VAR_tls_client_private_key or -var. | `string` | `null` | no |
| tls_client_private_key_version | 0 creates no secret. 1 creates it. Increment to rotate. | `number` | `0` | no |
| valkey | Valkey charm configuration options. `config` must not set `system-users` or `tls-client-private-key`. | `object` | `{}` | no |

## Outputs

| Name | Description | Type |
|------|-------------|------|
| components | All deployed applications, `null` when not deployed. Each entry is the module's `juju_application` object (name, charm channel/revision/base, units, config, ...). `opentelemetry_collector` is `{ name }` only, because its upstream module exposes `app_name` and no `application` output. | `object` |
| credentials | Connection details as `valkey = { app_name, client_port, tls_port, sentinel_port, sentinel_tls_port, data_integrator_app }`. It holds no password, because data-integrator hands out client credentials. Run `juju run <data_integrator_app>/leader get-credentials` to get them. | `map(object)` |
| metadata | Metadata of the product deployment (deployed_at, version). CC008 also lists `updated_at`. The module leaves it out because `timestamp()` would make every plan show a change. | `object` |
| models | Map of model name to `{ model_uuid, components }`. `components` holds only deployed components, in the same shape as the `components` output. | `map(object)` |
| offers | Map of offers exposed by this product module, keyed like `provides`, for example `valkey_client`. Each value is `{ kind = "offer", url }`, so it can be passed as a target to another module. | `map(object)` |
| provides | Valkey provides endpoints, the charm module's `provides` map. | `map(object)` |
| requires | Valkey requires endpoints, the charm module's `requires` map. | `map(object)` |
