#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of global literals for the Valkey charm."""

CHARM = "valkey"
CHARM_USER = "valkey"
CONTAINER = "valkey"

CONFIG_FILE = "/var/lib/valkey/valkey.conf"
ACL_FILE = "/var/lib/valkey/users.acl"

PEER_RELATION = "valkey-peers"
STATUS_PEERS_RELATION = "status-peers"

INTERNAL_USER = "charmed-operator"
INTERNAL_USER_PASSWORD_CONFIG = "system-users"

CLIENT_PORT = 6379
