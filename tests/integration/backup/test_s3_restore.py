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

import base64
import logging
import time
from datetime import datetime, timedelta, timezone

import jubilant
import pytest

from literals import CharmUsers, Substrate
from tests.integration.backup.test_s3_backup import (
    APP_NAME,
    BACKUP_ID_RE,
    S3_INTEGRATOR_APP,
    _wait_active,
)
from tests.integration.ha.helpers.helpers import (
    get_unit_name_from_primary_ip,
    send_process_control_signal,
)
from tests.integration.helpers import (
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


def _deploy_cluster_and_s3(juju: jubilant.Juju, microceph: dict) -> None:
    """Deploy the valkey cluster + s3-integrator and wire up the S3 relation.

    Idempotent: skips individual steps when the relevant apps / relations
    already exist (safe to call at the top of each test).
    """
    status = juju.status()

    if APP_NAME not in status.apps:
        juju.deploy(APP_NAME, channel="9/edge", num_units=3, trust=True, base="ubuntu@24.04")
    if S3_INTEGRATOR_APP not in status.apps:
        juju.deploy(S3_INTEGRATOR_APP, channel="latest/edge")

    # s3-integrator requires the CA chain base64-encoded in its tls-ca-chain config.
    ca_chain = base64.b64encode(microceph["tls-ca-chain"][0].encode()).decode()
    juju.config(
        S3_INTEGRATOR_APP,
        {
            "bucket": microceph["bucket"],
            "endpoint": microceph["endpoint"],
            "region": microceph["region"],
            "path": microceph["path"],
            "s3-uri-style": "path",
            "tls-ca-chain": ca_chain,
        },
    )

    # Credentials go through the sync-s3-credentials action, not config;
    # wait for the agent to settle before dispatching so the action is registered.
    juju.wait(
        lambda s: jubilant.all_agents_idle(s, S3_INTEGRATOR_APP),
        timeout=600,
    )
    juju.run(
        f"{S3_INTEGRATOR_APP}/0",
        "sync-s3-credentials",
        {
            "access-key": microceph["access-key"],
            "secret-key": microceph["secret-key"],
        },
    )
    _wait_active(juju, S3_INTEGRATOR_APP)

    # Only integrate when the S3 relation does not yet exist.
    try:
        juju.integrate(APP_NAME, S3_INTEGRATOR_APP)
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise
        logger.info("S3 relation already exists, skipping integrate")

    _wait_active(juju, APP_NAME, S3_INTEGRATOR_APP)


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

    The object's first bytes are ``b'CORRUPT'`` -- not ``b'REDIS'`` or
    ``b'VALKEY'`` -- so ``download_backup`` raises ``ValkeyRestoreError`` on
    the magic-byte check, triggering ``_restore_teardown`` without touching
    the live RDB.

    Uses a timestamp one hour in the past to avoid colliding with real backup
    ids that might be generated during the same test run.
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
    juju: jubilant.Juju,
    microceph: dict,
    s3_bucket,
    substrate: Substrate,
) -> None:
    """Write data -> backup -> mutate -> restore -> original value is back on all units."""
    _deploy_cluster_and_s3(juju, microceph)

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
    # The restore steps (DOWNLOAD → RESTORE → RESYNC → COMPLETED) are driven by
    # relation_changed / update_status hooks; _wait_active uses active workload
    # status + all agents idle as the convergence signal.
    _wait_active(juju, APP_NAME, timeout=1200)

    # Verify every unit has the pre-backup value.
    for unit_name in juju.status().apps[APP_NAME].units:
        got = _read_key(juju, unit_name, "restore_test_key")
        assert got == "original", f"Expected 'original' on {unit_name}, got {got!r}"


@pytest.mark.abort_on_fail
def test_restore_disaster_recovery(
    juju: jubilant.Juju,
    microceph: dict,
    s3_bucket,
    substrate: Substrate,
) -> None:
    """Remove the app entirely, redeploy a fresh cluster, restore from S3 -- data comes back."""
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
    _deploy_cluster_and_s3(juju, microceph)

    # Restore the pre-wipe snapshot.
    task = juju.run(f"{APP_NAME}/leader", "restore-backup", {"backup-id": backup_id})
    assert task.success, task.stderr
    assert "restore" in task.results, f"Unexpected action results: {task.results}"

    _wait_active(juju, APP_NAME, timeout=1200)

    got = _read_key(juju, _leader_unit_name(juju), "dr_key")
    assert got == "dr-value", f"Expected 'dr-value' after DR restore, got {got!r}"


@pytest.mark.abort_on_fail
def test_corrupt_restore_keeps_cluster_and_failover(
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

    # Allow the restore hooks (relation_changed-driven) to fire and settle.
    # The download step is fast (magic-byte check on a tiny object), so 15 s
    # is a comfortable head start before polling for agent idle.
    time.sleep(15)
    juju.wait(
        lambda s: jubilant.all_agents_idle(s, APP_NAME),
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
