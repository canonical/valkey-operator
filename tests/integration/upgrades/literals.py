#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of literals for upgrades tests."""

CHARM_CHANNEL = "9/edge"
CHARM_REVISIONS_TO_DEPLOY = {"x86_64": 177, "aarch64": 178}
WORKLOAD_VERSION = {"previous": "9.0.4", "target": "9.0.4"}
NUM_UNITS = 3
GLIDE_RUNNER_NAME = "glide-runner"
