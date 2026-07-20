# How to manage LDAP

LDAP (Lightweight Directory Access Protocol) enables centralized authentication for Valkey, 
reducing the overhead of managing local credentials. LDAP support in Charmed Valkey also enables
role-based access control.

This guide goes over the steps to integrate LDAP as an authentication method with the Valkey charm 
within the Juju ecosystem.

## Prerequisites 

The following components are required before proceeding:

* Charmed Valkey deployed on either a VM or Kubernetes.
* A Kubernetes Juju controller to deploy the LDAP provider

## Deploy LDAP server charm

If you run Charmed Valkey on a VM deployment, use a separate Juju controller with a K8s
model in order to deploy the [`glauth-k8s` charm](https://charmhub.io/glauth-k8s). We will then 
create a cross-controller relation to the Valkey VM model.

With Charmed Valkey deployed on Kubernetes, deploy GLAuth alongside without a separate Juju model.

Deploy `glauth-k8s`, `self-signed-certificates`, and `postgresql-k8s`:

```shell
juju deploy glauth-k8s --channel latest/edge --trust
juju deploy self-signed-certificates
juju deploy postgresql-k8s --channel 14/stable --trust
```

```{caution}
**[Self-signed certificates](https://en.wikipedia.org/wiki/Self-signed_certificate) are not recommended for a production environment.**

Check the [Choosing a TLS provider](https://canonical-certificate-management.readthedocs-hosted.com/operator/understanding-tls/#choosing-a-tls-provider) page for an overview of all the TLS certificates charms available. 
```

Integrate `glauth-k8s` with `self-signed-certificates` and `postgresql-k8s`:

```shell
juju integrate glauth-k8s self-signed-certificates
juju integrate glauth-k8s:pg-database postgresql-k8s:database
```

Deploy the [`glauth-utils` charm](https://charmhub.io/glauth-utils) to manage LDAP users, and 
integrate it with the GLAuth application:

```shell
juju deploy glauth-utils --channel latest/edge --trust
juju integrate glauth-k8s glauth-utils
```

Users and groups can now be created using [glauth-utils](https://github.com/canonical/glauth-utils/blob/main/SAMPLES.md).

## Create a cross-model relation (VM only)

This step is not needed with Valkey on Kubernetes. Proceed to the next section: {ref}`define-roles`.

### Expose LDAP

Deploy the [Traefik charm](https://charmhub.io/traefik-k8s) in order to expose LDAP endpoints
from the K8s cluster:

```shell
juju deploy traefik-k8s --trust
```

Integrate Traefik with the LDAP server:

```shell
juju integrate traefik-k8s:ingress glauth-k8s:ingress-per-unit
```

### Expose cross-model relations

To offer the GLAuth interfaces, run:

```shell
juju offer glauth-k8s:ldap ldap
juju offer glauth-k8s:send-ca-cert send-ca-cert
```

### Consume offers

Switch to the VM controller:

```shell
juju switch <vm_controller>:<model-name>
```

Consume the LDAP offers:

```shell
juju consume <k8s_controller>:admin/<k8s-model-name>.ldap
juju consume <k8s_controller>:admin/<k8s-model-name>.send-ca-cert
```

(define-roles)=
## Define roles and permissions

Charmed Valkey supports a role-based access control model to allow permissions based on LDAP groups.
To define the desired roles, configure `entity-permissions` through Data Integrator and configure 
the mapping of these roles to LDAP groups to Charmed Valkey.

### Set up Data Integrator

Deploy the Data Integrator charm in the same model as Charmed Valkey:

```shell
juju deploy data-integrator --channel latest/edge
```

The configuration of `entity-permissions` expects a list of role definitions in JSON syntax. Let's
assume we want to configure two roles, one with read and write permissions and one with read-only
permissions:

```json
[
    {
        "resource_name": "ldap_users_write",
        "resource_type": "acl",
        "privileges": ["+@read", "+@write", "~*"]
    },
    {
        "resource_name": "ldap_users_read",
        "resource_type": "acl",
        "privileges": ["+@read", "~*"]    
    }
]
```

Configure permissions for Data Integrator:

```shell
juju config data-integrator prefix-name="*" entity-permissions='[{"resource_name": "ldap_users_write", "resource_type": "acl", "privileges": ["read", "write", "~*"]}, {"resource_name": "ldap_users_read", "resource_type": "acl", "privileges": ["read", "~*"]}]'
```

Now integrate with Valkey to provide the role definition:

```shell
juju integrate valkey:valkey-client data-integrator:valkey
```

### Configure role mapping

After setting up a role-based access control model in Valkey, configure the mapping of LDAP groups
to your defined roles in Valkey:

```shell
juju config valkey ldap-map="<ldap_group_name>:ldap_users_write,<another_ldap_group>:ldap_users_read"
```

Due to a limitation in GLAuth, it might also be required to configure the attribute that contains 
the username in the LDAP directory:

```shell
juju config valkey ldap-search-dn-attribute="mail"
```

## Enable LDAP

After completing all required configuration, integrate Valkey with GLAuth to enable LDAP::

```shell
juju integrate valkey:ldap-ca-cert glauth-k8s:send-ca-cert
juju integrate valkey:ldap glauth-k8s:ldap
```

Wait for the deployment to settle and log in to Valkey with the username and password from LDAP.
The permissions in Valkey are set up according to the defined roles and the configured role mapping.

If something goes wrong or a configuration is missing, Charmed Valkey will display a `blocked` 
status with more information, for example: `LDAP: Missing config for 'ldap-map'`.

## Synchronize LDAP users

Charmed Valkey adds all users from the configured LDAP groups to its ACL files. Over time, the group
assignments in LDAP might evolve: new users might be added to or existing users might be removed from
LDAP groups.

Synchronize the LDAP users in Valkey by running the `sync-ldap-users` action on the leader unit:

```shell
juju run valkey/leader sync-ldap-users
```

This will update the ACL files on all units.

## Disable LDAP

You can disable LDAP in Valkey by removing the relations with GLAuth:

```shell
juju remove-relation valkey:ldap-ca-cert glauth-k8s:send-ca-cert
juju remove-relation valkey:ldap glauth-k8s:ldap
```

This removes all LDAP users that were previously added to Valkey's ACL files.  
