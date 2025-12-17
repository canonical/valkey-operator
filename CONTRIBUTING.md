# Contributing

To make contributions to this charm, you'll need a working [development setup](https://documentation.ubuntu.com/juju/3.6/howto/manage-your-juju-deployment/set-up-your-juju-deployment-local-testing-and-development/#set-things-up).

## Repository structure

This project is a monorepo, aiming at reusing shared code between the different flavors of the charmed Valkey operator.
Shared code should be located in the `common` directory and can be used as a "local Python package dependency".

The different operators, such as the valkey-operator for k8s, are located in separate directories and their 
`charmcraft.yaml` file is living in that directory. 

Code that is specific for a flavor of the charmed Valkey operator should not be added to the `common` directory, for
example event handlers that are specific for a Kubernetes or machine environment should only be implemented in the
respective charm itself.

### Using charmlibs in shared code

Without any adjustments, charmlibs can only be used in the charm's root directory, using `charmcraft fetch-lib` to add
or update a charmlib. In order to be able to use charmlibs as part of the shared code, the following workaround is put
in place (credits: https://github.com/canonical/mongo-single-kernel-library/blob/8/edge/single_kernel_mongo/charmcraft.yaml):

```shell
~/charmed-valkey-operator/common/common$ mv lib_fetch_charmcraft.yaml charmcraft.yaml
~/charmed-valkey-operator/common/common$ charmcraft fetch-lib charms.data_platform_libs.v1.data_interfaces
Library charms.data_platform_libs.v1.data_interfaces was already up to date in version 1.3.
~/charmed-valkey-operator/common/common$ mv charmcraft.yaml lib_fetch_charmcraft.yaml
```

This workaround needs to be applied every time when adding or updating a charmlib that is part of the shared code.

## Build the charm

Building the charms relies on copying the shared code to the charm's root directory, because charmcraft cannot handle code
that is outside of this directory. To achieve a seamless workflow for charm developers, the tool `charmcraftlocal` is
used. It can be invoked using `ccl` in the charm's root directory, for example:

```shell
~/charmed-valkey-operator/valkey-operator/kubernetes$ ccl pack
```

This will:
- search the charm's pyproject.toml for local Python dependencies
- copy them to the charm directory
- call `charmcraft pack`

As configured in `charmcraft.yaml`, the `charm-poetry` step of `charmcraft pack` will then replace the `common` package 
dependency with the adjusted path as part of the charm's root, making it available in the charm.

***Make sure to always use `ccl pack`. Using `charmcraft pack` directly will fail, because it does not copy the shared
code to the charm's root directory.***

For more information on the workflow, please refer to the documentation of https://pypi.org/project/charmcraftlocal/. 