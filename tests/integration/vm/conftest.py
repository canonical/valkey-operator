# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from platform import machine

import jubilant
import pytest

logger = logging.getLogger(__name__)


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
