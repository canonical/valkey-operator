#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import jubilant
import valkey

from literals import CharmUsers

from ..helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    are_apps_active_and_agents_idle,
    get_cluster_hostnames,
    get_password,
)
from .helpers import (
    assert_continuous_writes_consistent,
    assert_continuous_writes_increasing,
    start_continuous_writes,
    stop_continuous_writes,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
TEST_KEY = "test_key"
TEST_VALUE = "test_value"


def test_build_and_deploy(charm: str, juju: jubilant.Juju) -> None:
    """Build the charm-under-test and deploy it with three units."""
    juju.deploy(charm, resources=IMAGE_RESOURCE, num_units=1)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )

    assert len(juju.status().apps[APP_NAME].units) == 1, (
        "Unexpected number of units after initial deploy"
    )


async def test_scale_up(juju: jubilant.Juju) -> None:
    """Make sure new units are added to the valkey downtime."""
    init_units_count = len(juju.status().apps[APP_NAME].units)
    init_endpoints = ",".join(get_cluster_hostnames(juju, APP_NAME))
    # start writing data to the cluster
    start_continuous_writes(
        endpoints=init_endpoints,
        valkey_user=CharmUsers.VALKEY_ADMIN.value,
        valkey_password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        sentinel_user=CharmUsers.SENTINEL_ADMIN.value,
        sentinel_password=get_password(juju, user=CharmUsers.SENTINEL_ADMIN),
    )

    # scale up
    juju.add_unit(APP_NAME, num_units=2)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, idle_period=10, unit_count=init_units_count + 2
        ),
        timeout=1200,
    )
    num_units = len(juju.status().apps[APP_NAME].units)
    assert num_units == init_units_count + 2, (
        f"Expected {init_units_count + 2} units, got {num_units}."
    )

    # check if all units have been added to the cluster
    endpoints = ",".join(get_cluster_hostnames(juju, APP_NAME))

    sentinel_client = valkey.Sentinel(
        [(host, 26379) for host in endpoints.split(",")],
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        sentinel_kwargs={
            "password": get_password(juju, user=CharmUsers.SENTINEL_ADMIN),
            "username": CharmUsers.SENTINEL_ADMIN.value,
        },
    )
    master = sentinel_client.master_for("primary")
    info = master.info("replication")
    connected_slaves = info.get("connected_slaves", 0)
    assert connected_slaves == num_units - 1, (
        f"Expected {num_units - 1} connected slaves, got {connected_slaves}."
    )

    assert_continuous_writes_increasing(
        endpoints=endpoints,
        valkey_user=CharmUsers.VALKEY_ADMIN.value,
        valkey_password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
        sentinel_user=CharmUsers.SENTINEL_ADMIN.value,
        sentinel_password=get_password(juju, user=CharmUsers.SENTINEL_ADMIN),
    )
    stop_continuous_writes()
    assert_continuous_writes_consistent(
        endpoints=endpoints,
        valkey_user=CharmUsers.VALKEY_ADMIN.value,
        valkey_password=get_password(juju, user=CharmUsers.VALKEY_ADMIN),
    )
