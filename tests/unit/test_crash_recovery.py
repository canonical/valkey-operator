#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the full-cluster crash recovery startup logic.

On a cold start the charm reads each unit's on-disk data offset into peer data
and uses it to pick which unit should come up as the primary, so the unit with
the most recent data leads and the others resync from it.
"""

from unittest.mock import MagicMock

import pytest

from common.exceptions import ValkeyCannotGetPrimaryIPError
from core.cluster_state import ClusterState
from core.models import LocalDataOffset, ValkeyServer
from events.base_events import BaseEvents
from literals import PersistenceMode

OWN_ENDPOINT = "10.0.0.1"
PRIMARY_ENDPOINT = "10.0.0.9"


# --------------------------------------------------------------------------- #
# LocalDataOffset model
# --------------------------------------------------------------------------- #
def test_no_data_found_only_when_everything_is_zero():
    """The all-zeros sentinel marks a unit with no data; any set field clears it."""
    assert LocalDataOffset(replication_id="0", replication_offset="0", timestamp="0").no_data_found


@pytest.mark.parametrize(
    "replication_id,replication_offset,timestamp",
    [
        ("r1", "42", "1794420870"),  # RDB
        ("0", "0", "1794421964"),  # AOF only writes a timestamp
    ],
)
def test_data_found_when_any_field_is_set(replication_id, replication_offset, timestamp):
    """A single non-zero field means the unit has data on disk."""
    offset = LocalDataOffset(
        replication_id=replication_id,
        replication_offset=replication_offset,
        timestamp=timestamp,
    )

    assert not offset.no_data_found


# --------------------------------------------------------------------------- #
# ValkeyServer peer-data accessors
# --------------------------------------------------------------------------- #
def _server(offset: LocalDataOffset | None, has_model: bool = True) -> ValkeyServer:
    """Build a ValkeyServer without running __init__ (which needs a relation)."""
    server = object.__new__(ValkeyServer)
    server.model = MagicMock(local_data_offset=offset) if has_model else None
    return server


def test_store_local_data_offset_writes_a_validated_model():
    """Storing wraps the raw strings in a LocalDataOffset and writes it to peer data."""
    server = _server(None)
    server.update = MagicMock()

    server.store_local_data_offset("r1", "4242", "1794421900")

    server.update.assert_called_once_with(
        {
            "local_data_offset": LocalDataOffset(
                replication_id="r1", replication_offset="4242", timestamp="1794421900"
            )
        }
    )


def test_get_local_data_offset_returns_the_stored_model():
    """A unit that has written its offset reads it straight back from peer data."""
    offset = LocalDataOffset(
        replication_id="r1", replication_offset="4242", timestamp="1794421900"
    )

    assert _server(offset).get_local_data_offset() == offset


# --------------------------------------------------------------------------- #
# ClusterState offset aggregation
# --------------------------------------------------------------------------- #
def _cluster_state(offsets: list[LocalDataOffset | None]) -> ClusterState:
    """Build a ClusterState without __init__ and inject servers reporting `offsets`."""
    cs = object.__new__(ClusterState)
    servers = set()
    for offset in offsets:
        server = MagicMock()
        server.get_local_data_offset.return_value = offset
        servers.add(server)
    # `servers` is a cached_property; a direct assignment shadows it on the instance.
    cs.servers = servers
    return cs


NO_DATA = LocalDataOffset(replication_id="0", replication_offset="0", timestamp="0")


def test_all_and_any_units_stored_flags():
    """`any` is true once one unit has written, `all` only once every unit has."""
    assert _cluster_state([NO_DATA, None]).any_unit_local_data_offset_stored
    assert not _cluster_state([NO_DATA, None]).all_units_local_data_offset_stored
    assert _cluster_state([NO_DATA, NO_DATA]).all_units_local_data_offset_stored
    assert not _cluster_state([None, None]).any_unit_local_data_offset_stored


def test_latest_data_offset_is_none_when_no_unit_has_data():
    """Only the all-zeros sentinel (or nothing) on every unit means no data to follow."""
    assert _cluster_state([NO_DATA, NO_DATA, None]).latest_data_offset is None


def test_latest_data_offset_ignores_units_without_data():
    """Units reporting the no-data sentinel are dropped before comparing."""
    offset = LocalDataOffset(replication_id="0", replication_offset="0", timestamp="100")

    assert _cluster_state([offset, NO_DATA, None]).latest_data_offset == offset


def test_latest_data_offset_picks_newest_timestamp_across_replication_ids():
    """Differing replication ids (e.g. per-unit RDBs) fall back to the newest timestamp."""
    older = LocalDataOffset(replication_id="r1", replication_offset="10", timestamp="100")
    newer = LocalDataOffset(replication_id="r2", replication_offset="10", timestamp="200")

    assert _cluster_state([older, newer]).latest_data_offset == newer


def test_latest_data_offset_uses_timestamp_for_aof_only_offsets():
    """AOF offsets share the '0' replication id, so the newest timestamp wins."""
    older = LocalDataOffset(replication_id="0", replication_offset="0", timestamp="100")
    newer = LocalDataOffset(replication_id="0", replication_offset="0", timestamp="200")

    assert _cluster_state([older, newer]).latest_data_offset == newer


def test_latest_data_offset_compares_shared_replication_ids():
    """Units that share a non-zero replication id compare on the replication id branch."""
    offset = LocalDataOffset(replication_id="r1", replication_offset="10", timestamp="100")
    same = LocalDataOffset(replication_id="r1", replication_offset="10", timestamp="100")

    assert _cluster_state([offset, same]).latest_data_offset == offset


# --------------------------------------------------------------------------- #
# BaseEvents._get_primary_endpoint_for_start
# --------------------------------------------------------------------------- #
def _events(
    *,
    primary_ip=ValkeyCannotGetPrimaryIPError("no primary"),
    number_units_started: int = 0,
    planned_units: int = 2,
    is_leader: bool = False,
    persistence_mode: PersistenceMode = PersistenceMode.RDB,
    any_stored: bool = True,
    all_stored: bool = True,
    latest_offset: LocalDataOffset | None = None,
    own_offset: LocalDataOffset | None = None,
) -> tuple[BaseEvents, MagicMock]:
    """Build a BaseEvents with a fully mocked charm for the startup-primary decision."""
    events = object.__new__(BaseEvents)
    charm = MagicMock()
    events.charm = charm

    if isinstance(primary_ip, Exception):
        charm.sentinel_manager.get_primary_ip.side_effect = primary_ip
    else:
        charm.sentinel_manager.get_primary_ip.return_value = primary_ip

    charm.state.number_units_started = number_units_started
    charm.app.planned_units.return_value = planned_units
    charm.unit.is_leader.return_value = is_leader
    charm.state.cluster.persistence_config.mode = persistence_mode
    charm.state.any_unit_local_data_offset_stored = any_stored
    charm.state.all_units_local_data_offset_stored = all_stored
    charm.state.latest_data_offset = latest_offset
    charm.state.unit_server.get_local_data_offset.return_value = own_offset
    charm.state.unit_server.get_endpoint.return_value = OWN_ENDPOINT
    return events, charm


def test_primary_endpoint_follows_an_active_primary():
    """An already-elected primary is always the one a starting unit follows."""
    events, charm = _events(primary_ip=PRIMARY_ENDPOINT)

    assert events._get_primary_endpoint_for_start() == PRIMARY_ENDPOINT
    charm.cluster_manager.read_local_data_offset.assert_not_called()


def test_primary_endpoint_waits_when_another_unit_already_started():
    """With a peer already started there is a primary coming, so this unit waits."""
    events, _ = _events(number_units_started=1)

    assert events._get_primary_endpoint_for_start() is None


def test_single_unit_always_starts_as_its_own_primary():
    """A one-unit deployment has no one to follow, so it leads unconditionally."""
    events, _ = _events(planned_units=1)

    assert events._get_primary_endpoint_for_start() == OWN_ENDPOINT


def test_no_persistence_starts_the_leader_as_primary():
    """Without persistence there is no data to compare, so the leader just leads."""
    events, _ = _events(persistence_mode=PersistenceMode.NONE, is_leader=True)

    assert events._get_primary_endpoint_for_start() == OWN_ENDPOINT


def test_no_persistence_non_leader_waits():
    """A non-leader without persistence waits for the leader to become primary."""
    events, _ = _events(persistence_mode=PersistenceMode.NONE, is_leader=False)

    assert events._get_primary_endpoint_for_start() is None


def test_waits_until_every_unit_has_read_its_data():
    """The latest data is only known once all units have reported, so this unit waits."""
    events, charm = _events(any_stored=True, all_stored=False)

    assert events._get_primary_endpoint_for_start() is None
    charm.cluster_manager.read_local_data_offset.assert_called_once()


def test_unit_with_the_latest_data_starts_as_primary():
    """The unit whose offset matches the latest data on any unit leads the recovery."""
    offset = LocalDataOffset(replication_id="r1", replication_offset="10", timestamp="200")
    events, _ = _events(latest_offset=offset, own_offset=offset)

    assert events._get_primary_endpoint_for_start() == OWN_ENDPOINT


def test_unit_without_the_latest_data_waits():
    """A unit with older data must not lead; it waits to resync from the newest one."""
    latest = LocalDataOffset(replication_id="r1", replication_offset="10", timestamp="200")
    older = LocalDataOffset(replication_id="r1", replication_offset="10", timestamp="100")
    events, _ = _events(latest_offset=latest, own_offset=older)

    assert events._get_primary_endpoint_for_start() is None


def test_no_data_anywhere_starts_the_leader_as_primary():
    """When no unit has data on disk, the leader starts fresh as the primary."""
    events, _ = _events(latest_offset=None, is_leader=True)

    assert events._get_primary_endpoint_for_start() == OWN_ENDPOINT


def test_no_data_anywhere_non_leader_waits():
    """A non-leader with no data to compare waits for the leader to lead."""
    events, _ = _events(latest_offset=None, is_leader=False)

    assert events._get_primary_endpoint_for_start() is None
