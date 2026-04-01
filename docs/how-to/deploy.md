# How to deploy

This guide provides deployment instructions for Charmed Valkey.

## Prerequisites

The basic requirements for deploying a charm are the [**Juju client**](https://documentation.ubuntu.com/juju/3.6/) 
and a [**cloud**](https://juju.is/docs/juju/cloud).

## Setup

First, [bootstrap](https://juju.is/docs/juju/juju-bootstrap) the cloud controller
and create a [model](https://canonical-juju.readthedocs-hosted.com/en/latest/user/reference/model/):

```shell
juju bootstrap <cloud name> <controller name>
juju add-model <model name>
```

Then, use the [`juju deploy`](https://canonical-juju.readthedocs-hosted.com/en/latest/user/reference/juju-cli/list-of-juju-cli-commands/deploy/) command:

```shell
juju deploy valkey --channel 9/edge -n <number_of_replicas> --trust
```

If you are not sure where to start or would like a more guided walk through for
setting up your environment, see the {ref}`tutorial`.