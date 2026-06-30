#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Juju event wiring for S3 backups."""

from __future__ import annotations

import json
import logging
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
    ValkeyRestoreError,
    ValkeyRestoreUnhealthyError,
    ValkeyWorkloadCommandError,
)
from core.models import S3Parameters
from literals import PEER_RELATION, S3_RELATION_NAME, RestoreStep
from statuses import BackupStatuses, RestoreStatuses

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


def _safe_error(exc: ValkeyBackupError) -> str:
    """Render a backup error safe to return in an action result.

    Action results are readable by any Juju user, unlike ``juju
    debug-log``. The raw exception text can carry the S3 endpoint,
    request/host ids, or RDB stream metadata, so only the structured S3
    error code -- a fixed, non-sensitive token such as "AccessDenied" --
    is surfaced. Everything else collapses to a generic message; the full
    detail stays in the unit log.
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
        self.framework.observe(self.charm.on.restore_backup_action, self._on_restore_action)
        # Drive the async restore state machine on every peer data change and
        # on update-status (leader can advance without waiting for a remote change).
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

        # CA chain must be on disk for every unit so any unit can use TLS to S3.
        # Stored from the raw envelope (a follower needs only the CA, which may
        # arrive before the full credentials); store_tls_ca_chain is tolerant.
        self.charm.backup_manager.store_tls_ca_chain(dict(s3_info))

        if not self.charm.unit.is_leader():
            return
        if not self.charm.state.peer_relation:
            event.defer()
            return

        # Parse + normalise + validate the integrator envelope in one step:
        # S3Parameters trims whitespace, strips the separators that would
        # corrupt S3 key paths, and rejects an envelope missing a required
        # field or whose path/bucket strips to empty.
        try:
            params = S3Parameters.model_validate(dict(s3_info))
        except ValidationError as e:
            logger.warning("S3 integrator parameters invalid or incomplete: %s", e)
            return

        # leader_elected re-fires this handler; skip the create_bucket round
        # trip (a synchronous S3 call) when the envelope is unchanged. Compare
        # by value (model_dump) rather than identity. A real credentials
        # rotation still falls through.
        stored = self.charm.state.cluster.s3_credentials
        if stored is not None and stored.model_dump() == params.model_dump():
            return

        try:
            self.charm.backup_manager.create_bucket(params)
        except ValkeyBackupError as e:
            logger.error("Bucket setup failed: %s", e)
            return

        self.charm.state.cluster.update({"s3_credentials": params.model_dump_json(by_alias=True)})

    def _on_s3_credentials_gone(self, event: StorageConnectionInfoGoneEvent) -> None:
        """Handle removal of the S3 credentials relation."""
        if (
            self.charm.state.unit_server.is_backup_in_progress
            or self.charm.state.cluster.is_restore_in_progress
        ):
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
        # Audit the invocation itself, not just the manager-level transfer
        # (P1-24): ties a specific Juju action run to the resulting backup,
        # for forensics if an RDB later turns up somewhere unexpected.
        logger.info(
            "audit: create-backup action invoked action_id=%s unit=%s",
            event.id,
            self.charm.unit.name,
        )
        event.log("Streaming backup to S3 ...")
        # Surface the long-running backup in `juju status` while the action
        # blocks; is_action forces it past lower-priority statuses.
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

        Covers the preconditions shared by create-backup and list-backups.
        With ``check_running_operations`` (the default), it also rejects a
        backup already running on this unit; list-backups passes ``False``
        because it is read-only and safe to run concurrently. The same flag
        will let a future restore action share this guard.
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
        # Stable cluster: all participants active + a resolvable primary + no failover in flight.
        if not all(s.is_active for s in self.charm.state.servers):
            return "Not all units are active; wait for the cluster to settle."
        try:
            primary_ip = self.charm.sentinel_manager.get_primary_ip()
        except ValkeyCannotGetPrimaryIPError:
            return "No primary available; cannot restore."
        if "failover_in_progress" in (
            self.charm.sentinel_manager._get_sentinel_client()
            .primary(hostname=primary_ip)
            .get("flags", "")
        ):
            return "A Sentinel failover is in progress; cannot restore."
        return None

    def _on_restore_action(self, event: ops.ActionEvent) -> None:
        """Validate, then initiate the async restore workflow (leader only)."""
        if reason := self._restore_blocking_reason():
            event.set_results({"error": reason})
            event.fail(reason)
            return
        backup_id = event.params.get("backup-id", "")
        if not backup_id:
            event.fail("Must provide backup-id to restore.")
            return
        try:
            if backup_id not in self.charm.backup_manager.list_backups():
                event.fail(f"backup-id {backup_id} not found.")
                return
        except ValkeyBackupError as e:
            event.set_results({"error": _safe_error(e)})
            event.fail("Could not list backups. Check juju debug-log.")
            return

        logger.info(
            "audit: restore-backup action invoked action_id=%s backup_id=%s unit=%s",
            event.id,
            backup_id,
            self.charm.unit.name,
        )
        # Clear any stale RESTORE_FAILED from a prior attempt.
        self.charm.state.statuses.delete(
            RestoreStatuses.RESTORE_FAILED.value,
            scope="app",
            component=self.charm.backup_manager.name,
        )
        participants = ",".join(sorted(s.unit_name for s in self.charm.state.servers))
        self.charm.state.cluster.update(
            {
                "restore_id": backup_id,
                "restore_instruction": RestoreStep.DOWNLOAD.value,
                "restore_participants": participants,
            }
        )
        event.set_results({"restore": f"initiated for {backup_id}"})

    # ── restore workflow ─────────────────────────────────────────────────

    def _on_restore_workflow(self, _: ops.RelationChangedEvent | ops.UpdateStatusEvent) -> None:
        """Drive this unit's restore step, then (leader) advance the instruction."""
        cluster = self.charm.state.cluster
        unit = self.charm.state.unit_server

        # COMPLETED ordering: once the leader has cleared restore_id, units
        # clear their own per-unit restore state (and only then).
        if not cluster.is_restore_in_progress:
            if unit.restore_step != RestoreStep.NOT_STARTED:
                unit.update({"restore_step": "", "restore_role": ""})
            return

        instruction = cluster.restore_instruction
        step = unit.restore_step
        role = unit.restore_role  # "" until DOWNLOAD records it

        try:
            self._run_restore_step(instruction, step, role)
        except (ValkeyRestoreError, ValkeyBackupError, ValkeyWorkloadCommandError):
            logger.exception("Restore step failed; tearing down")
            self._restore_teardown()
            return

        if self.charm.unit.is_leader():
            self._advance_if_leader()

    def _run_restore_step(self, instruction: RestoreStep, step: RestoreStep, role: str) -> None:
        """Run exactly the step whose (instruction, prior-step) tuple matches. Else no-op."""
        cluster = self.charm.state.cluster
        unit = self.charm.state.unit_server
        bm = self.charm.backup_manager

        match (instruction, step):
            case (RestoreStep.DOWNLOAD, RestoreStep.NOT_STARTED):
                is_primary = bm.is_local_primary()
                unit.update({"restore_role": "primary" if is_primary else "replica"})
                if is_primary:
                    self.charm.sentinel_manager.suppress_failover()
                    bm.download_backup(cluster.restore_id)
                bm.set_restore_step(RestoreStep.DOWNLOAD)

            case (RestoreStep.RESTORE, RestoreStep.DOWNLOAD):
                if role == "primary":
                    self._do_primary_restore()
                bm.set_restore_step(RestoreStep.RESTORE)

            case (RestoreStep.RESYNC, RestoreStep.RESTORE):
                if role == "primary":
                    self.charm.sentinel_manager.resume_failover()
                else:
                    bm.wait_until_resynced()
                bm.set_restore_step(RestoreStep.RESYNC)

            case (RestoreStep.COMPLETED, RestoreStep.RESYNC):
                if role == "primary":
                    bm.cleanup_restore_files()
                bm.set_restore_step(RestoreStep.COMPLETED)

            case _:
                # Not our turn (tuple doesn't match an expected transition): no-op.
                return

    def _do_primary_restore(self) -> None:
        """Re-download the RDB if missing, then restore in-place; roll back on unhealthy state."""
        bm = self.charm.backup_manager
        if not self.charm.workload.path_exists(bm._download_path):
            bm.download_backup(self.charm.state.cluster.restore_id)
        try:
            bm.restore_on_primary()
        except ValkeyRestoreUnhealthyError:
            bm.roll_back()
            raise

    def _advance_if_leader(self) -> None:
        """Advance the instruction once every participant has reached it; clear on COMPLETED."""
        cluster = self.charm.state.cluster
        if not self.charm.state.can_restore_workflow_proceed:
            return
        instruction = cluster.restore_instruction
        if instruction == RestoreStep.COMPLETED:
            self.charm.state.statuses.delete(
                RestoreStatuses.RESTORE_FAILED.value,
                scope="app",
                component=self.charm.backup_manager.name,
            )
            cluster.update(
                {"restore_id": "", "restore_instruction": "", "restore_participants": ""}
            )
            return
        cluster.update(
            {"restore_instruction": self.charm.backup_manager.next_restore_step(instruction).value}
        )

    def _restore_teardown(self) -> None:
        """Resume failover suppression, flag failure, and (leader) clear restore state."""
        self.charm.sentinel_manager.resume_failover()
        self.charm.state.statuses.add(
            RestoreStatuses.RESTORE_FAILED.value,
            scope="app",
            component=self.charm.backup_manager.name,
        )
        if self.charm.unit.is_leader():
            self.charm.state.cluster.update(
                {"restore_id": "", "restore_instruction": "", "restore_participants": ""}
            )
