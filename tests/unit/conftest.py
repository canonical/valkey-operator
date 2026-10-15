#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import Mock, PropertyMock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_write_config_file(mocker):
    mocker.patch("workload_k8s.ValkeyK8sWorkload.write_config_file")


@pytest.fixture(autouse=True)
def mock_write_file(mocker):
    mocker.patch("workload_k8s.ValkeyK8sWorkload.write_file")


@pytest.fixture(autouse=True)
def mock_bind_address(mocker):
    mocker.patch(
        "core.cluster_state.ClusterState.bind_address",
        new_callable=PropertyMock,
        return_value="127.1.1.1",
    )


@pytest.fixture(autouse=True)
def mock_k8s_client(mocker):
    mocker.patch("lightkube.core.client.GenericSyncClient")


@pytest.fixture(autouse=True)
def mock_snap_cache(mocker):
    """Keep VM workload init off the real snapd and Snap Store."""
    mocker.patch("workload_vm.snap.SnapCache")


@pytest.fixture(autouse=True)
def mock_start_topology_observer(mocker):
    mocker.patch("managers.topology.TopologyManager.start_observer")


@pytest.fixture(autouse=True)
def mock_request_async_lock(mocker):
    """Queue no rolling operation, tests assert on the returned mock instead."""
    return mocker.patch("charmlibs.rollingops.RollingOpsManager.request_async_lock")


@pytest.fixture(autouse=True)
def tenacity_wait(mocker):
    mocker.patch("tenacity.nap.time")


@pytest.fixture(autouse=True)
def mock_cloud_spec(mocker):
    mocker.patch("ops.model.Model.get_cloud_spec")


@pytest.fixture(autouse=True)
def k8s_environment(monkeypatch):
    """Simulate a Kubernetes container environment by default."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "127.0.0.1")


@pytest.fixture
def vm_environment(monkeypatch):
    """Simulate a VM environment without Kubernetes environment variables."""
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)


@pytest.fixture(autouse=True)
def mock_refresh():
    """Fixture to shunt refresh logic and events."""
    refresh_mock = Mock()
    refresh_mock.in_progress = False
    refresh_mock.app_status_higher_priority = None
    refresh_mock.unit_status_higher_priority = None
    refresh_mock.unit_status_lower_priority.return_value = None
    refresh_mock.next_unit_allowed_to_refresh = True
    refresh_mock.workload_allowed_to_start = True

    versions_mock = Mock()
    versions_mock.charm = "v1/9.0.4"
    versions_mock.workload = "9.0.4"

    with (
        patch("charm_refresh.Machines", Mock(return_value=refresh_mock)),
        patch("events.refresh.MachinesValkeyRefresh", Mock(return_value=None)),
        patch("charm_refresh.Kubernetes", Mock(return_value=refresh_mock)),
        patch("events.refresh.K8sValkeyRefresh", Mock(return_value=None)),
        patch("charm_refresh._main._RefreshVersions", Mock(return_value=versions_mock)),
    ):
        yield
