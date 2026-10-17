#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the persistence modes (DA290).

Run only with a bootstrapped Juju controller and built charms:
    tox run -e integration -- tests/integration/ha/test_persistence.py --substrate k8s
"""

import logging

import jubilant
from tenacity import Retrying, stop_after_delay, wait_fixed

from literals import (
    AOF_DIR_NAME,
    DATA_STORAGE_PATH,
    SNAP_COMMON_PATH,
    CharmUsers,
    Substrate,
)
from tests.integration.cw_helpers import (
    assert_continuous_writes_consistent,
    assert_continuous_writes_increasing,
    configure_cw_runner,
    start_continuous_writes,
    stop_continuous_writes,
)
from tests.integration.ha.helpers.helpers import (
    RESTART_DELAY_PATCHED,
    get_unit_name_from_primary_ip,
    patch_restart_delay,
    run_in_workload,
    send_process_control_signal,
)
from tests.integration.helpers import (
    APP_NAME,
    DEPLOY_TIMEOUT_S,
    GLIDE_RUNNER_NAME,
    IMAGE_RESOURCE,
    are_apps_active_and_agents_idle,
    exec_valkey_cli,
    existing_app,
    fast_forward,
    get_cluster_addresses,
    get_password,
    get_primary_ip,
    get_storage_id,
    ping,
    remove_number_units,
    wait_for_failover,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3


def _workload_dir(substrate: Substrate, storage_path: str) -> str:
    """Absolute path of a storage dir as the workload sees it."""
    if substrate == Substrate.VM:
        return f"/{SNAP_COMMON_PATH}/{storage_path}"
    return f"/{storage_path}"


def _wait_active(juju: jubilant.Juju) -> None:
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=DEPLOY_TIMEOUT_S,
    )


def _wait_for_ping(juju: jubilant.Juju, address: str) -> None:
    """Wait for Valkey itself, since the VM service restarts 20 s after a crash."""
    password = get_password(juju)
    for attempt in Retrying(stop=stop_after_delay(300), wait=wait_fixed(5), reraise=True):
        with attempt:
            assert ping(address, CharmUsers.VALKEY_ADMIN.value, password), f"{address} is down"


def _assert_aof_on_everywhere(juju: jubilant.Juju) -> None:
    password = get_password(juju)
    for address in get_cluster_addresses(juju, APP_NAME):
        info = exec_valkey_cli(
            address, CharmUsers.VALKEY_ADMIN.value, password, "info persistence"
        ).stdout
        assert "aof_enabled:1" in info, f"AOF is not on at {address}"
        assert "aof_rewrite_in_progress:0" in info, f"AOF rewrite still running at {address}"


def _assert_writes_consistent(juju: jubilant.Juju) -> None:
    assert_continuous_writes_increasing(juju)
    stats = stop_continuous_writes(juju)
    assert_continuous_writes_consistent(
        endpoints=get_cluster_addresses(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju),
        last_written_value=stats.last_written_value,
    )


def test_build_and_deploy(
    charm: str, juju: jubilant.Juju, substrate: Substrate, glide_runner_charm: str
) -> None:
    """Deploy three Valkey units and the continuous writes runner."""
    if existing_app(juju):
        return

    storage = None
    if substrate == Substrate.VM:
        # a detachable volume, so a new unit can reuse it
        juju.cli("create-storage-pool", "valkey-storage", "lxd")
        storage = {"data": "valkey-storage,2G"}
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
        storage=storage,
    )
    juju.deploy(glide_runner_charm, GLIDE_RUNNER_NAME)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, GLIDE_RUNNER_NAME, idle_period=30
        ),
        timeout=DEPLOY_TIMEOUT_S,
    )


def test_switch_rdb_to_aof_under_writes(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Switch a live cluster to AOF, restart the primary and lose no write."""
    configure_cw_runner(juju, valkey_app=APP_NAME, substrate=substrate)
    start_continuous_writes(juju, clear=True)

    juju.config(APP_NAME, {"persistence-mode": "AOF"})
    _wait_active(juju)
    for attempt in Retrying(stop=stop_after_delay(300), wait=wait_fixed(10), reraise=True):
        with attempt:
            _assert_aof_on_everywhere(juju)

    # the restart loads the AOF, an unsafe switch would bring the primary back empty
    primary_ip = get_primary_ip(juju, APP_NAME)
    primary_unit = get_unit_name_from_primary_ip(juju, primary_ip, substrate)
    send_process_control_signal(primary_unit, juju.model, "SIGTERM", "valkey-server", substrate)
    _wait_for_ping(juju, primary_ip)
    _wait_active(juju)

    _assert_writes_consistent(juju)


def test_corrupted_aof_on_primary_heals(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Corrupt the primary's AOF and crash it: failover, heal and full resync follow."""
    configure_cw_runner(juju, valkey_app=APP_NAME, substrate=substrate)
    start_continuous_writes(juju, clear=True)

    primary_ip = get_primary_ip(juju, APP_NAME)
    primary_unit = get_unit_name_from_primary_ip(juju, primary_ip, substrate)
    # restarts slower than Sentinel's down-after make the failover deterministic
    patch_restart_delay(
        juju, unit_name=primary_unit, delay=RESTART_DELAY_PATCHED, substrate=substrate
    )

    aof_dir = f"{_workload_dir(substrate, DATA_STORAGE_PATH)}/{AOF_DIR_NAME}"
    # a line without the '*' prefix is a format error that aof-load-truncated cannot recover
    run_in_workload(
        juju,
        primary_unit,
        f"f=$(ls {aof_dir}/*.incr.aof | sort -V | tail -n 1) && printf 'GARBAGE\\r\\n' >> \"$f\"",
        substrate,
    )
    send_process_control_signal(primary_unit, juju.model, "SIGKILL", "valkey-server", substrate)

    with fast_forward(juju):
        wait_for_failover(
            juju, APP_NAME, old_primary_ip=primary_ip, unit_count=NUM_UNITS, timeout=900
        )
        # the heal runs on a later update-status than the failover and deletes the bad AOF
        for attempt in Retrying(stop=stop_after_delay(600), wait=wait_fixed(10), reraise=True):
            with attempt:
                run_in_workload(
                    juju, primary_unit, f"! grep -qs GARBAGE {aof_dir}/*.incr.aof", substrate
                )
    patch_restart_delay(juju, unit_name=primary_unit, delay=None, substrate=substrate)
    _wait_active(juju)

    _assert_writes_consistent(juju)


def test_reused_storage_keeps_writes_after_the_last_save(
    juju: jubilant.Juju, substrate: Substrate
) -> None:
    """Writes after the removal's SAVE are only in the AOF, so a reused volume must load it."""
    juju.config(APP_NAME, {"persistence-mode": "AOF"})
    # a lone primary, so no failover can drop an acknowledged write
    remove_number_units(juju, APP_NAME, num_units=2, substrate=substrate)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=1, idle_period=30
        ),
        timeout=DEPLOY_TIMEOUT_S,
    )
    unit = next(iter(juju.status().get_units(APP_NAME)))
    storage_id = get_storage_id(juju, unit, "data")

    configure_cw_runner(juju, valkey_app=APP_NAME, substrate=substrate)
    start_continuous_writes(juju, clear=True, sleep_interval=0.05)
    assert_continuous_writes_increasing(juju)
    # writes keep coming while the removal saves dump.rdb and stops Valkey
    remove_number_units(juju, APP_NAME, num_units=1, substrate=substrate)
    juju.wait(lambda status: not status.get_units(APP_NAME), timeout=DEPLOY_TIMEOUT_S)
    stats = stop_continuous_writes(juju)

    juju.add_unit(APP_NAME, attach_storage=storage_id if substrate == Substrate.VM else None)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, unit_count=1, idle_period=30
        ),
        timeout=DEPLOY_TIMEOUT_S,
    )

    new_unit = next(iter(juju.status().get_units(APP_NAME)))
    log = juju.cli("debug-log", "--replay", "--no-tail", "--include", new_unit)
    assert "Loading the AOF on this storage" in log, f"{new_unit} did not load the AOF"
    assert_continuous_writes_consistent(
        endpoints=get_cluster_addresses(juju, APP_NAME),
        username=CharmUsers.VALKEY_ADMIN.value,
        password=get_password(juju),
        last_written_value=stats.last_written_value,
    )
