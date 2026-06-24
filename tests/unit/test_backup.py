#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the S3 backup feature."""

from src.statuses import BackupStatuses


def test_backup_statuses_present():
    assert BackupStatuses.BACKUP_IN_PROGRESS.value.status == "maintenance"
    assert BackupStatuses.BACKUP_S3_PARAMETERS_MISSING.value.status == "blocked"
    assert BackupStatuses.BACKUP_FAILED.value.status == "blocked"


def test_peer_app_model_has_s3_credentials_field():
    from src.core.models import PeerAppModel

    fields = PeerAppModel.model_fields
    assert "s3_credentials" in fields
    assert fields["s3_credentials"].default is None


def test_cluster_s3_credentials_parses_json_and_defaults_empty(mocker):
    """The envelope parses back to a dict; unset reads as {} (never None)."""
    from src.core.models import ValkeyCluster

    cluster = ValkeyCluster.__new__(ValkeyCluster)

    cluster.model = mocker.MagicMock()
    cluster.model.s3_credentials = '{"bucket": "b", "tls-ca-chain": ["c1", "c2"]}'
    assert cluster.s3_credentials == {"bucket": "b", "tls-ca-chain": ["c1", "c2"]}

    cluster.model.s3_credentials = None
    assert cluster.s3_credentials == {}

    cluster.model = None
    assert cluster.s3_credentials == {}


def test_peer_unit_model_has_backup_id_field():
    from src.core.models import PeerUnitModel

    assert "backup_id" in PeerUnitModel.model_fields
    assert PeerUnitModel.model_fields["backup_id"].default == ""


def test_valkey_server_is_backup_in_progress_reflects_model_field():
    from src.core.models import PeerUnitModel, ValkeyServer

    server = ValkeyServer.__new__(ValkeyServer)
    server.model = PeerUnitModel(backup_id="2026-05-13T10:00:00Z")
    assert server.is_backup_in_progress is True

    server.model = PeerUnitModel(backup_id="")
    assert server.is_backup_in_progress is False

    server.model = None
    assert server.is_backup_in_progress is False


def test_cluster_state_exposes_s3_relation(cloud_spec):
    from ops import testing

    from src.charm import ValkeyCharm
    from src.literals import PEER_RELATION, S3_RELATION_NAME, STATUS_PEERS_RELATION

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    s3_rel = testing.Relation(
        id=3,
        endpoint=S3_RELATION_NAME,
        interface="s3",
        remote_app_name="s3-integrator",
    )
    state_in = testing.State(
        model=testing.Model(name="m", type="lxd", cloud_spec=cloud_spec),
        leader=True,
        relations={peer, status_peer, s3_rel},
        containers={testing.Container(name="valkey", can_connect=True)},
    )

    with ctx(ctx.on.update_status(), state_in) as manager:
        assert manager.charm.state.s3_relation is not None
        assert manager.charm.state.s3_relation.name == S3_RELATION_NAME


def test_backup_ca_path_is_charm_local_not_workload_tls_dir(mocker, tmp_path):
    """The S3 CA path is charm-process-local, never a workload TLS path.

    boto3 runs in the charm process, so the bundle must sit under the charm
    dir (``charm.charm_dir``), never in the workload-container ``tls_paths``
    and never on the workload object at all.
    """
    from src.core.base_workload import TLSPaths, WorkloadBase
    from src.literals import BACKUP_CA_FILENAME
    from src.managers.backup import BackupManager

    # backup CA must be neither a workload (container) TLS path nor any
    # attribute of the workload -- it belongs to the charm-process side.
    assert not hasattr(TLSPaths, "backup_ca")
    assert not hasattr(WorkloadBase, "backup_ca_path")

    state = mocker.MagicMock()
    state.charm.charm_dir = tmp_path
    mgr = BackupManager(state=state, workload=mocker.MagicMock())
    assert mgr._backup_ca_path == tmp_path / BACKUP_CA_FILENAME


def test_backup_manager_bucket_resource_built_with_checksum_workaround(mocker, tmp_path):
    import boto3

    from src.managers.backup import BackupManager

    state = mocker.MagicMock()
    state.charm.charm_dir = tmp_path
    workload = mocker.MagicMock()

    fake_session = mocker.MagicMock()
    fake_resource = mocker.MagicMock()
    fake_bucket = mocker.MagicMock()
    fake_resource.Bucket.return_value = fake_bucket
    fake_session.resource.return_value = fake_resource
    mocker.patch("boto3.Session", return_value=fake_session)

    mgr = BackupManager(state=state, workload=workload)
    bucket = mgr._get_bucket_resource(
        {
            "bucket": "b",
            "endpoint": "https://s3.example.com",
            "access-key": "AK",
            "secret-key": "SK",
            "region": "us-west-2",
        }
    )

    _, session_kwargs = boto3.Session.call_args
    assert session_kwargs["aws_access_key_id"] == "AK"
    assert session_kwargs["aws_secret_access_key"] == "SK"
    assert session_kwargs["region_name"] == "us-west-2"
    args, kwargs = fake_session.resource.call_args
    assert args[0] == "s3"
    assert kwargs["endpoint_url"] == "https://s3.example.com"
    cfg = kwargs["config"]
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"
    assert kwargs["verify"] is True  # no tls-ca-chain provided
    fake_resource.Bucket.assert_called_once_with("b")
    assert bucket is fake_bucket


def test_backup_manager_bucket_resource_uses_ca_chain_when_provided(mocker, tmp_path):
    import boto3

    from src.literals import BACKUP_CA_FILENAME
    from src.managers.backup import BackupManager

    state = mocker.MagicMock()
    state.charm.charm_dir = tmp_path
    workload = mocker.MagicMock()
    mocker.patch("boto3.Session")

    mgr = BackupManager(state=state, workload=workload)
    mgr._get_bucket_resource(
        {
            "bucket": "b",
            "endpoint": "x",
            "access-key": "AK",
            "secret-key": "SK",
            "tls-ca-chain": ["-----BEGIN CERTIFICATE-----\n..."],
        }
    )
    _, kwargs = boto3.Session.return_value.resource.call_args
    assert kwargs["verify"] == str(tmp_path / BACKUP_CA_FILENAME)


def test_backup_manager_store_tls_ca_chain_writes_charm_local_file(mocker, tmp_path):
    from src.literals import BACKUP_CA_FILENAME
    from src.managers.backup import BackupManager

    state = mocker.MagicMock()
    state.charm.charm_dir = tmp_path
    workload = mocker.MagicMock()
    mgr = BackupManager(state=state, workload=workload)

    certs = [
        "-----BEGIN CERTIFICATE-----\nMIICert1\n-----END CERTIFICATE-----",
        "-----BEGIN CERTIFICATE-----\nMIICert2\n-----END CERTIFICATE-----",
    ]
    mgr.store_tls_ca_chain({"tls-ca-chain": certs})
    assert (tmp_path / BACKUP_CA_FILENAME).read_text() == "\n".join(certs)

    mgr.remove_tls_ca_chain()
    assert not (tmp_path / BACKUP_CA_FILENAME).exists()


def test_backup_manager_store_tls_ca_chain_noop_without_chain(mocker, tmp_path):
    from src.literals import BACKUP_CA_FILENAME
    from src.managers.backup import BackupManager

    state = mocker.MagicMock()
    state.charm.charm_dir = tmp_path
    mgr = BackupManager(state=state, workload=mocker.MagicMock())
    mgr.store_tls_ca_chain({"bucket": "b"})
    assert not (tmp_path / BACKUP_CA_FILENAME).exists()


def test_backup_manager_store_tls_ca_chain_rejects_non_list(mocker, tmp_path):
    from src.literals import BACKUP_CA_FILENAME
    from src.managers.backup import BackupManager

    state = mocker.MagicMock()
    state.charm.charm_dir = tmp_path
    mgr = BackupManager(state=state, workload=mocker.MagicMock())
    # A bare string must not be char-joined into a corrupt bundle.
    mgr.store_tls_ca_chain({"tls-ca-chain": "-----BEGIN CERTIFICATE-----"})
    assert not (tmp_path / BACKUP_CA_FILENAME).exists()


def test_backup_manager_store_tls_ca_chain_rejects_non_pem_items(mocker, tmp_path):
    from src.literals import BACKUP_CA_FILENAME
    from src.managers.backup import BackupManager

    state = mocker.MagicMock()
    state.charm.charm_dir = tmp_path
    mgr = BackupManager(state=state, workload=mocker.MagicMock())
    # A list whose items lack a PEM armour header is malformed; the whole
    # chain is rejected rather than written as an unloadable CA bundle.
    mgr.store_tls_ca_chain({"tls-ca-chain": ["not-a-cert", "also-not-a-cert"]})
    assert not (tmp_path / BACKUP_CA_FILENAME).exists()


def test_create_bucket_us_east_1_omits_location_constraint(mocker):
    from src.managers.backup import BackupManager

    state = mocker.MagicMock()
    workload = mocker.MagicMock()
    fake_bucket = mocker.MagicMock()
    mocker.patch.object(BackupManager, "_get_bucket_resource", return_value=fake_bucket)

    BackupManager(state=state, workload=workload).create_bucket(
        {"bucket": "b", "region": "us-east-1"}
    )
    fake_bucket.create.assert_called_once_with()
    fake_bucket.wait_until_exists.assert_called_once()


def test_create_bucket_non_default_region_sets_location_constraint(mocker):
    from src.managers.backup import BackupManager

    state = mocker.MagicMock()
    workload = mocker.MagicMock()
    fake_bucket = mocker.MagicMock()
    mocker.patch.object(BackupManager, "_get_bucket_resource", return_value=fake_bucket)

    BackupManager(state=state, workload=workload).create_bucket(
        {"bucket": "b", "region": "eu-west-1"}
    )
    fake_bucket.create.assert_called_once_with(
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"}
    )


def test_create_bucket_tolerates_existing_buckets(mocker):
    from botocore.exceptions import ClientError

    from src.managers.backup import BackupManager

    state = mocker.MagicMock()
    workload = mocker.MagicMock()
    fake_bucket = mocker.MagicMock()
    mocker.patch.object(BackupManager, "_get_bucket_resource", return_value=fake_bucket)

    for token in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists", "BucketNameUnavailable"):
        fake_bucket.reset_mock()
        fake_bucket.create.side_effect = ClientError(
            {"Error": {"Code": token, "Message": token}}, "CreateBucket"
        )
        # Must not raise
        BackupManager(state=state, workload=workload).create_bucket(
            {"bucket": "b", "region": "us-east-1"}
        )


def test_create_bucket_raises_for_other_client_errors(mocker):
    import pytest
    from botocore.exceptions import ClientError

    from common.exceptions import ValkeyBackupError
    from src.managers.backup import BackupManager

    state = mocker.MagicMock()
    workload = mocker.MagicMock()
    fake_bucket = mocker.MagicMock()
    fake_bucket.create.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "CreateBucket"
    )
    mocker.patch.object(BackupManager, "_get_bucket_resource", return_value=fake_bucket)

    with pytest.raises(ValkeyBackupError):
        BackupManager(state=state, workload=workload).create_bucket(
            {"bucket": "b", "region": "us-east-1"}
        )


def test_list_backups_filters_by_prefix_and_sorts_descending(mocker):
    from src.managers.backup import BackupManager

    state = mocker.MagicMock()
    state.cluster.s3_credentials = {
        "bucket": "b",
        "path": "valkey",
        "access-key": "k",
        "secret-key": "s",
    }
    workload = mocker.MagicMock()
    fake_bucket = mocker.MagicMock()
    fake_objects = [
        mocker.MagicMock(key=k)
        for k in (
            "valkey/2026-05-13T10:00:00Z",
            "valkey/2026-05-12T10:00:00Z",
            "valkey/2026-05-14T10:00:00Z",
            # Non-backup objects under the prefix must be excluded.
            "valkey/.s3-lifecycle-marker",
            "valkey/subdir/something",
        )
    ]
    fake_bucket.objects.filter.return_value = fake_objects
    mocker.patch.object(BackupManager, "_get_bucket_resource", return_value=fake_bucket)

    result = BackupManager(state=state, workload=workload).list_backups()
    assert result == [
        "2026-05-14T10:00:00Z",
        "2026-05-13T10:00:00Z",
        "2026-05-12T10:00:00Z",
    ]
    fake_bucket.objects.filter.assert_called_once_with(Prefix="valkey/")


def test_list_backups_wraps_client_error(mocker):
    import pytest
    from botocore.exceptions import ClientError

    from common.exceptions import ValkeyBackupError
    from src.managers.backup import BackupManager

    state = mocker.MagicMock()
    state.cluster.s3_credentials = {
        "bucket": "b",
        "path": "p",
        "access-key": "k",
        "secret-key": "s",
    }
    workload = mocker.MagicMock()
    fake_bucket = mocker.MagicMock()
    fake_bucket.objects.filter.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "x"}}, "ListObjectsV2"
    )
    mocker.patch.object(BackupManager, "_get_bucket_resource", return_value=fake_bucket)

    with pytest.raises(ValkeyBackupError):
        BackupManager(state=state, workload=workload).list_backups()


def test_format_backup_list_renders_table():
    from src.managers.backup import BackupManager

    formatted = BackupManager.format_backup_list(["2026-05-13T10:00:00Z"])
    assert "backup-id" in formatted
    assert "backup-status" in formatted
    assert "2026-05-13T10:00:00Z" in formatted
    assert "finished" in formatted


def test_format_backup_list_empty():
    from src.managers.backup import BackupManager

    assert BackupManager.format_backup_list([]) == "No backups found."


def _make_state(mocker, *, backup_id="", admin_pw="pw", tls=False):
    state = mocker.MagicMock()
    state.cluster.s3_credentials = {
        "bucket": "b",
        "path": "valkey",
        "endpoint": "x",
        "access-key": "AK",
        "secret-key": "SK",
    }
    state.unit_server.model.backup_id = backup_id
    state.unit_server.valkey_admin_password = admin_pw
    state.unit_server.is_tls_enabled = tls
    state.endpoint = "127.0.0.1"
    return state


def _drain(reader) -> None:
    """Mimic boto3.upload_fileobj draining the stream to completion."""
    while reader.read(8192):
        pass


def test_create_backup_success_sets_lock_streams_and_clears(mocker):
    import io

    from src.managers.backup import BackupManager

    state = _make_state(mocker)
    workload = mocker.MagicMock()
    workload.cli = "valkey-cli"
    proc = mocker.MagicMock()
    proc.stdout = io.BytesIO(b"VALKEY0011" + b"\x00" * 200)
    proc.wait.return_value = (0, "")
    workload.exec_stream.return_value = proc

    fake_bucket = mocker.MagicMock()
    fake_bucket.upload_fileobj.side_effect = lambda reader, key, **kw: _drain(reader)
    mocker.patch.object(BackupManager, "_get_bucket_resource", return_value=fake_bucket)
    fixed_now = mocker.patch("src.managers.backup.datetime")
    fixed_now.now.return_value.strftime.return_value = "2026-05-13T10:00:00Z"

    mgr = BackupManager(state=state, workload=workload)
    backup_id = mgr.create_backup()

    assert backup_id == "2026-05-13T10:00:00Z"
    update_calls = state.unit_server.update.call_args_list
    assert update_calls[0].args[0] == {"backup_id": "2026-05-13T10:00:00Z"}
    assert update_calls[-1].args[0] == {"backup_id": ""}
    fake_bucket.upload_fileobj.assert_called_once()
    pos_args, _kwargs = fake_bucket.upload_fileobj.call_args
    assert pos_args[1] == "valkey/2026-05-13T10:00:00Z"
    proc.wait.assert_called_once()
    # A valid RDB was streamed, so no cleanup delete happened.
    fake_bucket.Object.assert_not_called()


def test_create_backup_rejects_empty_or_non_rdb_stream(mocker):
    import io

    import pytest

    from common.exceptions import ValkeyBackupError
    from src.managers.backup import BackupManager

    fixed_now = mocker.patch("src.managers.backup.datetime")
    fixed_now.now.return_value.strftime.return_value = "2026-05-13T10:00:00Z"

    for payload in (b"", b"-ERR auth failed\r\n"):
        state = _make_state(mocker)
        workload = mocker.MagicMock()
        workload.cli = "valkey-cli"
        proc = mocker.MagicMock()
        proc.stdout = io.BytesIO(payload)
        proc.wait.return_value = (0, "")
        workload.exec_stream.return_value = proc

        fake_bucket = mocker.MagicMock()
        fake_bucket.upload_fileobj.side_effect = lambda reader, key, **kw: _drain(reader)
        mocker.patch.object(BackupManager, "_get_bucket_resource", return_value=fake_bucket)

        with pytest.raises(ValkeyBackupError):
            BackupManager(state=state, workload=workload).create_backup()

        # The bogus object is deleted and the lock is released.
        fake_bucket.Object.assert_called_once_with("valkey/2026-05-13T10:00:00Z")
        fake_bucket.Object.return_value.delete.assert_called_once()
        assert state.unit_server.update.call_args_list[-1].args[0] == {"backup_id": ""}


def test_create_backup_deletes_object_and_raises_when_cli_fails(mocker):
    import pytest

    from common.exceptions import ValkeyBackupError
    from src.managers.backup import BackupManager

    state = _make_state(mocker)
    workload = mocker.MagicMock()
    workload.cli = "valkey-cli"
    proc = mocker.MagicMock()
    proc.wait.return_value = (1, "WRONGPASS")
    workload.exec_stream.return_value = proc

    fake_bucket = mocker.MagicMock()
    mocker.patch.object(BackupManager, "_get_bucket_resource", return_value=fake_bucket)
    fixed_now = mocker.patch("src.managers.backup.datetime")
    fixed_now.now.return_value.strftime.return_value = "2026-05-13T10:00:00Z"

    with pytest.raises(ValkeyBackupError):
        BackupManager(state=state, workload=workload).create_backup()

    fake_bucket.Object.assert_called_once_with("valkey/2026-05-13T10:00:00Z")
    fake_bucket.Object.return_value.delete.assert_called_once()
    last_update = state.unit_server.update.call_args_list[-1]
    assert last_update.args[0] == {"backup_id": ""}


def test_get_statuses_idle(mocker):
    from src.managers.backup import BackupManager
    from src.statuses import CharmStatuses

    state = mocker.MagicMock()
    state.statuses.get.return_value.root = []
    state.unit_server.is_backup_in_progress = False
    state.s3_relation = None
    state.cluster.s3_credentials = None

    statuses = BackupManager(state=state, workload=mocker.MagicMock()).get_statuses(scope="unit")
    assert statuses == [CharmStatuses.ACTIVE_IDLE.value]


def test_get_statuses_backup_in_progress_unit_scope(mocker):
    from src.managers.backup import BackupManager
    from src.statuses import BackupStatuses

    state = mocker.MagicMock()
    state.statuses.get.return_value.root = []
    state.unit_server.is_backup_in_progress = True
    state.s3_relation = None
    state.cluster.s3_credentials = None

    statuses = BackupManager(state=state, workload=mocker.MagicMock()).get_statuses(scope="unit")
    assert BackupStatuses.BACKUP_IN_PROGRESS.value in statuses


def _blocking_evt(mocker, *, relation=True, credentials=True, alive=True):
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.state.s3_relation = mocker.MagicMock() if relation else None
    charm.state.cluster.s3_credentials = {"bucket": "b"} if credentials else None
    charm.workload.alive.return_value = alive
    charm.state.unit_server.is_backup_in_progress = False
    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    return evt


def test_blocking_reason_no_relation(mocker):
    assert "No S3 relation" in _blocking_evt(mocker, relation=False)._blocking_reason()


def test_blocking_reason_no_credentials(mocker):
    assert "credentials" in _blocking_evt(mocker, credentials=False)._blocking_reason().lower()


def test_blocking_reason_workload_down(mocker):
    assert "not running" in _blocking_evt(mocker, alive=False)._blocking_reason()


def test_blocking_reason_none_when_all_ok(mocker):
    assert _blocking_evt(mocker)._blocking_reason() is None


def test_blocking_reason_in_progress_check_is_toggleable(mocker):
    # The default checks for a running backup (create-backup); list-backups
    # passes check_running_operations=False because it is read-only.
    evt = _blocking_evt(mocker)
    evt.charm.state.unit_server.is_backup_in_progress = True
    assert "already in progress" in evt._blocking_reason()
    assert evt._blocking_reason(check_running_operations=False) is None


def test_on_s3_credentials_changed_stores_ca_on_all_units(mocker):
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.unit.is_leader.return_value = False
    charm.state.peer_relation = mocker.MagicMock()
    charm.backup_manager = mocker.MagicMock()

    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    evt.s3_requirer = mocker.MagicMock()
    evt.s3_requirer.get_storage_connection_info.return_value = {
        "bucket": "b",
        "endpoint": "https://e/",
        "path": "/p/",
        "access-key": "AK",
        "secret-key": "SK",
        "tls-ca-chain": ["-----CERT-----"],
    }

    evt._on_s3_credentials_changed(mocker.MagicMock())
    charm.backup_manager.store_tls_ca_chain.assert_called_once()
    charm.state.cluster.update.assert_not_called()


def test_on_s3_credentials_changed_leader_writes_databag(mocker):
    import json

    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.unit.is_leader.return_value = True
    charm.state.peer_relation = mocker.MagicMock()

    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    evt.s3_requirer = mocker.MagicMock()
    evt.s3_requirer.get_storage_connection_info.return_value = {
        "bucket": " b ",
        "endpoint": "https://e/",
        "path": "/p/",
        "access-key": "AK",
        "secret-key": "SK",
    }

    evt._on_s3_credentials_changed(mocker.MagicMock())
    charm.backup_manager.create_bucket.assert_called_once()
    args, _ = charm.state.cluster.update.call_args
    payload = args[0]
    creds = json.loads(payload["s3_credentials"])
    assert creds["bucket"] == "b"
    assert creds["endpoint"] == "https://e"
    assert creds["path"] == "p"


def test_safe_error_surfaces_s3_code_only():
    """Only the structured S3 error code reaches the action result."""
    from botocore.exceptions import ClientError

    from src.common.exceptions import ValkeyBackupError
    from src.events.backup import _safe_error

    client_err = ClientError(
        {
            "Error": {
                "Code": "AccessDenied",
                "Message": "leak https://s3.internal RequestId=ABC123",
            }
        },
        "PutObject",
    )
    wrapped = ValkeyBackupError(client_err)
    wrapped.__cause__ = client_err

    msg = _safe_error(wrapped)
    assert msg == "S3 request failed: AccessDenied"
    assert "s3.internal" not in msg
    assert "RequestId" not in msg


def test_safe_error_generic_for_non_client_errors():
    """Errors that are not S3 ClientErrors collapse to a generic message."""
    from src.common.exceptions import ValkeyBackupError
    from src.events.backup import _safe_error

    wrapped = ValkeyBackupError("valkey-cli --rdb exited 1: connection refused 10.1.2.3:6379")
    msg = _safe_error(wrapped)
    assert "10.1.2.3" not in msg
    assert "debug-log" in msg


def test_on_s3_credentials_changed_rejects_path_that_strips_to_empty(mocker):
    """path='/' normalises to '' and must be rejected, not stored."""
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.unit.is_leader.return_value = True
    charm.state.peer_relation = mocker.MagicMock()

    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    evt.s3_requirer = mocker.MagicMock()
    evt.s3_requirer.get_storage_connection_info.return_value = {
        "bucket": "b",
        "endpoint": "https://e",
        "path": "/",
        "access-key": "AK",
        "secret-key": "SK",
    }

    evt._on_s3_credentials_changed(mocker.MagicMock())
    charm.backup_manager.create_bucket.assert_not_called()
    charm.state.cluster.update.assert_not_called()


def test_on_s3_credentials_changed_skips_when_envelope_unchanged(mocker):
    """An unchanged envelope must not trigger another create_bucket call."""
    from src.events.backup import BackupEvents

    envelope = {
        "bucket": "b",
        "endpoint": "https://e",
        "path": "p",
        "access-key": "AK",
        "secret-key": "SK",
    }
    charm = mocker.MagicMock()
    charm.unit.is_leader.return_value = True
    charm.state.peer_relation = mocker.MagicMock()
    charm.state.cluster.s3_credentials = dict(envelope)  # already stored

    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    evt.s3_requirer = mocker.MagicMock()
    evt.s3_requirer.get_storage_connection_info.return_value = dict(envelope)

    evt._on_s3_credentials_changed(mocker.MagicMock())
    charm.backup_manager.create_bucket.assert_not_called()
    charm.state.cluster.update.assert_not_called()


def test_on_s3_credentials_changed_missing_params_skips_databag(mocker):
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.unit.is_leader.return_value = True
    charm.state.peer_relation = mocker.MagicMock()

    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    evt.s3_requirer = mocker.MagicMock()
    evt.s3_requirer.get_storage_connection_info.return_value = {"bucket": "b"}

    evt._on_s3_credentials_changed(mocker.MagicMock())
    charm.state.cluster.update.assert_not_called()
    charm.backup_manager.create_bucket.assert_not_called()


def test_on_s3_credentials_gone_defers_during_backup(mocker):
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.state.unit_server.is_backup_in_progress = True

    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    event = mocker.MagicMock()
    evt._on_s3_credentials_gone(event)
    event.defer.assert_called_once()
    charm.backup_manager.remove_tls_ca_chain.assert_not_called()


def test_on_s3_credentials_gone_removes_ca_and_clears_databag(mocker):
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.state.unit_server.is_backup_in_progress = False
    charm.unit.is_leader.return_value = True

    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    evt._on_s3_credentials_gone(mocker.MagicMock())
    charm.backup_manager.remove_tls_ca_chain.assert_called_once_with()
    charm.state.cluster.update.assert_called_once_with({"s3_credentials": ""})


def test_on_create_backup_action_happy(mocker):
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.backup_manager.create_backup.return_value = "2026-05-13T10:00:00Z"
    charm.state.unit_server.is_backup_in_progress = False
    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    mocker.patch.object(evt, "_blocking_reason", return_value=None)

    event = mocker.MagicMock()
    evt._on_create_backup_action(event)
    event.set_results.assert_called_with({"backup-id": "2026-05-13T10:00:00Z"})
    event.fail.assert_not_called()


def test_on_create_backup_action_audit_logs_invocation(mocker, caplog):
    """Each create-backup invocation is audit-logged with its action id and unit."""
    import logging

    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.unit.name = "valkey/2"
    charm.backup_manager.create_backup.return_value = "2026-05-13T10:00:00Z"
    charm.state.unit_server.is_backup_in_progress = False
    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    mocker.patch.object(evt, "_blocking_reason", return_value=None)

    event = mocker.MagicMock()
    event.id = "42"
    with caplog.at_level(logging.INFO):
        evt._on_create_backup_action(event)

    audit = [r.message for r in caplog.records if "audit: create-backup" in r.message]
    assert audit, "expected an audit log line for the action invocation"
    assert "action_id=42" in audit[0]
    assert "unit=valkey/2" in audit[0]


def test_on_create_backup_action_fails_when_guard_blocks(mocker):
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    mocker.patch.object(evt, "_blocking_reason", return_value="No S3 relation.")

    event = mocker.MagicMock()
    evt._on_create_backup_action(event)
    event.fail.assert_called_once_with("No S3 relation.")
    charm.backup_manager.create_backup.assert_not_called()


def test_on_create_backup_action_handles_backup_error(mocker):
    from common.exceptions import ValkeyBackupError
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.backup_manager.create_backup.side_effect = ValkeyBackupError("boom")
    charm.state.unit_server.is_backup_in_progress = False
    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    mocker.patch.object(evt, "_blocking_reason", return_value=None)

    event = mocker.MagicMock()
    evt._on_create_backup_action(event)
    event.fail.assert_called_once()


def test_on_list_backups_action_returns_table(mocker):
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.backup_manager.list_backups.return_value = ["2026-05-13T10:00:00Z"]
    charm.backup_manager.format_backup_list.return_value = "table"
    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    mocker.patch.object(evt, "_blocking_reason", return_value=None)

    event = mocker.MagicMock()
    event.params = {"output": "table"}
    evt._on_list_backups_action(event)
    event.set_results.assert_called_with({"backups": "table"})


def test_on_list_backups_action_returns_json(mocker):
    import json

    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.backup_manager.list_backups.return_value = [
        "2026-05-14T10:00:00Z",
        "2026-05-13T10:00:00Z",
    ]
    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    mocker.patch.object(evt, "_blocking_reason", return_value=None)

    event = mocker.MagicMock()
    event.params = {"output": "json"}
    evt._on_list_backups_action(event)
    _, kwargs_or_args = event.set_results.call_args
    payload = event.set_results.call_args.args[0]["backups"]
    assert json.loads(payload) == [
        {"backup-id": "2026-05-14T10:00:00Z", "backup-status": "finished"},
        {"backup-id": "2026-05-13T10:00:00Z", "backup-status": "finished"},
    ]
    # The text formatter is not used for JSON output.
    charm.backup_manager.format_backup_list.assert_not_called()


def test_on_list_backups_action_rejects_invalid_format(mocker):
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm

    event = mocker.MagicMock()
    event.params = {"output": "yaml"}
    evt._on_list_backups_action(event)
    event.fail.assert_called_once()
    assert "invalid output format" in event.fail.call_args[0][0]
    charm.backup_manager.list_backups.assert_not_called()


def test_storage_detaching_refuses_during_backup(cloud_spec):
    import pytest
    from ops import testing

    from src.charm import ValkeyCharm
    from src.literals import (
        DATA_STORAGE,
        PEER_RELATION,
        STATUS_PEERS_RELATION,
    )

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"backup_id": "2026-05-13T10:00:00Z"},
    )
    status_peer = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    storage = testing.Storage(name=DATA_STORAGE)
    state_in = testing.State(
        model=testing.Model(name="m", type="lxd", cloud_spec=cloud_spec),
        leader=True,
        relations={peer, status_peer},
        storages={storage},
        containers={testing.Container(name="valkey", can_connect=True)},
    )

    # Must raise so the hook errors and Juju retries teardown until the
    # backup finishes -- a plain return would let scale-down proceed.
    with pytest.raises(testing.errors.UncaughtCharmError) as exc_info:
        ctx.run(ctx.on.storage_detaching(storage), state_in)
    assert "ValkeyBackupInProgressError" in str(exc_info.value)


def test_charm_constructs_backup_manager_and_events(cloud_spec):
    from ops import testing

    from src.charm import ValkeyCharm
    from src.literals import PEER_RELATION, STATUS_PEERS_RELATION

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    state_in = testing.State(
        model=testing.Model(name="m", type="lxd", cloud_spec=cloud_spec),
        leader=True,
        relations={peer, status_peer},
        containers={testing.Container(name="valkey", can_connect=True)},
    )
    with ctx(ctx.on.update_status(), state_in) as manager:
        assert manager.charm.backup_manager.__class__.__name__ == "BackupManager"
        assert manager.charm.backup_events is not None


def test_on_list_backups_action_runs_while_a_backup_is_in_progress(mocker):
    """list-backups is read-only.

    A backup running on the unit must not block it (the in-progress check
    is create-backup only).
    """
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.state.s3_relation = mocker.MagicMock()
    charm.state.cluster.s3_credentials = {"bucket": "b"}
    charm.workload.alive.return_value = True
    charm.state.unit_server.is_backup_in_progress = True  # backup running here
    charm.backup_manager.list_backups.return_value = []
    charm.backup_manager.format_backup_list.return_value = "No backups found."
    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm

    event = mocker.MagicMock()
    event.params = {}  # default output format (table)
    evt._on_list_backups_action(event)
    event.fail.assert_not_called()
    charm.backup_manager.list_backups.assert_called_once()


def test_on_s3_credentials_gone_non_leader_does_not_clear_databag(mocker):
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.state.unit_server.is_backup_in_progress = False
    charm.unit.is_leader.return_value = False

    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    evt._on_s3_credentials_gone(mocker.MagicMock())
    charm.backup_manager.remove_tls_ca_chain.assert_called_once()
    charm.state.cluster.update.assert_not_called()


def test_on_s3_credentials_changed_defers_without_peer_relation(mocker):
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.unit.is_leader.return_value = True
    charm.state.peer_relation = None

    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm
    evt.s3_requirer = mocker.MagicMock()
    evt.s3_requirer.get_storage_connection_info.return_value = {
        "bucket": "b",
        "endpoint": "e",
        "path": "p",
        "access-key": "AK",
        "secret-key": "SK",
    }

    event = mocker.MagicMock()
    evt._on_s3_credentials_changed(event)
    event.defer.assert_called_once()


def test_on_create_backup_action_rejected_when_backup_already_running(mocker):
    """The in-progress check lives in _blocking_reason (default-on for create)."""
    from src.events.backup import BackupEvents

    charm = mocker.MagicMock()
    charm.state.s3_relation = mocker.MagicMock()
    charm.state.cluster.s3_credentials = {"bucket": "b"}
    charm.workload.alive.return_value = True
    charm.state.unit_server.is_backup_in_progress = True
    evt = BackupEvents.__new__(BackupEvents)
    evt.charm = charm

    event = mocker.MagicMock()
    evt._on_create_backup_action(event)
    event.fail.assert_called_once()
    assert "already in progress" in event.fail.call_args[0][0]
    charm.backup_manager.create_backup.assert_not_called()


def test_get_statuses_credentials_missing(mocker):
    from src.managers.backup import BackupManager
    from src.statuses import BackupStatuses

    state = mocker.MagicMock()
    state.statuses.get.return_value.root = []
    state.unit_server.is_backup_in_progress = False
    state.s3_relation = mocker.MagicMock()
    state.cluster.s3_credentials = None

    statuses = BackupManager(state=state, workload=mocker.MagicMock()).get_statuses(scope="app")
    assert BackupStatuses.BACKUP_S3_PARAMETERS_MISSING.value in statuses


def test_create_backup_kills_producer_on_upload_failure(mocker):
    """A mid-stream upload failure stops valkey-cli; boto3 aborts the MPU itself.

    No explicit object delete is issued -- a failed multipart/PutObject leaves
    no object to delete, and boto3's managed transfer aborts the upload.
    """
    import pytest
    from botocore.exceptions import ClientError

    from common.exceptions import ValkeyBackupError
    from src.managers.backup import BackupManager

    state = _make_state(mocker)
    workload = mocker.MagicMock()
    workload.cli = "valkey-cli"
    proc = mocker.MagicMock()
    workload.exec_stream.return_value = proc

    fake_bucket = mocker.MagicMock()
    fake_bucket.upload_fileobj.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "x"}}, "PutObject"
    )
    mocker.patch.object(BackupManager, "_get_bucket_resource", return_value=fake_bucket)
    fixed_now = mocker.patch("src.managers.backup.datetime")
    fixed_now.now.return_value.strftime.return_value = "2026-05-13T10:00:00Z"

    with pytest.raises(ValkeyBackupError):
        BackupManager(state=state, workload=workload).create_backup()

    proc.kill.assert_called_once()
    fake_bucket.Object.assert_not_called()
    # The lock is still released on the way out.
    assert state.unit_server.update.call_args_list[-1].args[0] == {"backup_id": ""}
