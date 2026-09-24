# Valkey Kubernetes Product Module

Terraform product module to deploy a complete, production-ready Charmed Valkey solution on Kubernetes.

This module conforms to CC008 Charm Terraform Standards.

## Deployed stack

- **Juju model**: Creates a new model or consumes an existing one
- **Valkey**: High-availability cluster (3 units by default, `--trust` enabled)
- **Client TLS**: Deploys `self-signed-certificates` by default on `client-certificates`, or consumes an external provider through `tls.client_certificates`
- **Data Integrator**: Deploys `data-integrator` related to Valkey on `valkey-client` for managing client credentials
- **COS (Optional)**: Deploys `opentelemetry-collector-k8s` under `deploy` and connects it to COS, in-model or through offers
- **Backup (Optional)**: Deploys a bundled backup integrator charm (`s3-integrator`, `azure-storage-integrator`, or `gcs-integrator`) under `deploy`, or integrates with external object storage providers
- **LDAP (Optional)**: Integrates with LDAP providers and CA certificates

## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.11 |
| juju | >= 2.2.1 |

## Usage

### Basic deployment

```hcl
module "valkey" {
  source = "git::https://github.com/canonical/valkey-operator//terraform/products/valkey-k8s?ref=tf-1.0.0"

  model = {
    name = "valkey-k8s"
  }
}
```

### Retrieving client credentials

Client credentials are generated dynamically by the relation between Valkey and Data Integrator. Passwords are not written to Terraform state. Retrieve them after apply using the Juju CLI:

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
  source = "git::https://github.com/canonical/valkey-operator//terraform/products/valkey-k8s?ref=tf-1.0.0"

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

### Consuming an external TLS certificates provider

To consume an external CA (such as Vault or manual-tls-certificates), set `tls.client_certificates` and leave `tls.deploy` unset. Setting both fails at plan. `tls = {}` turns client TLS off.

```hcl
module "valkey" {
  source = "git::https://github.com/canonical/valkey-operator//terraform/products/valkey-k8s?ref=tf-1.0.0"

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

### Sensitive configurations and secrets

Sensitive credentials (passwords, private keys, object-store credentials) are declared as ephemeral variables and created as Juju secrets using provider write-only attributes (`value_wo`). Terraform transmits them to the Juju controller during apply and writes `null` to `terraform.tfstate`.

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
variable "admin_password" {
  type      = string
  sensitive = true
  ephemeral = true
  default   = null
}

# 2. Forward the variable into the product module
module "valkey" {
  source = "./terraform/products/valkey-k8s"

  admin_password         = var.admin_password
  admin_password_version = 1
  # ...
}
```

Supply credentials through environment variables or command-line flags, at plan and at apply:

```bash
export TF_VAR_admin_password="StrongPassword123!"
export TF_VAR_tls_client_private_key="$(cat tls.key)"
export TF_VAR_s3_access_key="..."
export TF_VAR_s3_secret_key="..."
```

To rotate a secret, update the credential in your environment and increment the corresponding version variable (e.g. `admin_password_version = 2`). Setting the version back to `0` removes the secret and its grant. Terraform only needs the credential on runs that create or rotate the secret. Any other plan or apply works with it unset.

### Provider and lifecycle notes

- **Risk:** Valkey is published on `9/edge` and `9/beta` only for now. `risk = "candidate"` or `"stable"` fails until Valkey reaches those channels.
- **arm64:** Juju deploys an application as amd64 unless the application or the model sets an arch. Set `model = { name = "...", constraints = "arch=arm64" }`, or run `juju set-model-constraints arch=arm64` on an existing model. Valkey and every bundled charm then follow the model. The upstream self-signed-certificates and opentelemetry-collector-k8s modules default to `arch=amd64`; this module passes `deploy.constraints` (default `null`) to remove that pin.
- **Provider configuration:** Per CC008, the module defines `provider "juju" {}` in `providers.tf`. Because the module owns its provider configuration, Terraform does not permit `count`, `for_each`, or `depends_on` on the `module "valkey"` block. When destroying or removing the module, run `terraform destroy` (or `terraform destroy -target=module.valkey`) before removing the block from your configuration.
- **Grant timing:** Juju can only grant a secret after the application exists, so a hook can run before the grant. If Valkey reads the secret in that window, the hook errors and Juju retries it. This needs `automatically-retry-hooks` left at its default, `true`. Tests with provider 2.3.1 never hit the window.
- **Offers from upstream modules:** Some bundled charm modules create offers that this module does not control. The `self-signed-certificates` module always creates two offers with fixed names, `certificates` and `send-ca-cert`. The `s3-integrator`, `azure-storage-integrator` and `gcs-integrator` modules always create one offer named after the integrator application. Offer names are unique per model, so two instances of this product module in one model collide. Deploy each instance into its own model.
- **Valkey offers:** Each entry in `offered_endpoints` creates an offer named `<valkey app_name>-<endpoint>`.

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| admin_password | Admin password for charmed-operator. Supply through TF_VAR_admin_password or -var. | `string` | `null` | no |
| admin_password_version | 0 creates no secret. 1 creates it. Increment to rotate. | `number` | `0` | no |
| azure_secret_key | Azure Storage Account key or connection string. Supply through TF_VAR_azure_secret_key or -var. | `string` | `null` | no |
| azure_secret_version | 0 creates no secret. 1 creates it. Increment to rotate. | `number` | `0` | no |
| backup | Remote storage backup configuration. Deploys a bundled integrator under 'deploy' or consumes an existing integrator. | `object` | `{}` | no |
| certificate_transfer | CA certificate transfer integration (consumed relation). | `object` | `{}` | no |
| cos | COS configuration. Deploys the collector under `deploy` and connects it to COS, or connects Valkey to same-model COS apps. | `object` | `{}` | no |
| data_integrator | Data Integrator charm. Omitted: deployed. `{}` or `{ deploy = null }` skips it. | `object` | `{ deploy = {} }` | no |
| gcs_secret_key | GCP service-account JSON key for gcs-integrator. Supply through TF_VAR_gcs_secret_key or -var. | `string` | `null` | no |
| gcs_secret_version | 0 creates no secret. 1 creates it. Increment to rotate. | `number` | `0` | no |
| juju_controller | Juju controller connection details. Ephemeral: supply at plan and at apply. | `object` | `null` | no |
| ldap | LDAP authentication and certificate integrations. | `object` | `{}` | no |
| logging_config | Logging configuration to apply to the model. Needs `model.create = true`. | `string` | `null` | no |
| model | Juju model configuration (constraints, create, name, owner). `constraints`, `logging_config` and `proxy` need `create = true`. | `object` | `{"create": true, "name": "valkey", "owner": "admin"}` | no |
| offered_endpoints | Valkey provides endpoints to offer. Each offer is named `<app_name>-<endpoint>`. | `set(string)` | `[]` | no |
| proxy | Proxy settings to apply to the Juju model. Needs `model.create = true`. | `object` | `null` | no |
| risk | Risk level for the solution (edge, beta, candidate, stable). | `string` | `"edge"` | no |
| s3_access_key | AWS S3 Access key for s3-integrator. Supply through TF_VAR_s3_access_key or -var. | `string` | `null` | no |
| s3_secret_key | AWS S3 Secret key for s3-integrator. Supply through TF_VAR_s3_secret_key or -var. | `string` | `null` | no |
| s3_secret_version | 0 creates no secret. 1 creates it. Increment to rotate. | `number` | `0` | no |
| tls | Client TLS. Omitted: bundled self-signed-certificates. Set `client_certificates` (without `deploy`) for an external provider. `{}` turns client TLS off. | `object` | `{ deploy = {} }` | no |
| tls_client_private_key | Private key for client TLS certificates. Supply through TF_VAR_tls_client_private_key or -var. | `string` | `null` | no |
| tls_client_private_key_version | 0 creates no secret. 1 creates it. Increment to rotate. | `number` | `0` | no |
| valkey | Valkey charm configuration options. `config` must not set `system-users` or `tls-client-private-key`. | `object` | `{}` | no |

## Outputs

| Name | Description | Type |
|------|-------------|------|
| components | All deployed applications, `null` when not deployed. Each entry is the module's `juju_application` object (name, charm channel/revision/base, units, config, ...). `self_signed_certificates` and `opentelemetry_collector` are `{ name }` only, because their upstream modules expose `app_name` and no `application` output. | `object` |
| credentials | Client connection details and data-integrator application name. | `object` |
| metadata | Metadata of the product deployment (deployed_at, version). CC008 also lists `updated_at`; it is left out because `timestamp()` would make every plan show a change. | `object` |
| models | Map of model name to `{ model_uuid, components }`. `components` holds only deployed components, in the same shape as the `components` output. | `map(object)` |
| offers | Map of offer URLs exposed by this product module, keyed like `provides` (e.g. `valkey_client`). | `map(string)` |
| provides | Map of all provides endpoints from the product. | `map(object)` |
| requires | Map of all requires endpoints from the product. | `map(object)` |
