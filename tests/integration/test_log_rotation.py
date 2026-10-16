#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import jubilant
from tenacity import Retrying, stop_after_delay, wait_fixed

from literals import PRIMARY_NAME, TOPOLOGY_OBSERVER_LOG_FILENAME, CharmUsers, Substrate
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    are_apps_active_and_agents_idle,
    exec_valkey_cli,
    fast_forward,
    get_cluster_addresses,
    get_password,
)

logger = logging.getLogger(__name__)

UNIT = f"{APP_NAME}/0"
CHARM_DIR = f"/var/lib/juju/agents/unit-{APP_NAME}-0/charm"


def grow(path: str, size: str) -> str:
    """Return a shell command that appends ``size`` bytes to ``path``."""
    return f'head -c {size} /dev/zero | tr "\\0" x >> {path}'


def is_fresh(path: str) -> str:
    """Return a shell command that succeeds if ``path`` is under 1M."""
    return f"test $(stat -c %s {path}) -lt 1048576"


def test_build_and_deploy(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Build the charm-under-test and deploy it with one unit."""
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=1,
        trust=True,
    )
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )


def test_valkey_and_sentinel_logs_rotate(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Both logs rotate once they pass 200M and are recreated on the next write."""
    if substrate == Substrate.K8S:
        log_dir = "/var/log/valkey"
        # the container shell already runs as _daemon_ (rootless container)
        prefix, sudo, run_as = ["--container", "valkey"], "", ""
    else:
        log_dir = "/var/snap/valkey-charmed/common/var/log/valkey"
        # juju ssh lands as the ubuntu user, and the log dir is 0750 snap_daemon:root
        prefix, sudo, run_as = [], "sudo ", "sudo -u snap_daemon"
    logs = [f"{log_dir}/valkey.log", f"{log_dir}/sentinel.log"]

    logger.info("Growing %s past the 200M rotation threshold", logs)
    grow_logs = " && ".join(grow(log, "201M") for log in logs)
    juju.cli("ssh", *prefix, UNIT, f"{run_as} sh -c '{grow_logs}'")

    logger.info("Waiting for logrotate to rotate %s", logs)
    for attempt in Retrying(stop=stop_after_delay(180), wait=wait_fixed(10), reraise=True):
        with attempt:
            for log in logs:
                juju.cli("ssh", *prefix, UNIT, f"{sudo}test -f {log}.1")

    # BGSAVE and SENTINEL RESET make each process log a line, which recreates its file
    address = get_cluster_addresses(juju, APP_NAME)[0]
    exec_valkey_cli(address, CharmUsers.VALKEY_ADMIN.value, get_password(juju), "bgsave")
    exec_valkey_cli(
        address,
        CharmUsers.SENTINEL_CHARM_ADMIN.value,
        get_password(juju, user=CharmUsers.SENTINEL_CHARM_ADMIN),
        f"sentinel reset {PRIMARY_NAME}",
        sentinel=True,
    )
    for attempt in Retrying(stop=stop_after_delay(30), wait=wait_fixed(5), reraise=True):
        with attempt:
            for log in logs:
                juju.cli("ssh", *prefix, UNIT, f"{sudo}sh -c '{is_fresh(log)}'")


def test_topology_observer_log_rotates(juju: jubilant.Juju, substrate: Substrate) -> None:
    """The observer log rotates past 10M on the observer's next write."""
    log_file = f"{CHARM_DIR}/{TOPOLOGY_OBSERVER_LOG_FILENAME}"
    # K8s lands in the charm container as the charm user, the VM charm runs as root
    sudo = "" if substrate == Substrate.K8S else "sudo "

    # a running observer only tracks its own writes, so kill it and let update-status restart
    # it: the new process opens the grown file and rotates on its first line. The brackets keep
    # pkill from matching this shell.
    logger.info("Growing %s past 10M and stopping the observer", log_file)
    juju.cli(
        "ssh",
        UNIT,
        f"{sudo}sh -c '{grow(log_file, '11M')}' && {sudo}pkill -f '[t]opology_observer.py'",
    )

    with fast_forward(juju):
        for attempt in Retrying(stop=stop_after_delay(180), wait=wait_fixed(10), reraise=True):
            with attempt:
                juju.cli(
                    "ssh", UNIT, f"{sudo}sh -c 'test -f {log_file}.1 && {is_fresh(log_file)}'"
                )
