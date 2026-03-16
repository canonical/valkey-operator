# How to manage private keys

You can manage private keys used by the charm to generate the certificate signing
requests (CSR) by storing the private key in a [juju secret](https://canonical-juju.readthedocs-hosted.com/en/latest/user/reference/secret/)
and then referencing the secret in the [charm configuration](https://documentation.ubuntu.com/juju/latest/howto/manage-applications/index.html#configure-an-application).

## Store the private key in a Juju secret

To store the private key in a juju secret, run the following command:

```text
$ juju add-secret tls-private-key private-key=$(base64 -w0 private-key.pem)
secret:d6s4hbnmp25c765uceo0
```
You can use the secret ID from the output to reference the secret in the charm configuration.

Now that the secret is stored, you can grant the secret to the application using the following command:

```text
juju grant-secret tls-private-key valkey
```

## Reference the secret in the charm configuration

To set the private key for TLS encryption, run:

```text
juju config valkey tls-client-private-key=secret:d6s4hbnmp25c765uceo0
```

Once the configuration is set, the charm will use the private key stored in the secret
to generate new certificate signing requests (CSR) to acquire new certificates from the TLS provider.
