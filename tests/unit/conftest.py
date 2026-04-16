#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from unittest.mock import PropertyMock

import pytest
from ops import testing


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
def tenacity_wait(mocker):
    mocker.patch("tenacity.nap.time")


@pytest.fixture(autouse=True)
def cloud_spec():
    return testing.CloudSpec(
        type="kubernetes",
        endpoint="https://127.0.0.1:8443",
        credential=testing.CloudCredential(
            auth_type="clientcertificate",
            attributes={
                "client-cert": "foo",
                "client-key": "bar",
                "server-cert": "baz",
            },
        ),
    )


@pytest.fixture(autouse=True)
def cloud_spec_vm():
    return testing.CloudSpec(
        type="lxd",
        endpoint="https://127.0.0.1:8443",
        credential=testing.CloudCredential(
            auth_type="clientcertificate",
            attributes={
                "client-cert": "foo",
                "client-key": "bar",
                "server-cert": "baz",
            },
        ),
    )
