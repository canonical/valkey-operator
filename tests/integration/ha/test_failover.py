#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import logging

import jubilant
import pytest
from jubilant import Juju
from tenacity import Retrying, stop_after_attempt, wait_fixed

from literals import CharmUsers, Substrate
from tests.integration.cw_helpers import (
    assert_continuous_writes_consistent,
    assert_continuous_writes_increasing,
)
from tests.integration.ha.helpers.helpers import (
    get_unit_name_from_primary_ip,
    send_process_control_signal,
)

from ..helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    TLS_CHANNEL,
    TLS_NAME,
    are_apps_active_and_agents_idle,
    exec_valkey_cli,
    existing_app,
    get_cluster_hostnames,
    get_number_connected_replicas,
    get_password,
    get_primary_ip,
    ping,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
VM_RESTART_DELAY_DEFAULT = 20
K8S_RESTART_DELAY_DEFAULT = 5
VM_RESTART_DELAY_PATCHED = 120
FAILOVER_DELAY = 45
TEST_KEY = "test_key"
TEST_VALUE = "42"
VM_PROCESS_PATTERN = "/usr/bin/valkey-server"
K8S_PROCESS_PATTERN = "valkey-server"


@pytest.mark.parametrize("tls_enabled", [False, True], ids=["tls_off", "tls_on"])
def test_build_and_deploy(
    tls_enabled: bool, charm: str, juju: jubilant.Juju, substrate: Substrate
) -> None:
    """Build the charm-under-test and deploy it with three units."""
    if app := existing_app(juju):
        logger.info(f"App {app} already exists, skipping deploy.")
        return

    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
    )

    if tls_enabled:
        juju.deploy(TLS_NAME, channel=TLS_CHANNEL)
        juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )

    assert len(juju.status().apps[APP_NAME].units) == NUM_UNITS, (
        f"Unexpected number of units after initial deploy: expected {NUM_UNITS}, got {len(juju.status().apps[APP_NAME].units)}"
    )


async def test_kill_db_process_on_primary(
    juju: Juju, substrate: Substrate, c_writes, c_writes_async_clean
) -> None:
    """Make sure the cluster can self-heal when the leader goes down."""
    app_name = existing_app(juju) or APP_NAME

    # make sure we have at least two units so we can stop one of them
    init_units_count = len(juju.status().get_units(app_name))
    if init_units_count < 2:
        juju.add_unit(app_name, num_units=2 - init_units_count)
        juju.wait(
            lambda status: are_apps_active_and_agents_idle(
                status, app_name, idle_period=10, unit_count=2
            ),
            timeout=1200,
        )

    init_units_count = len(juju.status().get_units(app_name))
    c_writes.start()
    await asyncio.sleep(10)

    primary_ip = get_primary_ip(juju, app_name)
    assert primary_ip, "Failed to get primary endpoint from Juju status."

    # Cut the network to the primary unit
    logger.info("Axing away primary unit at %s", primary_ip)
    primary_unit_name = get_unit_name_from_primary_ip(juju, primary_ip, substrate)

    db_process_name = K8S_PROCESS_PATTERN if substrate == Substrate.K8S else VM_PROCESS_PATTERN

    # axe away the database process of the primary
    send_process_control_signal(
        unit_name=primary_unit_name,
        model_full_name=juju.model,
        signal="SIGKILL",
        db_process=db_process_name,
        substrate=substrate,
    )
    # We have 20s before systemd restarts the process
    # make sure the process is stopped
    admin_password = get_password(juju, CharmUsers.VALKEY_ADMIN)
    if substrate == Substrate.VM:
        # K8s restarts much faster so pinging to check will be very flakey
        logger.info("Pinging primary unit to ensure it's down.")
        assert not ping(primary_ip, CharmUsers.VALKEY_ADMIN, admin_password), (
            "Primary unit is still responding after SIGKILL."
        )

    # ensure the stopped unit was restarted
    logger.info("Waiting for primary unit to restart.")
    await asyncio.sleep(
        VM_RESTART_DELAY_DEFAULT if substrate == Substrate.VM else K8S_RESTART_DELAY_DEFAULT
    )
    assert ping(primary_ip, CharmUsers.VALKEY_ADMIN, admin_password), (
        "Primary unit is not responding after restart delay."
    )
    logger.info("Primary unit is available again.")

    logger.info("Checking number of connected replicas after primary restart.")
    hostnames = get_cluster_hostnames(juju, app_name)
    number_of_replicas = await get_number_connected_replicas(
        hostnames, CharmUsers.VALKEY_ADMIN, admin_password
    )
    assert number_of_replicas == init_units_count - 1, (
        f"Expected {init_units_count - 1} replicas to be connected after primary restart, got {number_of_replicas}"
    )

    # ensure data is written in the cluster
    logger.info("Checking continuous writes are increasing after primary restart.")
    await assert_continuous_writes_increasing(
        hostnames=hostnames, username=CharmUsers.VALKEY_ADMIN, password=admin_password
    )

    await c_writes.async_stop()

    assert_continuous_writes_consistent(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        ignore_count=True,  # we ignore count here as we know we will miss writes during primary down
    )


async def test_freeze_db_process_on_primary(
    juju: Juju, substrate: Substrate, c_writes, c_writes_async_clean
) -> None:
    """Make sure the cluster can self-heal when the leader goes down."""
    app_name = existing_app(juju) or APP_NAME
    hostnames = get_cluster_hostnames(juju, app_name)

    # make sure we have at least two units so we can stop one of them
    init_units_count = len(juju.status().get_units(app_name))
    if init_units_count < 2:
        juju.add_unit(app_name, num_units=2 - init_units_count)
        juju.wait(
            lambda status: are_apps_active_and_agents_idle(
                status, app_name, idle_period=10, unit_count=2
            ),
            timeout=1200,
        )

    init_units_count = len(juju.status().get_units(app_name))
    c_writes.start()
    await asyncio.sleep(10)

    primary_ip = get_primary_ip(juju, app_name)
    assert primary_ip, "Failed to get primary endpoint from Juju status."

    # Cut the network to the primary unit
    logger.info("Axing away primary unit at %s", primary_ip)
    primary_unit_name = get_unit_name_from_primary_ip(juju, primary_ip, substrate)

    db_process_name = K8S_PROCESS_PATTERN if substrate == Substrate.K8S else VM_PROCESS_PATTERN

    # axe away the database process of the primary
    send_process_control_signal(
        unit_name=primary_unit_name,
        model_full_name=juju.model,
        signal="SIGSTOP",
        db_process=db_process_name,
        substrate=substrate,
    )
    # make sure the process is stopped
    logger.info("Pinging primary unit to ensure it's down.")
    admin_password = get_password(juju, CharmUsers.VALKEY_ADMIN)
    assert not ping(primary_ip, CharmUsers.VALKEY_ADMIN, admin_password), (
        "Primary unit is still responding after SIGSTOP."
    )

    # ensure the stopped unit was restarted
    logger.info("Waiting for failover to happen.")
    await asyncio.sleep(FAILOVER_DELAY)

    new_primary_ip = get_primary_ip(juju, app_name)
    assert new_primary_ip != primary_ip, "Primary IP did not change after failover delay."
    logger.info("Failover successful, new primary is at %s", new_primary_ip)

    number_of_replicas = await get_number_connected_replicas(
        hostnames, CharmUsers.VALKEY_ADMIN, admin_password
    )
    assert number_of_replicas == init_units_count - 2, (
        f"Expected {init_units_count - 2} replicas to be connected, got {number_of_replicas}"
    )

    await assert_continuous_writes_increasing(
        hostnames=hostnames, username=CharmUsers.VALKEY_ADMIN, password=admin_password
    )

    send_process_control_signal(
        unit_name=primary_unit_name,
        model_full_name=juju.model,
        signal="SIGCONT",
        db_process=db_process_name,
        substrate=substrate,
    )

    # give time to the unit to start and sync with the other units
    # it will detect a failover happened and switch to be a replica
    for attempt in Retrying(stop=stop_after_attempt(10), wait=wait_fixed(5)):
        with attempt:
            if (
                "role:master"
                in exec_valkey_cli(
                    primary_ip, CharmUsers.VALKEY_ADMIN, admin_password, "info replication"
                ).stdout
            ):
                logger.warning(
                    "Unit is still primary after SIGCONT, waiting for unit to pick up on failover..."
                )
                raise Exception("Unit is still primary after SIGCONT.")
    assert ping(primary_ip, CharmUsers.VALKEY_ADMIN, admin_password), (
        "Old primary unit is not responding after SIGCONT."
    )
    logger.info("Old primary unit is available again.")

    logger.info("Checking number of connected replicas after primary restart.")
    number_of_replicas = await get_number_connected_replicas(
        hostnames, CharmUsers.VALKEY_ADMIN, admin_password
    )
    assert number_of_replicas == init_units_count - 1, (
        f"Expected {init_units_count - 1} replicas to be connected after primary restart, got {number_of_replicas}"
    )

    # ensure data is written in the cluster
    logger.info("Checking continuous writes are increasing after primary restart.")
    await assert_continuous_writes_increasing(
        hostnames=hostnames, username=CharmUsers.VALKEY_ADMIN, password=admin_password
    )

    await c_writes.async_stop()

    assert_continuous_writes_consistent(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        ignore_count=True,  # we ignore count here as we know we will miss writes during primary down
    )
