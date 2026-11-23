#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Address changes and moves to a new space on VM."""

from contextlib import ExitStack, contextmanager
from itertools import chain, repeat
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

from charmlibs.rollingops import OperationResult
from ops import testing

from common.custom_events import RefreshTLSCertificatesEvent
from common.exceptions import (
    CannotSeeAllActiveSentinelsError,
    SentinelFailoverError,
    ValkeyWorkloadCommandError,
)
from src.charm import ValkeyCharm
from src.literals import (
    CLIENT_TLS_RELATION_NAME,
    PEER_RELATION,
    RESTART_OPERATION_ID,
    ROLLINGOPS_PEER_RELATION,
)
from src.statuses import CharmStatuses, ClusterStatuses

from .helpers import status_is

CONTAINER = "valkey"

OLD_IP = "127.0.1.1"
# must match the autouse `mock_bind_address` fixture in conftest.py
NEW_IP = "127.1.1.1"


def _peer_relation(
    private_ip: str = OLD_IP,
    pending_ip: str = "",
    peers_data: dict | None = None,
    start_state: str = "started",
) -> testing.PeerRelation:
    unit_data = {"start-state": start_state, "private-ip": private_ip}
    if pending_ip:
        unit_data["pending-ip"] = pending_ip
    return testing.PeerRelation(
        id=1, endpoint=PEER_RELATION, local_unit_data=unit_data, peers_data=peers_data or {}
    )


def _state(*, relations: set, leader: bool = True) -> testing.State:
    return testing.State(
        leader=leader,
        relations=relations,
        containers={testing.Container(name=CONTAINER, can_connect=True)},
        model=testing.Model(name="my-vm-model", type="lxd"),
    )


def _client_tls_relation() -> testing.Relation:
    return testing.Relation(
        id=2,
        endpoint=CLIENT_TLS_RELATION_NAME,
        interface="tls-certificates",
        remote_app_name="self-signed-certificates",
    )


def _reconcile_env(cert_ip: str = OLD_IP) -> ExitStack:
    """Patch the workload/manager side effects the address reconcile performs."""
    patches = (
        patch("managers.config.ConfigManager.configure_services"),
        patch("managers.auth.AuthManager.configure_auth"),
        patch("managers.sentinel.SentinelManager.get_primary_ip", return_value="127.1.1.2"),
        patch("managers.sentinel.SentinelManager.restart_service"),
        # stands in for `openssl x509 -ext subjectAltName`: the on-disk SANs carry cert_ip.
        # exec returns (stdout, stderr) -- the second element matters because other
        # update-status work unpacks both.
        patch(
            "workload_vm.ValkeyVmWorkload.exec",
            return_value=(f"DNS:www.example.com, IP Address:{cert_ip}", None),
        ),
        patch("workload_vm.ValkeyVmWorkload.restart"),
        patch("managers.tls.TLSManager.build_sans_ip", return_value=frozenset({NEW_IP})),
        patch(
            "managers.tls.TLSManager.build_sans_dns", return_value=frozenset({"www.example.com"})
        ),
        patch("managers.tls.TLSManager.will_certificate_expire", return_value=False),
        patch("managers.cluster.ClusterManager.is_healthy", return_value=True),
        patch("managers.sentinel.SentinelManager.is_healthy", return_value=True),
        patch("managers.cluster.ClusterManager.reconcile_min_replicas_to_write"),
        # unrelated update-status self-heal: it would otherwise drive the sentinel
        # CLI through the openssl stub above.
        patch("managers.sentinel.SentinelManager.reconcile_failover_suppression"),
    )
    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


def test_update_status_queues_a_move_when_the_binding_changed(
    vm_environment, mock_request_async_lock
):
    """The move is only recorded, and the unit signs a cert valid for both addresses."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation()}, leader=False)

    with (
        _reconcile_env(),
        patch(
            "managers.tls.TLSManager.create_and_store_self_signed_certificate"
        ) as mock_create_certificate,
        patch("managers.config.ConfigManager.configure_services") as mock_configure,
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    unit_data = state_out.get_relation(1).local_unit_data
    assert unit_data["pending-ip"] == NEW_IP
    assert unit_data["private-ip"] == OLD_IP
    # rollingops drops the second, identical request
    mock_request_async_lock.assert_called_with(RESTART_OPERATION_ID, kwargs={})
    mock_create_certificate.assert_called_once()
    mock_configure.assert_not_called()
    assert status_is(state_out, ClusterStatuses.MOVING_ADDRESS.value)


def test_update_status_requests_a_certificate_for_the_new_address(vm_environment):
    """With a provider, the unit asks for a cert with both addresses before it moves."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation(), _client_tls_relation()})

    with (
        _reconcile_env(),
        patch(
            "managers.tls.TLSManager.create_and_store_self_signed_certificate"
        ) as mock_create_certificate,
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert any(isinstance(e, RefreshTLSCertificatesEvent) for e in ctx.emitted_events)
    mock_create_certificate.assert_not_called()
    assert state_out.get_relation(1).local_unit_data["pending-ip"] == NEW_IP
    assert not state_out.deferred


def test_update_status_waits_for_the_refresh_before_moving(
    vm_environment, mock_request_async_lock
):
    """The move starts only after the refresh, so no unit restarts twice."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation()})

    with (
        _reconcile_env(),
        patch(
            "managers.refresh.RefreshManager.refresh_in_progress",
            new_callable=PropertyMock,
            return_value=True,
        ),
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert "pending-ip" not in state_out.get_relation(1).local_unit_data
    mock_request_async_lock.assert_not_called()
    assert status_is(state_out, ClusterStatuses.MOVING_ADDRESS.value)


def test_update_status_drops_the_move_when_the_binding_moved_back(vm_environment):
    """A binding that moves back before the restart leaves the unit where it is."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation(private_ip=NEW_IP, pending_ip="127.9.9.9")})

    with _reconcile_env(cert_ip=NEW_IP):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert "pending-ip" not in state_out.get_relation(1).local_unit_data


def test_update_status_keeps_a_move_the_restart_callback_started(vm_environment):
    """Once the callback recorded the new address, the move finishes in the callback."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation(private_ip=NEW_IP, pending_ip=NEW_IP)})

    with _reconcile_env(cert_ip=NEW_IP):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert state_out.get_relation(1).local_unit_data["pending-ip"] == NEW_IP


def test_update_status_does_not_reconcile_when_ip_unchanged(vm_environment):
    """A unit whose address and SANs are unchanged must not reissue certificates."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation(private_ip=NEW_IP)})

    with (
        _reconcile_env(cert_ip=NEW_IP),
        patch(
            "managers.tls.TLSManager.create_and_store_self_signed_certificate"
        ) as mock_create_certificate,
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    mock_create_certificate.assert_not_called()
    assert not any(isinstance(e, RefreshTLSCertificatesEvent) for e in ctx.emitted_events)
    assert status_is(state_out, CharmStatuses.ACTIVE_IDLE.value)


def test_update_status_regenerates_self_signed_certificate_when_sans_changed(
    vm_environment, mock_request_async_lock
):
    """An ingress address can change while the bind address stays, such as a new public IP."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation(private_ip=NEW_IP)})

    with (
        _reconcile_env(),
        patch(
            "managers.tls.TLSManager.create_and_store_self_signed_certificate"
        ) as mock_create_certificate,
    ):
        ctx.run(ctx.on.update_status(), state_in)

    mock_create_certificate.assert_called_once()
    mock_request_async_lock.assert_called_once()


def test_update_status_refreshes_client_certificate_when_sans_changed(vm_environment):
    """With a provider related, a SAN change without an IP change requests a new certificate."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation(private_ip=NEW_IP), _client_tls_relation()})

    with (
        _reconcile_env(),
        patch(
            "managers.tls.TLSManager.create_and_store_self_signed_certificate"
        ) as mock_create_certificate,
    ):
        ctx.run(ctx.on.update_status(), state_in)

    assert any(isinstance(e, RefreshTLSCertificatesEvent) for e in ctx.emitted_events)
    mock_create_certificate.assert_not_called()


def test_update_status_does_not_reconcile_before_an_address_is_recorded(
    vm_environment, mock_request_async_lock
):
    """`private-ip` is first written on start, so an empty one is not an address change."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation(private_ip="")})

    with _reconcile_env():
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert "pending-ip" not in state_out.get_relation(1).local_unit_data
    mock_request_async_lock.assert_not_called()


def test_config_changed_defers_on_certificate_read_failure(vm_environment):
    """config-changed has no periodic retry, so it must defer instead of dropping the reconcile."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation(private_ip=NEW_IP)})

    with (
        _reconcile_env(),
        patch("events.base_events.BaseEvents._update_internal_users_password"),
        patch(
            "managers.tls.TLSManager.get_current_sans",
            side_effect=ValkeyWorkloadCommandError("cannot read certificate"),
        ),
    ):
        state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert [d.name for d in state_out.deferred] == ["config_changed"]


def test_update_status_survives_certificate_read_failure(vm_environment):
    """A failed cert read must not error the unit, update-status runs on every interval."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation(), _client_tls_relation()})

    with (
        _reconcile_env(),
        patch(
            "managers.tls.TLSManager.get_current_sans",
            side_effect=ValkeyWorkloadCommandError("cannot read certificate"),
        ),
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert not state_out.deferred


def test_update_status_is_noop_on_k8s():
    """K8s uses hostnames, so the address reconcile must not even read the binding."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation()})

    with (
        _reconcile_env(),
        patch(
            "core.cluster_state.ClusterState.bind_address",
            new_callable=PropertyMock,
            side_effect=ValueError("no binding on k8s"),
        ),
        patch(
            "managers.tls.TLSManager.create_and_store_self_signed_certificate"
        ) as mock_create_certificate,
    ):
        ctx.run(ctx.on.update_status(), state_in)

    mock_create_certificate.assert_not_called()


def test_endpoint_is_the_recorded_address_after_the_binding_changed(vm_environment):
    """Renders keep the recorded address until the move, so no hook moves a unit early."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    with _reconcile_env(), ctx(ctx.on.update_status(), _state(relations={_peer_relation()})) as m:
        assert m.charm.state.endpoint == OLD_IP


def test_leader_elected_keeps_the_recorded_address(vm_environment):
    """Hooks that fire mid-move must not record the new binding, only the move does."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    with _reconcile_env(), ctx(ctx.on.update_status(), _state(relations={_peer_relation()})) as m:
        m.charm.base_events._ensure_workload_ready()
        assert m.charm.state.unit_server.model.private_ip == OLD_IP


def test_k8s_records_the_current_address():
    """The recorded address is only kept on VM, K8s units use their hostname."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    with _reconcile_env(), ctx(ctx.on.update_status(), _state(relations={_peer_relation()})) as m:
        m.charm.base_events._ensure_workload_ready()
        assert m.charm.state.unit_server.model.private_ip == NEW_IP


def test_start_from_scratch_records_the_new_binding(vm_environment):
    """A unit whose Valkey is down starts on the current binding, as after a DHCP change."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(relations={_peer_relation(pending_ip=NEW_IP)})

    with _reconcile_env(), patch("workload_vm.ValkeyVmWorkload.alive", return_value=False):
        state_out = ctx.run(ctx.on.start(), state_in)

    unit_data = state_out.get_relation(1).local_unit_data
    assert unit_data["private-ip"] == NEW_IP
    assert "pending-ip" not in unit_data


def test_config_changed_does_not_queue_a_move_before_the_unit_started(
    vm_environment, mock_request_async_lock
):
    """The start flow drops restarts, so the move is queued once the unit has started."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state_in = _state(
        relations={_peer_relation(start_state="starting_waiting_valkey")}, leader=False
    )

    with _reconcile_env():
        state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert "pending-ip" not in state_out.get_relation(1).local_unit_data
    mock_request_async_lock.assert_not_called()


MOVE_PATCHES = {
    "restart": "workload_vm.ValkeyVmWorkload.restart",
    "run_id": "managers.cluster.ClusterManager.run_id",
    "in_sync": "managers.cluster.ClusterManager.is_back_in_sync",
    "failover": "managers.sentinel.SentinelManager.failover",
    "get_primary_ip": "managers.sentinel.SentinelManager.get_primary_ip",
    "reset": "managers.sentinel.SentinelManager.reset_sentinel_states",
    "verify": "managers.sentinel.SentinelManager.verify_expected_replica_count",
    "render_valkey": "managers.config.ConfigManager.set_config_properties",
    "render_sentinel": "managers.config.ConfigManager.set_sentinel_config_properties",
    "sans": "managers.tls.TLSManager.get_current_sans",
    "sans_changed": "managers.tls.TLSManager.certificate_sans_require_update",
    "self_sign": "managers.tls.TLSManager.create_and_store_self_signed_certificate",
}


@contextmanager
def _move(relations: set, planned_units: int = 3):
    """Yield the charm and the mocked move steps, with a replica moving to NEW_IP."""
    ctx = testing.Context(ValkeyCharm, app_trusted=True)
    state = testing.State(
        relations=relations | {testing.PeerRelation(id=9, endpoint=ROLLINGOPS_PEER_RELATION)},
        containers={testing.Container(name=CONTAINER, can_connect=True)},
        model=testing.Model(name="my-vm-model", type="lxd"),
        planned_units=planned_units,
    )
    with ExitStack() as stack:
        stack.enter_context(_reconcile_env(cert_ip=NEW_IP))
        mocks = SimpleNamespace(
            **{name: stack.enter_context(patch(target)) for name, target in MOVE_PATCHES.items()}
        )
        mocks.run_id.return_value = "run-1"
        mocks.in_sync.return_value = True
        mocks.get_primary_ip.return_value = "127.1.1.2"
        mocks.sans.return_value = {"sans_ip": {OLD_IP, NEW_IP}, "sans_dns": set()}
        mocks.sans_changed.return_value = False
        with ctx(ctx.on.update_status(), state) as manager:
            yield manager.charm, mocks


def test_move_replica_records_renders_restarts_and_resets(vm_environment):
    """A replica moves without a failover and resets every Sentinel afterwards."""
    with _move({_peer_relation(pending_ip=NEW_IP)}) as (charm, mocks):
        assert charm.base_events.restart_workload() == OperationResult.RELEASE

        model = charm.state.unit_server.model
        assert (model.private_ip, model.pending_ip, model.restart_run_id) == (NEW_IP, "", "")
        mocks.failover.assert_not_called()
        mocks.restart.assert_called_once()
        mocks.render_valkey.assert_called_once_with(primary_endpoint="127.1.1.2")
        mocks.render_sentinel.assert_called_once_with(primary_endpoint="127.1.1.2")
        mocks.self_sign.assert_not_called()
        mocks.reset.assert_called_once()
        mocks.verify.assert_called_once()


def test_move_primary_waits_for_the_pending_replicas(vm_environment):
    """The primary moves last, so the replica that takes over has already moved."""
    peer = {1: {"start-state": "started", "private-ip": "127.0.1.2", "pending-ip": "127.1.1.2"}}
    with _move({_peer_relation(pending_ip=NEW_IP, peers_data=peer)}) as (charm, mocks):
        mocks.get_primary_ip.return_value = OLD_IP
        assert charm.base_events.restart_workload() == OperationResult.RETRY_RELEASE

        assert charm.state.unit_server.model.private_ip == OLD_IP
        mocks.failover.assert_not_called()
        mocks.render_valkey.assert_not_called()


def test_move_primary_fails_over_first(vm_environment):
    """The primary hands over before it moves, like a refresh does."""
    with _move({_peer_relation(pending_ip=NEW_IP)}) as (charm, mocks):
        # Sentinel reports this unit until the failover, then the new primary
        mocks.get_primary_ip.side_effect = chain([OLD_IP], repeat("127.1.1.2"))
        assert charm.base_events.restart_workload() == OperationResult.RELEASE

        mocks.failover.assert_called_once()
        mocks.render_valkey.assert_called_once_with(primary_endpoint="127.1.1.2")
        assert charm.state.unit_server.model.private_ip == NEW_IP


def test_move_unreachable_primary_waits_for_sentinel_to_fail_over(vm_environment):
    """While Sentinel still reports the dead old IP as primary, the unit must not follow it."""
    with _move({_peer_relation(pending_ip=NEW_IP)}) as (charm, mocks):
        mocks.get_primary_ip.return_value = OLD_IP
        mocks.run_id.side_effect = ValkeyWorkloadCommandError("unreachable")
        mocks.failover.side_effect = SentinelFailoverError("unreachable")
        assert charm.base_events.restart_workload() == OperationResult.RETRY_RELEASE

        assert charm.state.unit_server.model.private_ip == OLD_IP
        mocks.render_valkey.assert_not_called()


def test_move_unreachable_replica_restarts_valkey(vm_environment):
    """A Valkey unreachable on the old IP still gets restarted on the new one."""
    with _move({_peer_relation(pending_ip=NEW_IP)}) as (charm, mocks):
        mocks.run_id.side_effect = ValkeyWorkloadCommandError("unreachable")
        assert charm.base_events.restart_workload() == OperationResult.RELEASE

        mocks.restart.assert_called_once()
        assert charm.state.unit_server.model.private_ip == NEW_IP


def test_move_waits_for_the_refresh(vm_environment):
    """A move queued before a refresh doesn't take a unit down while the refresh runs."""
    with (
        _move({_peer_relation(pending_ip=NEW_IP)}) as (charm, mocks),
        patch(
            "managers.refresh.RefreshManager.refresh_in_progress",
            new_callable=PropertyMock,
            return_value=True,
        ),
    ):
        assert charm.base_events.restart_workload() == OperationResult.RETRY_RELEASE

        assert charm.state.unit_server.model.private_ip == OLD_IP
        mocks.restart.assert_not_called()


def test_move_single_unit_renders_itself_as_primary(vm_environment):
    """A single unit can't fail over, and its own Sentinel no longer answers on the old IP."""
    with _move({_peer_relation(pending_ip=NEW_IP)}, planned_units=1) as (charm, mocks):
        assert charm.base_events.restart_workload() == OperationResult.RELEASE

        mocks.failover.assert_not_called()
        mocks.render_valkey.assert_called_once_with(primary_endpoint=NEW_IP)


def test_move_waits_for_a_provider_certificate_with_the_new_address(vm_environment):
    """The unit serves a cert valid for both addresses before it moves."""
    with _move({_peer_relation(pending_ip=NEW_IP), _client_tls_relation()}) as (charm, mocks):
        mocks.sans.return_value = {"sans_ip": {OLD_IP}, "sans_dns": set()}
        assert charm.base_events.restart_workload() == OperationResult.RETRY_RELEASE

        assert charm.state.unit_server.model.private_ip == OLD_IP
        mocks.render_valkey.assert_not_called()


def test_move_reissues_the_self_signed_certificate(vm_environment):
    """Without a provider, the unit signs a cert for the new address right before the restart."""
    with _move({_peer_relation(pending_ip=NEW_IP)}) as (charm, mocks):
        mocks.sans_changed.return_value = True
        assert charm.base_events.restart_workload() == OperationResult.RELEASE

        mocks.self_sign.assert_called_once()


def test_move_holds_the_lock_until_the_sentinel_reset_passes(vm_environment):
    """Stale Sentinel entries count toward the failover majority, so the reset must pass."""
    with _move({_peer_relation(pending_ip=NEW_IP)}) as (charm, mocks):
        mocks.reset.side_effect = CannotSeeAllActiveSentinelsError("not yet")
        assert charm.base_events.restart_workload() == OperationResult.RETRY_HOLD

        assert charm.state.unit_server.model.pending_ip == NEW_IP


def test_move_retry_does_not_fail_over_again(vm_environment):
    """A retry after the unit recorded its new address skips the move preparation."""
    with _move({_peer_relation(private_ip=NEW_IP, pending_ip=NEW_IP)}) as (charm, mocks):
        mocks.get_primary_ip.return_value = OLD_IP
        assert charm.base_events.restart_workload() == OperationResult.RELEASE

        mocks.failover.assert_not_called()
        mocks.run_id.assert_not_called()
