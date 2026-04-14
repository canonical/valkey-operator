# How to manage client connections

[Relations](https://documentation.ubuntu.com/juju/latest/reference/relation/index.html) are connections between two 
applications with compatible endpoints. These connections simplify creating and 
managing users, passwords, and other shared data.

Relations to Valkey are supported via the {spellexception}`valkey_client` interface.

## Integrate an application charm with Valkey

If the application charm supports the `valkey_client` relation interface, just create 
an integration between the two charms:

```shell
juju integrate valkey application
```

The Charmed Valkey operator provides the following information over the relation interface:

- `username`: The username created in Valkey.
- `password`: The password that was set for the username.
- `endpoints`: The endpoints for read and write access (Primary endpoints).
- `read_only_endpoints` The endpoints for read-only access (Replica endpoints).
- `sentinel_endpoints` The endpoints of Valkey Sentinel, if Valkey HA runs in Sentinel-mode.
- `mode`: The High Availability mode Valkey is operating in (can be `sentinel` only currently).
- `tls`: Whether TLS is enabled. This is `True` if TLS is enabled and `False` otherwise.
- `tls_ca`: The CA certificate used to sign the server certificate.
- `version`: The version of Valkey that is being deployed.

To remove the relation:

```shell
juju remove-relation valkey application
```

### Implement the client interface

The following section describes how to add the client interface implementation to a charm.

First, add the `valkey_client` interface to your charm in the `metadata.yaml` file:

```yaml
requires:
  valkey:
    interface: valkey_client
```

The `valkey_client` relation interface is based on the `data-interfaces` [library](https://pypi.org/project/dpcharmlibs-interfaces/).
To add it to your Python code:

```python
from dpcharmlibs.interfaces import (
    RequirerCommonModel,
    ResourceRequirerEventHandler,
    ValkeyResponseModel,
)
```

Then, create the interface in your charm's `__init__` method by instantiating the `ResourceRequirerEventHandler`
with the following parameters:

- `charm`: The charm instance.
- `relation_name`: The name of the relation. This should match the name you defined in the `metadata.yaml` file.
- `requests`: The access request that is sent to Charmed Valkey. Set your desired key prefix as `resource`.
- `response_model`: The model for the data provided over the relation interface; use the `ValkeyResponseModel`.

```python
class MyCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        self.valkey_interface = ResourceRequirerEventHandler(
            charm=self,
            relation_name="valkey",
            requests=[RequirerCommonModel(resource="test-keyspace:*")],
            response_model=ValkeyResponseModel,
            )
```

Next, add observers for the events provided by the library, and define callback 
functions to handle these events:

```python
    framework.observe(self.valkey_interface.on.resource_created, self._on_resource_created)
    framework.observe(self.valkey_interface.on.endpoints_changed, self._on_endpoints_changed)
    framework.observe(self.valkey_interface.on.read_only_endpoints_changed, self._on_endpoints_changed)
```

Access the data provided over the relation interface in your callback functions and
set up your Valkey client with the retrieved information:

```python
def _on_resource_created(self, event: ResourceCreatedEvent) -> None:
    response = event.response # This is the response model
    valkey_endpoints = response.endpoints
    valkey_username = response.username
    valkey_password = response.password
    ...
```

For more information, please refer to the [README of data-interfaces](https://github.com/canonical/data-platform-charmlibs/blob/main/interfaces/README.md#requirer-charm)

Charmed Valkey also supports backwards compatibility with `data-interfaces v0`.

## Non-charmed applications and external clients

The `valkey_client` interface is supported by the `data-integrator` charm. This charm
automatically creates and manages product credentials needed to authenticate with 
different kinds of data platform charmed products:

Deploy the Data Integrator charm with the desired `prefix-name`:

```shell
juju deploy data-integrator
juju config data-integrator prefix-name="test-keyspace:*"
```

Integrate the two applications with:

```shell
juju integrate data-integrator valkey
```

To retrieve information, enter:

```shell
juju run data-integrator/leader get-credentials
```

The output displays the client credentials created and the connection information:

```yaml
ok: "True"
valkey:
  endpoints: valkey-1.valkey-endpoints:6379
  mode: sentinel
  password: <PASSWORD>
  read-only-endpoints: valkey-0.valkey-endpoints:6379
  sentinel-endpoints: valkey-0.valkey-endpoints:26379,valkey-1.valkey-endpoints:26379
  tls: "False"
  tls-ca: None
  username: relation-5-40749865b6c7d821
```

Use these information to connect to Valkey:

```shell
valkey-cli -h valkey-1.valkey-endpoints -p 6379 --user relation-6-4871b52b360fdb1d --pass <PASSWORD>
```

Now you can access the database:

```shell
valkey-1.valkey-endpoints:6379> set test-keyspace:mykey 42
```

## Enable mutual TLS

Charmed Valkey optionally supports client-side TLS in addition to server-side TLS. 
This mode is called mutual TLS (mTLS).

To use mTLS, first enable client TLS in Valkey by setting up a TLS provider. 
For details, please refer to [How to enable TLS](tls.md).

After enabling client TLS, Charmed Valkey will update the TLS information in the 
relation interface:

```yaml
  endpoints: valkey-1.valkey-endpoints:6380
    ...
  tls: "True"
  tls-ca: |-
    -----BEGIN CERTIFICATE-----
    ...
    -----END CERTIFICATE-----
```

The provided `tls-ca` is the certificate authority (CA certificate) that was used 
to sign Valkey's server certificate. If the related client charm uses a different 
certificate authority to sign its TLS certificates, it has to provide its CA certificate
to Charmed Valkey via the `certificate_transfer` [interface](https://documentation.ubuntu.com/charmlibs/reference/charmlibs/interfaces/certificate-transfer/).

To add a certificate authority to Charmed Valkey's trusted CA certificates, integrate
the TLS provider charm with Valkey:

```shell
juju integrate valkey:certificate-transfer <YOUR_TLS_PROVIDER>
```

This CA certificate can now be used by Valkey to verify client-side TLS certificates.

To connect to Valkey using mTLS, simply provide your client certificate and private 
key as parameters to your client connection. For example, when connecting with `valkey-cli`:

```shell
valkey-cli -h valkey-1.valkey-endpoints -p 6380 --tls --cert <YOUR_CLIENT_CERTIFICATE> --key <YOUR_CLIENT_PRIVATE_KEY> --cacert <SERVER_CA_FILE>
```

After connecting, log in with the credentials provided through the relation interface:

```shell
valkey-1.valkey-endpoints:6379> AUTH relation-5-40749865b6c7d821 <PASSWORD>
```

## Enable authentication through client certificate

Charmed Valkey also supports password-less authentication via client certificates.
This requires [mutual TLS](enable-mutual-tls).

Clients can log in to Valkey without providing a username and password, when the 
common name (CN) of the presented client certificates matches the username that
has been created for the client relation.

To use this feature, simply add the username provided through the relation interface
as common name for the certificate request:

```yaml
valkey:
  endpoints: valkey-1.valkey-endpoints:6380
    ...
  username: relation-5-40749865b6c7d821
```

Clients presenting a TLS certificate with this username as `CN` will be able to connect
to Valkey without username and password.
