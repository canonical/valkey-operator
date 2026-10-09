#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of literals for upgrades tests."""

CHARM_CHANNEL = "9/edge"
# deploy previous revisions of the charm to ensure we have something to upgrade
# otherwise upgrade tests might be a no-op
CHARM_REVISIONS_TO_DEPLOY = {"x86_64": 125, "aarch64": 126}
NUM_UNITS = 3
GLIDE_RUNNER_NAME = "glide-runner"
