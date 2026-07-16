#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests verifying rootless (non-root) operation of the K8s charm."""

import logging
from time import sleep

import jubilant
import pytest
from tenacity import Retrying, stop_after_attempt, wait_fixed

from literals import CharmUsers, Substrate
from tests.integration.ha.helpers.helpers import (
    RESTART_DELAY_PATCHED,
    get_unit_name_from_primary_ip,
    patch_restart_delay,
    send_process_control_signal,
)
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    TLS_CHANNEL,
    TLS_NAME,
    are_agents_idle,
    are_apps_active_and_agents_idle,
    download_client_certificate_from_unit,
    get_cluster_addresses,
    get_password,
    get_primary_ip,
    ping,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3
ROOTLESS_UID = 584792
PROCESS_PATTERN = "valkey-server"


def test_build_and_deploy(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Deploy 3-unit Valkey on K8s — skipped on VM."""
    if substrate != Substrate.K8S:
        pytest.skip("Rootless tests are K8s-only")

    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE,
        num_units=NUM_UNITS,
        trust=True,
    )
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=600,
        delay=5,
        successes=3,
    )


def test_charm_container_runs_as_non_root(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Charm container must not run as root and must have no sudo privileges."""
    if substrate != Substrate.K8S:
        pytest.skip("Rootless tests are K8s-only")

    for unit_name in juju.status().apps[APP_NAME].units:
        uid_result = juju.exec(command="id -u", unit=unit_name)
        uid = int(uid_result.stdout.strip())
        assert uid != 0, f"{unit_name} charm container is running as root (UID 0)"
        logger.info("%s charm container: UID %s (non-root confirmed)", unit_name, uid)

        # sudo -n exits non-zero when the user has no passwordless sudo access.
        sudo_result = juju.exec(command="sudo -n true 2>&1; echo exit:$?", unit=unit_name)
        exit_code = int(sudo_result.stdout.strip().rsplit("exit:", 1)[-1])
        assert exit_code != 0, (
            f"{unit_name} charm container user (UID {uid}) has unexpected sudo privileges"
        )
        logger.info(
            "%s charm container: sudo check exit=%s (no sudo confirmed)",
            unit_name,
            exit_code,
        )


def test_workload_container_runs_as_non_root(juju: jubilant.Juju, substrate: Substrate) -> None:
    """Valkey workload container must run as UID 584792 (_daemon_), not root."""
    if substrate != Substrate.K8S:
        pytest.skip("Rootless tests are K8s-only")

    for unit_name in juju.status().apps[APP_NAME].units:
        uid_str = juju.ssh(unit_name, "id -u", container="valkey")
        uid = int(uid_str)
        assert uid == ROOTLESS_UID, (
            f"{unit_name} workload container is running as UID {uid}, expected {ROOTLESS_UID}"
        )
        logger.info("%s workload container: UID %s (non-root confirmed)", unit_name, uid)


def test_topology_observer_log_with_tls(juju: jubilant.Juju, substrate: Substrate) -> None:
    """After enabling TLS the topology observer must write its log into the charm directory."""
    if substrate != Substrate.K8S:
        pytest.skip("Rootless tests are K8s-only")

    status = juju.status()
    if TLS_NAME not in status.apps:
        juju.deploy(TLS_NAME, channel=TLS_CHANNEL)
    tls_already_integrated = any(
        rel.related_app == TLS_NAME
        for rels in status.apps[APP_NAME].relations.values()
        for rel in rels
    )
    if not tls_already_integrated:
        juju.integrate(f"{APP_NAME}:client-certificates", TLS_NAME)
    juju.wait(
        lambda status: are_agents_idle(status, APP_NAME, idle_period=30),
        timeout=900,
    )

    units = juju.status().apps[APP_NAME].units
    unit_name = next(name for name, unit in units.items() if unit.leader)

    charm_dir_result = juju.exec(command="printenv JUJU_CHARM_DIR", unit=unit_name)
    charm_dir = charm_dir_result.stdout.strip()

    log_path = f"{charm_dir}/topology_observer.log"
    ca_path = f"{charm_dir}/valkey_ca.pem"

    # Give the topology observer time to start and write its first log line.
    for attempt in Retrying(stop=stop_after_attempt(10), wait=wait_fixed(6), reraise=True):
        with attempt:
            result = juju.exec(
                command=f"test -f {log_path} && echo exists",
                unit=unit_name,
            )
            assert result.stdout.strip() == "exists", (
                f"Topology observer log file {log_path} not found in charm container"
            )

    # Confirm the log has content (the observer started and wrote at least one line).
    result = juju.exec(command=f"wc -l < {log_path}", unit=unit_name)
    line_count = int(result.stdout.strip())
    assert line_count > 0, "Topology observer log file is empty — observer may not have started"
    logger.info("Topology observer log: %s line(s) written to %s", line_count, log_path)

    # Confirm the CA cert was written to the charm directory (not /tmp).
    result = juju.exec(command=f"test -f {ca_path} && echo exists", unit=unit_name)
    assert result.stdout.strip() == "exists", f"TLS CA cert {ca_path} not found in charm container"
    logger.info("TLS CA cert written to %s (non-privileged path confirmed)", ca_path)


def test_topology_changed_dispatched_after_failover(
    juju: jubilant.Juju, substrate: Substrate
) -> None:
    """After a primary failover the topology observer must dispatch a topology_changed event."""
    if substrate != Substrate.K8S:
        pytest.skip("Rootless tests are K8s-only")

    download_client_certificate_from_unit(juju, APP_NAME)
    primary_ip = get_primary_ip(juju, APP_NAME, tls_enabled=True)
    assert primary_ip, "Could not determine primary IP before failover"

    units = juju.status().apps[APP_NAME].units
    primary_unit = get_unit_name_from_primary_ip(juju, primary_ip, substrate)
    observer_unit = next(name for name, unit in units.items() if unit.leader)

    charm_dir_result = juju.exec(command="printenv JUJU_CHARM_DIR", unit=observer_unit)
    charm_dir = charm_dir_result.stdout.strip()
    log_path = f"{charm_dir}/topology_observer.log"

    # Record log line count before the failover.
    result = juju.exec(
        command=f"wc -l < {log_path} 2>/dev/null || echo 0",
        unit=observer_unit,
    )
    lines_before = int(result.stdout.strip())

    # Extend the restart delay so the primary stays down long enough for sentinel to elect.
    patch_restart_delay(
        juju=juju,
        unit_name=primary_unit,
        delay=RESTART_DELAY_PATCHED,
        substrate=substrate,
    )

    send_process_control_signal(
        unit_name=primary_unit,
        model_full_name=juju.model,
        signal="SIGKILL",
        db_process=PROCESS_PATTERN,
        substrate=substrate,
    )
    logger.info(
        "Killed primary unit %s at %s; waiting for sentinel failover",
        primary_unit,
        primary_ip,
    )

    # Wait for a new primary to be elected (sentinel typically takes ~30 s).
    sleep(35)

    addresses = get_cluster_addresses(juju, APP_NAME)
    new_primary_ip = get_primary_ip(
        juju,
        APP_NAME,
        tls_enabled=True,
        addresses=[ip for ip in addresses if ip != primary_ip],
    )
    assert new_primary_ip and new_primary_ip != primary_ip, (
        "Sentinel did not elect a new primary after killing the old one"
    )
    logger.info("New primary elected at %s", new_primary_ip)

    # The topology observer on a surviving unit should have logged the dispatch.
    for attempt in Retrying(stop=stop_after_attempt(10), wait=wait_fixed(5), reraise=True):
        with attempt:
            result = juju.exec(
                command=f"wc -l < {log_path} 2>/dev/null || echo 0",
                unit=observer_unit,
            )
            lines_after = int(result.stdout.strip())
            assert lines_after > lines_before, (
                f"Topology observer log did not grow after failover "
                f"(before={lines_before}, after={lines_after})"
            )

    # Confirm the dispatch keyword appears in the new log lines.
    result = juju.exec(
        command=f"tail -n +{lines_before + 1} {log_path}",
        unit=observer_unit,
    )
    assert "Primary change detected" in result.stdout, (
        "Expected 'Primary change detected' in observer log after failover, but found:\n"
        + result.stdout
    )
    logger.info("Primary change detected confirmed in observer log")

    # Restore the cluster to a healthy state before the next test.
    admin_password = get_password(juju, CharmUsers.VALKEY_ADMIN)
    for attempt in Retrying(stop=stop_after_attempt(20), wait=wait_fixed(10), reraise=True):
        with attempt:
            assert ping(primary_ip, CharmUsers.VALKEY_ADMIN, admin_password, tls_enabled=True), (
                f"Primary unit at {primary_ip} did not come back online"
            )
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=900,
    )
