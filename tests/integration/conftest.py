# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from platform import machine

import jubilant
import pytest

from literals import Substrate
from tests.integration.continuous_writes import ContinuousWrites
from tests.integration.helpers import APP_NAME

logger = logging.getLogger(__name__)


@pytest.fixture(scope="function")
def c_writes(juju: jubilant.Juju):
    """Create instance of the ContinuousWrites."""
    app = APP_NAME
    logger.info(f"Creating ContinuousWrites instance for app with name {app}")
    return ContinuousWrites(juju, app)


@pytest.fixture(scope="function")
def c_writes_runner(juju: jubilant.Juju, c_writes: ContinuousWrites):
    """Start continuous write operations and clears writes at the end of the test."""
    c_writes.start()
    yield
    logger.info("Clearing continuous writes after test completion")
    logger.info(c_writes.clear())


@pytest.fixture(scope="function")
async def c_writes_async_clean(c_writes: ContinuousWrites):
    """Clear continuous write operations at the end of the test."""
    yield
    logger.info("Clearing continuous writes after test completion")
    logger.info(await c_writes.async_clear())


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
