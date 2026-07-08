#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for S3 restore against MicroCeph.

Three scenarios:
  1. rollback      – write → backup → mutate → restore → original value back on all units.
  2. disaster-recovery – write → backup → remove app → redeploy → restore → data back.
  3. corrupt-restore   – attempt restore of a corrupt S3 object → old data preserved →
                         Sentinel failover still works (suppression-leak regression guard).

Run only with a bootstrapped Juju controller and built charm:
    tox run -e integration -- tests/integration/backup/test_s3_restore.py --substrate k8s
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import jubilant
import pytest

from literals import CharmUsers, Substrate
from statuses import RestoreStatuses
from tests.integration.backup.helpers import BACKUP_ID_RE, deploy_and_relate_s3
from tests.integration.ha.helpers.helpers import (
    get_unit_name_from_primary_ip,
    send_process_control_signal,
)
from tests.integration.helpers import (
    APP_NAME,
    are_apps_active_and_agents_idle,
    does_status_match,
    exec_valkey_cli,
    get_password,
    get_primary_ip,
)

logger = logging.getLogger(__name__)

# valkey-server process name -- used with pkill for SIGKILL failover test.
_VALKEY_PROCESS = "valkey-server"

# Sentinel promotes a replica after down-after-milliseconds (30 000 ms default)
# plus election overhead.  90 s is a comfortable ceiling for the test host.
_FAILOVER_WAIT_S = 90


# ── helpers ──────────────────────────────────────────────────────────────────


def _wait_restore_active(juju: jubilant.Juju) -> None:
    """Wait for the valkey app to converge back to active/idle after a restore.

    The restore steps (RESTORE -> RESYNC -> COMPLETED) are driven by
    relation_changed / update_status hooks; active workloads + idle agents is the
    convergence signal. Generous timeout -- a restore restarts the primary and
    resyncs replicas.
    """
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME, idle_period=30),
        timeout=1200,
        delay=5,
        successes=3,
    )


def _write_key(juju: jubilant.Juju, key: str, value: str) -> None:
    """Write *key=value* to the Valkey primary via valkey-cli."""
    password = get_password(juju)
    primary_ip = get_primary_ip(juju, APP_NAME)
    exec_valkey_cli(
        primary_ip,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=password,
        command=f"SET {key} {value}",
    )


def _read_key(juju: jubilant.Juju, unit_name: str, key: str) -> str | None:
    """GET *key* from the named unit; returns None for missing keys."""
    status = juju.status()
    model_info = juju.show_model()
    unit = status.apps[APP_NAME].units[unit_name]
    # K8s: use pod IP; VM: use public address (mirrors get_cluster_addresses logic).
    address = unit.address if model_info.type == "kubernetes" else unit.public_address
    password = get_password(juju)
    result = exec_valkey_cli(
        address,
        username=CharmUsers.VALKEY_ADMIN.value,
        password=password,
        command=f"GET {key}",
    )
    return result.stdout if result.stdout else None


def _leader_unit_name(juju: jubilant.Juju) -> str:
    """Return the unit name of the current Juju leader for the valkey app."""
    for unit_name, unit in juju.status().apps[APP_NAME].units.items():
        if unit.is_leader:
            return unit_name
    raise ValueError(f"No leader found in app {APP_NAME}")


def upload_corrupt_backup(juju: jubilant.Juju, s3_bucket, microceph: dict) -> str:  # noqa: ARG001
    """Upload a corrupt (non-RDB) object to the S3 bucket; return its backup-id.

    Its bytes aren't the RDB magic, so download_backup fails the magic-byte check
    and tears down without touching the live RDB. The id is timestamped an hour
    back to avoid colliding with real backups made during the run.
    """
    backup_id = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    key = f"{microceph['path']}/{backup_id}"
    s3_bucket.put_object(Key=key, Body=b"CORRUPT_NOT_A_REAL_RDB\x00" * 16)
    logger.info("Uploaded corrupt object at S3 key=%s backup_id=%s", key, backup_id)
    return backup_id


def get_primary_unit(juju: jubilant.Juju, substrate: Substrate) -> str:
    """Return the unit name of the current Valkey primary node."""
    primary_ip = get_primary_ip(juju, APP_NAME)
    return get_unit_name_from_primary_ip(juju, primary_ip, substrate)


# ── tests ────────────────────────────────────────────────────────────────────


@pytest.mark.abort_on_fail
def test_restore_rollback(
    charm: str,
    juju: jubilant.Juju,
    microceph: dict,
    s3_bucket,
    substrate: Substrate,
) -> None:
    """Write data -> backup -> mutate -> restore -> original value is back on all units."""
    deploy_and_relate_s3(juju, charm, substrate, microceph)

    _write_key(juju, "restore_test_key", "original")

    task = juju.run(f"{APP_NAME}/leader", "create-backup")
    assert task.success, task.stderr
    backup_id = task.results["backup-id"]
    assert BACKUP_ID_RE.match(backup_id), f"Unexpected backup-id format: {backup_id!r}"

    # Overwrite the key so the restore has something visible to undo.
    _write_key(juju, "restore_test_key", "mutated")

    # restore-backup action initiates the async restore workflow (leader only).
    task = juju.run(f"{APP_NAME}/leader", "restore-backup", {"backup-id": backup_id})
    assert task.success, task.stderr
    assert "restore" in task.results, f"Unexpected action results: {task.results}"

    # Wait for the restore workflow to complete and the cluster to return to active.
    _wait_restore_active(juju)

    # Verify every unit has the pre-backup value.
    for unit_name in juju.status().apps[APP_NAME].units:
        got = _read_key(juju, unit_name, "restore_test_key")
        assert got == "original", f"Expected 'original' on {unit_name}, got {got!r}"


@pytest.mark.abort_on_fail
def test_restore_disaster_recovery(
    charm: str,
    juju: jubilant.Juju,
    microceph: dict,
    s3_bucket,
    substrate: Substrate,
) -> None:
    """Remove the app entirely, redeploy a fresh cluster, restore from S3 -- data comes back."""
    # Independently runnable: deploy the cluster + S3 wiring if a prior test did
    # not already leave them in place (deploy_and_relate_s3 is idempotent).
    deploy_and_relate_s3(juju, charm, substrate, microceph)

    _write_key(juju, "dr_key", "dr-value")

    task = juju.run(f"{APP_NAME}/leader", "create-backup")
    assert task.success, task.stderr
    backup_id = task.results["backup-id"]
    assert BACKUP_ID_RE.match(backup_id), f"Unexpected backup-id format: {backup_id!r}"

    # Simulate a catastrophic loss: remove the entire application.
    juju.remove_application(APP_NAME)
    juju.wait(lambda s: APP_NAME not in s.apps, timeout=600, delay=5)

    # Redeploy a blank 3-unit cluster and reconnect it to the existing S3 bucket
    # (the backup objects are still there).
    deploy_and_relate_s3(juju, charm, substrate, microceph)

    # Restore the pre-wipe snapshot.
    task = juju.run(f"{APP_NAME}/leader", "restore-backup", {"backup-id": backup_id})
    assert task.success, task.stderr
    assert "restore" in task.results, f"Unexpected action results: {task.results}"

    _wait_restore_active(juju)

    got = _read_key(juju, _leader_unit_name(juju), "dr_key")
    assert got == "dr-value", f"Expected 'dr-value' after DR restore, got {got!r}"


@pytest.mark.abort_on_fail
def test_corrupt_restore_keeps_cluster_and_failover(
    charm: str,
    juju: jubilant.Juju,
    microceph: dict,
    s3_bucket,
    substrate: Substrate,
) -> None:
    """A failed restore leaves old data intact and Sentinel failover still works.

    Regression guard for the suppression-leak bug: if suppress_failover() is
    not matched by resume_failover() on the _restore_teardown path, Sentinel
    will silently refuse to promote a replica after this test kills the primary
    process.
    """
    # Independently runnable (deploy_and_relate_s3 is idempotent).
    deploy_and_relate_s3(juju, charm, substrate, microceph)

    _write_key(juju, "safe_key", "safe-value")

    corrupt_id = upload_corrupt_backup(juju, s3_bucket, microceph)
    primary_before = get_primary_unit(juju, substrate)

    # Initiate a restore of the corrupt object.  The action itself succeeds
    # (the backup-id is present in S3 and matches _BACKUP_ID_RE), but the
    # async download step raises ValkeyRestoreError on the magic-byte check,
    # causing _restore_teardown() to call resume_failover() and set RESTORE_FAILED.
    task = juju.run(f"{APP_NAME}/leader", "restore-backup", {"backup-id": corrupt_id})
    assert task.success, task.stderr
    assert "restore" in task.results, f"Unexpected action results: {task.results}"

    # The corrupt object fails the magic-byte check in the restore step, so the
    # workflow tears down and the leader records RESTORE_FAILED on the app. Wait
    # on that status -- a real convergence signal -- rather than a bare sleep,
    # and settle the agents before probing data.
    juju.wait(
        lambda s: (
            does_status_match(
                s, expected_app_statuses={APP_NAME: [RestoreStatuses.RESTORE_FAILED.value]}
            )
            and jubilant.all_agents_idle(s, APP_NAME)
        ),
        timeout=600,
        delay=5,
    )

    # Old data must still be present (restore rolled back or never committed).
    got = _read_key(juju, _leader_unit_name(juju), "safe_key")
    assert got == "safe-value", f"Old data lost after corrupt restore; got {got!r}"

    # Verify that Sentinel failover suppression was resumed by _restore_teardown:
    # kill the primary valkey process and expect a replica to take over.
    send_process_control_signal(
        unit_name=primary_before,
        model_full_name=juju.model,
        signal="SIGKILL",
        db_process=_VALKEY_PROCESS,
        substrate=substrate,
    )

    # Wait for down-after-milliseconds (30 s) + election overhead.
    time.sleep(_FAILOVER_WAIT_S)

    primary_after = get_primary_unit(juju, substrate)
    assert primary_after != primary_before, (
        f"Primary did not change after killing {primary_before}; "
        "Sentinel failover suppression may not have been resumed on "
        "corrupt-restore teardown (suppression-leak regression)."
    )
