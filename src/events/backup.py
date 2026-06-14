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
from charms.data_platform_libs.v0.s3 import (
    CredentialsChangedEvent,
    CredentialsGoneEvent,
    S3Requirer,
)

from common.exceptions import ValkeyBackupError
from literals import S3_RELATION_NAME

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
            self.s3_requirer.on.credentials_changed, self._on_s3_credentials_changed
        )
        self.framework.observe(self.s3_requirer.on.credentials_gone, self._on_s3_credentials_gone)
        # Recover credentials when leadership moves.
        self.framework.observe(self.charm.on.leader_elected, self._on_s3_credentials_changed)
        self.framework.observe(self.charm.on.create_backup_action, self._on_create_backup_action)
        self.framework.observe(self.charm.on.list_backups_action, self._on_list_backups_action)

    # ── guard ────────────────────────────────────────────────────────────

    def _blocking_reason(self) -> str | None:
        """Return why backup actions cannot run, or None if they can.

        Covers the preconditions shared by create-backup and list-backups.
        create-backup additionally rejects a backup already in progress on
        this unit; list-backups is read-only and does not.
        """
        if not self.charm.state.s3_relation:
            return "No S3 relation. Integrate with s3-integrator first."
        if not self.charm.state.cluster.s3_credentials:
            return "S3 credentials unavailable. Check s3-integrator config."
        if not self.charm.workload.alive():
            return "Valkey is not running on this unit."
        return None

    # ── event handlers ──────────────────────────────────────────────────

    def _on_s3_credentials_changed(
        self, event: CredentialsChangedEvent | ops.LeaderElectedEvent
    ) -> None:
        """Handle initial and updated S3 integrator credentials."""
        s3 = self.s3_requirer.get_s3_connection_info()
        if not s3:
            return

        # CA chain must be on disk for every unit so any unit can use TLS to S3.
        self.charm.backup_manager.store_tls_ca_chain(s3)

        if not self.charm.unit.is_leader():
            return
        if not self.charm.state.peer_relation:
            event.defer()
            return

        for k, v in list(s3.items()):
            if isinstance(v, str):
                s3[k] = v.strip()
        s3["endpoint"] = s3.get("endpoint", "").rstrip("/")
        s3["path"] = s3.get("path", "").strip("/")
        s3["bucket"] = s3.get("bucket", "").strip("/")

        # Validate AFTER normalisation: path="/" or bucket="/" collapse to ""
        # here, and an empty path makes list_backups enumerate the whole
        # bucket (cross-tenant leak in a shared bucket).
        required = ["bucket", "endpoint", "path", "access-key", "secret-key"]
        missing = [p for p in required if not s3.get(p)]
        if missing:
            logger.warning(
                "S3 integrator parameters missing or empty after normalisation: %s", missing
            )
            return

        # leader_elected re-fires this handler; skip the create_bucket round
        # trip (a synchronous S3 call) when the normalised envelope is
        # unchanged. A real credentials rotation still falls through.
        if self.charm.state.cluster.s3_credentials == s3:
            return

        try:
            self.charm.backup_manager.create_bucket(s3)
        except ValkeyBackupError as e:
            logger.error("Bucket setup failed: %s", e)
            return

        self.charm.state.cluster.update({"s3_credentials": json.dumps(s3)})

    def _on_s3_credentials_gone(self, event: CredentialsGoneEvent) -> None:
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
        reason = self._blocking_reason()
        if reason is None and self.charm.state.unit_server.is_backup_in_progress:
            reason = "A backup is already in progress on this unit."
        if reason:
            event.set_results({"error": reason})
            event.fail(reason)
            return
        # Audit the invocation itself, not just the manager-level transfer
        # (P1-24): ties a specific Juju action run to the resulting backup
        # for forensics on a leaked or unexpected RDB.
        logger.info(
            "audit: create-backup action invoked action_id=%s unit=%s",
            event.id,
            self.charm.unit.name,
        )
        event.log("Streaming backup to S3 ...")
        try:
            backup_id = self.charm.backup_manager.create_backup()
        except ValkeyBackupError as e:
            logger.exception("Backup failed")
            event.set_results({"error": _safe_error(e)})
            event.fail("Backup failed. Check juju debug-log for details.")
            return
        event.set_results({"backup-id": backup_id})

    def _on_list_backups_action(self, event: ops.ActionEvent) -> None:
        """List backups currently in S3, newest first."""
        if reason := self._blocking_reason():
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
        event.set_results({"backups": self.charm.backup_manager.format_backup_list(ids)})
