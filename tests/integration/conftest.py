# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import jubilant
import pytest

from tests.integration.continuous_writes import ContinuousWrites
from tests.integration.helpers import APP_NAME

logger = logging.getLogger(__name__)


@pytest.fixture(scope="function")
def c_writes(juju: jubilant.Juju):
    """Create instance of the ContinuousWrites."""
    app = APP_NAME
    logger.debug(f"Creating ContinuousWrites instance for app with name {app}")
    return ContinuousWrites(juju, app, log_written_values=True)


@pytest.fixture(scope="function")
def c_writes_runner(juju: jubilant.Juju, c_writes: ContinuousWrites):
    """Start continuous write operations and clears writes at the end of the test."""
    c_writes.start()
    yield
    logger.info("Clearing continuous writes after test completion")
    logger.info(c_writes.clear())
