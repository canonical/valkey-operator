#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import MagicMock, patch

import ops.testing
import pytest
import yaml
from charmlibs.interfaces.tls_certificates import (
    CertificateAvailableEvent,
    PrivateKey,
    ProviderCertificate,
)
from ops import testing

from src.charm import ValkeyCharm
from src.common.exceptions import ValkeyWorkloadCommandError
from src.literals import (
    CLIENT_TLS_RELATION_NAME,
    INTERNET_CERTS_SECRET_LABEL_SUFFIX,
    PEER_RELATION,
    STATUS_PEERS_RELATION,
    TLS_CLIENT_PRIVATE_KEY_CONFIG,
    TLSCARotationState,
)
from src.statuses import TLSStatuses

from .helpers import status_is

CONTAINER = "valkey"

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]


def test_client_tls_relation_created(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(id=3, endpoint=CLIENT_TLS_RELATION_NAME)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    state_out = ctx.run(ctx.on.relation_created(relation=client_tls_relation), state_in)
    assert status_is(state_out, TLSStatuses.ENABLING_CLIENT_TLS.value)


def test_enable_internal_tls_by_default(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with patch("charmlibs.pathops.ContainerPath.mkdir"):
        state_out = ctx.run(ctx.on.relation_created(relation=peer_relation), state_in)
        secret_out = state_out.get_secret(
            label=f"{PEER_RELATION}.{APP_NAME}.app.{INTERNET_CERTS_SECRET_LABEL_SUFFIX}"
        )
        assert secret_out.latest_content.get("internal-ca-certificate")
        assert secret_out.latest_content.get("internal-ca-private-key")


def test_enable_internal_tls_no_ca_cert_available(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(id=1, endpoint=PEER_RELATION)
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with patch("managers.tls.TLSManager.create_and_store_self_signed_certificate") as create_certs:
        state_out = ctx.run(ctx.on.relation_created(relation=peer_relation), state_in)
        create_certs.assert_not_called()
        assert "valkey_peers_relation_created" in [e.name for e in state_out.deferred]


def test_client_tls_relation_broken(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "start-state": "started",
            "tls-client-state": "tls",
            "client-cert-ready": "true",
        },
        local_app_data={},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(id=3, endpoint=CLIENT_TLS_RELATION_NAME)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("charmlibs.pathops.ContainerPath.mkdir"),
        patch("managers.tls.TLSManager.rehash_ca_certificates"),
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
        patch("managers.sentinel.SentinelManager.restart_service"),
    ):
        state_out = ctx.run(ctx.on.relation_broken(relation=client_tls_relation), state_in)
        assert reload_tls.call_count == 2
        assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "false"
        assert state_out.get_relation(1).local_unit_data.get("tls-client-state") == "no-tls"

        secret_out = state_out.get_secret(
            label=f"{PEER_RELATION}.{APP_NAME}.app.{INTERNET_CERTS_SECRET_LABEL_SUFFIX}"
        )
        assert secret_out.latest_content.get("internal-ca-certificate")
        assert secret_out.latest_content.get("internal-ca-private-key")


def test_client_tls_relation_broken_disabling_tls_fails(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "start-state": "started",
            "tls-client-state": "tls",
            "client-cert-ready": "true",
        },
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(id=3, endpoint=CLIENT_TLS_RELATION_NAME)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("charmlibs.pathops.ContainerPath.mkdir"),
        patch("managers.tls.TLSManager.rehash_ca_certificates"),
        patch(
            "managers.config.ConfigManager.set_config_properties", side_effect=ValueError("failed")
        ),
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
    ):
        state_out = ctx.run(ctx.on.relation_broken(relation=client_tls_relation), state_in)
        reload_tls.assert_not_called()
        assert "client_certificates_relation_broken" in [e.name for e in state_out.deferred]
        assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "true"
        assert state_out.get_relation(1).local_unit_data.get("tls-client-state") == "to-no-tls"


def test_client_tls_relation_broken_no_internal_ca_cert_available(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "start-state": "started",
            "tls-client-state": "tls",
            "client-cert-ready": "true",
        },
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(id=3, endpoint=CLIENT_TLS_RELATION_NAME)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
    ):
        state_out = ctx.run(ctx.on.relation_broken(relation=client_tls_relation), state_in)
        reload_tls.assert_not_called()
        assert "client_certificates_relation_broken" in [e.name for e in state_out.deferred]
        assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "true"
        assert state_out.get_relation(1).local_unit_data.get("tls-client-state") == "tls"


def test_client_tls_relation_broken_writing_internal_cert_fails(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "start-state": "started",
            "tls-client-state": "tls",
            "client-cert-ready": "true",
        },
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(id=3, endpoint=CLIENT_TLS_RELATION_NAME)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("charmlibs.pathops.ContainerPath.mkdir"),
        patch("core.base_workload.WorkloadBase.write_file", side_effect=PermissionError("failed")),
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
        patch("managers.sentinel.SentinelManager.restart_service"),
    ):
        state_out = ctx.run(ctx.on.relation_broken(relation=client_tls_relation), state_in)
        reload_tls.assert_called_once()
        assert "client_certificates_relation_broken" in [e.name for e in state_out.deferred]
        assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "false"
        assert state_out.get_relation(1).local_unit_data.get("tls-client-state") == "no-tls"


def test_client_tls_relation_broken_run_deferred_event(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "start-state": "started",
            "tls-client-state": "to-no-tls",
            "client-cert-ready": "true",
        },
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(id=3, endpoint=CLIENT_TLS_RELATION_NAME)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("managers.cluster.ClusterManager.reload_tls_settings"),
        patch("managers.sentinel.SentinelManager.restart_service"),
        patch("charmlibs.pathops.ContainerPath.mkdir"),
    ):
        state_out = ctx.run(ctx.on.relation_broken(relation=client_tls_relation), state_in)
        assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "false"
        assert state_out.get_relation(1).local_unit_data.get("tls-client-state") == "no-tls"


def test_client_certificate_available(cloud_spec):
    ca = MagicMock("my_ca")
    csr = MagicMock("my_csr")
    cert = MagicMock("my_cert")

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "tls-client-state": "to-tls"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=3,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    certificate = ProviderCertificate(
        relation_id=3, certificate=cert, certificate_signing_request=csr, ca=ca, chain=[cert, ca]
    )
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with ctx(ctx.on.update_status(), state_in) as manager:
        charm: ValkeyCharm = manager.charm
        event = MagicMock(spec=CertificateAvailableEvent)

        with (
            patch(
                "charmlibs.interfaces.tls_certificates.TLSCertificatesRequiresV4.get_assigned_certificates",
                return_value=([certificate], None),
            ),
            patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
            patch("managers.sentinel.SentinelManager.restart_service"),
            patch("managers.tls.TLSManager.write_certificate"),
            patch("managers.tls.TLSManager.will_certificate_expire"),
        ):
            event.certificate = certificate.certificate
            charm.tls_events._on_certificate_available(event)
            state_out = manager.run()

            reload_tls.assert_called_once()
            assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "true"
            assert state_out.get_relation(1).local_unit_data.get("tls-client-state") == "tls"


def test_client_certificate_available_enabling_fails(cloud_spec):
    ca = MagicMock("my_ca")
    csr = MagicMock("my_csr")
    cert = MagicMock("my_cert")

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "tls-client-state": "to-tls"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=3,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    certificate = ProviderCertificate(
        relation_id=3, certificate=cert, certificate_signing_request=csr, ca=ca, chain=[cert, ca]
    )
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with ctx(ctx.on.update_status(), state_in) as manager:
        charm: ValkeyCharm = manager.charm
        event = MagicMock(spec=CertificateAvailableEvent)

        with (
            patch(
                "charmlibs.interfaces.tls_certificates.TLSCertificatesRequiresV4.get_assigned_certificates",
                return_value=([certificate], None),
            ),
            patch(
                "managers.config.ConfigManager.set_config_properties",
                side_effect=ValueError("failed"),
            ),
            patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
            patch("managers.tls.TLSManager.write_certificate"),
            patch("managers.tls.TLSManager.will_certificate_expire"),
        ):
            event.certificate = certificate.certificate
            charm.tls_events._on_certificate_available(event)
            state_out = manager.run()

            reload_tls.assert_not_called()
            assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "true"
            assert state_out.get_relation(1).local_unit_data.get("tls-client-state") == "to-tls"


def test_client_certificate_available_not_all_units_ready(cloud_spec):
    ca = MagicMock("my_ca")
    client_csr = MagicMock("my_csr")
    client_cert = MagicMock("my_cert")

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "start-state": "started",
            "tls-client-state": "to-tls",
        },
        peers_data={1: {"start-state": "started", "client-cert-ready": "False"}},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=4,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    client_certificate = ProviderCertificate(
        relation_id=4,
        certificate=client_cert,
        certificate_signing_request=client_csr,
        ca=ca,
        chain=[client_cert, ca],
    )

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        planned_units=2,
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with ctx(ctx.on.update_status(), state_in) as manager:
        charm: ValkeyCharm = manager.charm
        event = MagicMock(spec=CertificateAvailableEvent)

        with (
            patch(
                "charmlibs.interfaces.tls_certificates.TLSCertificatesRequiresV4.get_assigned_certificates",
                side_effect=[([client_certificate], None)],
            ),
            patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
            patch("managers.tls.TLSManager.write_certificate"),
            patch("managers.tls.TLSManager.will_certificate_expire"),
        ):
            event.certificate = client_certificate.certificate
            charm.tls_events._on_certificate_available(event)
            state_out = manager.run()

            reload_tls.assert_not_called()
            assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "true"
            assert state_out.get_relation(1).local_unit_data.get("tls-client-state") == "to-tls"


def test_check_certificate_expiration(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "tls-client-state": "tls"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(id=3, endpoint=CLIENT_TLS_RELATION_NAME)
    container = testing.Container(name=CONTAINER, can_connect=True)

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with patch("workload_k8s.ValkeyK8sWorkload.exec", return_value="0"):
        state_out = ctx.run(ctx.on.update_status(), state_in)
        assert not state_out.get_relation(1).local_unit_data.get("tls-certificate-expiring")
        assert not status_is(state_out, TLSStatuses.CERTIFICATE_EXPIRING.value)

    with (
        patch(
            "core.base_workload.WorkloadBase.exec",
            side_effect=ValkeyWorkloadCommandError("failed"),
        ),
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)
        reload_tls.assert_not_called()
        assert state_out.get_relation(1).local_unit_data.get("tls-certificate-expiring") == "true"
        assert status_is(state_out, TLSStatuses.CERTIFICATE_EXPIRING.value)


def test_client_certificate_renewed(cloud_spec):
    # Mock the certificate values that are in the relation databag otherwise
    ca = MagicMock("my_ca")
    ca.raw = "my_ca"
    csr = MagicMock("my_csr")
    csr.raw = "my_csr"
    cert = MagicMock("my_cert")
    cert.raw = "my_cert"
    private_key = MagicMock("my_key")
    private_key.raw = "my_key"

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "tls-client-state": "tls"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=3,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    certificate = ProviderCertificate(
        relation_id=3, certificate=cert, certificate_signing_request=csr, ca=ca, chain=[cert, ca]
    )
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with ctx(ctx.on.update_status(), state_in) as manager:
        charm: ValkeyCharm = manager.charm
        event = MagicMock(spec=CertificateAvailableEvent)

        with (
            patch(
                "charmlibs.interfaces.tls_certificates.TLSCertificatesRequiresV4.get_assigned_certificates",
                return_value=([certificate], private_key),
            ),
            patch("charmlibs.pathops.ContainerPath.mkdir"),
            patch("charmlibs.pathops.ContainerPath.exists", return_value=True),
            patch("charmlibs.pathops.ContainerPath.read_text", return_value="my_ca"),
            patch("charmlibs.pathops.ContainerPath.write_text"),
            patch("workload_k8s.ValkeyK8sWorkload.write_file") as write_certs,
            patch("managers.tls.TLSManager.rehash_ca_certificates"),
            patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
        ):
            event.certificate = certificate.certificate
            charm.tls_events._on_certificate_available(event)
            state_out = manager.run()

            # we store the cert, the key and the ca cert
            assert write_certs.call_count == 3
            reload_tls.assert_called_once()
            assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "true"


def test_new_client_ca_single_unit(cloud_spec):
    # Mock the certificate values that are in the relation databag otherwise
    ca = MagicMock("my_new_ca")
    ca.raw = "my_new_ca"
    csr = MagicMock("my_csr")
    csr.raw = "my_csr"
    cert = MagicMock("my_cert")
    cert.raw = "my_cert"
    private_key = MagicMock("my_key")
    private_key.raw = "my_key"

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "tls-client-state": "tls"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=3,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    certificate = ProviderCertificate(
        relation_id=3, certificate=cert, certificate_signing_request=csr, ca=ca, chain=[cert, ca]
    )
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with ctx(ctx.on.update_status(), state_in) as manager:
        charm: ValkeyCharm = manager.charm
        event = MagicMock(spec=CertificateAvailableEvent)

        with (
            patch(
                "charmlibs.interfaces.tls_certificates.TLSCertificatesRequiresV4.get_assigned_certificates",
                return_value=([certificate], private_key),
            ),
            patch("charmlibs.pathops.ContainerPath.exists", return_value=True),
            patch("charmlibs.pathops.ContainerPath.read_text", return_value="my_old_ca"),
            patch("charmlibs.pathops.ContainerPath.write_text"),
            patch("workload_k8s.ValkeyK8sWorkload.write_file") as write_certs,
            patch("managers.tls.TLSManager.rehash_ca_certificates"),
            patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
        ):
            event.certificate = certificate.certificate
            charm.tls_events._on_certificate_available(event)
            state_out = manager.run()

            # we copy the old ca and then store the cert, the key and the ca cert
            assert write_certs.call_count == 4
            reload_tls.assert_called_once()
            assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "true"
            assert (
                state_out.get_relation(1).local_unit_data.get("tls-ca-rotation")
                == TLSCARotationState.NEW_CA_ADDED.value
            )


def test_new_client_ca_rotation_started(cloud_spec):
    # Mock the certificate values that are in the relation databag otherwise
    ca = MagicMock("my_new_ca")
    ca.raw = "my_new_ca"
    csr = MagicMock("my_csr")
    csr.raw = "my_csr"
    cert = MagicMock("my_cert")
    cert.raw = "my_cert"
    private_key = MagicMock("my_key")
    private_key.raw = "my_key"

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "tls-client-state": "tls"},
        peers_data={1: {"start-state": "started", "tls-client-state": "tls"}},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=3,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    certificate = ProviderCertificate(
        relation_id=3, certificate=cert, certificate_signing_request=csr, ca=ca, chain=[cert, ca]
    )
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with ctx(ctx.on.update_status(), state_in) as manager:
        charm: ValkeyCharm = manager.charm
        event = MagicMock(spec=CertificateAvailableEvent)

        with (
            patch(
                "charmlibs.interfaces.tls_certificates.TLSCertificatesRequiresV4.get_assigned_certificates",
                return_value=([certificate], private_key),
            ),
            patch("charmlibs.pathops.ContainerPath.exists", return_value=True),
            patch("charmlibs.pathops.ContainerPath.read_text", return_value="my_old_ca"),
            patch("charmlibs.pathops.ContainerPath.write_text"),
            patch("workload_k8s.ValkeyK8sWorkload.write_file") as write_certs,
            patch("managers.tls.TLSManager.rehash_ca_certificates"),
            patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
        ):
            event.certificate = certificate.certificate
            charm.tls_events._on_certificate_available(event)
            state_out = manager.run()

            # we copy the old ca and then store the cert, the key and the ca cert
            assert write_certs.call_count == 4
            reload_tls.assert_not_called()
            assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "true"
            assert (
                state_out.get_relation(1).local_unit_data.get("tls-ca-rotation")
                == TLSCARotationState.NEW_CA_ADDED.value
            )


def test_internal_peer_ca_rotation_single_unit(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "tls-client-state": "tls"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("managers.tls.TLSManager.will_certificate_expire", return_value=True),
        patch("managers.tls.TLSManager.generate_ca_certificate") as generate_ca,
        patch("core.models.ValkeyCluster.internal_ca_certificate", return_value="my_new_ca"),
        patch("managers.tls.TLSManager.create_and_store_self_signed_certificate") as create_certs,
        patch("charmlibs.pathops.ContainerPath.exists", return_value=True),
        patch("charmlibs.pathops.ContainerPath.read_text", return_value="my_old_ca"),
        patch("charmlibs.pathops.ContainerPath.write_text"),
        patch("charmlibs.pathops.ContainerPath.mkdir"),
        patch("managers.tls.TLSManager.rehash_ca_certificates"),
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
        patch("managers.sentinel.SentinelManager.restart_service"),
    ):
        state_out = ctx.run(ctx.on.relation_changed(peer_relation, remote_unit=1), state_in)

        create_certs.assert_called_once()
        generate_ca.assert_called_once()
        assert reload_tls.call_count == 2
        assert (
            state_out.get_relation(1).local_unit_data.get("tls-ca-rotation")
            == TLSCARotationState.NO_ROTATION.value
        )


def test_internal_peer_ca_rotation_started(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "tls-client-state": "tls"},
        peers_data={1: {"start-state": "started", "tls-client-state": "tls"}},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("managers.tls.TLSManager.will_certificate_expire", return_value=True),
        patch("managers.tls.TLSManager.generate_ca_certificate") as generate_ca,
        patch("core.models.ValkeyCluster.internal_ca_certificate", return_value="my_new_ca"),
        patch("managers.tls.TLSManager.create_and_store_self_signed_certificate") as create_certs,
        patch("charmlibs.pathops.ContainerPath.exists", return_value=True),
        patch("charmlibs.pathops.ContainerPath.read_text", return_value="my_old_ca"),
        patch("charmlibs.pathops.ContainerPath.write_text"),
        patch("charmlibs.pathops.ContainerPath.mkdir"),
        patch("managers.tls.TLSManager.rehash_ca_certificates"),
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
        patch("managers.sentinel.SentinelManager.restart_service"),
    ):
        state_out = ctx.run(ctx.on.relation_changed(peer_relation, remote_unit=1), state_in)

        create_certs.assert_called_once()
        generate_ca.assert_called_once()
        reload_tls.assert_not_called()
        assert (
            state_out.get_relation(1).local_unit_data.get("tls-ca-rotation")
            == TLSCARotationState.NEW_CA_ADDED.value
        )


def test_ca_rotation_not_all_units_added(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "start-state": "started",
            "tls-client-state": "tls",
            "tls-ca-rotation": "new-ca-added",
        },
        peers_data={
            1: {
                "start-state": "started",
                "tls-client-state": "tls",
            }
        },
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=3,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        planned_units=2,
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
    ):
        state_out = ctx.run(ctx.on.relation_changed(peer_relation), state_in)

        # we copy the old ca and then store the cert, the key and the ca cert
        reload_tls.assert_not_called()
        assert (
            state_out.get_relation(1).local_unit_data.get("tls-ca-rotation")
            == TLSCARotationState.NEW_CA_ADDED.value
        )


def test_ca_rotation_all_units_added(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "start-state": "started",
            "tls-client-state": "tls",
            "tls-ca-rotation": "new-ca-added",
        },
        peers_data={
            1: {
                "start-state": "started",
                "tls-client-state": "tls",
                "tls-ca-rotation": "new-ca-added",
            }
        },
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=3,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        planned_units=2,
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
        patch("managers.sentinel.SentinelManager.restart_service"),
    ):
        state_out = ctx.run(ctx.on.relation_changed(peer_relation), state_in)

        # we copy the old ca and then store the cert, the key and the ca cert
        reload_tls.assert_called_once()
        assert (
            state_out.get_relation(1).local_unit_data.get("tls-ca-rotation")
            == TLSCARotationState.CA_UPDATED.value
        )


def test_ca_rotation_not_all_units_ca_updated(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "start-state": "started",
            "tls-client-state": "tls",
            "tls-ca-rotation": "ca-updated",
        },
        peers_data={
            1: {
                "start-state": "started",
                "tls-client-state": "tls",
                "tls-ca-rotation": "ca-added",
            }
        },
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=3,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        planned_units=2,
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
    ):
        state_out = ctx.run(ctx.on.relation_changed(peer_relation), state_in)

        # we copy the old ca and then store the cert, the key and the ca cert
        reload_tls.assert_not_called()
        assert (
            state_out.get_relation(1).local_unit_data.get("tls-ca-rotation")
            == TLSCARotationState.CA_UPDATED.value
        )


def test_ca_rotation_all_units_ca_updated(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "start-state": "started",
            "tls-client-state": "tls",
            "tls-ca-rotation": "ca-updated",
        },
        peers_data={
            1: {
                "start-state": "started",
                "tls-client-state": "tls",
                "tls-ca-rotation": "ca-updated",
            }
        },
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=3,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        containers={container},
        planned_units=2,
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
        patch("managers.sentinel.SentinelManager.restart_service"),
        patch("managers.tls.TLSManager.rehash_ca_certificates"),
    ):
        state_out = ctx.run(ctx.on.relation_changed(peer_relation), state_in)

        # we copy the old ca and then store the cert, the key and the ca cert
        reload_tls.assert_called_once()
        assert (
            state_out.get_relation(1).local_unit_data.get("tls-ca-rotation")
            == TLSCARotationState.NO_ROTATION.value
        )


def test_private_key_without_client_tls(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)

    private_key = PrivateKey.generate()
    secret = ops.testing.Secret(
        {"private-key": private_key.raw},
        label=TLS_CLIENT_PRIVATE_KEY_CONFIG,
    )

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation},
        secrets={secret},
        config={TLS_CLIENT_PRIVATE_KEY_CONFIG: secret.id},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)
    secret_out = state_out.get_secret(label=f"{PEER_RELATION}.{APP_NAME}.app")
    assert secret_out.latest_content.get("tls-client-private-key") == private_key.raw
    assert status_is(state_out, TLSStatuses.PRIVATE_KEY_BUT_NO_TLS.value)


def test_invalid_private_key(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "tls-client-state": "tls"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=3,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    container = testing.Container(name=CONTAINER, can_connect=True)

    private_key = "invalid-private-key"
    secret = ops.testing.Secret(
        {"private-key": private_key},
        label=TLS_CLIENT_PRIVATE_KEY_CONFIG,
    )

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        secrets={secret},
        config={TLS_CLIENT_PRIVATE_KEY_CONFIG: secret.id},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)
    # ensure the secret was not populated with the invalid private key
    with pytest.raises(KeyError):
        state_out.get_secret(label=f"{PEER_RELATION}.{APP_NAME}.app")
    assert status_is(state_out, TLSStatuses.PRIVATE_KEY_INVALID.value)


def test_private_key_refreshes_certificate(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "tls-client-state": "tls"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=3,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    container = testing.Container(name=CONTAINER, can_connect=True)

    private_key = PrivateKey.generate()
    user_secret = ops.testing.Secret(
        {"private-key": private_key.raw},
        label=TLS_CLIENT_PRIVATE_KEY_CONFIG,
    )

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        secrets={user_secret},
        config={TLS_CLIENT_PRIVATE_KEY_CONFIG: user_secret.id},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)
    secret_out = state_out.get_secret(label=f"{PEER_RELATION}.{APP_NAME}.app")
    assert secret_out.latest_content.get("tls-client-private-key") == private_key.raw
    assert ctx.emitted_events[1].handle.kind == "refresh_tls_certificates_event"


def test_private_key_secret_changed(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started", "tls-client-state": "tls"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    client_tls_relation = testing.Relation(
        id=3,
        endpoint=CLIENT_TLS_RELATION_NAME,
    )
    container = testing.Container(name=CONTAINER, can_connect=True)

    private_key = PrivateKey.generate()
    user_secret = ops.testing.Secret(
        {"private-key": private_key.raw},
        label=TLS_CLIENT_PRIVATE_KEY_CONFIG,
    )

    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, client_tls_relation},
        secrets={user_secret},
        config={TLS_CLIENT_PRIVATE_KEY_CONFIG: user_secret.id},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    state_out = ctx.run(ctx.on.secret_changed(secret=user_secret), state_in)
    secret_out = state_out.get_secret(label=f"{PEER_RELATION}.{APP_NAME}.app")
    assert secret_out.latest_content.get("tls-client-private-key") == private_key.raw
    assert ctx.emitted_events[1].handle.kind == "refresh_tls_certificates_event"
