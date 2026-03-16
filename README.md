## Valkey operator
[![CharmHub Badge](https://charmhub.io/valkey/badge.svg)](https://charmhub.io/valkey)
[![docs badge](https://canonical-charmed-valkey.readthedocs-hosted.com/en/latest/)](https://canonical-charmed-valkey.readthedocs-hosted.com/)

Charmed Valkey is an open-source Juju charm that will automate the deployment, 
scaling, configuration and operations of Valkey databases across clouds, virtual
machines and bare metal, using the Juju orchestration framework.

[Valkey](https://valkey.io) is a community-driven, open-source, high-performance
key-value data store compatible with Redis® clients and ecosystem tooling.

The charm can be deployed on Kubernetes and VM clouds and aims to simplify Valkey
operations from Day 0 to Day 2, offering secure defaults integration interfaces,
and lifecycle automation.

## Basic usage

Bootstrap a [MicroK8s controller](https://documentation.ubuntu.com/juju/3.6/tutorial/#set-up-a-juju-controller)
and create a new Juju model:

```shell
juju add-model sample-model
```

To deploy a single unit of Valkey, run the following command:

```shell
juju deploy valkey --channel 9/edge --trust
```

To deploy Valkey with multiple units, specify the number of desired units with the `-n` option:

```shell
juju deploy valkey -n 3 --channel 9/edge --trust
```

Valkey can be scaled out using the `juju add-unit` command:

```shell
juju add-unit valkey -n <num_of_desired_units>
```

For example, to scale a deployment with three Valkey units to five, run:

```shell
juju add-unit valkey -n 2
```

Even when scaling multiple units at the same time, the charmed operator uses a rolling restart 
sequence to make sure the cluster stays available and healthy during the operation.

## Download details

Charmed Valkey is shipped in the track `9/edge`: [Valkey 9/edge](https://charmhub.io/valkey?channel=9/edge)

It is based on the following platform:
- Noble (Ubuntu 24.04)
- Supported architectures: `amd64`.

## Documentation

The [charmed Valkey documentation](https://canonical-charmed-valkey.readthedocs-hosted.com) provides a 
tutorial for basic usage, multiple how-to guides about operational topics, and detailed 
information about supported interfaces and integrations.

## Community and support

The charmed Valkey operator is an open-source project that welcomes community contributions, suggestions,
fixes and constructive feedback.

- Report [issues](https://github.com/canonical/valkey-operator/issues)
- [Contact us on Matrix](https://matrix.to/#/#charmhub-data-platform:ubuntu.com)
- Explore [Canonical Data & AI solutions](https://canonical.com/data)

Charmed Valkey is covered by the [Ubuntu Code of
Conduct](https://ubuntu.com/community/ethos/code-of-conduct).

## Contributing

Please see the [Juju docs](https://documentation.ubuntu.com/juju/3.6/howto/manage-applications/) for 
guidelines and best practices, and the [contribution guide](CONTRIBUTING.md) for developer guidance.

## License and copyright

Charmed Valkey is free software, distributed under the Apache Software License, version 2.0. 
See [LICENSE](LICENSE) for more information.
