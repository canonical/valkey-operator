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
    ValkeyClusterNotReadyError,
)
from core.models import S3Parameters
from literals import (
    PEER_RELATION,
    RESTORE_LOAD_TIMEOUT_S,
    RESTORE_RESYNC_TIMEOUT_S,
    S3_RELATION_NAME,
    RestoreStep,
)
from statuses import BackupStatuses, RestoreStatuses

if TYPE_CHECKING:
    from charm import ValkeyCharm

logger = logging.getLogger(__name__)


def _safe_error(exc: ValkeyBackupError) -> str:
    """Render a backup error safe to return in an action result.

    Action results are world-readable, so surface only the structured S3 error
    code (e.g. "AccessDenied"); the full detail stays in the unit log.
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

        # Every unit needs the S3 CA on disk for TLS; a follower may receive it
        # before full credentials, and store_tls_ca_chain tolerates that.
        self.charm.backup_manager.store_tls_ca_chain(dict(s3_info))

        if not self.charm.unit.is_leader():
            return
        if not self.charm.state.peer_relation:
            event.defer()
            return

        # S3Parameters trims/validates the envelope and rejects missing/empty fields.
        try:
            params = S3Parameters.model_validate(dict(s3_info))
        except ValidationError as e:
            logger.warning("S3 integrator parameters invalid or incomplete: %s", e)
            return

        # leader_elected re-fires this; skip the create_bucket round trip when the
        # envelope is unchanged (compare by value). A real rotation still falls through.
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
        # Audit the action invocation (P1-24): ties an action run to its backup.
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
        # Require a stable cluster: all active, a resolvable primary, no failover in flight.
        if not all(s.is_active for s in self.charm.state.servers):
            return "Not all units are active; wait for the cluster to settle."
        try:
            self.charm.sentinel_manager.get_primary_ip()
        except ValkeyCannotGetPrimaryIPError:
            return "No primary available; cannot restore."
        if self.charm.sentinel_manager.is_failover_in_progress():
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
            event.set_results({"error": "Must provide backup-id to restore."})
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
            "audit: restore action invoked action_id=%s backup_id=%s unit=%s",
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
                "restore_instruction": RestoreStep.RESTORE.value,
                "restore_participants": participants,
            }
        )
        event.set_results({"restore": f"initiated for {backup_id}"})

    # ── restore workflow ─────────────────────────────────────────────────

    def _on_restore_workflow(self, _: ops.RelationChangedEvent | ops.UpdateStatusEvent) -> None:
        """Drive the restore state machine as far as this unit can in one hook.

        Each pass runs this unit's matching step and (leader only) advances the
        shared instruction once every participant has caught up, then repeats
        until it can make no further local progress. So a single-unit cluster
        completes in a single hook instead of crawling one step per
        ~5-min update_status; a multi-unit cluster runs itself right up to the
        next cross-unit barrier, where the peer relation_changed cascade (with
        update_status as a backstop) carries it forward. Bounded: a pass that
        moves neither the shared instruction nor this unit's own step ends it.
        """
        while True:
            # Restore done: once the leader clears restore_id, each unit clears
            # its own per-unit state (and only then).
            if not self.charm.state.cluster.is_restore_in_progress:
                if self.charm.state.unit_server.restore_step != RestoreStep.NOT_STARTED:
                    self.charm.state.unit_server.update({"restore_step": "", "restore_role": ""})
                return

            instruction = self.charm.state.cluster.restore_instruction
            step = self.charm.state.unit_server.restore_step
            role = self.charm.state.unit_server.restore_role  # "" until RESTORE records it

            try:
                self._run_restore_step(instruction, step, role)
            except Exception as e:
                # Catch everything: teardown MUST resume_failover() on any failure to
                # undo the cluster-wide suppression, else Sentinel can never promote
                # again. Service-control errors sit outside the restore-error hierarchy,
                # so a narrower except would let them escape.
                logger.exception("Restore step failed; tearing down")
                self._restore_teardown(e)
                return

            if self.charm.unit.is_leader():
                self._advance_if_leader()

            # Stop when this hook can make no further local progress: neither the
            # shared instruction nor this unit's own step moved. In multi-unit
            # that fixed point is the cross-unit barrier; the relation_changed
            # cascade resumes the workflow once peers catch up.
            if (
                self.charm.state.cluster.restore_instruction == instruction
                and self.charm.state.unit_server.restore_step == step
            ):
                return

    def _run_restore_step(self, instruction: RestoreStep, step: RestoreStep, role: str) -> None:
        """Run exactly the step whose (instruction, prior-step) tuple matches. Else no-op."""
        match (instruction, step):
            case (RestoreStep.RESTORE, RestoreStep.NOT_STARTED):
                # Fused download+restore: primary suppresses failover then
                # downloads and swaps the RDB in one sweep; replicas just record
                # the step. No cross-unit work sits between download and restore
                # (suppress_failover already hits every sentinel), so a split
                # barrier buys nothing.
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
        """Validate the object, then restore in-place, rolling back on any failure.

        stop -> back up the current dump -> download the restore RDB onto the
        data partition -> restart happen inside restore_on_primary; the cluster
        manager then confirms the server came up and finished loading. Any failure
        (bad download, unhealthy server) rolls back to the pre-restore copy before
        propagating to teardown.
        """
        # Pre-stop gate: reject an object that isn't a real RDB (wrong magic /
        # missing) while valkey still serves, so a bad backup-id never bounces
        # the primary. Outside the try: nothing has changed, nothing to roll back.
        self.charm.backup_manager.verify_backup_is_rdb(self.charm.state.cluster.restore_id)
        try:
            self.charm.backup_manager.restore_on_primary()
            self.charm.cluster_manager.wait_until_loaded(RESTORE_LOAD_TIMEOUT_S)
        except Exception:
            self.charm.backup_manager.roll_back()
            raise

    def _advance_if_leader(self) -> None:
        """Advance the instruction once every participant has reached it; clear on COMPLETED."""
        if not self.charm.state.can_restore_workflow_proceed:
            return
        instruction = self.charm.state.cluster.restore_instruction
        if instruction == RestoreStep.COMPLETED:
            self.charm.state.statuses.delete(
                RestoreStatuses.RESTORE_FAILED.value,
                scope="app",
                component=self.charm.backup_manager.name,
            )
            self.charm.state.cluster.update(
                {"restore_id": "", "restore_instruction": "", "restore_participants": ""}
            )
            return
        self.charm.state.cluster.update(
            {"restore_instruction": self.charm.backup_manager.next_restore_step(instruction).value}
        )

    def _restore_teardown(self, exc: Exception | None = None) -> None:
        """Resume failover, flag failure, and (leader) clear restore state.

        RESTORE_UNHEALTHY when the cluster never became ready
        (ValkeyClusterNotReadyError), RESTORE_FAILED otherwise.
        """
        self.charm.sentinel_manager.resume_failover()
        status = (
            RestoreStatuses.RESTORE_UNHEALTHY
            if isinstance(exc, ValkeyClusterNotReadyError)
            else RestoreStatuses.RESTORE_FAILED
        )
        self.charm.state.statuses.add(
            status.value,
            scope="app",
            component=self.charm.backup_manager.name,
        )
        if self.charm.unit.is_leader():
            self.charm.state.cluster.update(
                {"restore_id": "", "restore_instruction": "", "restore_participants": ""}
            )
