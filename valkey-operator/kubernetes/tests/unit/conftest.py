#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import pytest


@pytest.fixture(autouse=True)
def mock_write_config_file(mocker):
    mocker.patch("workload.ValkeyK8sWorkload.write_config_file")
