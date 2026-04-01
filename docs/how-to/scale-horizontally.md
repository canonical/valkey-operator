# How to scale horizontally

Adding and removing units from a Valkey deployment is done by scaling [Juju units](https://juju.is/docs/juju/unit). 
 
## Add a unit

To add additional units to your deployed Valkey application, run the following command:

```shell
juju add-unit valkey -n 1
```

Where `-n 1` specifies the number of units to add.

You can now watch the new unit join the deployment with `watch juju status`.
It usually takes a few minutes for a unit to be added to an existing deployment.

```text
Model     Controller      Cloud/Region        Version  SLA          Timestamp
tutorial  k8s-controller  microk8s/localhost  3.6.14   unsupported  19:21:12+01:00

App                       Version  Status  Scale  Charm                     Channel   Rev  Address         Exposed  Message
self-signed-certificates           active      1  self-signed-certificates  1/stable  586  10.152.183.111  no       
valkey                             active      4  valkey                    9/edge     11  10.152.183.123  no       

Unit                         Workload  Agent  Address      Ports  Message
self-signed-certificates/0*  active    idle   10.1.44.89          
valkey/0*                    active    idle   10.1.44.126         
valkey/1                     active    idle   10.1.44.117         
valkey/2                     active    idle   10.1.44.127         
valkey/3                     active    idle   10.1.44.68          
```

### Remove units

Removing a unit from the application scales down the replicas. If you currently have
three units, one is the primary and two are replicas. Removing a unit will reduce the
number of replicas to one.

Before scaling down, list all the units with `juju status`:

* `valkey/0`
* `valkey/1`
* `valkey/2`
* `valkey/3`

To scale the application down to three units, run:

```shell
juju remove-unit valkey --num-units 1
```

Safely removing the unit will take a few moments. You’ll know that the unit was
successfully removed when `juju status` reports:

```text
Model     Controller      Cloud/Region        Version  SLA          Timestamp
tutorial  k8s-controller  microk8s/localhost  3.6.14   unsupported  19:23:44+01:00

App                       Version  Status  Scale  Charm                     Channel   Rev  Address         Exposed  Message
self-signed-certificates           active      1  self-signed-certificates  1/stable  586  10.152.183.111  no       
valkey                             active      3  valkey                    9/edge     11  10.152.183.123  no       

Unit                         Workload  Agent  Address      Ports  Message
self-signed-certificates/0*  active    idle   10.1.44.89          
valkey/0*                    active    idle   10.1.44.126         
valkey/1                     active    idle   10.1.44.117         
valkey/2                     active    idle   10.1.44.127         
```
