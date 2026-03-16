# How to disable TLS

To follow this guide, you need to have a running Charmed Valkey deployment with TLS
enabled. See [How to enable TLS](#enable-tls) for more information.

In general, to disable encryption with TLS, remove the relation between Valkey and 
the TLS provider on the client-certificates endpoint:

```text
Model     Controller      Cloud/Region        Version  SLA          Timestamp
tutorial  k8s-controller  microk8s/localhost  3.6.14   unsupported  19:06:13+01:00

App                       Version  Status  Scale  Charm                     Channel   Rev  Address         Exposed  Message
self-signed-certificates           active      1  self-signed-certificates  1/stable  586  10.152.183.111  no       
valkey                             active      3  valkey                    9/edge     11  10.152.183.123  no       

Unit                         Workload  Agent  Address      Ports  Message
self-signed-certificates/0*  active    idle   10.1.44.89          
valkey/0*                    active    idle   10.1.44.126         
valkey/1                     active    idle   10.1.44.117         
valkey/2                     active    idle   10.1.44.127         

Integration provider                   Requirer                    Interface         Type     Message
self-signed-certificates:certificates  valkey:client-certificates  tls-certificates  regular  
valkey:status-peers                    valkey:status-peers         status_peers      peer     
valkey:valkey-peers                    valkey:valkey-peers         valkey_peers      peer     
```

To disable the client-to-server communication, run:

```text
juju remove-relation valkey:client-certificates self-signed-certificates
```

After some time, you'll see that the relation between `self-signed-certificates` and Valkey
has been removed:

```text
Model     Controller      Cloud/Region        Version  SLA          Timestamp
tutorial  k8s-controller  microk8s/localhost  3.6.14   unsupported  19:08:08+01:00

App                       Version  Status  Scale  Charm                     Channel   Rev  Address         Exposed  Message
self-signed-certificates           active      1  self-signed-certificates  1/stable  586  10.152.183.111  no       
valkey                             active      3  valkey                    9/edge     11  10.152.183.123  no       

Unit                         Workload  Agent  Address      Ports  Message
self-signed-certificates/0*  active    idle   10.1.44.89          
valkey/0*                    active    idle   10.1.44.126         
valkey/1                     active    idle   10.1.44.117         
valkey/2                     active    idle   10.1.44.127         

Integration provider  Requirer             Interface     Type  Message
valkey:status-peers   valkey:status-peers  status_peers  peer  
valkey:valkey-peers   valkey:valkey-peers  valkey_peers  peer  
```

You have successfully disabled encryption with TLS for Valkey. You can verify that
the database is running without encryption by checking the `valkey-cli` command
without the `tls` directive:

```text
$ valkey-cli -h 10.1.44.126 -p 6379
10.1.44.126:6379> ping
(error) NOAUTH Authentication required.
```

Notice that the database is running without encryption for client connections only.
For internal peer-to-peer communication, Charmed Valkey always uses TLS by default.
