#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import jubilant
import pytest

from .helpers import APP_NAME, IMAGE_RESOURCE, CharmStatuses, does_status_match

logger = logging.getLogger(__name__)

NUM_UNITS = 3


@pytest.mark.abort_on_fail
def test_build_and_deploy(charm: str, juju: jubilant.Juju) -> None:
    """Build the charm-under-test and deploy it with three units."""
    juju.deploy(charm, resources=IMAGE_RESOURCE, num_units=NUM_UNITS)
    juju.wait(
        lambda status: does_status_match(
            status,
            expected_app_statuses={APP_NAME: [CharmStatuses.SCALING_NOT_IMPLEMENTED.value]},
        ),
        timeout=600,
    )
