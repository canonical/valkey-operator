#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for S3 backups of Valkey RDB snapshots."""

from __future__ import annotations

import logging
import pathlib
import re
from datetime import datetime, timezone
from typing import IO, TYPE_CHECKING, Any, cast

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError
from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope

from common.client import ValkeyClient
from common.exceptions import ValkeyBackupError
from literals import BACKUP_CA_FILENAME, BACKUP_ID_FORMAT, CharmUsers
from statuses import BackupStatuses, CharmStatuses

if TYPE_CHECKING:
    from mypy_boto3_s3.service_resource import Bucket, S3ServiceResource

    from core.base_workload import WorkloadBase
    from core.cluster_state import ClusterState

logger = logging.getLogger(__name__)

# RDB streams begin with a magic header: "REDIS" on upstream Redis, "VALKEY"
# on Valkey. An upload that does not start with one of these is not a valid
# snapshot and must not be recorded as a backup.
_RDB_MAGIC = (b"REDIS", b"VALKEY")

# A backup id is BACKUP_ID_FORMAT == "%Y-%m-%dT%H:%M:%SZ". Anything in the
# bucket prefix that does not match (stray uploads, lifecycle markers,
# future PITR/AOF objects) must not appear in list-backups output.
_BACKUP_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# A CA-chain entry must look like PEM: an item that lacks the armour header
# (e.g. base64 with no "-----BEGIN ... -----", or a stray non-cert string)
# would produce a CA bundle boto3 cannot load. Same shape as managers/tls.py.
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
    # Narrow the protocol's ``state: StatusesStateProtocol`` to the charm's
    # concrete state object so attribute access type-checks. ClusterState
    # structurally satisfies StatusesStateProtocol; the override warning is
    # pyright being strict about mutable-variable invariance, and the same
    # narrowing is used by the other managers.
    state: "ClusterState"

    def __init__(self, state: "ClusterState", workload: "WorkloadBase"):
        self.state = state  # pyright: ignore[reportIncompatibleVariableOverride]
        self.workload = workload

    @property
    def _backup_ca_path(self) -> pathlib.Path:
        """Charm-local path to the S3 endpoint CA bundle used by boto3.

        Deliberately charm-process-local, NOT a workload ``tls_paths`` entry:
        boto3 runs in the charm process, not the workload container, so on
        K8s the two do not share a filesystem and the bundle could not live
        in the workload's (container) TLS dir. Keeping it out of that dir
        also stops the S3 endpoint CA being trusted as a Valkey client CA.
        """
        return self.state.charm.charm_dir / BACKUP_CA_FILENAME

    # ── boto3 client construction ────────────────────────────────────────

    def _get_bucket_resource(self, s3_parameters: dict[str, object]):
        """Build a boto3 Bucket resource configured per the s3-integrator envelope."""
        verify: bool | str = True
        if s3_parameters.get("tls-ca-chain"):
            verify = str(self._backup_ca_path)

        # Scope the credentials to a Session so they are not free-floating
        # kwargs that show up in repr(args) of any boto3 traceback.
        session = boto3.Session(
            aws_access_key_id=s3_parameters["access-key"],
            aws_secret_access_key=s3_parameters["secret-key"],
            region_name=s3_parameters.get("region"),
        )
        s3 = cast(
            "S3ServiceResource",
            session.resource(
                "s3",
                endpoint_url=s3_parameters.get("endpoint"),
                config=Config(
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
                verify=verify,
            ),
        )
        return s3.Bucket(cast("str", s3_parameters["bucket"]))

    # ── bucket lifecycle ────────────────────────────────────────────────

    def create_bucket(self, s3_parameters: dict[str, Any]) -> None:
        """Create the configured bucket; idempotent across S3 implementations."""
        bucket = self._get_bucket_resource(s3_parameters)
        region = s3_parameters.get("region")
        try:
            if region and region != "us-east-1":
                bucket.create(CreateBucketConfiguration={"LocationConstraint": region})
            else:
                bucket.create()
            # Bound the wait: the boto3 default is 20 * 5s = up to 100s, which
            # would block leader_elected when the S3 endpoint is slow. The
            # resource waiter forwards WaiterConfig to the underlying
            # waiter.wait(); the stub just does not model that kwarg.
            bucket.wait_until_exists(
                WaiterConfig={"Delay": 1, "MaxAttempts": 5}  # pyright: ignore[reportCallIssue]
            )
        except ClientError as e:
            # Match the structured error code, not a substring of the rendered
            # message: alt-S3 backends localise/recase the message text.
            code = e.response.get("Error", {}).get("Code", "")
            if code in {
                "BucketAlreadyOwnedByYou",
                "BucketAlreadyExists",
                "BucketNameUnavailable",
            }:
                logger.info("Using existing bucket %s", s3_parameters["bucket"])
                return
            raise ValkeyBackupError(e) from e

    # ── TLS CA chain ─────────────────────────────────────────────────────

    def store_tls_ca_chain(self, s3_parameters: dict[str, Any]) -> None:
        """Write the S3 endpoint CA chain to the charm-local path for boto3."""
        chain = s3_parameters.get("tls-ca-chain")
        if not chain:
            return
        # A misconfigured integrator may send a bare string; "\n".join would
        # then iterate characters and write a corrupt CA bundle. Require a
        # list of PEM certificates -- each item must carry a PEM armour
        # header, mirroring the TLS manager's key check.
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
        """Return valid backup ids in the configured bucket, newest first.

        ``bucket.objects.filter`` auto-paginates; results are filtered to the
        backup-id format so unrelated objects under the prefix are excluded.
        """
        s3_parameters = self.state.cluster.s3_credentials
        path = s3_parameters["path"]
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
        started = datetime.now(timezone.utc)
        backup_id = started.strftime(BACKUP_ID_FORMAT)
        key = f"{s3_parameters['path']}/{backup_id}"
        # Structured audit trail: who/what/where for forensics on a backup
        # that lands somewhere unexpected. Endpoint is logged; creds never.
        logger.info(
            "backup.started backup_id=%s unit=%s bucket=%s endpoint=%s",
            backup_id,
            self.state.unit_server.unit_name,
            s3_parameters.get("bucket"),
            s3_parameters.get("endpoint"),
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
            # Do not retry the whole upload: reader is backed by proc.stdout
            # and cannot be rewound. boto3 retries individual parts itself.
            bucket.upload_fileobj(
                cast("IO[bytes]", reader),
                key,
                Config=TransferConfig(multipart_chunksize=8 * 1024 * 1024),
            )
            rc, stderr = proc.wait()
            if rc != 0:
                raise ValkeyBackupError(f"valkey-cli --rdb exited {rc}: {stderr}")
            # valkey-cli can exit 0 having written nothing (or an error blob)
            # to stdout. Refuse to record an object that is not a real RDB.
            if reader.bytes_read == 0 or not reader.head.startswith(_RDB_MAGIC):
                raise ValkeyBackupError(
                    f"Uploaded object is not a valid RDB stream "
                    f"({reader.bytes_read} bytes); refusing to record this backup"
                )
        except ValkeyBackupError:
            # A complete-but-invalid object (bad exit code / bad RDB magic) is
            # in the bucket; delete it. (A mid-stream ClientError is handled
            # below, where boto3 has already aborted the multipart upload.)
            self._delete_object_best_effort(bucket, key)
            logger.warning("backup.failed backup_id=%s", backup_id)
            raise
        except ClientError as e:
            # upload_fileobj failed mid-stream; boto3 aborts the multipart
            # upload itself, so there is no object to delete -- just stop the
            # producer so valkey-cli does not linger.
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
        """Delete an S3 object, swallowing any error.

        Used on backup-failure cleanup paths where a delete that itself
        fails must not mask the original error; broadest catch on purpose.
        """
        try:
            bucket.Object(key).delete()
        except Exception as e:
            logger.warning("Failed to delete partial S3 object %s: %s", key, e)

    # ── advanced statuses ───────────────────────────────────────────────

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Contribute backup-related statuses to the StatusHandler."""
        # Copy: ``.root`` is the live list inside the StatusObjectList model,
        # and the appends below would otherwise mutate persisted state.
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

        return status_list or [CharmStatuses.ACTIVE_IDLE.value]
