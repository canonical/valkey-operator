# Contributing

To make contributions to this charm, you'll need a working [development setup](https://documentation.ubuntu.com/juju/3.6/howto/manage-your-juju-deployment/set-up-your-juju-deployment-local-testing-and-development/#set-things-up).

## Pull requests

- The default branch is `9/edge`. All changes land via pull requests, which are squash-merged with
  conventional-commit subjects (`type(scope): summary`, e.g. `fix(tls): ...`, `test(ha): ...`).
- You must have signed the [Canonical Contributor Licence Agreement](https://ubuntu.com/legal/contributors);
  a CLA check runs on every pull request.
- **Merging to `9/edge` releases**: every push to `9/edge` (except docs-only changes) runs the full
  CI suite and then publishes the charm to Charmhub track `9`, channel `edge`, tagging the commit
  `rev<N>`.

## Testing

We use `tox` for linting and testing:

```shell
tox run -e lint      # ruff check + ruff format --check + codespell + shellcheck
tox run -e static    # pyright static type checks
tox run -e format    # auto-fix style; also refreshes poetry.lock
tox run -e unit      # unit tests (pytest + ops Scenario) with coverage
```

Integration tests need a bootstrapped Juju controller (microk8s or lxd), a built charm in the
repository root, and the built requirer charm (`charmcraft pack` inside
`tests/integration/clients/requirer-charm/`):

```shell
tox run -e integration -- tests/integration/test_charm.py --substrate k8s   # or vm
```

Locally, the integration env creates a Juju model named `testing`; remove it
(`juju destroy-model testing`) before re-running.

## Build the charm

Simply run `charmcraft pack` in the repository root.

You can also use `charmcraftcache` if desired.

## Run the charm

Make sure you have prepared an environment for deploying the charm code, e.g. a `microk8s` cloud + controller bootstrapped
in Juju. For details, see [development setup](https://documentation.ubuntu.com/juju/3.6/howto/manage-your-juju-deployment/set-up-your-juju-deployment-local-testing-and-development/#set-things-up).

Deploy with `--trust` (mandatory) and, on Kubernetes, the image resource using the
`upstream-source` value from `metadata.yaml`:

```shell
juju deploy ./valkey_ubuntu@24.04-amd64.charm -n 3 \
  --resource valkey-image=<upstream-source from metadata.yaml> --trust
```

If you deploy `valkey` on a VM cloud, you don't need to specify the image resource.
