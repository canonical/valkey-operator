#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import patch

import yaml
from ops import testing

from charm import ValkeyCharm
from common.exceptions import ValkeyWorkloadCommandError
from literals import (
    LDAP_CA_CERT_RELATION,
    PEER_RELATION,
    STATUS_PEERS_RELATION,
)

CONTAINER = "valkey"

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]


def test_ldap_new_ca_cert(cloud_spec):
    ldap_cert = "ldap_certificate"
    ldap_ca_cert = "ldap_ca_certificate"

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    ldap_ca_cert_relation = testing.Relation(
        id=5,
        endpoint=LDAP_CA_CERT_RELATION,
        remote_units_data={
            0: {
                "certificate": ldap_cert,
                "ca": ldap_ca_cert,
                "chain": f'["{ldap_cert}", "{ldap_ca_cert}"]',
            }
        },
    )

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=False,  # must be stored on all units
        relations={peer_relation, status_peer_relation, ldap_ca_cert_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("charmlibs.pathops.ContainerPath.mkdir"),
        patch("workload_k8s.ValkeyK8sWorkload.write_file") as write_ldap_ca,
        patch("managers.tls.TLSManager.rehash_ca_certificates") as rehash_ca_certs,
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
    ):
        ctx.run(ctx.on.relation_changed(relation=ldap_ca_cert_relation), state_in)
        write_ldap_ca.assert_called_once()
        rehash_ca_certs.assert_not_called()
        reload_tls.assert_not_called()


def test_ldap_ca_removed(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    ldap_ca_cert_relation = testing.Relation(id=5, endpoint=LDAP_CA_CERT_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, ldap_ca_cert_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("workload_k8s.ValkeyK8sWorkload.remove_file") as remove_ldap_ca,
        patch("managers.cluster.ClusterManager.reload_tls_settings") as reload_tls,
    ):
        ctx.run(ctx.on.relation_broken(relation=ldap_ca_cert_relation), state_in)
        remove_ldap_ca.assert_called_once()
        reload_tls.assert_not_called()


def test_ca_available_error_defers(cloud_spec):
    ldap_cert = "ldap_certificate"
    ldap_ca_cert = "ldap_ca_certificate"

    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    ldap_ca_cert_relation = testing.Relation(
        id=5,
        endpoint=LDAP_CA_CERT_RELATION,
        remote_units_data={
            0: {
                "certificate": ldap_cert,
                "ca": ldap_ca_cert,
                "chain": f'["{ldap_cert}", "{ldap_ca_cert}"]',
            }
        },
    )
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, ldap_ca_cert_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with (
        patch(
            "core.base_workload.WorkloadBase.make_dir",
            side_effect=ValkeyWorkloadCommandError("Pebble down"),
        ),
    ):
        state_out = ctx.run(ctx.on.relation_changed(relation=ldap_ca_cert_relation), state_in)
    assert "certificate_available" in [e.name for e in state_out.deferred]


def test_ca_removed_error_defers(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    ldap_ca_cert_relation = testing.Relation(id=5, endpoint=LDAP_CA_CERT_RELATION)
    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, ldap_ca_cert_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )
    with patch(
        "workload_k8s.ValkeyK8sWorkload.remove_file",
        side_effect=ValkeyWorkloadCommandError("Pebble down"),
    ):
        state_out = ctx.run(ctx.on.relation_broken(relation=ldap_ca_cert_relation), state_in)
    assert "certificate_removed" in [e.name for e in state_out.deferred]
