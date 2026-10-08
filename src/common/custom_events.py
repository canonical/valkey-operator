# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of custom events for the charm."""

import ops


class TopologyChangedEvent(ops.EventBase):
    """A custom event for topology changes."""


class TopologyChangedCharmEvents(ops.CharmEvents):
    """A CharmEvent extension to observe topology changes."""

    topology_changed = ops.EventSource(TopologyChangedEvent)


class RefreshTLSCertificatesEvent(ops.EventBase):
    """Event for refreshing peer TLS certificates."""
