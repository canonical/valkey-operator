#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for S3 backups of Valkey RDB snapshots."""

from __future__ import annotations

import logging
import pathlib
import re
from datetime import datetime, timezone
from typing import IO, TYPE_CHECKING, Any, BinaryIO, cast

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError
from charmlibs import pathops
from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope

from common.client import ValkeyClient
from common.exceptions import ValkeyBackupError, ValkeyRestoreError
from literals import (
    BACKUP_CA_FILENAME,
    BACKUP_ID_FORMAT,
    PRE_RESTORE_SUFFIX,
    CharmUsers,
    RestoreStep,
)
from statuses import BackupStatuses, CharmStatuses, RestoreStatuses

if TYPE_CHECKING:
    from mypy_boto3_s3.literals import BucketLocationConstraintType
    from mypy_boto3_s3.service_resource import Bucket, S3ServiceResource

    from core.base_workload import WorkloadBase
    from core.cluster_state import ClusterState
    from core.models import S3Parameters

logger = logging.getLogger(__name__)

# RDB streams start with "REDIS" (Redis) or "VALKEY"; anything else is not a
# valid snapshot and must not be recorded as a backup.
_RDB_MAGIC = (b"REDIS", b"VALKEY")

# Only ISO-8601 backup ids (BACKUP_ID_FORMAT) belong in list-backups; this
# skips stray uploads, lifecycle markers, and future PITR/AOF objects.
_BACKUP_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# A CA-chain entry must look like PEM (armour header) or boto3 can't load the
# bundle. Same shape as managers/tls.py.
_PEM_HEADER_RE = re.compile(r"-+BEGIN [A-Z ]+-+")


class _CountingReader:
    """Wrap a binary stream, counting bytes read and capturing the head.

    boto3's ``upload_fileobj`` reads through this, letting ``create_backup``
    assert post-upload that the stream was non-empty and started with a
    valid RDB magic header.
    """

    def __init__(self, stream: IO[bytes], head_size: int = 16):
        self._stream = stream
        self._head_size = head_size
        self.head = b""
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        self.bytes_read += len(chunk)
        if len(self.head) < self._head_size:
            self.head += chunk[: self._head_size - len(self.head)]
        return chunk


class BackupManager(ManagerStatusProtocol):
    """Manage S3 backup uploads for the local Valkey instance."""

    name: str = "backup"
    # Narrow the protocol's state to ClusterState so attribute access type-checks;
    # the pyright override warning is invariance strictness (other managers do the same).
    state: "ClusterState"

    def __init__(self, state: "ClusterState", workload: "WorkloadBase"):
        self.state = state  # pyright: ignore[reportIncompatibleVariableOverride]
        self.workload = workload

    @property
    def _backup_ca_path(self) -> pathlib.Path:
        """Charm-local path to the S3 endpoint CA bundle used by boto3.

        Charm-process-local, not a workload ``tls_paths`` entry: boto3 runs in
        the charm process (a separate filesystem from the workload on K8s), and
        this keeps the S3 CA from being trusted as a Valkey client CA.
        """
        return self.state.charm.charm_dir / BACKUP_CA_FILENAME

    @property
    def _dump_path(self) -> pathops.PathProtocol:
        """Live Valkey RDB path (``dump.rdb``) inside the workload's data directory."""
        return self.workload.working_dir / "dump.rdb"

    @property
    def _dump_tmp_path(self) -> pathops.PathProtocol:
        """Temp path for a fresh download; renamed atomically onto ``_dump_path``.

        On the data partition next to ``dump.rdb``, so the promote is a
        same-partition rename (not a cross-device copy).
        """
        return self.workload.working_dir / "dump.rdb.part"

    @property
    def _pre_restore_path(self) -> pathops.PathProtocol:
        """Pre-restore RDB snapshot kept for rollback, on the archive partition.

        On archive_dir so the rollback copy doesn't double the data partition.
        """
        return self.workload.archive_dir / ("dump.rdb" + PRE_RESTORE_SUFFIX)

    # ── boto3 client construction ────────────────────────────────────────

    def _get_bucket_resource(self, s3_parameters: "S3Parameters") -> "Bucket":
        """Build a boto3 Bucket resource configured per the s3-integrator envelope."""
        verify: bool | str = True
        if s3_parameters.tls_ca_chain:
            verify = self._backup_ca_path.as_posix()

        # Scope creds to a Session so they don't surface in boto3 traceback repr(args).
        session = boto3.Session(
            aws_access_key_id=s3_parameters.access_key,
            aws_secret_access_key=s3_parameters.secret_key,
            region_name=s3_parameters.region,
        )
        s3 = cast(
            "S3ServiceResource",
            session.resource(
                "s3",
                endpoint_url=s3_parameters.endpoint,
                config=Config(
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
                verify=verify,
            ),
        )
        return s3.Bucket(s3_parameters.bucket)

    # ── bucket lifecycle ────────────────────────────────────────────────

    def create_bucket(self, s3_parameters: "S3Parameters") -> None:
        """Create the configured bucket; idempotent across S3 implementations."""
        bucket = self._get_bucket_resource(s3_parameters)
        region = s3_parameters.region
        try:
            # us-east-1 must NOT be sent as a LocationConstraint (CreateBucket
            # rejects it); any other region is passed explicitly. See aws-sdk-js#3647.
            if region and region != "us-east-1":
                bucket.create(
                    CreateBucketConfiguration={
                        # stub wants a region Literal; any non-default region is valid.
                        "LocationConstraint": cast("BucketLocationConstraintType", region)
                    }
                )
            else:
                bucket.create()
            # Bound the wait (boto3 default is up to 100s) so a slow endpoint can't
            # block leader_elected. The stub doesn't model WaiterConfig here.
            bucket.wait_until_exists(
                WaiterConfig={"Delay": 1, "MaxAttempts": 5}  # pyright: ignore[reportCallIssue]
            )
        except ClientError as e:
            # Match the structured code, not the message (alt-S3 backends recase it).
            code = e.response.get("Error", {}).get("Code", "")
            if code in {
                "BucketAlreadyOwnedByYou",
                "BucketAlreadyExists",
                "BucketNameUnavailable",
            }:
                logger.info("Using existing bucket %s", s3_parameters.bucket)
                return
            raise ValkeyBackupError(e) from e

    # ── TLS CA chain ─────────────────────────────────────────────────────

    def store_tls_ca_chain(self, s3_parameters: dict[str, Any]) -> None:
        """Write the S3 endpoint CA chain to the charm-local path for boto3."""
        chain = s3_parameters.get("tls-ca-chain")
        if not chain:
            return
        # Require a list of PEM certs: a bare string would make "\n".join iterate
        # characters and write a corrupt bundle (mirrors the TLS manager's check).
        if not isinstance(chain, list) or not all(
            isinstance(c, str) and _PEM_HEADER_RE.search(c) for c in chain
        ):
            logger.warning("tls-ca-chain is malformed (not a list of PEM certificates); ignoring")
            return
        raw = "\n".join(chain)
        self._backup_ca_path.write_text(raw)

    def remove_tls_ca_chain(self) -> None:
        """Delete the charm-local S3 endpoint CA bundle, if present."""
        self._backup_ca_path.unlink(missing_ok=True)

    # ── list ────────────────────────────────────────────────────────────

    def list_backups(self) -> list[str]:
        """Return valid backup ids in the configured bucket, newest first (auto-paginated)."""
        s3_parameters = self.state.cluster.s3_credentials
        if s3_parameters is None:
            raise ValkeyBackupError("S3 credentials unavailable")
        path = s3_parameters.path
        bucket = self._get_bucket_resource(s3_parameters)
        try:
            keys = [obj.key for obj in bucket.objects.filter(Prefix=f"{path}/")]
        except ClientError as e:
            raise ValkeyBackupError(e) from e
        ids = [k.removeprefix(f"{path}/") for k in keys]
        ids = [bid for bid in ids if _BACKUP_ID_RE.match(bid)]
        ids.sort(reverse=True)
        return ids

    @staticmethod
    def format_backup_list(ids: list[str]) -> str:
        """Render a backup list as a text table sized to the data."""
        if not ids:
            return "No backups found."
        width = max(len("backup-id"), max(len(bid) for bid in ids))
        header = f"{'backup-id':<{width}} | backup-status"
        separator = "-" * len(header)
        rows = "\n".join(f"{bid:<{width}} | finished" for bid in ids)
        return f"{header}\n{separator}\n{rows}"

    # ── create ──────────────────────────────────────────────────────────

    def create_backup(self) -> str:
        """Stream a fresh RDB from the local Valkey instance to S3.

        Sets a per-unit lock on the running unit's databag, streams
        ``valkey-cli --rdb -`` stdout into ``bucket.upload_fileobj``,
        and cleans up the S3 object on failure.
        """
        s3_parameters = self.state.cluster.s3_credentials
        if s3_parameters is None:
            raise ValkeyBackupError("S3 credentials unavailable")
        started = datetime.now(timezone.utc)
        backup_id = started.strftime(BACKUP_ID_FORMAT)
        key = f"{s3_parameters.path}/{backup_id}"
        # Structured audit trail for forensics; endpoint logged, creds never.
        logger.info(
            "backup.started backup_id=%s unit=%s bucket=%s endpoint=%s",
            backup_id,
            self.state.unit_server.unit_name,
            s3_parameters.bucket,
            s3_parameters.endpoint,
        )

        self.state.unit_server.update({"backup_id": backup_id})
        bucket = self._get_bucket_resource(s3_parameters)
        # Pass the admin password via VALKEYCLI_AUTH, never on argv (P1-2).
        proc = self.workload.exec_stream(
            self._build_rdb_command(),
            env={"VALKEYCLI_AUTH": self.state.unit_server.valkey_admin_password},
        )
        reader = _CountingReader(proc.stdout)

        try:
            # Don't retry the whole upload: reader can't rewind (boto3 retries parts).
            bucket.upload_fileobj(
                cast("IO[bytes]", reader),
                key,
                Config=TransferConfig(multipart_chunksize=8 * 1024 * 1024),
            )
            rc, stderr = proc.wait()
            if rc != 0:
                raise ValkeyBackupError(f"valkey-cli --rdb exited {rc}: {stderr}")
            # valkey-cli can exit 0 with no/invalid stdout; refuse a non-RDB object.
            if reader.bytes_read == 0 or not reader.head.startswith(_RDB_MAGIC):
                raise ValkeyBackupError(
                    f"Uploaded object is not a valid RDB stream "
                    f"({reader.bytes_read} bytes); refusing to record this backup"
                )
        except ValkeyBackupError:
            # A complete-but-invalid object is in the bucket; delete it. (A
            # mid-stream ClientError is handled below, after boto3 aborts.)
            self._delete_object_best_effort(bucket, key)
            logger.warning("backup.failed backup_id=%s", backup_id)
            raise
        except ClientError as e:
            # boto3 aborts the multipart upload itself; just stop the producer.
            proc.kill()
            logger.warning("backup.failed backup_id=%s", backup_id)
            raise ValkeyBackupError(e) from e
        else:
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            logger.info(
                "backup.completed backup_id=%s bytes=%d elapsed_seconds=%.1f",
                backup_id,
                reader.bytes_read,
                elapsed,
            )
        finally:
            self.state.unit_server.update({"backup_id": ""})

        return backup_id

    # ── restore ─────────────────────────────────────────────────────────

    def verify_backup_is_rdb(self, backup_id: str) -> None:
        """Confirm the S3 object starts with the RDB magic, cheaply, before touching valkey.

        NOT a full download: a ranged GET of just the first 16 bytes -- a
        super-small metadata read to validate the magic header. So a missing or
        non-RDB backup-id fails while valkey is still serving; the primary is
        stopped only once we know the object is plausibly a real snapshot.
        """
        s3_parameters = self.state.cluster.s3_credentials
        if s3_parameters is None:
            raise ValkeyRestoreError("S3 credentials unavailable")
        bucket = self._get_bucket_resource(s3_parameters)
        try:
            head = (
                bucket.Object(f"{s3_parameters.path}/{backup_id}")
                .get(Range="bytes=0-15")["Body"]
                .read()
            )
        except ClientError as e:
            raise ValkeyRestoreError(e) from e
        if not head.startswith(_RDB_MAGIC):
            raise ValkeyRestoreError(f"Object for {backup_id} is not a valid RDB stream")

    def download_backup(self, backup_id: str) -> None:
        """Stream the full RDB straight onto the data partition as ``dump.rdb``.

        Streams the S3 object body directly into the workload's data partition:
        push_data_file copies in bounded chunks, so the full object is never
        buffered whole in the (small) charm container regardless of RDB size. It
        lands under ``dump.rdb.part`` and is renamed onto ``dump.rdb`` (same
        partition -> atomic) so the final file never appears partial. The RDB
        magic is validated up front by verify_backup_is_rdb (a tiny ranged GET)
        before the primary is stopped; restore_on_primary has already moved the
        old dump aside, so the data partition holds only ~1x the dataset with no
        cross-device copy to install it.
        """
        s3_parameters = self.state.cluster.s3_credentials
        if s3_parameters is None:
            raise ValkeyRestoreError("S3 credentials unavailable")
        bucket = self._get_bucket_resource(s3_parameters)

        try:
            body = bucket.Object(f"{s3_parameters.path}/{backup_id}").get()["Body"]
        except ClientError as e:
            raise ValkeyRestoreError(e) from e

        # Stream S3 -> data partition; the StreamingBody is a binary read()-able
        # at runtime (cast for the stub), copied in bounded chunks by the workload.
        self.workload.push_data_file(
            cast(BinaryIO, body),
            self._dump_tmp_path,
            user=self.workload.user,
            group=self.workload.user,
        )
        # Atomic promote: same-partition rename, so dump.rdb only ever appears complete.
        self.workload.move_file(self._dump_tmp_path, self._dump_path)

    # ── restore steps ────────────────────────────────────────────────────

    @staticmethod
    def next_restore_step(step: RestoreStep) -> RestoreStep:
        """Return the step following ``step`` in the restore workflow."""
        order = [
            RestoreStep.NOT_STARTED,
            RestoreStep.RESTORE,
            RestoreStep.RESYNC,
            RestoreStep.COMPLETED,
        ]
        return order[order.index(step) + 1]

    def set_restore_step(self, step: RestoreStep) -> None:
        """Record this unit's completed restore step on its databag."""
        self.state.unit_server.update({"restore_step": step.value})

    def restore_on_primary(self) -> None:
        """Stop valkey, back up the current dump, download the restore RDB in its place, restart.

        Moves the current dump to the archive partition for rollback, then
        downloads the restore RDB straight onto the (now-free) data partition, so
        neither partition ever holds more than ~1x the dataset. Bypasses
        restart_workload/RestartLock (can't bracket a file swap); concurrent
        restarts are held off by the is_restore_in_progress defer. The caller
        confirms the server came up healthy (cluster_manager.wait_until_loaded).
        """
        self.workload.stop_service(self.workload.valkey_service)
        # On a redelivered hook _pre_restore_path already holds the ORIGINAL data;
        # don't overwrite it (the only rollback copy). Skip the aside if it exists.
        if not self.workload.path_exists(self._pre_restore_path):
            self.workload.move_file(self._dump_path, self._pre_restore_path)
        # Data partition is now free; download the restore RDB directly onto it.
        self.download_backup(self.state.cluster.restore_id)
        self.workload.start_service(self.workload.valkey_service)

    def roll_back(self) -> None:
        """Restore the pre-restore dump and restart (stop_service FIRST to defeat auto-restart)."""
        self.workload.stop_service(self.workload.valkey_service)
        if self.workload.path_exists(self._pre_restore_path):
            self.workload.move_file(self._pre_restore_path, self._dump_path)
        self.workload.start_service(self.workload.valkey_service)

    def cleanup_restore_files(self) -> None:
        """Remove the pre-restore rollback copy after a successful restore."""
        self.workload.remove_file(self._pre_restore_path)

    # ── helpers ─────────────────────────────────────────────────────────

    def _build_rdb_command(self) -> list[str]:
        """Build the argv for ``valkey-cli --rdb -`` against the local server."""
        client = ValkeyClient(
            username=CharmUsers.VALKEY_ADMIN.value,
            password=self.state.unit_server.valkey_admin_password,
            tls=self.state.unit_server.is_tls_enabled,
            workload=self.workload,
        )
        prefix = client.build_command_prefix(json_output=False, hostname=self.state.endpoint)
        return prefix + ["--rdb", "-"]

    @staticmethod
    def _delete_object_best_effort(bucket: "Bucket", key: str) -> None:
        """Delete an S3 object, swallowing any error (best-effort cleanup)."""
        try:
            bucket.Object(key).delete()
        except Exception as e:
            logger.warning("Failed to delete partial S3 object %s: %s", key, e)

    # ── advanced statuses ───────────────────────────────────────────────

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Contribute backup- and restore-related statuses to the StatusHandler."""
        # Copy: .root is the live list; the appends below must not mutate persisted state.
        status_list: list[StatusObject] = list(
            self.state.statuses.get(
                scope=scope,
                component=self.name,
                running_status_only=True,
            ).root
        )

        if scope == "unit" and self.state.unit_server.is_backup_in_progress:
            status_list.append(BackupStatuses.BACKUP_IN_PROGRESS.value)

        if scope == "app" and self.state.s3_relation and not self.state.cluster.s3_credentials:
            status_list.append(BackupStatuses.BACKUP_S3_PARAMETERS_MISSING.value)

        if scope == "app" and self.state.cluster.is_restore_in_progress:
            status_list.append(RestoreStatuses.RESTORE_IN_PROGRESS.value)

        return status_list or [CharmStatuses.ACTIVE_IDLE.value]
