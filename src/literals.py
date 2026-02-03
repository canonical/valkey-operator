#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of global literals for the Valkey charm."""

from enum import StrEnum

CHARM = "valkey"
CHARM_USER = "valkey"
CONTAINER = "valkey"

SNAP_NAME = "charmed-valkey"
SNAP_REVISION = 14
SNAP_SERVICE = "server"
SNAP_COMMON_PATH = "/var/snap/charmed-valkey/common"
SNAP_CURRENT_PATH = "/var/snap/charmed-valkey/current"
SNAP_CONFIG_FILE = "etc/charmed-valkey/valkey.conf"
SNAP_ACL_FILE = "etc/charmed-valkey/users.acl"

CONFIG_FILE = "/var/lib/valkey/valkey.conf"
ACL_FILE = "/var/lib/valkey/users.acl"

PEER_RELATION = "valkey-peers"
STATUS_PEERS_RELATION = "status-peers"

INTERNAL_USER = "charmed-operator"
INTERNAL_USER_PASSWORD_CONFIG = "system-users"

CLIENT_PORT = 6379


class Substrate(StrEnum):
    """Substrate types."""

    VM = "vm"
    K8S = "k8s"
