#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import jubilant

from literals import CharmUsers
from tests.integration.cw_helpers import (
    assert_continuous_writes_consistent,
    assert_continuous_writes_increasing,
)
from tests.integration.helpers import (
    APP_NAME,
    are_apps_active_and_agents_idle,
    get_cluster_hostnames,
    get_number_connected_slaves,
    get_password,
    seed_valkey,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
TEST_KEY = "test_key"
TEST_VALUE = "test_value"


def test_build_and_deploy(charm: str, juju: jubilant.Juju) -> None:
    """Build the charm-under-test and deploy it with three units."""
    juju.deploy(charm, num_units=1, trust=True)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )

    assert len(juju.status().apps[APP_NAME].units) == 1, (
        "Unexpected number of units after initial deploy"
    )


async def test_seed_data(juju: jubilant.Juju) -> None:
    """Seed some data to the cluster."""
    await seed_valkey(juju, target_gb=1)


async def test_scale_up(juju: jubilant.Juju, c_writes, c_writes_runner) -> None:
    """Make sure new units are added to the valkey downtime."""
    init_units_count = len(juju.status().apps[APP_NAME].units)

    # scale up
    juju.add_unit(APP_NAME, num_units=NUM_UNITS - init_units_count)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, idle_period=10, unit_count=NUM_UNITS
        ),
        timeout=1200,
    )
    num_units = len(juju.status().apps[APP_NAME].units)
    assert num_units == NUM_UNITS, f"Expected {NUM_UNITS} units, got {num_units}."

    # check if all units have been added to the cluster
    hostnames = get_cluster_hostnames(juju, APP_NAME)

    connected_slaves = await get_number_connected_slaves(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    assert connected_slaves == NUM_UNITS - 1, (
        f"Expected {NUM_UNITS - 1} connected slaves, got {connected_slaves}."
    )

    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
    logger.info("Stopping continuous writes after scale up test.")
    logger.info(await c_writes.async_stop())
    assert_continuous_writes_consistent(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
