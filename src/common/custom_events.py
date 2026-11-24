# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of custom events for the charm."""

import ops


class TopologyChangedEvent(ops.EventBase):
    """A custom event for topology changes."""


class RefreshTLSCertificatesEvent(ops.EventBase):
    """Event for refreshing peer TLS certificates."""


class ValkeyCharmEvents(ops.CharmEvents):
    """A CharmEvent extension for the charm's custom events."""

    topology_changed = ops.EventSource(TopologyChangedEvent)
    refresh_tls_certificates = ops.EventSource(RefreshTLSCertificatesEvent)
