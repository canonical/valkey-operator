# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of custom events for the charm."""

import ops


class RestartWorkloadEvent(ops.EventBase):
    """Event for restarting the workload when certain events happen, e.g. IP change."""

    def __init__(
        self, handle: ops.Handle, restart_valkey: bool = True, restart_sentinel: bool = True
    ):
        super().__init__(handle)
        self.restart_valkey = restart_valkey
        self.restart_sentinel = restart_sentinel

    def snapshot(self) -> dict[str, str]:
        """Save the state of the event."""
        return {
            "restart_valkey": str(self.restart_valkey),
            "restart_sentinel": str(self.restart_sentinel),
        }

    def restore(self, snapshot: dict[str, str]) -> None:
        """Restore the state of the event."""
        self.restart_valkey = snapshot.get("restart_valkey", "True") == "True"
        self.restart_sentinel = snapshot.get("restart_sentinel", "True") == "True"


class UnitFullyStartedEvent(ops.EventBase):
    """Event that signals that the unit's has fully started.

    This event will be deferred until:
        The Sentinel service is running and was discovered by other units.
        The Valkey service is running and the current node is in sync with the primary (if a replica).
    """

    def __init__(self, handle: ops.Handle, is_primary: bool = False):
        super().__init__(handle)
        self.is_primary = is_primary

    def snapshot(self) -> dict[str, str]:
        """Save the state of the event."""
        return {"is_primary": str(self.is_primary)}

    def restore(self, snapshot: dict[str, str]) -> None:
        """Restore the state of the event."""
        self.is_primary = snapshot.get("is_primary", "False") == "True"


class TopologyChangedEvent(ops.EventBase):
    """A custom event for topology changes."""


class TopologyChangedCharmEvents(ops.CharmEvents):
    """A CharmEvent extension to observe topology changes."""

    topology_changed = ops.EventSource(TopologyChangedEvent)


class RefreshTLSCertificatesEvent(ops.EventBase):
    """Event for refreshing peer TLS certificates."""
