#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""DA261 — 2-unit write-availability.

Verifies that small clusters stay available for writes: min-replicas-to-write
is only enabled at >= 3 units, so a 2-unit primary keeps accepting writes even
when its sole replica is unavailable (e.g. paused, or during a rolling
restart). This is the deliberate counterpart to the durability guarantee on
larger clusters.

See the DA261 design spec for the rationale.
"""

import logging
from time import sleep

import jubilant

from literals import CharmUsers, Substrate

from ..helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    are_apps_active_and_agents_idle,
    exec_valkey_cli,
    existing_app,
    get_password,
    get_primary_ip,
)
from .helpers.helpers import (
    get_unit_name_from_primary_ip,
    send_process_control_signal,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 2
# min-replicas-max-lag is 10s; wait past it so the replica is provably stale
# when we write, proving the lag gate does not freeze writes on 2-unit.
LAG_WAIT_SECONDS = 15
PROCESS_PATTERN = "valkey-server"


def test_build_and_deploy_two_units(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Deploy Valkey with exactly two units."""
    if app := existing_app(juju):
        logger.info(f"App {app} already exists, skipping deploy.")
        return

    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
    )
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )
    assert len(juju.status().apps[APP_NAME].units) == NUM_UNITS


def test_writes_available_when_sole_replica_lags(
    juju: jubilant.Juju, substrate: Substrate
) -> None:
    """Verify a 2-unit primary keeps accepting writes when its replica is down.

    With min-replicas-to-write=0 on 2 units, the primary must not refuse writes
    when the sole replica exceeds the lag threshold or disappears entirely.
    """
    model_full_name = juju.model
    admin_password = get_password(juju, CharmUsers.VALKEY_ADMIN)
    primary_ip = get_primary_ip(juju, APP_NAME)
    primary_unit = get_unit_name_from_primary_ip(juju, primary_ip, substrate)
    replica_unit = next(
        name for name in juju.status().apps[APP_NAME].units if name != primary_unit
    )

    logger.info("Baseline write to primary %s should succeed", primary_unit)
    out = exec_valkey_cli(
        hostname=primary_ip,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        command="SET da261_baseline ok",
    ).stdout
    assert "OK" in out, f"baseline write should succeed, got: {out!r}"

    logger.info("Pausing replica %s with SIGSTOP to induce lag without killing it", replica_unit)
    send_process_control_signal(
        unit_name=replica_unit,
        model_full_name=model_full_name,
        signal="STOP",
        db_process=PROCESS_PATTERN,
        substrate=substrate,
    )
    try:
        logger.info(
            "Waiting %ss so the paused replica is past min-replicas-max-lag", LAG_WAIT_SECONDS
        )
        sleep(LAG_WAIT_SECONDS)

        logger.info("Write should still succeed while the sole replica is paused")
        out = exec_valkey_cli(
            hostname=primary_ip,
            username=CharmUsers.VALKEY_ADMIN,
            password=admin_password,
            command="SET da261_during_lag ok",
        ).stdout
        assert "OK" in out, f"write should succeed on 2-unit despite paused replica, got: {out!r}"
        assert "NOREPLICAS" not in out, f"2-unit primary must not refuse writes, got: {out!r}"
    finally:
        logger.info("Resuming replica %s", replica_unit)
        send_process_control_signal(
            unit_name=replica_unit,
            model_full_name=model_full_name,
            signal="CONT",
            db_process=PROCESS_PATTERN,
            substrate=substrate,
        )

    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=10),
        timeout=120,
    )

    logger.info("Write should still succeed after the replica resumes")
    out = exec_valkey_cli(
        hostname=primary_ip,
        username=CharmUsers.VALKEY_ADMIN,
        password=admin_password,
        command="SET da261_after_recover ok",
    ).stdout
    assert "OK" in out, f"write should succeed after replica resumes, got: {out!r}"
