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

Charmed Valkey works with any provider of the `ldap` interface. This guide covers the two
Canonical-maintained ones: the [Authentik LDAP outpost](https://charmhub.io/authentik-ldap-outpost),
which exposes an Authentik directory over LDAP, and [GLAuth](https://charmhub.io/glauth-k8s), a
lightweight LDAP server. Both are Kubernetes charms.

The exact way you deploy them depends on the substrate Charmed Valkey runs on:

`````{tab-set}
:sync-group: substrate

````{tab-item} VM
:sync: vm

Use a separate Juju controller with a Kubernetes model to deploy the LDAP provider. You will
create a
[cross-model relation](https://documentation.ubuntu.com/juju/3.6/howto/manage-relations/index.html#add-a-cross-model-relation)
to the Valkey VM model later in this guide.
````

````{tab-item} K8s
:sync: k8s

No separate Juju model is required -- run the LDAP provider alongside Charmed
Valkey in the same model.
````

`````

```{caution}
**[Self-signed certificates](https://en.wikipedia.org/wiki/Self-signed_certificate) are not recommended for a production environment.**

Check the [Choosing a TLS provider](https://canonical-certificate-management.readthedocs-hosted.com/operator/understanding-tls/#choosing-a-tls-provider) page for an overview of all the TLS certificates charms available. 
```

`````{tab-set}
:sync-group: ldap-provider

````{tab-item} Authentik
:sync: authentik

Deploy the Authentik server, its worker, the LDAP outpost, and the supporting
database, ingress, and certificate charms:

```shell
juju deploy postgresql-k8s --channel 14/stable --trust
juju deploy authentik-server --channel latest/stable --trust
juju deploy authentik-worker --channel latest/stable --trust
juju deploy authentik-ldap-outpost --channel latest/stable --trust
juju deploy traefik-k8s --trust
juju deploy self-signed-certificates
```

Integrate them:

```shell
juju integrate authentik-server:pg-database postgresql-k8s:database
juju integrate authentik-server:authentik-cluster authentik-worker
juju integrate authentik-server:authentik-server-info authentik-ldap-outpost
juju integrate authentik-server:traefik-route traefik-k8s
juju integrate authentik-ldap-outpost:traefik-route traefik-k8s
juju integrate traefik-k8s:certificates self-signed-certificates
```

Traefik is required, not optional: the outpost does not provision a certificate
of its own, so Traefik terminates LDAPS for it on port 636.

Users and groups are managed in Authentik itself. To reach its web interface,
generate a recovery link for the bootstrap administrator:

```shell
juju run authentik-server/leader create-recovery-link
```

```{note}
The outpost defaults to `search_mode=cached` and `bind_mode=cached`, which serve
a periodically refreshed snapshot of the directory. Directory changes -- new
users, changed group membership, changed passwords -- can take until the next
synchronization to reach Valkey. Set both to `direct` if every request must
reflect the live Authentik state.
```
````

````{tab-item} GLAuth
:sync: glauth

Deploy `glauth-k8s`, `self-signed-certificates`, and `postgresql-k8s`
(see the [GLAuth tutorial](https://canonical-identity.readthedocs-hosted.com/tutorial/charms/glauth/)
for a walkthrough):

```shell
juju deploy glauth-k8s --channel latest/edge --trust
juju deploy self-signed-certificates
juju deploy postgresql-k8s --channel 14/stable --trust
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
````

`````

## Create a cross-model relation

Whether this step is needed depends on the substrate Charmed Valkey runs on:

`````{tab-set}
:sync-group: substrate

````{tab-item} VM
:sync: vm

The LDAP provider runs on a separate Kubernetes controller and model, so Valkey
needs a cross-model relation to reach it.

**Expose LDAP**

Traefik exposes the LDAP endpoint outside the Kubernetes cluster. The Authentik
outpost is already related to it; for GLAuth, deploy and integrate Traefik now:

```shell
juju deploy traefik-k8s --trust
juju integrate traefik-k8s:ingress glauth-k8s:ingress-per-unit
```

**Expose cross-model relations**

Offer the `ldap` endpoint of the provider you deployed -- `authentik-ldap-outpost`
or `glauth-k8s` -- together with the CA certificate of the charm that issued its
serving certificate:

```shell
juju offer <ldap_provider>:ldap ldap
juju offer self-signed-certificates:send-ca-cert send-ca-cert
```

**Consume offers**

Switch to the VM controller:

```shell
juju switch <vm_controller>:<model-name>
```

Consume the LDAP offers:

```shell
juju consume <k8s_controller>:admin/<k8s-model-name>.ldap
juju consume <k8s_controller>:admin/<k8s-model-name>.send-ca-cert
```
````

````{tab-item} K8s
:sync: k8s

The LDAP provider already shares the same Juju model as Charmed Valkey, so no
cross-model relation is required. Proceed to the next section:
{ref}`define-roles`.
````

`````

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
juju config data-integrator prefix-name="*" entity-permissions='[{"resource_name": "ldap_users_write", "resource_type": "acl", "privileges": ["+@read", "+@write", "~*"]}, {"resource_name": "ldap_users_read", "resource_type": "acl", "privileges": ["+@read", "~*"]}]'
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

Two more settings depend on how your directory names groups and exposes user DNs.

Valkey lists the members of a group named in `ldap-map` with the query in
`ldap-query-template`, substituting `{group}` with the group name. It then binds as the user
whose DN it reads from the attribute named in `ldap-search-dn-attribute`.

`````{tab-set}
:sync-group: ldap-provider

````{tab-item} Authentik
:sync: authentik

Authentik names groups with a `cn` RDN, which the default `ldap-query-template`
already matches -- leave it alone.

Authentik publishes no attribute holding an entry's DN. It does render a user's
Authentik attributes as LDAP attributes, substituting `%s` with the username, so
give every user an attribute holding its own bind DN. In the Authentik interface,
add this to each user's **Attributes**, substituting your configured base DN:

```yaml
entryDN: cn=%s,ou=users,dc=ldap,dc=goauthentik,dc=io
```

Then point Valkey at that attribute:

```shell
juju config valkey ldap-search-dn-attribute="entryDN"
```
````

````{tab-item} GLAuth
:sync: glauth

GLAuth models groups as organizational units rather than the `cn` the default
template expects, so override it:

```shell
juju config valkey ldap-query-template='(&(objectClass=posixAccount)(memberOf=ou={group},*))'
```

GLAuth also returns no DN attribute, so fall back to the email address of your
LDAP users:

```shell
juju config valkey ldap-search-dn-attribute="mail"
```
````

`````

## Enable LDAP

After completing all required configuration, integrate Valkey with the LDAP provider and with the
charm holding the CA certificate that signed the provider's serving certificate:

`````{tab-set}
:sync-group: ldap-provider

````{tab-item} Authentik
:sync: authentik

```shell
juju integrate valkey:ldap-ca-cert self-signed-certificates:send-ca-cert
juju integrate valkey:ldap authentik-ldap-outpost:ldap
```
````

````{tab-item} GLAuth
:sync: glauth

```shell
juju integrate valkey:ldap-ca-cert self-signed-certificates:send-ca-cert
juju integrate valkey:ldap glauth-k8s:ldap
```
````

`````

Wait for the deployment to settle and log in to Valkey with the username and password from LDAP.
The permissions in Valkey are set up according to the defined roles and the configured role mapping.

If something goes wrong or a configuration is missing, Charmed Valkey will display a `blocked` 
status with more information, for example: `LDAP: Missing config for 'ldap-map'`.

### Test LDAP authentication

Get the endpoint for login from `juju status` and log in using `valkey-cli`:

```shell
valkey-cli -h <your-ip-address> -p 6379
```

Authenticate with your username and password from LDAP:

```shell
AUTH <ldap username> <ldap password>
```

Now perform a basic health check:

```shell
ping
```

You should receive this response from the Valkey server:

```text
PONG
```

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

You can disable LDAP in Valkey by removing the relations with the LDAP provider:

```shell
juju remove-relation valkey:ldap-ca-cert self-signed-certificates:send-ca-cert
juju remove-relation valkey:ldap <ldap_provider>:ldap
```

This removes all LDAP users that were previously added to Valkey's ACL files.  
