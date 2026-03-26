#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import logging

import jubilant
import pytest
from tenacity import Retrying, stop_after_attempt, wait_fixed

from literals import CharmUsers, Substrate
from tests.integration.continuous_writes import ContinuousWrites
from tests.integration.cw_helpers import (
    assert_continuous_writes_consistent,
    assert_continuous_writes_increasing,
)
from tests.integration.ha.helpers.helpers import (
    K8S_RESTART_DELAY_DEFAULT,
    RESTART_DELAY_PATCHED,
    VM_RESTART_DELAY_DEFAULT,
    get_unit_name_from_primary_ip,
    patch_restart_delay,
    reboot_unit,
    send_process_control_signal,
)

from ..helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    TLS_CHANNEL,
    TLS_NAME,
    are_apps_active_and_agents_idle,
    download_client_certificate_from_unit,
    exec_valkey_cli,
    existing_app,
    get_cluster_hostnames,
    get_ip_from_unit,
    get_number_connected_replicas,
    get_password,
    get_primary_ip,
    ping,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
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


@pytest.mark.parametrize("tls_enabled", [False, True], ids=["tls_off", "tls_on"])
async def test_kill_db_process_on_primary(
    tls_enabled: bool,
    juju: jubilant.Juju,
    substrate: Substrate,
    c_writes: ContinuousWrites,
    c_writes_async_clean,
) -> None:
    """Make sure the cluster can self-heal when the leader goes down."""
    app_name = existing_app(juju) or APP_NAME
    if tls_enabled:
        download_client_certificate_from_unit(juju, APP_NAME)
        c_writes.tls_enabled = tls_enabled

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

    primary_ip = get_primary_ip(juju, app_name, tls_enabled=tls_enabled)
    assert primary_ip, "Failed to get primary endpoint from valkey."

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
        assert not ping(
            primary_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
        ), "Primary unit is still responding after SIGKILL."

    # ensure the stopped unit was restarted
    logger.info("Waiting for primary unit to restart.")
    await asyncio.sleep(
        VM_RESTART_DELAY_DEFAULT if substrate == Substrate.VM else K8S_RESTART_DELAY_DEFAULT
    )
    for attempt in Retrying(stop=stop_after_attempt(10), wait=wait_fixed(5), reraise=True):
        with attempt:
            assert ping(
                primary_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
            ), "Primary unit is not responding after restart delay."
            logger.info("Primary unit is available again.")

    logger.info("Checking number of connected replicas after primary restart.")
    hostnames = get_cluster_hostnames(juju, app_name)
    number_of_replicas = await get_number_connected_replicas(
        hostnames, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
    )
    assert number_of_replicas == init_units_count - 1, (
        f"Expected {init_units_count - 1} replicas to be connected after primary restart, got {number_of_replicas}"
    )

    # ensure data is written in the cluster
    logger.info("Checking continuous writes are increasing after primary restart.")
    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        tls_enabled=tls_enabled,
    )

    await c_writes.async_stop()

    assert_continuous_writes_consistent(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        ignore_count=True,  # we ignore count here as we know we will miss writes during primary down
        tls_enabled=tls_enabled,
    )


@pytest.mark.parametrize("tls_enabled", [False, True], ids=["tls_off", "tls_on"])
async def test_freeze_db_process_on_primary(
    tls_enabled: bool, juju: jubilant.Juju, substrate: Substrate, c_writes, c_writes_async_clean
) -> None:
    """Make sure the cluster can self-heal when the leader goes down."""
    app_name = existing_app(juju) or APP_NAME
    hostnames = get_cluster_hostnames(juju, app_name)
    if tls_enabled:
        download_client_certificate_from_unit(juju, APP_NAME)
        c_writes.tls_enabled = tls_enabled

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

    primary_ip = get_primary_ip(juju, app_name, tls_enabled=tls_enabled)
    assert primary_ip, "Failed to get primary endpoint from valkey."

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
    assert not ping(
        primary_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
    ), "Primary unit is still responding after SIGSTOP."

    # ensure the stopped unit was restarted
    logger.info("Waiting for failover to happen.")
    await asyncio.sleep(FAILOVER_DELAY)

    new_primary_ip = get_primary_ip(juju, app_name, tls_enabled=tls_enabled)
    assert new_primary_ip != primary_ip, "Primary IP did not change after failover delay."
    logger.info("Failover successful, new primary is at %s", new_primary_ip)

    number_of_replicas = await get_number_connected_replicas(
        hostnames, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
    )
    assert number_of_replicas == init_units_count - 2, (
        f"Expected {init_units_count - 2} replicas to be connected, got {number_of_replicas}"
    )

    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        tls_enabled=tls_enabled,
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
                    primary_ip,
                    CharmUsers.VALKEY_ADMIN,
                    admin_password,
                    "info replication",
                    tls_enabled=tls_enabled,
                ).stdout
            ):
                logger.warning(
                    "Unit is still primary after SIGCONT, waiting for unit to pick up on failover..."
                )
                raise Exception("Unit is still primary after SIGCONT.")
    assert ping(primary_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled), (
        "Old primary unit is not responding after SIGCONT."
    )
    logger.info("Old primary unit is available again.")

    logger.info("Checking number of connected replicas after primary restart.")
    number_of_replicas = await get_number_connected_replicas(
        hostnames, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
    )
    assert number_of_replicas == init_units_count - 1, (
        f"Expected {init_units_count - 1} replicas to be connected after primary restart, got {number_of_replicas}"
    )

    # ensure data is written in the cluster
    logger.info("Checking continuous writes are increasing after primary restart.")
    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        tls_enabled=tls_enabled,
    )

    await c_writes.async_stop()

    assert_continuous_writes_consistent(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        ignore_count=True,  # we ignore count here as we know we will miss writes during primary down
        tls_enabled=tls_enabled,
    )


@pytest.mark.parametrize("tls_enabled", [False, True], ids=["tls_off", "tls_on"])
async def test_full_cluster_restart(
    tls_enabled: bool, juju: jubilant.Juju, c_writes, c_writes_async_clean, substrate: Substrate
) -> None:
    """Make sure the cluster can self-heal after all members went down."""
    app_name = existing_app(juju) or APP_NAME
    if tls_enabled:
        download_client_certificate_from_unit(juju, APP_NAME)
        c_writes.tls_enabled = tls_enabled

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

    # update the restart delay for all units
    for unit in juju.status().get_units(app_name):
        patch_restart_delay(
            juju,
            unit_name=unit,
            delay=RESTART_DELAY_PATCHED,
            substrate=substrate,
        )

    db_process_name = K8S_PROCESS_PATTERN if substrate == Substrate.K8S else VM_PROCESS_PATTERN
    for unit in juju.status().get_units(app_name):
        send_process_control_signal(
            unit_name=unit,
            model_full_name=juju.model,
            signal="SIGTERM",
            db_process=db_process_name,
            substrate=substrate,
        )

    # make sure the process is stopped
    admin_password = get_password(juju, CharmUsers.VALKEY_ADMIN)
    for unit, unit_info in juju.status().get_units(app_name).items():
        unit_ip = unit_info.public_address if substrate == Substrate.VM else unit_info.address
        logger.info("Pinging %s to ensure it's down.", unit)
        assert not ping(
            unit_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
        ), f"{unit} still responding after SIGTERM."

    # ensure the stopped unit was restarted
    logger.info("Waiting for units to restart.")
    await asyncio.sleep(RESTART_DELAY_PATCHED + 10)

    for unit, unit_info in juju.status().get_units(app_name).items():
        unit_ip = unit_info.public_address if substrate == Substrate.VM else unit_info.address
        logger.info("Pinging %s to ensure it's up.", unit)
        assert ping(unit_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled), (
            f"{unit} is not responding after restart delay."
        )

    logger.info("All units are available again.")

    logger.info("Checking number of connected replicas after primary restart.")
    hostnames = get_cluster_hostnames(juju, app_name)
    number_of_replicas = await get_number_connected_replicas(
        hostnames, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
    )
    assert number_of_replicas == init_units_count - 1, (
        f"Expected {init_units_count - 1} replicas to be connected after primary restart, got {number_of_replicas}"
    )

    # ensure data is written in the cluster
    logger.info("Checking continuous writes are increasing after primary restart.")
    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        tls_enabled=tls_enabled,
    )

    await c_writes.async_stop()

    assert_continuous_writes_consistent(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        ignore_count=True,  # we ignore count here as we know we will miss writes during primary down
        tls_enabled=tls_enabled,
    )

    # reset the restart delay to the original value
    for unit in juju.status().get_units(app_name):
        patch_restart_delay(
            juju,
            unit_name=unit,
            delay=None,
            substrate=substrate,
        )


@pytest.mark.parametrize("tls_enabled", [False, True], ids=["tls_off", "tls_on"])
async def test_full_cluster_crash(
    tls_enabled: bool, juju: jubilant.Juju, c_writes, c_writes_async_clean, substrate: Substrate
) -> None:
    """Make sure the cluster can self-heal after all members went down."""
    app_name = existing_app(juju) or APP_NAME
    if tls_enabled:
        download_client_certificate_from_unit(juju, APP_NAME)
        c_writes.tls_enabled = tls_enabled

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

    # update the restart delay for all units
    for unit in juju.status().get_units(app_name):
        patch_restart_delay(
            juju,
            unit_name=unit,
            delay=RESTART_DELAY_PATCHED,
            substrate=substrate,
        )

    db_process_name = K8S_PROCESS_PATTERN if substrate == Substrate.K8S else VM_PROCESS_PATTERN
    for unit in juju.status().get_units(app_name):
        send_process_control_signal(
            unit_name=unit,
            model_full_name=juju.model,
            signal="SIGKILL",
            db_process=db_process_name,
            substrate=substrate,
        )

    # make sure the process is stopped
    admin_password = get_password(juju, CharmUsers.VALKEY_ADMIN)
    for unit, unit_info in juju.status().get_units(app_name).items():
        unit_ip = unit_info.public_address if substrate == Substrate.VM else unit_info.address
        logger.info("Pinging %s to ensure it's down.", unit)
        assert not ping(
            unit_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
        ), f"{unit} still responding after SIGKILL."

    # ensure the stopped unit was restarted
    logger.info("Waiting for units to restart.")
    await asyncio.sleep(RESTART_DELAY_PATCHED + 10)

    for unit, unit_info in juju.status().get_units(app_name).items():
        unit_ip = unit_info.public_address if substrate == Substrate.VM else unit_info.address
        logger.info("Pinging %s to ensure it's up.", unit)
        assert ping(unit_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled), (
            f"{unit} is not responding after restart delay."
        )

    logger.info("All units are available again.")

    logger.info("Checking number of connected replicas after primary restart.")
    hostnames = get_cluster_hostnames(juju, app_name)
    number_of_replicas = await get_number_connected_replicas(
        hostnames, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
    )
    assert number_of_replicas == init_units_count - 1, (
        f"Expected {init_units_count - 1} replicas to be connected after primary restart, got {number_of_replicas}"
    )

    # ensure data is written in the cluster
    logger.info("Checking continuous writes are increasing after primary restart.")
    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        tls_enabled=tls_enabled,
    )

    await c_writes.async_stop()

    assert_continuous_writes_consistent(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        ignore_count=True,  # we ignore count here as we know we will miss writes during primary down
        tls_enabled=tls_enabled,
    )

    # reset the restart delay to the original value
    for unit in juju.status().get_units(app_name):
        patch_restart_delay(
            juju,
            unit_name=unit,
            delay=None,
            substrate=substrate,
        )


@pytest.mark.parametrize("tls_enabled", [False, True], ids=["tls_off", "tls_on"])
async def test_reboot_primary(
    tls_enabled: bool, juju: jubilant.Juju, c_writes, c_writes_async_clean, substrate: Substrate
) -> None:
    """Make sure the cluster can self-heal when the leader goes down."""
    app_name = existing_app(juju) or APP_NAME
    if tls_enabled:
        download_client_certificate_from_unit(juju, APP_NAME)
        c_writes.tls_enabled = tls_enabled

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
    await c_writes.async_clear()
    c_writes.start()
    await asyncio.sleep(10)

    primary_ip = get_primary_ip(juju, app_name, tls_enabled=tls_enabled)
    assert primary_ip, "Failed to get primary endpoint from valkey."

    # Reboot the primary unit
    logger.info("Rebooting primary unit at %s", primary_ip)
    primary_unit_name = get_unit_name_from_primary_ip(juju, primary_ip, substrate)

    reboot_unit(juju, primary_unit_name, substrate)

    # wait for unit to reboot
    await asyncio.sleep(3)

    # make sure the process is stopped
    admin_password = get_password(juju, CharmUsers.VALKEY_ADMIN)
    logger.info("Pinging primary unit to ensure it's down.")
    assert not ping(
        primary_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
    ), "Primary unit is still responding after reboot."

    logger.info("Waiting for primary unit to reboot and become available.")
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, app_name, idle_period=30, unit_count=init_units_count
        ),
        timeout=1200,
    )

    c_writes.update()

    # on k8s we get a new ip
    new_ip = get_ip_from_unit(juju, primary_unit_name)
    assert ping(new_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled), (
        "Primary unit is not responding after reboot."
    )

    number_of_replicas = await get_number_connected_replicas(
        get_cluster_hostnames(juju, app_name),
        CharmUsers.VALKEY_ADMIN,
        admin_password,
        tls_enabled=tls_enabled,
    )
    assert number_of_replicas == init_units_count - 1, (
        f"Expected {init_units_count - 1} replicas to be connected, got {number_of_replicas}"
    )

    await assert_continuous_writes_increasing(
        hostnames=get_cluster_hostnames(juju, app_name),
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        tls_enabled=tls_enabled,
    )

    await c_writes.async_stop()

    assert_continuous_writes_consistent(
        hostnames=get_cluster_hostnames(juju, app_name),
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        tls_enabled=tls_enabled,
        ignore_count=True,  # we ignore count here as we know we will miss writes during primary down
    )


@pytest.mark.parametrize("tls_enabled", [False, True], ids=["tls_off", "tls_on"])
async def test_full_cluster_reboot(
    tls_enabled: bool, juju: jubilant.Juju, c_writes, c_writes_async_clean, substrate: Substrate
) -> None:
    """Make sure the cluster can self-heal after all members went down."""
    app_name = existing_app(juju) or APP_NAME
    if tls_enabled:
        download_client_certificate_from_unit(juju, APP_NAME)
        c_writes.tls_enabled = tls_enabled

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

    for unit in juju.status().get_units(app_name):
        reboot_unit(juju, unit, substrate)

    await asyncio.sleep(3)

    # make sure the process is stopped
    admin_password = get_password(juju, CharmUsers.VALKEY_ADMIN)
    for unit, unit_info in juju.status().get_units(app_name).items():
        unit_ip = unit_info.public_address if substrate == Substrate.VM else unit_info.address
        logger.info("Pinging %s to ensure it's down.", unit)
        assert not ping(
            unit_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
        ), f"{unit} still responding after reboot."

    # ensure the stopped unit was restarted
    logger.info("Waiting for cluster to become available.")
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, app_name, idle_period=30, unit_count=init_units_count
        ),
        timeout=1200,
    )

    c_writes.update()

    for unit, unit_info in juju.status().get_units(app_name).items():
        unit_ip = unit_info.public_address if substrate == Substrate.VM else unit_info.address
        logger.info("Pinging %s to ensure it's up.", unit)
        assert ping(unit_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled), (
            f"{unit} is not responding after restart delay."
        )

    logger.info("All units are available again.")

    logger.info("Checking number of connected replicas after primary restart.")
    hostnames = get_cluster_hostnames(juju, app_name)
    number_of_replicas = await get_number_connected_replicas(
        hostnames, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=tls_enabled
    )
    assert number_of_replicas == init_units_count - 1, (
        f"Expected {init_units_count - 1} replicas to be connected after primary restart, got {number_of_replicas}"
    )

    # ensure data is written in the cluster
    logger.info("Checking continuous writes are increasing after primary restart.")
    await assert_continuous_writes_increasing(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        tls_enabled=tls_enabled,
    )

    await c_writes.async_stop()

    assert_continuous_writes_consistent(
        hostnames=hostnames,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        tls_enabled=tls_enabled,
        ignore_count=True,  # we ignore count here as we know we will miss writes during primary down
    )
