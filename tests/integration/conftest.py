# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from platform import machine

import jubilant
import pytest

from literals import Substrate
from tests.integration.helpers import are_apps_active_and_agents_idle

logger = logging.getLogger(__name__)

CW_RUNNER_NAME = "cw-runner"


@pytest.fixture
def cw_runner_charm(arch: str) -> str:
    """Path to the charm file to use for testing."""
    # Return str instead of pathlib.Path since python-libjuju's model.deploy(), juju deploy, and
    # juju bundle files expect local charms to begin with `./` or `/` to distinguish them from
    # Charmhub charms.
    return f"./tests/integration/clients/requirer-charm/requirer-charm_ubuntu@24.04-{arch}.charm"


@pytest.fixture(scope="function")
def c_writes(juju: jubilant.Juju, cw_runner_charm: str) -> None:
    """Deploy continous writes runner charm if not already deployed."""
    if CW_RUNNER_NAME not in juju.status().apps:
        juju.deploy(cw_runner_charm, app=CW_RUNNER_NAME)
        juju.wait(
            lambda status: are_apps_active_and_agents_idle(status, CW_RUNNER_NAME, idle_period=30),
            timeout=600,
            delay=5,
            successes=3,
        )


@pytest.fixture(scope="session")
def substrate(request) -> Substrate:
    """Substrate that we are testing."""
    return Substrate(request.config.option.substrate)


@pytest.fixture(scope="package")
def arch() -> str:
    """Fixture to provide the platform architecture for testing."""
    platforms = {
        "x86_64": "amd64",
        "aarch64": "arm64",
    }
    return platforms.get(machine(), "amd64")


@pytest.fixture
def charm(arch: str) -> str:
    """Path to the charm file to use for testing."""
    # Return str instead of pathlib.Path since python-libjuju's model.deploy(), juju deploy, and
    # juju bundle files expect local charms to begin with `./` or `/` to distinguish them from
    # Charmhub charms.
    return f"./valkey_ubuntu@24.04-{arch}.charm"


@pytest.fixture(scope="module")
def juju(arch: str):
    # `testing` is the default model created by concierge
    juju = jubilant.Juju(model="testing")
    juju.wait_timeout = 1000
    juju.cli("set-model-constraints", f"arch={arch}")
    yield juju
