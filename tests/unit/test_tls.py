#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import MagicMock, patch

from charmlibs.interfaces.tls_certificates import CertificateAvailableEvent, ProviderCertificate
from ops import testing

from src.charm import ValkeyCharm
from src.literals import (
    CLIENT_TLS_RELATION_NAME,
    PEER_RELATION,
    STATUS_PEERS_RELATION,
)
from src.statuses import TLSStatuses

from .helpers import status_is

CONTAINER = "valkey"


def test_client_tls_relation_created(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"started": "True"},
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


def test_client_tls_relation_broken(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "started": "True",
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
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
    ):
        state_out = ctx.run(ctx.on.relation_broken(relation=client_tls_relation), state_in)
        reload_tls.assert_called_once()
        assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "false"
        assert state_out.get_relation(1).local_unit_data.get("tls-client-state") == "no-tls"


def test_client_tls_relation_broken_disabling_tls_fails(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "started": "True",
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


def test_client_tls_relation_broken_run_deferred_event(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "started": "True",
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

    with patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls:
        state_out = ctx.run(ctx.on.relation_broken(relation=client_tls_relation), state_in)
        reload_tls.assert_called_once()
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
        local_unit_data={"started": "True", "tls-client-state": "to-tls"},
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
            patch("managers.tls.TLSManager.write_certificate"),
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
        local_unit_data={"started": "True", "tls-client-state": "to-tls"},
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
            "started": "True",
            "tls-client-state": "to-tls",
        },
        peers_data={1: {"started": "True", "client-cert-ready": "False"}},
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
        ):
            event.certificate = client_certificate.certificate
            charm.tls_events._on_certificate_available(event)
            state_out = manager.run()

            reload_tls.assert_not_called()
            assert state_out.get_relation(1).local_unit_data.get("client-cert-ready") == "true"
            assert state_out.get_relation(1).local_unit_data.get("tls-client-state") == "to-tls"
