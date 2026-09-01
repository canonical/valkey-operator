#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the TopologyManager observer lifecycle."""

from unittest.mock import MagicMock, patch

from literals import CharmUsers
from managers.topology import TopologyManager


def _make_manager(
    pid: int = 1234,
    signature: str | None = None,
    endpoints: tuple[str, ...] = ("10.0.0.1", "10.0.0.2"),
    tls: bool = False,
    password: str = "sentinel-password",
    client_ca: str = "ca-pem",
):
    """Build a TopologyManager whose observer inputs are fully determined.

    `signature` defaults to the digest of the very inputs given here, i.e. the
    observer on the databag was launched with exactly the current topology.
    """
    state = MagicMock()
    state.unit_server.model.topology_observer_pid = pid
    state.unit_server.is_tls_enabled = tls
    servers = []
    for endpoint in endpoints:
        server = MagicMock(is_active=True)
        server.get_endpoint.return_value = endpoint
        servers.append(server)
    state.servers = servers
    state.cluster.internal_users_credentials = {CharmUsers.SENTINEL_CHARM_ADMIN.value: password}

    workload = MagicMock()
    workload.read_file.return_value = client_ca
    manager = TopologyManager(state=state, workload=workload)
    state.unit_server.model.topology_observer_signature = (
        manager.observer_signature() if signature is None else signature
    )
    return manager, state


def test_restart_is_noop_when_running_and_inputs_unchanged():
    """A re-delivered peer event must not churn the observer or the databag.

    Restarting unconditionally rewrites `topology_observer_pid`, and that write
    wakes every peer, which wakes the leader again -- a self-sustaining loop.
    """
    manager, state = _make_manager()

    with (
        patch("managers.topology.os.kill") as mock_kill,
        patch.object(TopologyManager, "stop_observer") as mock_stop,
        patch.object(TopologyManager, "start_observer") as mock_start,
    ):
        manager.restart_observer()

    mock_stop.assert_not_called()
    mock_start.assert_not_called()
    state.unit_server.update.assert_not_called()
    # only the liveness probe, never a signal
    mock_kill.assert_called_once_with(1234, 0)


def test_restart_when_topology_changed():
    """A changed host set must relaunch the observer with the new arguments."""
    manager, _ = _make_manager(signature="stale-digest-from-a-different-topology")

    with (
        patch("managers.topology.os.kill"),
        patch.object(TopologyManager, "stop_observer") as mock_stop,
        patch.object(TopologyManager, "start_observer") as mock_start,
    ):
        manager.restart_observer()

    mock_stop.assert_called_once()
    mock_start.assert_called_once()


def test_restart_when_observer_process_is_gone():
    """A dead observer is relaunched even though its inputs are unchanged."""
    manager, _ = _make_manager()

    with (
        patch("managers.topology.os.kill", side_effect=OSError),
        patch.object(TopologyManager, "stop_observer") as mock_stop,
        patch.object(TopologyManager, "start_observer") as mock_start,
    ):
        manager.restart_observer()

    mock_stop.assert_called_once()
    mock_start.assert_called_once()


def test_restart_when_no_observer_recorded():
    """A unit that never started an observer starts one."""
    manager, _ = _make_manager(pid=0)

    with (
        patch("managers.topology.os.kill") as mock_kill,
        patch.object(TopologyManager, "stop_observer") as mock_stop,
        patch.object(TopologyManager, "start_observer") as mock_start,
    ):
        manager.restart_observer()

    mock_kill.assert_not_called()
    mock_stop.assert_called_once()
    mock_start.assert_called_once()


def test_signature_tracks_every_launch_argument():
    """Host set, TLS mode and the Sentinel password each change the digest."""
    baseline, _ = _make_manager()
    digest = baseline.observer_signature()

    changed_hosts, _ = _make_manager(endpoints=("10.0.0.1", "10.0.0.9"))
    changed_tls, _ = _make_manager(tls=True)
    changed_password, _ = _make_manager(password="rotated-password")

    assert changed_hosts.observer_signature() != digest
    assert changed_tls.observer_signature() != digest
    assert changed_password.observer_signature() != digest


def test_signature_tracks_the_ca_when_tls_is_on():
    """A CA rotation must relaunch the observer, which holds its own copy of the CA."""
    before, _ = _make_manager(tls=True, client_ca="old-ca-pem")
    after, _ = _make_manager(tls=True, client_ca="rotated-ca-pem")

    assert before.observer_signature() != after.observer_signature()


def test_signature_ignores_the_ca_when_tls_is_off():
    """Without TLS the observer is never handed a CA, so it must not be read."""
    manager, _ = _make_manager(tls=False)

    manager.observer_signature()

    manager.workload.read_file.assert_not_called()


def test_signature_survives_an_unreadable_ca():
    """An unreadable CA must not crash the leader's peer hook."""
    manager, _ = _make_manager(tls=True)
    manager.workload.read_file.side_effect = OSError("gone")

    assert manager.observer_signature()


def test_signature_is_order_independent():
    """Peer ordering is not a topology change; the digest must not move."""
    one, _ = _make_manager(endpoints=("10.0.0.1", "10.0.0.2"))
    other, _ = _make_manager(endpoints=("10.0.0.2", "10.0.0.1"))

    assert one.observer_signature() == other.observer_signature()


def test_signature_does_not_leak_the_password():
    """The digest goes into a relation databag, so it must not carry the secret."""
    manager, _ = _make_manager(password="super-secret-password")

    assert "super-secret-password" not in manager.observer_signature()
