# How to manage passwords

This guide provides instructions for creating, updating, and otherwise managing passwords.

To read or write data in Valkey, we need to authenticate ourselves.

For this guide, we will use Charmed {spellexception}`Valkey's` internal admin user
`charmed-operator`. This user is only for internal use, and it is created automatically
by Charmed Valkey.

We will go through setting a user-defined password for this admin user and configuring
Valkey.

To create an external client user, refer to [client connections](clients).

## Auto-generated credentials

For security purposes, Charmed Valkey automatically generates users and passwords 
for its operations and internal administration tasks. These credentials are stored
in a Juju secret owned by the charm. Inspect the secret:

```shell
juju show-secret valkey-peers.valkey.app.internal_users_secret --reveal
```

```{caution}
This secret is only for internal use. It must not be updated by users.
```

To override the auto-generated passwords for the internal users, follow the instructions
of the next section.

## Configure a user-provided password

First, create a secret in `Juju` containing your password:

```shell
juju add-secret passwords charmed-operator=<NEW_PASSWORD>
```

You will get the `secret` ID as a response:

```text
secret:d6s4mr7mp25c765ucep0
```

Make note of the string following `secret:`.

Grant the secret to Charmed Valkey:

```shell
juju grant-secret passwords valkey
```

Configure the secret's URI as `system-users` credentials to Charmed Valkey:

```shell
juju config valkey system-users=secret:d6s4mr7mp25c765ucep0
```

Charmed Valkey will now apply the new password to its internal admin user. You can
check the progress by running `juju status`. After a few moments, the deployment will settle:

```text
Model     Controller      Cloud/Region        Version  SLA          Timestamp
tutorial  k8s-controller  microk8s/localhost  3.6.14   unsupported  19:28:26+01:00

App                       Version  Status  Scale  Charm                     Channel   Rev  Address         Exposed  Message
self-signed-certificates           active      1  self-signed-certificates  1/stable  586  10.152.183.111  no       
valkey                             active      3  valkey                    9/edge     11  10.152.183.123  no       

Unit                         Workload  Agent  Address      Ports  Message
self-signed-certificates/0*  active    idle   10.1.44.89          
valkey/0*                    active    idle   10.1.44.126         
valkey/1                     active    idle   10.1.44.117         
valkey/2                     active    idle   10.1.44.127         
```

Now you can use the password to access Valkey. Select the IP address for one of the units to connect to:

```shell
valkey-cli -h 10.1.44.126 -p 6379
```

Authenticate with the username and password you just configured:

```text
10.1.44.126:6379> AUTH charmed-operator <NEW_PASSWORD>
```

Check the current health of the server with this command:

```text
10.1.44.126:6379> ping
```

## Update the password

To update your user-configured password, simply update the value of the secret. Here's an example:

```shell
juju update-secret passwords charmed-operator=<MORE_SECURE_PASSWORD>
```

After running this command, Charmed Valkey will immediately update the password.
Once the deployment has settled to `active`/`idle` state again, you can no longer use
the old password to access Valkey. Instead, you will receive an error similar to this:

```text
(error) WRONGPASS invalid username-password pair or user is disabled.
```

Instead, use your updated password to authenticate:

```text
10.1.44.126:6379> AUTH charmed-operator moresecurepassword
```

## Handle multiple passwords

Charmed Valkey maintains multiple internal users with different permissions for
different scopes:

* `charmed-operator`: the user that manages the database instances
* `charmed-replication`: the user performs replication between primary and replica instances of Valkey
* `charmed-sentinel-operator`: the user that manages Sentinel for Valkey
* `charmed-sentinel-peers`: the user for communication between Sentinel instances
* `charmed-sentinel-valkey`: the user that Sentinel uses to connect to Valkey
* `charmed-stats`: the user for monitoring and observability

It is possible to manage the passwords for all of above's users with a Juju secret, 
or just for some of them.

To set the password for the `charmed-operator` and `charmed-sentinel-operator` users, 
but keeping the automatically generated passwords for all other users, run the following
command:

```shell
juju update-secret passwords charmed-operator=<MORE_SECURE_PASSWORD> charmed-sentinel-operator=<SENTINEL_PASSWORD>
```
