#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of global literals for the Valkey charm."""

CHARM = "valkey"
CHARM_USER = "valkey"
CONTAINER = "valkey"

CONFIG_FILE = "/var/lib/valkey/valkey.conf"
VALKEY_LOG_FILE = "/var/lib/valkey/valkey.log"
SENTINEL_LOG_FILE = "/var/lib/valkey/sentinel.log"
ACL_FILE = "/var/lib/valkey/users.acl"
SENTINEL_CONFIG_FILE = "/var/lib/valkey/sentinel.conf"
DATA_DIR = "/var/lib/valkey/data"

PEER_RELATION = "valkey-peers"
STATUS_PEERS_RELATION = "status-peers"

INTERNAL_USER = "charmed-operator"
SENTINEL_USER = "charmed-replication"
INTERNAL_USER_PASSWORD_CONFIG = "system-users"

CLIENT_PORT = 6379
SENTINEL_PORT = 26379

PRIMARY_NAME = "primary"
QUORUM_NUMBER = 2
