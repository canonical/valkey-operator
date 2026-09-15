(cryptography_page)=
# Cryptography

This document describes the cryptography used by Charmed Valkey.

## Resource checksums

````{tab-set}
```{tab-item} VM
:sync: vm
Charmed Valkey uses the `valkey-charmed` snap, where each revision of the charm pins a revision 
of the snap to provide reproducible environments.

The snap bundles its workload together with all the essential dependencies and tools needed to support 
the operator’s life cycle. For further details, refer to the `snapcraft.yaml` file in the [Valkey artifacts](https://github.com/canonical/valkey-artifacts/blob/9.0/edge/valkey/snaps/charmed/snap/snapcraft.yaml) repository.
```

```{tab-item} K8s
:sync: k8s
Charmed Valkey uses the `valkey-charmed` rock, where each revision of the charm pins a revision 
of the rock to provide reproducible environments.

The rock is an OCI image derived from the respective snap. The `valkey-charmed` snap bundles its 
workload together with all the essential dependencies and tools needed to support the operator’s 
life cycle. For further details, refer to the `snapcraft.yaml` file in the [Valkey artifacts](https://github.com/canonical/valkey-artifacts/blob/9.0/edge/valkey/snaps/charmed/snap/snapcraft.yaml) repository.
```
````

Every artifact bundled into a snap is verified against its MD5, SHA256, or SHA512 checksum after download. 
The installation of certified snap into the rock is ensured by snap primitives that verify their `squashfs` 
file systems images GPG signature. For more information on the snap verification process, refer to the [{spellexception}`snapcraft.io` documentation](https://snapcraft.io/docs/assertions).

## Sources verification

Valkey is built by Canonical from upstream source codes on [Launchpad](https://launchpad.net/ubuntu/+source/valkey).

Charmed Valkey, the snap and the rock are published and released programmatically using release 
pipelines implemented via GitHub Actions in their respective repositories.

All repositories in GitHub are set up with branch protection rules, requiring:

* new commits to be merged to main branches via pull request with at least 2 approvals from repository maintainers
* developers to sign the [Canonical Contributor License Agreement (CLA)](https://canonical.com/legal/contributors)

## Encryption

Charmed Valkey can be used to deploy a secure Valkey cluster that provides encryption-in-transit 
capabilities out of the box for:

* Cluster internal communications (node-to-node)
* External clients connections

To set up a secure connection, Charmed Valkey needs to be integrated with TLS Certificate Provider charms,
e.g. `self-signed-certificates` operator. Certificate Signing Requests (CSRs) are generated for every unit 
using the `tls_certificates_interface` library that uses the `cryptography` Python library to create 
[X.509 compatible certificates](https://charmhub.io/topics/security-with-x-509-certificates). The CSR is signed by the TLS Certificate Provider, returned to the units, 
and stored in a Juju secret. The relation also provides the CA certificate, which is then loaded from the [Juju secret](https://documentation.ubuntu.com/juju/3.6/reference/secret/).

Encryption at rest is currently not supported, although it can be provided by the substrate (cloud or on-premises).

## Authentication

In Charmed Valkey, authentication layers can be enabled for:

1. Admin communication
2. Cluster communication
3. Client communication

### Admin communication to Valkey

Authentication of the admin user to Valkey is based on username and password. Credentials are exchanged via [Juju secrets](https://canonical.com/juju/docs/juju-cli/3.6/howto/manage-secrets/).

### Valkey cluster communication

Authentication among members of a Valkey cluster is based on username and password.

### Clients communication to Valkey

Clients can authenticate to Valkey using:
1. Username and password
2. LDAP through Canonical Identity Platform

Valkey internally stores passwords hashed with SHA256 in configuration files on the Charmed Valkey 
units in plain text format. These files are only readable and writable by the root user and the 
snap/rock-internal `_daemon_` user running the Valkey server snap/pebble commands.
