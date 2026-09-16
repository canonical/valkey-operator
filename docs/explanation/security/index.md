(security_index)=
# Security hardening guide

This document provides an overview of security features and guidance for hardening the security 
of [Valkey](https://charmhub.io/valkey) deployments, including setting up and managing a secure environment.

## Environment

The environment where Charmed Valkey operates can be divided into two components:

1. Cloud
2. Juju

### Cloud

Charmed Valkey can be deployed on top of several clouds and virtualisation layers:

| Cloud     | Security guides                                                                                                                                                                                                                                                        |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OpenStack | [OpenStack Security Guide](https://docs.openstack.org/security-guide/)                                                                                                                                                                                                 |
| AWS       | [Best Practices for Security, Identity and Compliance](https://aws.amazon.com/architecture/security-identity-compliance), [AWS security credentials](https://docs.aws.amazon.com/IAM/latest/UserGuide/security-creds.html)                                             |
| Azure     | [Azure security best practices and patterns](https://learn.microsoft.com/en-us/azure/security/fundamentals/best-practices-and-patterns), [Managed identities for Azure resource](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/) |
| GCP       | [Google security overview](https://cloud.google.com/docs/security)                                                                                                                                                                                                     |  |

### Juju 

Juju is the component responsible for orchestrating the entire life cycle, from deployment to Day 2 operations. For more information on Juju security hardening, see the
[Juju security page](https://documentation.ubuntu.com/juju/latest/explanation/juju-security/index.html) and the [How to harden your deployment](https://documentation.ubuntu.com/juju/3.6/howto/manage-your-juju-deployment/harden-your-juju-deployment/) guide.

#### Cloud credentials

When configuring cloud credentials to be used with Juju, ensure that users have the correct permissions to operate at the required level.
Juju superusers responsible for bootstrapping and managing controllers require elevated permissions to manage several kinds of resources, 
such as virtual machines, networks, storage, etc. Please refer to the links below for more information on the policies required to be used depending on the cloud. 

| Cloud     | Cloud user policies                                                                                                                                                                                                                            |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OpenStack | [OpenStack cloud and Juju](https://canonical-juju.readthedocs-hosted.com/en/latest/user/reference/cloud/list-of-supported-clouds/the-openstack-cloud-and-juju/)                                                                                |
| AWS       | [Juju AWS Permission](https://discourse.charmhub.io/t/juju-aws-permissions/5307), [AWS Instance Profiles](https://discourse.charmhub.io/t/using-aws-instance-profiles-with-juju-2-9/5185), [Juju on AWS](https://canonical.com/juju/docs/juju-cli/3.6/reference/cloud/list-of-supported-clouds/amazon-ec2/#cloud-ec2) |
| Azure     | [Juju Azure Permission](https://documentation.ubuntu.com/juju/3.6/reference/cloud/list-of-supported-clouds/the-microsoft-azure-cloud-and-juju/), [How to use Juju with Microsoft Azure](https://discourse.charmhub.io/t/how-to-use-juju-with-microsoft-azure/15219)                                                         |
| GCP       | [Google {spellexception}`GCE` cloud and Juju](https://canonical-juju.readthedocs-hosted.com/en/latest/user/reference/cloud/list-of-supported-clouds/the-google-gce-cloud-and-juju/)                                                            |

#### Juju users

It is very important that Juju users are set up with minimal permissions depending on the scope of their operations. 
Please refer to the [User access levels](https://documentation.ubuntu.com/juju/3.6/reference/user/#user-access-levels) documentation for more information on the access levels and corresponding abilities.

Juju user credentials must be stored securely and rotated regularly to limit the chances of unauthorised access due to credentials leakage.

## Applications

In the following, we provide guidance on how to harden your deployment using:

1. Operating system
2. Security upgrades
3. Encryption
4. Authentication
5. Authorisation
6. Monitoring and auditing

### Operating system

Valkey runs on top of Ubuntu 26.04 LTS (Resolute Raccoon). Deploy a [Landscape Client Charm](https://charmhub.io/landscape-client) to connect the underlying VM 
to a Landscape User Account to manage security upgrades and integrate [Ubuntu Pro](https://ubuntu.com/pro) subscriptions.

### Security upgrades

Charmed Valkey installs a pinned revision of the `valkey-snap`, where each revision of the charm pins 
a revision of the snap to provide reproducible environments.

New versions of the Valkey operator may be released to provide patching of vulnerabilities (CVEs). It is important 
to refresh the charm regularly to make sure the workload is as secure as possible.

### Encryption

For internal communication between Valkey peers, Charmed Valkey always enables TLS by default.
The TLS certificates for this purpose are managed by the charm itself. 

For most production settings, Valkey should be deployed with encryption for external connections, too.
To do that, you need to relate Charmed Valkey to one of the TLS certificate operator charms. 
Please refer to the [Certificate Management documentation](https://canonical-certificate-management.readthedocs-hosted.com) 
for more information on how to select the right certificate provider for your use case.

Encryption in transit for backups is provided by the storage provider. S3-compatible object storage, 
Azure Blob storage and Google Cloud Storage are supported.

For more information on encryption, see the [Cryptography](cryptography) explanation page and [How to enable TLS](../../how-to/tls) guide.

### Authentication and Authorisation

Charmed Valkey authenticates clients via Access Control Lists, allowing named users to be created 
and assigned fine-grained permissions. 

Authentication and authorisation are enabled by default. Connecting to Valkey without authentication
(using the `default` user) is disabled by default. Charmed Valkey creates an internal admin user 
with full access to the Valkey cluster. Additional users are created for each client relation. 
These client users are restricted to access only the range of keys specified in their relation 
request.

As an additional layer of authentication and authorisation, Charmed Valkey supports LDAP. 
See [How to manage LDAP](../../how-to/ldap.md) for more information.

### Monitoring and auditing

Charmed Valkey provides native integration with the [Canonical Observability Stack (COS)](https://charmhub.io/topics/canonical-observability-stack). 
To reduce the blast radius of infrastructure disruptions, the general recommendation is to deploy 
COS and the observed application into separate environments, isolated from one another. 
Refer to the [COS production deployments best practices](https://charmhub.io/topics/canonical-observability-stack/reference/best-practices) for more information.

````{tab-set}
```{tab-item} VM
:sync: vm
Logging is enabled by default. The logs are stored in the `/var/snap/valkey-charmed/common/var/log/valkey` 
directory of the Valkey container.
```

```{tab-item} K8s
:sync: k8s
Logging is enabled by default. The logs are stored in the `/var/log/valkey` directory of the Valkey container.
```
```` 

It is recommended to integrate the charm with [COS](https://discourse.charmhub.io/t/9900), 
from where the logs can be easily persisted and queried using [Loki](https://charmhub.io/loki-k8s)/[Grafana](https://charmhub.io/grafana).

## Additional Resources

For details on the cryptography used by Charmed Valkey, see the [Cryptography](cryptography) explanation page.


```{toctree}
:titlesonly:
:maxdepth: 2
:hidden:

Cryptography <cryptography>
```