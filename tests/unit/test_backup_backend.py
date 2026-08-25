#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the storage backends behind BackupManager.

The only place boto3 is faked: everything above the StorageBackend Protocol is
tested against a fake backend in test_backup.py / test_restore.py. Exceptions are
imported flat (`common.exceptions`) because src/ imports are flat, so the class
the backend raises is the flat one, not the `src.`-prefixed copy.
"""

import pytest
from botocore.exceptions import ClientError


def _s3_params(**overrides):
    """Build a valid S3Parameters, overriding individual fields by name.

    Flat import: src/ imports are flat, so `core.models.S3Parameters` is the class
    production builds and isinstance-checks against -- the `src.`-prefixed copy is
    a different class object and would miss every isinstance dispatch.
    """
    from core.models import S3Parameters

    base = {
        "bucket": "b",
        "endpoint": "https://e",
        "path": "valkey",
        "access-key": "AK",
        "secret-key": "SK",
    }
    base.update(overrides)
    return S3Parameters.model_validate(base)


def _backend(mocker, **overrides):
    """Build an S3Backend with its boto3 Bucket faked out; return (backend, bucket)."""
    from src.managers.backup_backend import S3Backend

    bucket = mocker.MagicMock()
    mocker.patch.object(S3Backend, "_bucket", return_value=bucket)
    return S3Backend(_s3_params(**overrides), mocker.MagicMock()), bucket


# ── client construction ─────────────────────────────────────────────────


def test_s3backend_bucket_built_with_checksum_workaround(mocker, tmp_path):
    import boto3

    from src.managers.backup_backend import S3Backend

    fake_session = mocker.MagicMock()
    fake_resource = mocker.MagicMock()
    fake_bucket = mocker.MagicMock()
    fake_resource.Bucket.return_value = fake_bucket
    fake_session.resource.return_value = fake_resource
    mocker.patch("boto3.Session", return_value=fake_session)

    params = _s3_params(endpoint="https://s3.example.com", region="us-west-2")
    bucket = S3Backend(params, tmp_path / "ca.pem")._bucket()

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


def test_s3backend_bucket_uses_ca_chain_when_provided(mocker, tmp_path):
    import boto3

    from src.managers.backup_backend import S3Backend

    mocker.patch("boto3.Session")
    ca_path = tmp_path / "s3_ca_chain.pem"

    params = _s3_params(tls_ca_chain=["-----BEGIN CERTIFICATE-----\n..."])
    S3Backend(params, ca_path)._bucket()

    _, kwargs = boto3.Session.return_value.resource.call_args
    assert kwargs["verify"] == ca_path.as_posix()


def test_s3backend_location_names_the_destination_without_credentials(mocker):
    """The audit trail gets host/bucket/prefix -- never anything from the URL's userinfo."""
    backend, _ = _backend(mocker, endpoint="https://AKIA:SECRET@s3.example.com:9000")
    assert backend.location == "s3://s3.example.com:9000/b/valkey"
    assert "SECRET" not in backend.location

    plain, _ = _backend(mocker, endpoint="https://s3.example.com")
    assert plain.location == "s3://s3.example.com/b/valkey"


# ── bucket lifecycle ────────────────────────────────────────────────────


def test_s3backend_ensure_container_us_east_1_omits_location_constraint(mocker):
    backend, bucket = _backend(mocker, region="us-east-1")

    backend.ensure_container()

    bucket.create.assert_called_once_with()
    bucket.wait_until_exists.assert_called_once()


def test_s3backend_ensure_container_non_default_region_sets_location_constraint(mocker):
    backend, bucket = _backend(mocker, region="eu-west-1")

    backend.ensure_container()

    bucket.create.assert_called_once_with(
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"}
    )


def test_s3backend_ensure_container_tolerates_existing_buckets(mocker):
    backend, bucket = _backend(mocker, region="us-east-1")

    for token in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists", "BucketNameUnavailable"):
        bucket.reset_mock()
        bucket.create.side_effect = ClientError(
            {"Error": {"Code": token, "Message": token}}, "CreateBucket"
        )
        backend.ensure_container()  # must not raise


def test_s3backend_ensure_container_wraps_other_client_errors(mocker):
    from common.exceptions import StorageBackendError

    backend, bucket = _backend(mocker, region="us-east-1")
    bucket.create.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "CreateBucket"
    )

    with pytest.raises(StorageBackendError) as excinfo:
        backend.ensure_container()
    assert excinfo.value.safe_code == "AccessDenied"


# ── list ────────────────────────────────────────────────────────────────


def test_s3backend_list_object_ids_filters_by_prefix_and_strips_it(mocker):
    backend, bucket = _backend(mocker)
    bucket.objects.filter.return_value = [
        mocker.MagicMock(key=k)
        for k in ("valkey/2026-05-13T10:00:00Z", "valkey/.s3-lifecycle-marker")
    ]

    assert backend.list_object_ids() == ["2026-05-13T10:00:00Z", ".s3-lifecycle-marker"]
    bucket.objects.filter.assert_called_once_with(Prefix="valkey/")


def test_s3backend_list_object_ids_wraps_client_error(mocker):
    from common.exceptions import StorageBackendError

    backend, bucket = _backend(mocker)
    bucket.objects.filter.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "x"}}, "ListObjectsV2"
    )

    with pytest.raises(StorageBackendError) as excinfo:
        backend.list_object_ids()
    assert excinfo.value.safe_code == "NoSuchBucket"


# ── head / upload / download / delete ───────────────────────────────────


def test_s3backend_head_ranges_over_the_first_bytes_only(mocker):
    backend, bucket = _backend(mocker)
    bucket.Object.return_value.get.return_value = {"Body": mocker.Mock(read=lambda: b"REDIS0011")}

    assert backend.head("2026-05-13T10:00:00Z") == b"REDIS0011"
    bucket.Object.assert_called_once_with("valkey/2026-05-13T10:00:00Z")
    bucket.Object.return_value.get.assert_called_once_with(Range="bytes=0-15")


def test_s3backend_head_wraps_client_error(mocker):
    from common.exceptions import StorageBackendError

    backend, bucket = _backend(mocker)
    bucket.Object.return_value.get.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject"
    )

    with pytest.raises(StorageBackendError) as excinfo:
        backend.head("2026-05-13T10:00:00Z")
    assert excinfo.value.safe_code == "NoSuchKey"


def test_s3backend_upload_streams_to_the_backup_id_key_in_parts(mocker):
    backend, bucket = _backend(mocker)
    reader = mocker.MagicMock()

    backend.upload("2026-05-13T10:00:00Z", reader)

    args, kwargs = bucket.upload_fileobj.call_args
    assert args[0] is reader
    assert args[1] == "valkey/2026-05-13T10:00:00Z"
    # Multipart, so a long stream never has to be buffered whole.
    assert kwargs["Config"].multipart_chunksize == 8 * 1024 * 1024


def test_s3backend_upload_wraps_client_error(mocker):
    from common.exceptions import StorageBackendError

    backend, bucket = _backend(mocker)
    bucket.upload_fileobj.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucket", "Message": "x"}}, "PutObject"
    )

    with pytest.raises(StorageBackendError) as excinfo:
        backend.upload("2026-05-13T10:00:00Z", mocker.MagicMock())
    assert excinfo.value.safe_code == "NoSuchBucket"


def test_s3backend_download_returns_the_body_and_its_length(mocker):
    """One GET yields both: the size rides along instead of costing a second round trip."""
    backend, bucket = _backend(mocker)
    body = mocker.Mock()
    bucket.Object.return_value.get.return_value = {"Body": body, "ContentLength": 4096}

    obj = backend.download("2026-05-13T10:00:00Z")

    assert obj.body is body
    assert obj.size == 4096
    bucket.Object.assert_called_once_with("valkey/2026-05-13T10:00:00Z")


def test_s3backend_download_tolerates_a_response_without_content_length(mocker):
    backend, bucket = _backend(mocker)
    bucket.Object.return_value.get.return_value = {"Body": mocker.Mock()}

    assert backend.download("2026-05-13T10:00:00Z").size is None


def test_s3backend_download_wraps_client_error(mocker):
    from common.exceptions import StorageBackendError

    backend, bucket = _backend(mocker)
    bucket.Object.return_value.get.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "x"}}, "GetObject"
    )

    with pytest.raises(StorageBackendError) as excinfo:
        backend.download("2026-05-13T10:00:00Z")
    assert excinfo.value.safe_code == "AccessDenied"


def test_s3backend_delete_removes_the_backup_id_key(mocker):
    backend, bucket = _backend(mocker)

    backend.delete("2026-05-13T10:00:00Z")

    bucket.Object.assert_called_once_with("valkey/2026-05-13T10:00:00Z")
    bucket.Object.return_value.delete.assert_called_once()


def test_s3backend_delete_swallows_errors(mocker):
    """Cleanup is best-effort: it must never mask the failure that triggered it."""
    backend, bucket = _backend(mocker)
    bucket.Object.return_value.delete.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "x"}}, "DeleteObject"
    )

    backend.delete("2026-05-13T10:00:00Z")  # must not raise


# ── dispatch ────────────────────────────────────────────────────────────


def test_build_backend_selects_by_credentials_type(mocker):
    """The one place a credentials type maps to a backend; BackupManager never sees it."""
    from managers.backup_backend import S3Backend, build_backend

    assert isinstance(build_backend(_s3_params(), mocker.MagicMock()), S3Backend)


def test_build_backend_rejects_unknown_credentials(mocker):
    """An unregistered credentials type fails loudly rather than silently doing nothing."""
    from common.exceptions import StorageBackendError
    from managers.backup_backend import build_backend

    with pytest.raises(StorageBackendError):
        build_backend(object(), mocker.MagicMock())  # pyright: ignore[reportArgumentType]
