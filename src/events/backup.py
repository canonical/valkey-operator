#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Juju event wiring for S3 backups."""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING

import ops
from botocore.exceptions import ClientError
from object_storage import (
    S3Requirer,
    StorageConnectionInfoChangedEvent,
    StorageConnectionInfoGoneEvent,
)
from pydantic import ValidationError

from common.exceptions import (
    ValkeyBackupError,
    ValkeyCannotGetPrimaryIPError,
    ValkeyClusterNotReadyError,
    ValkeyWorkloadCommandError,
)
from core.models import S3Parameters
from literals import (
    PEER_RELATION,
    RESTORE_LOAD_TIMEOUT_S,
    RESTORE_RESYNC_TIMEOUT_S,
    S3_RELATION_NAME,
    RestoreFailure,
    RestoreStep,
)
from statuses import BackupStatuses, RestoreStatuses

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


def _safe_error(exc: ValkeyBackupError) -> str:
    """Return an action-safe error string.

    Action results are world-readable, so surface only the object-storage error
    code (e.g. "AccessDenied"); the detail goes to the unit log.
    """
    cause = exc.__cause__ or (exc.args[0] if exc.args else None)
    if isinstance(cause, ClientError):
        code = cause.response.get("Error", {}).get("Code", "")
        if code:
            return f"S3 request failed: {code}"
    return "Backup operation failed. See juju debug-log on this unit for details."


class BackupEvents(ops.Object):
    """Backup-related Juju event observers."""

    def __init__(self, charm: "ValkeyCharm"):
        super().__init__(charm, key="backup")
        self.charm = charm
        self.s3_requirer = S3Requirer(self.charm, S3_RELATION_NAME)

        self.framework.observe(
            self.s3_requirer.on.storage_connection_info_changed, self._on_s3_credentials_changed
        )
        self.framework.observe(
            self.s3_requirer.on.storage_connection_info_gone, self._on_s3_credentials_gone
        )
        # Recover credentials when leadership moves.
        self.framework.observe(self.charm.on.leader_elected, self._on_s3_credentials_changed)
        self.framework.observe(self.charm.on.create_backup_action, self._on_create_backup_action)
        self.framework.observe(self.charm.on.list_backups_action, self._on_list_backups_action)
        self.framework.observe(self.charm.on.restore_action, self._on_restore_action)
        # Drive the async restore state machine on peer data changes and update-status.
        self.framework.observe(
            self.charm.on[PEER_RELATION].relation_changed, self._on_restore_workflow
        )
        self.framework.observe(self.charm.on.update_status, self._on_restore_workflow)

    # ── event handlers ──────────────────────────────────────────────────

    def _on_s3_credentials_changed(
        self, event: StorageConnectionInfoChangedEvent | ops.LeaderElectedEvent
    ) -> None:
        """Handle initial and updated S3 integrator credentials."""
        if not (s3_info := self.s3_requirer.get_storage_connection_info()):
            return
        logger.info("S3 credentials changed; refreshing backup configuration")

        # Every unit needs the S3 CA on disk (store_tls_ca_chain tolerates partial info).
        self.charm.backup_manager.store_tls_ca_chain(dict(s3_info))

        if not self.charm.unit.is_leader():
            return
        if not self.charm.state.peer_relation:
            event.defer()
            return

        # S3Parameters trims/validates the payload and rejects missing/empty fields.
        try:
            params = S3Parameters.model_validate(dict(s3_info))
        except ValidationError as e:
            logger.warning("S3 integrator parameters invalid or incomplete: %s", e)
            return

        # leader_elected re-fires this; skip the create_bucket round trip if unchanged.
        stored = self.charm.state.cluster.s3_credentials
        if stored is not None and stored.model_dump() == params.model_dump():
            return

        # Don't swap the bucket/creds an in-flight backup or restore is using; the
        # CA is already stored and the current creds stay in the databag meanwhile.
        if self._backup_or_restore_in_progress():
            logger.info("Backup or restore in progress; deferring S3 credentials rotation")
            event.defer()
            return

        try:
            self.charm.backup_manager.create_bucket(params)
        except ValkeyBackupError as e:
            logger.error("Bucket setup failed: %s", e)
            return

        self.charm.state.cluster.update({"s3_credentials": params.model_dump_json(by_alias=True)})

    def _backup_or_restore_in_progress(self) -> bool:
        """Return whether a backup (on any unit) or a restore is running.

        Cluster-wide: a backup runs on any single unit, so the leader must not
        clobber the shared S3 credentials from under it.
        """
        return (
            self.charm.state.is_backup_in_progress_any
            or self.charm.state.cluster.is_restore_in_progress
        )

    def _on_s3_credentials_gone(self, event: StorageConnectionInfoGoneEvent) -> None:
        """Handle removal of the S3 credentials relation."""
        if self._backup_or_restore_in_progress():
            logger.warning("Backup or restore in progress; deferring credentials_gone")
            event.defer()
            return

        self.charm.backup_manager.remove_tls_ca_chain()

        if self.charm.unit.is_leader():
            self.charm.state.cluster.update({"s3_credentials": ""})

    def _on_create_backup_action(self, event: ops.ActionEvent) -> None:
        """Run a streaming RDB backup of this unit's Valkey instance to S3."""
        if reason := self._blocking_reason():
            event.set_results({"error": reason})
            event.fail(reason)
            return
        # Audit log: tie this action invocation to the backup it produces.
        logger.info(
            "audit: create-backup action invoked action_id=%s unit=%s",
            event.id,
            self.charm.unit.name,
        )
        event.log("Streaming backup to S3 ...")
        # Surface the running backup in juju status; is_action beats lower-priority statuses.
        self.charm.status.set_running_status(
            BackupStatuses.BACKUP_IN_PROGRESS.value,
            scope="unit",
            is_action=True,
            component_name=self.charm.backup_manager.name,
            statuses_state=self.charm.state.statuses,
        )
        try:
            backup_id = self.charm.backup_manager.create_backup()
        except ValkeyBackupError as e:
            logger.exception("Backup failed")
            event.set_results({"error": _safe_error(e)})
            event.fail("Backup failed. Check juju debug-log for details.")
            return
        finally:
            self.charm.state.statuses.delete(
                BackupStatuses.BACKUP_IN_PROGRESS.value,
                scope="unit",
                component=self.charm.backup_manager.name,
            )
        event.set_results({"backup-id": backup_id})

    def _on_list_backups_action(self, event: ops.ActionEvent) -> None:
        """List backups currently in S3, newest first."""
        if (output_format := event.params.get("output", "table").lower()) not in {"json", "table"}:
            event.fail("Failed: invalid output format, must be either 'json' or 'table'.")
            return
        # Read-only: a backup running on this unit must not block listing.
        if reason := self._blocking_reason(check_running_operations=False):
            event.set_results({"error": reason})
            event.fail(reason)
            return
        logger.info(
            "audit: list-backups action invoked action_id=%s unit=%s",
            event.id,
            self.charm.unit.name,
        )
        try:
            ids = self.charm.backup_manager.list_backups()
        except ValkeyBackupError as e:
            logger.exception("List backups failed")
            event.set_results({"error": _safe_error(e)})
            event.fail("List backups failed. Check juju debug-log for details.")
            return
        if output_format == "json":
            backups = json.dumps([{"backup-id": bid, "backup-status": "finished"} for bid in ids])
        else:
            backups = self.charm.backup_manager.format_backup_list(ids)
        event.set_results({"backups": backups})

    # ── guard ────────────────────────────────────────────────────────────

    def _blocking_reason(self, check_running_operations: bool = True) -> str | None:
        """Return why a backup action cannot run, or None if it can.

        Shared by create-backup and list-backups; the latter passes
        ``check_running_operations=False`` since it is read-only.
        """
        if not self.charm.state.s3_relation:
            return "No S3 relation. Integrate with s3-integrator first."
        if not self.charm.state.cluster.s3_credentials:
            return "S3 credentials unavailable. Check s3-integrator config."
        if not self.charm.workload.alive():
            return "Valkey is not running on this unit."
        if check_running_operations and self.charm.state.unit_server.is_backup_in_progress:
            return "A backup is already in progress on this unit."
        if check_running_operations and self.charm.state.cluster.is_restore_in_progress:
            return "A restore is in progress; backups are paused."
        return None

    def _restore_blocking_reason(self) -> str | None:
        """Return why a restore cannot start, or None if it can."""
        if not self.charm.unit.is_leader():
            return "Restore must be run on the leader unit."
        if not self.charm.state.s3_relation:
            return "No S3 relation. Integrate with s3-integrator first."
        if not self.charm.state.cluster.s3_credentials:
            return "S3 credentials unavailable. Check s3-integrator config."
        if self.charm.state.is_backup_in_progress_any:
            return "A backup is in progress; cannot restore."
        if self.charm.state.cluster.is_restore_in_progress:
            return "A restore is already in progress."
        # A restore restarts the primary; don't start one while TLS is changing.
        if self.charm.state.is_tls_transitioning:
            return "A TLS transition is in progress; wait for it to settle."
        # Require a stable cluster: all active, a resolvable primary, no failover in flight.
        if not all(s.is_active for s in self.charm.state.servers):
            return "Not all units are active; wait for the cluster to settle."
        return self._unstable_primary_reason()

    def _unstable_primary_reason(self) -> str | None:
        """Return why the Sentinel-managed primary isn't restorable, or None if it is."""
        try:
            self.charm.sentinel_manager.get_primary_ip()
            if self.charm.sentinel_manager.is_failover_in_progress():
                return "A Sentinel failover is in progress; cannot restore."
        except ValkeyCannotGetPrimaryIPError:
            return "No primary available; cannot restore."
        except ValkeyWorkloadCommandError:
            # A sentinel query failed outright; treat as an unsettled cluster.
            return "Could not query Sentinel; wait for the cluster to settle."
        return None

    def _on_restore_action(self, event: ops.ActionEvent) -> None:
        """Validate, then initiate the async restore workflow (leader only)."""
        if reason := self._restore_blocking_reason():
            event.set_results({"error": reason})
            event.fail(reason)
            return
        if not (backup_id := event.params.get("backup-id", "")):
            event.set_results({"error": "Must provide backup-id to restore."})
            event.fail("Must provide backup-id to restore.")
            return
        try:
            if backup_id not in self.charm.backup_manager.list_backups():
                event.fail(f"backup-id {backup_id} not found.")
                return
        except ValkeyBackupError as e:
            logger.exception("Could not list backups for restore")
            event.set_results({"error": _safe_error(e)})
            event.fail("Could not list backups. Check juju debug-log.")
            return

        logger.info(
            "audit: restore action invoked action_id=%s backup_id=%s unit=%s",
            event.id,
            backup_id,
            self.charm.unit.name,
        )
        # Clear any stale terminal status from a prior attempt.
        self._clear_terminal_restore_statuses()
        participants = ",".join(sorted(s.unit_name for s in self.charm.state.servers))
        # Per-attempt token: restore_id is the backup-id (repeats on a re-run), so
        # failure markers are keyed to this fresh token instead.
        self.charm.state.cluster.update(
            {
                "restore_id": backup_id,
                "restore_token": uuid.uuid4().hex,
                "restore_instruction": RestoreStep.RESTORE.value,
                "restore_participants": participants,
            }
        )
        event.set_results({"restore": f"initiated for {backup_id}"})

    # ── restore workflow ─────────────────────────────────────────────────

    def _on_restore_workflow(self, _: ops.RelationChangedEvent | ops.UpdateStatusEvent) -> None:
        """Drive the restore state machine as far as this unit can in one hook.

        Each pass runs this unit's matching step and (leader) advances the shared
        instruction once all participants catch up, looping until no local
        progress is made. So a single-unit restore finishes in one hook; a
        multi-unit one runs to the next cross-unit barrier, where the peer
        relation_changed cascade (update_status as backstop) resumes it.
        """
        while True:
            # Restore done: each unit clears its own per-unit state once restore_id is gone.
            if not self.charm.state.cluster.is_restore_in_progress:
                if (
                    self.charm.state.unit_server.restore_step != RestoreStep.NOT_STARTED
                    or self.charm.state.unit_server.restore_failed
                ):
                    self.charm.state.unit_server.update(
                        {"restore_step": "", "restore_role": "", "restore_failed": ""}
                    )
                return

            # A participant failed: the leader tears the whole restore down (only
            # it can clear the app-level restore_id; the failing unit is often not
            # the leader).
            if self.charm.unit.is_leader() and self.charm.state.failed_restore_kind:
                self._clear_failed_restore()
                return
            # This unit already failed this attempt (token-scoped, so a stale marker
            # won't block a new one): wait for the leader to tear down.
            if self.charm.state.unit_server.restore_failure_kind(
                self.charm.state.cluster.restore_token
            ):
                return

            instruction = self.charm.state.cluster.restore_instruction
            step = self.charm.state.unit_server.restore_step
            role = self.charm.state.unit_server.restore_role  # "" until RESTORE records it

            try:
                self._run_restore_step(instruction, step, role)
            except Exception as e:
                # Catch everything: teardown must record the failure marker and
                # resume failover on ANY error (service-control errors sit outside
                # the restore-error hierarchy), or the restore wedges.
                logger.exception("Restore step failed; tearing down")
                self._restore_teardown(e)
                return

            if self.charm.unit.is_leader():
                self._advance_if_leader()

            # Fixed point: neither the shared instruction nor this unit's step moved.
            # In multi-unit this is the cross-unit barrier; relation_changed resumes
            # the workflow once peers catch up.
            if (
                self.charm.state.cluster.restore_instruction == instruction
                and self.charm.state.unit_server.restore_step == step
            ):
                return

    def _run_restore_step(self, instruction: RestoreStep, step: RestoreStep, role: str) -> None:
        """Run exactly the step whose (instruction, prior-step) tuple matches. Else no-op."""
        match (instruction, step):
            case (RestoreStep.RESTORE, RestoreStep.NOT_STARTED):
                # Fused download+restore: primary suppresses failover, downloads
                # and swaps the RDB in one sweep; replicas just record the step.
                is_primary = self.charm.cluster_manager.is_primary()
                self.charm.state.unit_server.update(
                    {"restore_role": "primary" if is_primary else "replica"}
                )
                if is_primary:
                    self.charm.sentinel_manager.suppress_failover()
                    self._do_primary_restore()
                self.charm.backup_manager.set_restore_step(RestoreStep.RESTORE)

            case (RestoreStep.RESYNC, RestoreStep.RESTORE):
                if role == "primary":
                    self.charm.sentinel_manager.resume_failover()
                else:
                    self.charm.cluster_manager.wait_until_resynced(RESTORE_RESYNC_TIMEOUT_S)
                self.charm.backup_manager.set_restore_step(RestoreStep.RESYNC)

            case (RestoreStep.COMPLETED, RestoreStep.RESYNC):
                if role == "primary":
                    self.charm.backup_manager.cleanup_restore_files()
                self.charm.backup_manager.set_restore_step(RestoreStep.COMPLETED)

            case _:
                # Not our turn: tuple doesn't match a valid transition.
                return

    def _do_primary_restore(self) -> None:
        """Validate, restore in-place, and roll back on any failure.

        restore_on_primary does stop -> move dump aside -> download -> restart;
        the caller then confirms it loaded. Any failure rolls back to the
        pre-restore copy before propagating to teardown.
        """
        # Pre-stop (outside the try, nothing to roll back yet): reject a non-RDB
        # object before bouncing the primary, and persist memory to disk so the
        # rollback copy restore_on_primary moves aside is current, not stale.
        self.charm.backup_manager.verify_backup_is_rdb(self.charm.state.cluster.restore_id)
        self.charm.cluster_manager.save_database_blocking()
        try:
            self.charm.backup_manager.restore_on_primary()
            self.charm.cluster_manager.wait_until_loaded(RESTORE_LOAD_TIMEOUT_S)
        except Exception:
            self.charm.backup_manager.roll_back()
            raise
        finally:
            # A restart (restore or rollback) resets runtime min-replicas-to-write
            # to the rendered 1, so reassert the topology-correct value or a small
            # cluster is write-frozen. Best-effort: a raise in a finally must not
            # mask the failure or false-fail a success.
            try:
                self.charm.cluster_manager.reconcile_min_replicas_to_write()
            except Exception:
                logger.exception("min-replicas reconcile after restore failed")

    def _advance_if_leader(self) -> None:
        """Advance the instruction once every participant has reached it; clear on COMPLETED."""
        if not self.charm.state.can_restore_workflow_proceed:
            return
        instruction = self.charm.state.cluster.restore_instruction
        if instruction == RestoreStep.COMPLETED:
            self._clear_terminal_restore_statuses()
            self._clear_restore_state()
            return
        self.charm.state.cluster.update(
            {"restore_instruction": self.charm.backup_manager.next_restore_step(instruction).value}
        )

    def _restore_teardown(self, exc: Exception | None = None) -> None:
        """Record this unit's restore failure so the leader can tear it down.

        The failing unit is often not the juju leader, and only the leader can
        clear the app-level restore_id, so the failure is recorded on this unit's
        own databag; the leader acts on it via _clear_failed_restore (inline here
        if this unit is the leader). resume_failover runs first but best-effort:
        a raise must not stop the marker being recorded, or the restore re-wedges.
        """
        try:
            self.charm.sentinel_manager.resume_failover()
        except Exception:
            logger.exception("resume_failover during restore teardown failed")
        kind = (
            RestoreFailure.UNHEALTHY
            if isinstance(exc, ValkeyClusterNotReadyError)
            else RestoreFailure.FAILED
        )
        # Stamp with the attempt token so it can't be misread against a later restore.
        marker = f"{kind.value}:{self.charm.state.cluster.restore_token}"
        self.charm.state.unit_server.update({"restore_failed": marker})
        if self.charm.unit.is_leader():
            # Already resumed failover just above; don't do it twice.
            self._clear_failed_restore(resume=False)

    def _clear_failed_restore(self, resume: bool = True) -> None:
        """Leader-only: flag the failure (UNHEALTHY vs FAILED) and clear restore state.

        ``resume`` resumes failover as a backstop, since a failing peer's own
        best-effort resume may have raised. The leader-self teardown already
        resumed, so it passes resume=False to avoid a redundant SENTINEL RESET.
        """
        if resume:
            try:
                self.charm.sentinel_manager.resume_failover()
            except Exception:
                logger.exception("resume_failover during leader restore teardown failed")
        status = (
            RestoreStatuses.RESTORE_UNHEALTHY
            if self.charm.state.failed_restore_kind == RestoreFailure.UNHEALTHY.value
            else RestoreStatuses.RESTORE_FAILED
        )
        # Clear restore state FIRST, so a status-write failure (statuses.add,
        # unlike delete, doesn't swallow one) can't leave the restore wedged.
        self._clear_restore_state()
        try:
            self.charm.state.statuses.add(
                status.value,
                scope="app",
                component=self.charm.backup_manager.name,
            )
        except Exception:
            logger.exception("recording the restore-failure status failed")

    def _clear_terminal_restore_statuses(self) -> None:
        """Delete both terminal restore statuses (delete tolerates an absent one)."""
        for status in (RestoreStatuses.RESTORE_FAILED, RestoreStatuses.RESTORE_UNHEALTHY):
            self.charm.state.statuses.delete(
                status.value,
                scope="app",
                component=self.charm.backup_manager.name,
            )

    def _clear_restore_state(self) -> None:
        """Clear the app-level restore coordination fields, ending the workflow."""
        self.charm.state.cluster.update(
            {
                "restore_id": "",
                "restore_token": "",
                "restore_instruction": "",
                "restore_participants": "",
            }
        )
