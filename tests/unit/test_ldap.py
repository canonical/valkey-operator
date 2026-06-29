#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from ops import testing

from charm import ValkeyCharm
from common.exceptions import ValkeyWorkloadCommandError
from lib.charms.glauth_k8s.v0.ldap import LdapReadyEvent, LdapUnavailableEvent
from literals import (
    LDAP_CA_CERT_RELATION,
    LDAP_RELATION,
    PEER_RELATION,
    STATUS_PEERS_RELATION,
)
from statuses import AuthStatuses

from .helpers import status_is

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
        id=3,
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
    ldap_ca_cert_relation = testing.Relation(id=3, endpoint=LDAP_CA_CERT_RELATION)

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
        id=3,
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
    ldap_ca_cert_relation = testing.Relation(id=3, endpoint=LDAP_CA_CERT_RELATION)
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


def test_no_ldap_ca_cert_relation(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    ldap_relation = testing.Relation(id=3, endpoint=LDAP_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=True,
        relations={peer_relation, status_peer_relation, ldap_relation},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    state_out = ctx.run(ctx.on.relation_changed(relation=ldap_relation), state_in)
    assert status_is(state_out, AuthStatuses.LDAP_CA_CERT_MISSING.value, is_app=True)
    assert not status_is(state_out, AuthStatuses.LDAP_CA_CERT_MISSING.value, is_app=False)


def test_enable_ldap(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    ldap_secret = testing.Secret({"password": "dummy"})

    relation_data = {
        "auth_method": "simple",
        "base_dn": "dc=glauth,dc=com",
        "bind_dn": "cn=valkey,ou=ldap,dc=glauth,dc=com",
        "bind_password_secret": ldap_secret.id,
        "ldaps_urls": '["ldaps://glauth-k8s.ldap.svc.cluster.local:3894"]',
        "starttls": "True",
        "urls": '["ldap://glauth-k8s.ldap.svc.cluster.local:3893"]',
    }

    ldap_relation = testing.Relation(
        id=3,
        endpoint=LDAP_RELATION,
        remote_app_data=relation_data,
    )
    ldap_ca_cert_relation = testing.Relation(id=4, endpoint=LDAP_CA_CERT_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, ldap_relation, ldap_ca_cert_relation},
        secrets={ldap_secret},
        config={"ldap-map": "ldap_group:valkey_group"},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with ctx(ctx.on.update_status(), state_in) as manager:
        charm: ValkeyCharm = manager.charm
        event = MagicMock(spec=LdapReadyEvent)

        with (
            patch("managers.sentinel.SentinelManager.get_primary_ip"),
            patch("managers.config.ConfigManager.set_config_properties") as set_config,
        ):
            charm.ldap_events._on_ldap_ready(event)
            state_out = manager.run()

            ldap_config = charm.config_manager._generate_ldap_config()
            assert ldap_config["loadmodule"] == "/lib/libvalkey_ldap.so"
            assert ldap_config["ldap.search_bind_passwd"] == "dummy"
            assert ldap_config["ldap.search_base"] == relation_data["base_dn"]
            assert ldap_config["ldap.servers"] == relation_data["ldaps_urls"]
            assert ldap_config["ldap.search_bind_dn"] == relation_data["bind_dn"]
            assert ldap_config["ldap.search_attribute"] == "cn"
            assert ldap_config["ldap.search_dn_attribute"] == "DN"
            assert ldap_config["ldap.search_filter"] == "objectClass=posixAccount"

            set_config.assert_called_once()
            assert state_out.get_relation(1).local_unit_data.get("ldap-enabled") == "true"


def test_disable_ldap(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "start-state": "started",
            "ldap-enabled": "true",
        },
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    ldap_secret = testing.Secret({"password": "dummy"})

    ldap_relation = testing.Relation(
        id=3,
        endpoint=LDAP_RELATION,
        remote_app_data={"bind_password_secret": ldap_secret.id},
    )
    ldap_ca_cert_relation = testing.Relation(id=4, endpoint=LDAP_CA_CERT_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, ldap_relation, ldap_ca_cert_relation},
        secrets={ldap_secret},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with ctx(ctx.on.update_status(), state_in) as manager:
        charm: ValkeyCharm = manager.charm
        event = MagicMock(spec=LdapUnavailableEvent)

        with (
            patch("managers.sentinel.SentinelManager.get_primary_ip"),
            patch("managers.config.ConfigManager.set_config_properties") as set_config,
        ):
            charm.ldap_events._on_ldap_unavailable(event)
            state_out = manager.run()
            set_config.assert_called_once()
            assert state_out.get_relation(1).local_unit_data.get("ldap-enabled") == "false"


def test_invalid_config(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    ldap_secret = testing.Secret({"password": "dummy"})

    relation_data = {
        "auth_method": "simple",
        "base_dn": "dc=glauth,dc=com",
        "bind_dn": "cn=valkey,ou=ldap,dc=glauth,dc=com",
        "bind_password_secret": ldap_secret.id,
        "ldaps_urls": '["ldaps://glauth-k8s.ldap.svc.cluster.local:3894"]',
        "starttls": "True",
        "urls": '["ldap://glauth-k8s.ldap.svc.cluster.local:3893"]',
    }

    ldap_relation = testing.Relation(
        id=3,
        endpoint=LDAP_RELATION,
        remote_app_data=relation_data,
    )
    ldap_ca_cert_relation = testing.Relation(id=4, endpoint=LDAP_CA_CERT_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, ldap_relation, ldap_ca_cert_relation},
        secrets={ldap_secret},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with ctx(ctx.on.update_status(), state_in) as manager:
        charm: ValkeyCharm = manager.charm
        event = MagicMock(spec=LdapReadyEvent)

        with (
            patch("managers.sentinel.SentinelManager.get_primary_ip"),
            patch("managers.config.ConfigManager.set_config_properties") as set_config,
        ):
            charm.ldap_events._on_ldap_ready(event)
            state_out = manager.run()

            ldap_config = charm.config_manager._generate_ldap_config()
            assert ldap_config == {}

            set_config.assert_not_called()
            assert state_out.get_relation(1).local_unit_data.get("ldap-enabled") == "false"


def test_invalid_bind_secret(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    ldap_secret = testing.Secret({"invalid": "dummy"})

    relation_data = {
        "auth_method": "simple",
        "base_dn": "dc=glauth,dc=com",
        "bind_dn": "cn=valkey,ou=ldap,dc=glauth,dc=com",
        "bind_password_secret": ldap_secret.id,
        "ldaps_urls": '["ldaps://glauth-k8s.ldap.svc.cluster.local:3894"]',
        "starttls": "True",
        "urls": '["ldap://glauth-k8s.ldap.svc.cluster.local:3893"]',
    }

    ldap_relation = testing.Relation(
        id=3,
        endpoint=LDAP_RELATION,
        remote_app_data=relation_data,
    )
    ldap_ca_cert_relation = testing.Relation(id=4, endpoint=LDAP_CA_CERT_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, ldap_relation, ldap_ca_cert_relation},
        secrets={ldap_secret},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with ctx(ctx.on.update_status(), state_in) as manager:
        charm: ValkeyCharm = manager.charm
        event = MagicMock(spec=LdapReadyEvent)

        with (
            patch("managers.sentinel.SentinelManager.get_primary_ip"),
            patch("managers.config.ConfigManager.set_config_properties") as set_config,
        ):
            charm.ldap_events._on_ldap_ready(event)
            state_out = manager.run()

            ldap_config = charm.config_manager._generate_ldap_config()
            assert ldap_config == {}

            set_config.assert_not_called()
            assert state_out.get_relation(1).local_unit_data.get("ldap-enabled") == "false"


def test_config_change(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    ldap_secret = testing.Secret({"password": "dummy"})

    relation_data = {
        "auth_method": "simple",
        "base_dn": "dc=glauth,dc=com",
        "bind_dn": "cn=valkey,ou=ldap,dc=glauth,dc=com",
        "bind_password_secret": ldap_secret.id,
        "ldaps_urls": '["ldaps://glauth-k8s.ldap.svc.cluster.local:3894"]',
        "starttls": "True",
        "urls": '["ldap://glauth-k8s.ldap.svc.cluster.local:3893"]',
    }

    ldap_relation = testing.Relation(
        id=3,
        endpoint=LDAP_RELATION,
        remote_app_data=relation_data,
    )
    ldap_ca_cert_relation = testing.Relation(id=4, endpoint=LDAP_CA_CERT_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, ldap_relation, ldap_ca_cert_relation},
        secrets={ldap_secret},
        config={
            "ldap-map": "ldap_group:valkey_group",
            "ldap-search-attribute": "uid",
            "ldap-search-filter": "objectClass=user",
            "ldap-search-dn-attribute": "entryDN",
        },
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with ctx(ctx.on.config_changed(), state_in) as manager:
        charm: ValkeyCharm = manager.charm

        with (
            patch("managers.sentinel.SentinelManager.get_primary_ip"),
            patch("managers.config.ConfigManager.set_config_properties") as set_config,
        ):
            state_out = manager.run()

            ldap_config = charm.config_manager._generate_ldap_config()
            assert ldap_config["loadmodule"] == "/lib/libvalkey_ldap.so"
            assert ldap_config["ldap.search_bind_passwd"] == "dummy"
            assert ldap_config["ldap.search_base"] == relation_data["base_dn"]
            assert ldap_config["ldap.servers"] == relation_data["ldaps_urls"]
            assert ldap_config["ldap.search_bind_dn"] == relation_data["bind_dn"]
            assert ldap_config["ldap.search_attribute"] == "uid"
            assert ldap_config["ldap.search_dn_attribute"] == "entryDN"
            assert ldap_config["ldap.search_filter"] == "objectClass=user"

            set_config.assert_called_once()
            assert state_out.get_relation(1).local_unit_data.get("ldap-enabled") == "true"


def test_config_change_but_invalid(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={"start-state": "started"},
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    ldap_secret = testing.Secret({"password": "dummy"})

    relation_data = {
        "auth_method": "simple",
        "base_dn": "dc=glauth,dc=com",
        "bind_dn": "cn=valkey,ou=ldap,dc=glauth,dc=com",
        "bind_password_secret": ldap_secret.id,
        "ldaps_urls": '["ldaps://glauth-k8s.ldap.svc.cluster.local:3894"]',
        "starttls": "True",
        "urls": '["ldap://glauth-k8s.ldap.svc.cluster.local:3893"]',
    }

    ldap_relation = testing.Relation(
        id=3,
        endpoint=LDAP_RELATION,
        remote_app_data=relation_data,
    )

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, ldap_relation},
        secrets={ldap_secret},
        config={"ldap-map": "ldap_group:valkey_group"},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with (
        patch("managers.sentinel.SentinelManager.get_primary_ip"),
        patch("managers.config.ConfigManager.set_config_properties") as set_config,
    ):
        state_out = ctx.run(ctx.on.config_changed(), state_in)


        set_config.assert_not_called()
        assert not state_out.get_relation(1).local_unit_data.get("ldap-enabled") == "true"


def test_ldap_bind_secret_update(cloud_spec):
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    peer_relation = testing.PeerRelation(
        id=1,
        endpoint=PEER_RELATION,
        local_unit_data={
            "start-state": "started",
            "ldap-enabled": "true",
        },
    )
    status_peer_relation = testing.PeerRelation(id=2, endpoint=STATUS_PEERS_RELATION)
    ldap_secret = testing.Secret({"password": "dummy"})

    relation_data = {
        "auth_method": "simple",
        "base_dn": "dc=glauth,dc=com",
        "bind_dn": "cn=valkey,ou=ldap,dc=glauth,dc=com",
        "bind_password_secret": ldap_secret.id,
        "ldaps_urls": '["ldaps://glauth-k8s.ldap.svc.cluster.local:3894"]',
        "starttls": "True",
        "urls": '["ldap://glauth-k8s.ldap.svc.cluster.local:3893"]',
    }

    ldap_relation = testing.Relation(
        id=3,
        endpoint=LDAP_RELATION,
        remote_app_data=relation_data,
    )
    ldap_ca_cert_relation = testing.Relation(id=4, endpoint=LDAP_CA_CERT_RELATION)

    container = testing.Container(name=CONTAINER, can_connect=True)
    state_in = testing.State(
        leader=False,
        relations={peer_relation, status_peer_relation, ldap_relation, ldap_ca_cert_relation},
        secrets={ldap_secret},
        config={"ldap-map": "ldap_group:valkey_group"},
        containers={container},
        model=testing.Model(name="my-vm-model", type="lxd", cloud_spec=cloud_spec),
    )

    with ctx(ctx.on.secret_changed(secret=ldap_secret), state_in) as manager:
        charm: ValkeyCharm = manager.charm

        with (
            patch("managers.sentinel.SentinelManager.get_primary_ip"),
            patch("managers.config.ConfigManager.set_config_properties") as set_config,
        ):
            state_out = manager.run()

            ldap_config = charm.config_manager._generate_ldap_config()
            assert ldap_config["loadmodule"] == "/lib/libvalkey_ldap.so"
            assert ldap_config["ldap.search_bind_passwd"] == "dummy"
            assert ldap_config["ldap.search_base"] == relation_data["base_dn"]
            assert ldap_config["ldap.servers"] == relation_data["ldaps_urls"]
            assert ldap_config["ldap.search_bind_dn"] == relation_data["bind_dn"]

            set_config.assert_called_once()
