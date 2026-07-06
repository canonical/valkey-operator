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

from common.exceptions import ValkeyBackupError
from core.models import S3Parameters
from literals import S3_RELATION_NAME
from statuses import BackupStatuses

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
        if self.charm.state.unit_server.is_backup_in_progress:
            logger.warning("Backup in progress; deferring credentials_gone")
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
        return None
