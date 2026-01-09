# Contributing

To make contributions to this charm, you'll need a working [development setup](https://documentation.ubuntu.com/juju/3.6/howto/manage-your-juju-deployment/set-up-your-juju-deployment-local-testing-and-development/#set-things-up).

### Testing

We use `tox` for linting and testing. Run `tox -e lint` to perform code checking.

To execute unit tests,run `tox -e unit`:

```shell
======================================================================================================================== test session starts =========================================================================================================================
platform linux -- Python 3.12.12, pytest-9.0.2, pluggy-1.6.0 -- /home/rene/repos/charmed-valkey-operator/.tox/unit/bin/python
cachedir: .tox/unit/.pytest_cache
rootdir: /home/rene/repos/charmed-valkey-operator
configfile: pyproject.toml
plugins: mock-3.15.1, asyncio-1.3.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collected 4 items                                                                                                                                                                                                                                                    

tests/unit/test_charm.py::test_pebble_ready_leader_unit PASSED
tests/unit/test_charm.py::test_pebble_ready_non_leader_unit PASSED
tests/unit/test_charm.py::test_update_status_leader_unit PASSED
tests/unit/test_charm.py::test_update_status_non_leader_unit PASSED

========================================================================================================================= 4 passed in 0.99s ==========================================================================================================================
unit: commands[1]> poetry run coverage report
Name                        Stmts   Miss Branch BrPart  Cover   Missing
-----------------------------------------------------------------------
src/charm.py                   34      1      6      1    95%   65
src/core/base_workload.py      11      3      0      0    73%   16, 21, 30
src/core/cluster_state.py      39     11      6      0    62%   49-52, 74, 87-101
src/core/models.py             53      9      6      2    81%   51-54, 64, 92, 97, 115-117, 122
src/events/base_events.py      16      2      4      0    80%   30-31
src/literals.py                 6      0      0      0   100%
src/managers/cluster.py        23      0      6      0   100%
src/managers/config.py         36      0      4      0   100%
src/statuses.py                 6      0      0      0   100%
src/workload.py                32      4      2      1    85%   24, 70-73
-----------------------------------------------------------------------
TOTAL                         256     30     34      4    86%
unit: commands[2]> poetry run coverage xml
Wrote XML report to coverage.xml
  unit: OK (5.05=setup[0.04]+cmd[1.50,2.38,0.57,0.57] seconds)
  congratulations :) (5.11 seconds)
```

## Build the charm

Simply run `charmcraft pack` in the repository root.

You can also use `charmcraftcache` if desired.

## Run the charm

Make sure you have prepared an environment for deploying the charm code, e.g. a `microk8s` cloud + controller bootstrapped
in Juju. For details, see [development setup](https://documentation.ubuntu.com/juju/3.6/howto/manage-your-juju-deployment/set-up-your-juju-deployment-local-testing-and-development/#set-things-up).

In our case, we want to deploy `valkey` to a model `test`. Use the `upstream-source` from `metadata.yaml`:
```shell
$ juju deploy ./valkey-k8s_ubuntu@24.04-amd64.charm -n 3 --resource valkey-image=ghcr.io/canonical/valkey:9.0.1-26.04-edge

$ juju status
Model  Controller      Cloud/Region        Version  SLA          Timestamp
test   k8s-controller  microk8s/localhost  3.6.12   unsupported  16:12:56Z 

App         Version  Status   Scale  Charm       Channel  Rev  Address        Exposed  Message
valkey-k8s           active       3  valkey-k8s             1  10.152.183.39  no       

Unit           Workload  Agent  Address      Ports  Message
valkey-k8s/0*  active    idle   10.1.142.30             
valkey-k8s/1   blocked   idle   10.1.142.32         Scaling Valkey is not implemented yet, service not started
valkey-k8s/2   blocked   idle   10.1.142.31         Scaling Valkey is not implemented yet, service not started
```