#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import jubilant
import pytest
from tenacity import Retrying, stop_after_delay, wait_fixed

from literals import (
    PRIMARY_NAME,
    TOPOLOGY_OBSERVER_LOGS_SERVICE,
    CharmUsers,
    Substrate,
)
from tests.integration.ha.helpers.helpers import reboot_unit
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    OBSERVER_LOG,
    are_apps_active_and_agents_idle,
    exec_valkey_cli,
    fast_forward,
    get_cluster_addresses,
    get_password,
    leader_unit_name,
)

logger = logging.getLogger(__name__)

UNIT = f"{APP_NAME}/0"


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
    log_file = OBSERVER_LOG
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


def test_topology_observer_log_survives_pod_restart(
    juju: jubilant.Juju, substrate: Substrate
) -> None:
    """The observer log lives on the logs storage, so a new pod keeps the old lines."""
    if substrate != Substrate.K8S:
        pytest.skip("Pod restarts are K8s-only")

    first_line = juju.ssh(UNIT, f"head -1 {OBSERVER_LOG}").strip()
    assert first_line, "the observer log is empty"

    logger.info("Deleting the pod of %s", UNIT)
    reboot_unit(juju, UNIT, substrate)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
    )

    assert first_line in juju.ssh(UNIT, f"cat {OBSERVER_LOG}").splitlines()


def test_topology_observer_follows_leadership(juju: jubilant.Juju, substrate: Substrate) -> None:
    """After a leader change, only the new leader runs the observer and forwards its log."""
    juju.add_unit(APP_NAME, num_units=2)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, idle_period=30, unit_count=3
        ),
        timeout=1200,
    )

    old_leader = leader_unit_name(juju)
    # SIGSTOP hangs the agent but keeps the observer running
    pkill = "sudo pkill -x jujud" if substrate == Substrate.VM else "pkill -x containeragent"
    # retry once, e.g. on K8s a liveness restart can beat the lease expiry
    for attempt in range(2):
        logger.info("Freezing the agent of %s, attempt %d", old_leader, attempt + 1)
        juju.ssh(old_leader, pkill.replace("pkill", "pkill -STOP"))
        try:
            for retry in Retrying(stop=stop_after_delay(300), wait=wait_fixed(10), reraise=True):
                with retry:
                    assert leader_unit_name(juju) != old_leader
            break
        except (AssertionError, ValueError):
            logger.info("Leadership stayed on %s", old_leader)
        finally:
            # a restarted charm container leaves nothing to thaw
            juju.ssh(old_leader, f"{pkill.replace('pkill', 'pkill -CONT')} || true")

    new_leader = leader_unit_name(juju)
    assert new_leader != old_leader, f"leadership never left {old_leader}"
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, idle_period=30, unit_count=3
        ),
        timeout=1200,
    )

    for unit, count in ((new_leader, "1"), (old_leader, "0")):
        # the brackets keep pgrep from matching this shell
        assert juju.ssh(unit, "pgrep -fc '[t]opology_observer.py' || true").strip() == count, unit
    sudo = "" if substrate == Substrate.K8S else "sudo "
    assert "Starting new observer" in juju.ssh(new_leader, f"{sudo}cat {OBSERVER_LOG}")

    if substrate == Substrate.K8S:
        for unit, expected in ((new_leader, "active"), (old_leader, "inactive")):
            services = juju.ssh(
                unit,
                "PEBBLE_SOCKET=/charm/containers/valkey/pebble.socket "
                f"/charm/bin/pebble services {TOPOLOGY_OBSERVER_LOGS_SERVICE}",
            )
            assert f" {expected} " in services, f"{unit}: {services}"
